r"""First-contact viability check against the Databricks source.

Runs a handful of read-only queries through the Databricks SQL Statement Execution
API to prove the pipeline's core assumptions hold on live data, *before* any job is
built on them:

  1. auth + identity (current_user / metastore)
  2. the read-only source is reachable and how big it is
  3. the vendor/origin/series naming bridge produces sane names from real meetreeks rows
  4. the per-vendor series breakdown (Fugro vs Wiertsema)
  5. the ~55-rows/day -> 1-row/hour dedup assumption, measured on the busiest series
  6. open data-quality failures (the known duplicate-key critical)

Nothing here writes anything. The source stays read-only.

Run it (PowerShell), after authenticating in your own terminal so no secret is pasted
into a chat:

    $env:DATABRICKS_HOST = "https://<your-workspace>.azuredatabricks.net"
    $env:DATABRICKS_TOKEN = "<PAT>"            # or: databricks auth login --host <host>
    $env:DATABRICKS_WAREHOUSE_ID = "<sql-warehouse-id>"
    .\.venv\Scripts\python scripts\check_source.py

Auth resolves through the Databricks SDK default chain (env vars, CLI profile, or OAuth),
so `--profile <name>` works too if you use a ~/.databrickscfg profile.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "databricks", "lib"))
import source_read as sr  # noqa: E402


def run_sql(w, warehouse_id: str, sql: str, timeout_s: int = 180):
    """Execute a statement and block until it finishes; return (columns, rows)."""
    from databricks.sdk.service.sql import StatementState

    stmt = w.statement_execution.execute_statement(
        warehouse_id=warehouse_id, statement=sql, wait_timeout="30s")
    deadline = time.time() + timeout_s
    while stmt.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.time() > deadline:
            raise TimeoutError(f"statement timed out after {timeout_s}s")
        time.sleep(2)
        stmt = w.statement_execution.get_statement(stmt.statement_id)
    if stmt.status.state != StatementState.SUCCEEDED:
        err = stmt.status.error
        raise RuntimeError(f"{stmt.status.state}: {getattr(err, 'message', err)}")
    cols = [c.name for c in stmt.manifest.schema.columns] if stmt.manifest else []
    rows = stmt.result.data_array if (stmt.result and stmt.result.data_array) else []
    return cols, rows


def show(title: str, cols, rows, limit: int = 12) -> None:
    print(f"\n--- {title} ---")
    if cols:
        print("  " + " | ".join(cols))
    for r in rows[:limit]:
        print("  " + " | ".join("" if v is None else str(v) for v in r))
    if len(rows) > limit:
        print(f"  … (+{len(rows) - limit} more)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only viability check against gold_prd.")
    ap.add_argument("--warehouse", default=os.environ.get("DATABRICKS_WAREHOUSE_ID"),
                    help="SQL warehouse id (or set DATABRICKS_WAREHOUSE_ID).")
    ap.add_argument("--profile", default=None, help="~/.databrickscfg profile name.")
    args = ap.parse_args()

    if not args.warehouse:
        print("ERROR: no SQL warehouse. Set DATABRICKS_WAREHOUSE_ID or pass --warehouse.",
              file=sys.stderr)
        return 2

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        print("ERROR: databricks-sdk not installed. `.\\.venv\\Scripts\\pip install databricks-sdk`",
              file=sys.stderr)
        return 2

    try:
        w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not authenticate: {exc}\n"
              "Set DATABRICKS_HOST + DATABRICKS_TOKEN, or run `databricks auth login`.",
              file=sys.stderr)
        return 2

    def q(sql, timeout_s=180):
        return run_sql(w, args.warehouse, sql, timeout_s)

    print("=" * 66)
    print("Databricks source viability check (read-only)")
    print("=" * 66)

    # 1 · identity
    cols, rows = q("SELECT current_user() AS user, current_catalog() AS metastore")
    show("1 · identity", cols, rows)

    # 2 · source reachable + size
    cols, rows = q(f"SELECT count(*) AS rows, count(DISTINCT {sr.SERIES_KEY}) AS series "
                   f"FROM {sr.METING}")
    show("2 · source size (meting)", cols, rows)

    # 3 · naming bridge on real meetreeks rows
    cols, rows = q(f"""
        SELECT id, trim(code) AS code, bronsysteem, sensor_meetlocatie_sleutel,
               begin_filter, eind_filter
        FROM {sr.MEETREEKS} LIMIT 8
    """)
    show("3a · meetreeks sample (raw)", cols, rows)
    idx = {c: i for i, c in enumerate(cols)}
    print("\n--- 3b · naming bridge output (meetreeks_id -> vendor/origin/series) ---")
    print("  vendor | origin | series | rel_csv")
    for r in rows:
        sn = sr.name_series(r[idx["id"]], r[idx["code"]], r[idx["bronsysteem"]],
                            r[idx["sensor_meetlocatie_sleutel"]])
        print(f"  {sn.vendor} | {sn.origin} | {sn.series} | {sn.rel_csv}")

    # 4 · per-vendor breakdown
    cols, rows = q(f"""
        SELECT lower(trim(r.bronsysteem)) AS vendor,
               count(DISTINCT m.{sr.SERIES_KEY}) AS series
        FROM {sr.METING} m
        LEFT JOIN {sr.MEETREEKS} r ON r.id = m.{sr.SERIES_KEY}
        GROUP BY 1 ORDER BY 2 DESC
    """)
    show("4 · series per vendor", cols, rows)

    # 5 · dedup assumption on the busiest series (raw rows vs distinct hours)
    cols, rows = q(f"""
        WITH top AS (
            SELECT {sr.SERIES_KEY} AS mid, count(*) AS n
            FROM {sr.METING} WHERE {sr.VALUE_COLUMN} IS NOT NULL
            GROUP BY {sr.SERIES_KEY} ORDER BY n DESC LIMIT 1)
        SELECT t.mid AS meetreeks_id,
               t.n   AS raw_rows,
               count(DISTINCT date_trunc('HOUR', m.{sr.TIME_COLUMN})) AS distinct_hours,
               round(t.n / count(DISTINCT date_trunc('HOUR', m.{sr.TIME_COLUMN})), 1) AS rows_per_hour
        FROM top t JOIN {sr.METING} m ON m.{sr.SERIES_KEY} = t.mid
        WHERE m.{sr.VALUE_COLUMN} IS NOT NULL
        GROUP BY t.mid, t.n
    """)
    show("5 · hourly-dedup assumption (busiest series)", cols, rows)

    # 6 · open data-quality failures
    try:
        cols, rows = q(sr.dq_failures_sql() + " LIMIT 10")
        show("6 · open DQ failures (hl_grondwaterpeilmeting)", cols, rows)
    except Exception as exc:  # noqa: BLE001 - side table may not be granted
        print(f"\n--- 6 · DQ failures --- (skipped: {exc})")

    print("\n" + "=" * 66)
    print("DONE. If every section returned rows, the source side of the pipeline is viable.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
