# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # 03 · Export the copy -> input_output/output_databricks/
# MAGIC Turns the personal-workspace Delta tables into flat per-series CSVs plus one combined
# MAGIC metadata table, written into the Git folder working tree. Everything pulled from the
# MAGIC Databricks source lives under `input_output/output_databricks/`.
# MAGIC
# MAGIC Layout (forward-slashed, git-tracked):
# MAGIC ```
# MAGIC input_output/output_databricks/time_series_data/<filename>.csv   # Time,head — the raw hourly copy, unflagged
# MAGIC input_output/output_databricks/validation_flags/<filename>.csv   # Time,v1..v4,rule_a,rule_b — only flagged hours
# MAGIC input_output/output_databricks/metadata/object_data.csv          # one row per series (join on `filename`)
# MAGIC ```
# MAGIC `time_series_data/` stays an exact copy of the source; the automated flags from
# MAGIC `02_validate` travel separately, and only for the hours that were flagged (`v1`..`v4`
# MAGIC hold the original value, `rule_a`/`rule_b` are 0/1). `head` after v1–v4 = the raw
# MAGIC `head` with every flagged hour blanked. `object_data.csv` carries every
# MAGIC `source_copy_meetreeks` column (all of `meetreeks` + the `hl_cs_objecten` match,
# MAGIC incl. `matched`) plus per-check flag counts.
# MAGIC
# MAGIC `transport` controls shipping:
# MAGIC * **`write`** (default) — only write the files; **commit & push manually** from the
# MAGIC   Databricks Git-folder UI. Needs no shell `git`, so it also runs on serverless.
# MAGIC * `git` — also commit & push automatically (uses the `git_token_secret` PAT).
# MAGIC * `volume` — copy to the misc_files Volume for the local CLI bridge.

# COMMAND ----------
# Setup: imports, widgets, resolve the repo root, import the shared library.
import os
import shutil
import sys

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("transport", "write")       # write (default, no push) | git | volume
dbutils.widgets.text("branch", "main")
dbutils.widgets.text("run_id", "")
dbutils.widgets.text("repo_root", "")
dbutils.widgets.text("git_token_secret", "")     # "scope/key" of a PAT for push

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
TRANSPORT = dbutils.widgets.get("transport")
BRANCH = dbutils.widgets.get("branch")
RUN_ID = dbutils.widgets.get("run_id").strip() or "manual"

# Repo root: widget if set, otherwise walk up from the notebook until databricks_code/lib is found.
ROOT = dbutils.widgets.get("repo_root").strip()
if not ROOT:
    nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    p = nb_path if nb_path.startswith("/Workspace") else "/Workspace" + nb_path
    while not os.path.isdir(os.path.join(p, "databricks_code", "lib")):
        parent = os.path.dirname(p)
        if parent == p or parent in ("", "/"):
            raise FileNotFoundError("Could not find databricks_code/lib above the notebook; set the repo_root widget.")
        p = parent
    ROOT = p

LIB = os.path.join(ROOT, "databricks_code", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import git_sync  # noqa: E402
import source_read as sr  # noqa: E402

REL_DBX = "input_output/output_databricks"
OUTPUT_DBX = os.path.join(ROOT, *REL_DBX.split("/"))
TS_DIR = os.path.join(OUTPUT_DBX, "time_series_data")
FLAG_DIR = os.path.join(OUTPUT_DBX, "validation_flags")
META_DIR = os.path.join(OUTPUT_DBX, "metadata")
print("ROOT:", ROOT, "| transport:", TRANSPORT)

# COMMAND ----------
# Resolve names from the dimension: meetreeks_id -> (SeriesName, metadata row).
dim = spark.table(f"{OUT}.source_copy_meetreeks").toPandas()
names = {}
for _, r in dim.iterrows():
    sn = sr.name_series(r["meetreeks_id"], r.get("code"), r.get("bronsysteem"), r.get("sensor_sleutel"))
    names[r["meetreeks_id"]] = (sn, r)
print(len(names), "series in the dimension")
print(dim["matched"].value_counts().to_string())

# Rows come from 02_validate's table (raw value + flags in one scan). Without it, export
# the raw copy only, so 03 still works on its own.
FLAGS = ["v1", "v2", "v3", "v4", "rule_a", "rule_b"]
HAS_FLAGS = spark.catalog.tableExists(f"{OUT}.auto_validated_meting")
if HAS_FLAGS:
    rows_df = spark.table(f"{OUT}.auto_validated_meting").select(
        "meetreeks_id", "timestamp", "raw_head", *FLAGS)
    copy_run = spark.table(f"{OUT}.source_copy_meting").select("run_id").first().run_id
    flag_run = spark.table(f"{OUT}.auto_validated_meting").select("run_id").first().run_id
    if copy_run != flag_run:
        print(f"warn: flags are from run {flag_run}, the copy from run {copy_run} — "
              "rerun 02_validate so the flags match the data.")
else:
    print("warn: auto_validated_meting not found -> raw only, no flags. "
          "Run 02_validate first to export the flags.")
    rows_df = spark.table(f"{OUT}.source_copy_meting").select("meetreeks_id", "timestamp", "head")

# COMMAND ----------
# Write one flat CSV per series (+ its flags) and one combined metadata/object_data.csv.
# ONE global-sorted pass over the table (not one query per series): rows arrive grouped
# by meetreeks_id, so we buffer a single series at a time and flush it. The table is
# partitioned by vendor, so a per-series .where() can't prune and would rescan millions
# of rows per series; this scans once and keeps driver memory bounded to one series.
import pandas as pd  # noqa: E402

# Clean the output dirs so a rerun never leaves stale series behind.
for d in (TS_DIR, FLAG_DIR, META_DIR):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)

ordered = rows_df.orderBy("meetreeks_id", "timestamp")
VALUE = "raw_head" if HAS_FLAGS else "head"

used_names: dict[str, str] = {}   # chosen filename stem -> meetreeks_id (collision guard)
meta_rows: list[dict] = []
written_rel: list[str] = []
missing = 0


def _filename(sn) -> str:
    """Unique, human-readable stem. Codes can repeat across wells, so on a collision
    append a short slice of the (unique) meetreeks_id."""
    stem = sn.series
    if used_names.get(stem, sn.meetreeks_id) != sn.meetreeks_id:
        stem = f"{sn.series}__{sn.meetreeks_id[:12]}"
    used_names[stem] = sn.meetreeks_id
    return stem


def _flush(mid, rows, flag_rows):
    global missing
    if not rows:
        return
    entry = names.get(mid)
    if entry is None:            # series in the copy but not in the dimension
        missing += 1
        return
    sn, meta = entry
    stem = _filename(sn)
    pd.DataFrame(rows, columns=["Time", "head"]).to_csv(os.path.join(TS_DIR, stem + ".csv"), index=False)
    written_rel.append(f"{REL_DBX}/time_series_data/{stem}.csv")

    counts = {f"n_{f}": 0 for f in FLAGS}
    if HAS_FLAGS:
        fl = pd.DataFrame(flag_rows, columns=["Time", *FLAGS])
        for f in FLAGS:
            counts[f"n_{f}"] = int(fl[f].notna().sum() if f.startswith("v") else fl[f].sum())
        fl[["rule_a", "rule_b"]] = fl[["rule_a", "rule_b"]].astype(int)
        fl.to_csv(os.path.join(FLAG_DIR, stem + ".csv"), index=False)
        written_rel.append(f"{REL_DBX}/validation_flags/{stem}.csv")

    # Identity first, then every dimension column (meetreeks + cs_objecten match), then counts.
    row = {"filename": stem, "vendor": sn.vendor, "origin": sn.origin, "series": sn.series}
    row.update({k: (None if isinstance(v, float) and v != v else v)   # NaN -> empty cell
                for k, v in meta.items() if k not in row})
    row["n_rows"] = len(rows)
    if HAS_FLAGS:
        row.update(counts)
    meta_rows.append(row)


cur, buf, fbuf = None, [], []
for row in ordered.toLocalIterator():
    mid = row["meetreeks_id"]
    if mid != cur:
        _flush(cur, buf, fbuf)
        cur, buf, fbuf = mid, [], []
    if row[VALUE] is None:
        continue
    buf.append((row["timestamp"], row[VALUE]))
    if HAS_FLAGS and (row["rule_a"] or row["rule_b"]
                      or any(row[v] is not None for v in ("v1", "v2", "v3", "v4"))):
        fbuf.append((row["timestamp"], *(row[f] for f in FLAGS)))
_flush(cur, buf, fbuf)

# One combined metadata table (sorted for a stable, reviewable diff).
meta_df = pd.DataFrame(meta_rows).sort_values(["vendor", "origin", "series"]).reset_index(drop=True)
meta_df.to_csv(os.path.join(META_DIR, "object_data.csv"), index=False)
written_rel.append(f"{REL_DBX}/metadata/object_data.csv")

print(f"wrote {len(meta_rows)} series to {REL_DBX}/time_series_data/"
      + (" + validation_flags/" if HAS_FLAGS else "")
      + f" and {REL_DBX}/metadata/object_data.csv "
      f"({missing} series in the copy without a dimension name)")
print(meta_df["matched"].value_counts().to_string())

# COMMAND ----------
# Ship it.
def git_token():
    spec = dbutils.widgets.get("git_token_secret").strip()
    if not spec or "/" not in spec:
        return None
    scope, key = spec.split("/", 1)
    try:
        return dbutils.secrets.get(scope, key)
    except Exception as exc:  # noqa: BLE001
        print(f"warn: could not read secret {spec}: {exc}")
        return None


if TRANSPORT == "git":
    git_sync.commit_and_push(
        ROOT, paths=[REL_DBX],
        message=f"source replication: run {RUN_ID} ({len(meta_rows)} series)",
        branch=BRANCH, token=git_token())
    print(f"committed & pushed {REL_DBX}/")
elif TRANSPORT == "volume":
    dest = git_sync.write_tree_to_volume(ROOT, written_rel)
    print("copied to Volume for the local bridge:", dest)
    print("Now, on a machine with the Databricks CLI, run the bridge in databricks_code/README.md")
else:  # "write" (default): leave the files in the working tree; push manually
    print(f"{len(written_rel)} files written under {REL_DBX}/.")
    print("Nothing pushed. Commit & push from Databricks (Git folder UI) when ready.")

dbutils.notebook.exit(str(len(meta_rows)))
