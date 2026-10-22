# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # 01 · Source -> personal-workspace copy
# MAGIC Reads the read-only source, cleans every series to **hourly** values, and writes an
# MAGIC immutable per-well snapshot into the personal schema. The source is never modified.
# MAGIC
# MAGIC Writes two Delta tables to `waterkeringbeheer_prd.jvanruitenbeek`:
# MAGIC * `source_copy_meetreeks` — the series dimension: **every `meetreeks` column**, plus the
# MAGIC   matching `hl_cs_objecten.meetlocatie` row (coordinates, name, compartment, water-level
# MAGIC   area). `matched` = `matched` | `no match found in cs_objecten`; `match_key` says which
# MAGIC   join found it.
# MAGIC * `source_copy_meting` — hourly `(meetreeks_id, vendor, timestamp, head)`, run-tagged.
# MAGIC
# MAGIC Cleaning is done in Spark SQL (floor to the hour on the UTC twin, mean over duplicate
# MAGIC timestamps) so it scales to all series without pulling rows to the driver.

# COMMAND ----------
# Setup: imports, widgets, resolve the repo + lib dir, import the shared library.
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("dedup_method", "mean")    # mean|median|min|max|first|last
dbutils.widgets.text("limit_series", "0")       # 0 = all; set 1 for a first run
dbutils.widgets.text("run_id", "")              # blank = UTC %Y%m%dT%H%M%SZ
dbutils.widgets.text("repo_root", "")           # blank = derive from notebook location


def find_repo_root(marker: str = "databricks_code/lib") -> Path:
    here = Path(os.getcwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / marker).is_dir():
            return candidate
    raise RuntimeError(f"no ancestor of {here} contains {marker}")


ROOT = Path(dbutils.widgets.get("repo_root").strip() or find_repo_root())
LIB = ROOT / "databricks_code" / "lib"
if not LIB.is_dir():
    raise RuntimeError(f"library directory not found: {LIB} (cwd={os.getcwd()})")
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import source_read as sr  # noqa: E402

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
DEDUP = dbutils.widgets.get("dedup_method")
LIMIT = int(dbutils.widgets.get("limit_series"))
RUN_ID = dbutils.widgets.get("run_id").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
AGG = {"mean": "avg", "median": "percentile_approx", "min": "min", "max": "max",
       "first": "first", "last": "last"}.get(DEDUP, "avg")
print("run_id:", RUN_ID, "| dedup:", DEDUP, "| limit:", LIMIT or "all", "| root:", ROOT)

# COMMAND ----------
# hl_cs_objecten diagnostics: grant, how filled the link columns are, and the match
# rate of each join key on its own. Read-only; a failure here only disables location.
CS_READABLE = True
try:
    display(spark.sql("SHOW GRANTS ON SCHEMA gold_prd.hl_cs_objecten"))
except Exception as exc:  # noqa: BLE001  (SHOW GRANTS may need more than SELECT)
    print(f"SHOW GRANTS not permitted ({str(exc)[:120]}…) -> testing a real read instead")
try:
    print("meetlocatie rows:", spark.sql(f"SELECT count(*) AS n FROM {sr.MEETLOCATIE}").first().n)
    display(spark.sql(sr.cs_match_report_sql()))
    display(spark.sql(sr.meetlocatie_id_convention_sql()))
except Exception as exc:  # noqa: BLE001
    CS_READABLE = False
    print(f"hl_cs_objecten not readable ({str(exc)[:160]}…) -> every series will be "
          f"'{sr.NOT_MATCHED}'")

# COMMAND ----------
# Series dimension -> source_copy_meetreeks (overwrite).
# All meetreeks columns + the first-matching meetlocatie row (LEFT join: unmatched series
# stay in). Falls back to meetreeks-only if hl_cs_objecten is not readable.
try:
    if not CS_READABLE:
        raise PermissionError("hl_cs_objecten not readable")
    catalog = spark.sql(sr.series_catalog_sql(include_location=True))
    catalog.limit(1).collect()  # force resolution now so a grant error surfaces here
    print("dimension: meetlocatie match OK")
except Exception as exc:  # noqa: BLE001
    print(f"dimension: meetlocatie match unavailable ({str(exc)[:160]}…) -> meetreeks-only")
    catalog = spark.sql(sr.series_catalog_sql(include_location=False))

(catalog.write.mode("overwrite").option("overwriteSchema", "true")
        .saveAsTable(f"{OUT}.source_copy_meetreeks"))
print(f"{OUT}.source_copy_meetreeks:", catalog.count(), "series")
display(spark.sql(f"""
    SELECT matched, match_key, count(*) AS series,
           count_if(cs_n_candidates > 1) AS ambiguous, count(lon) AS with_coords
    FROM {OUT}.source_copy_meetreeks
    GROUP BY matched, match_key ORDER BY matched, match_key
"""))

# COMMAND ----------
# Optional: restrict to the N longest series (fast first runs).
selected_filter = ""
if LIMIT > 0:
    top = spark.sql(f"""
        SELECT {sr.SERIES_KEY} AS meetreeks_id
        FROM {sr.METING}
        WHERE {sr.VALUE_COLUMN} IS NOT NULL
        GROUP BY {sr.SERIES_KEY}
        ORDER BY count(*) DESC
        LIMIT {LIMIT}
    """)
    ids = [r.meetreeks_id for r in top.collect()]
    quoted = ",".join("'" + i + "'" for i in ids)
    selected_filter = f"AND m.{sr.SERIES_KEY} IN ({quoted})"
    print("selected:", ids)

# COMMAND ----------
# Clean to hourly -> source_copy_meting (overwrite, run-tagged).
# Floor the UTC twin to the hour, collapse duplicate hours per series. `vendor` is
# carried from the dimension so the export job and the Delta copy partition cheaply.
agg_expr = (f"{AGG}(m.{sr.VALUE_COLUMN})" if AGG != "percentile_approx"
            else f"percentile_approx(m.{sr.VALUE_COLUMN}, 0.5)")
copy_df = spark.sql(f"""
    SELECT
        m.{sr.SERIES_KEY}                         AS meetreeks_id,
        lower(trim(r.bronsysteem))                AS vendor,
        date_trunc('HOUR', m.{sr.TIME_COLUMN})    AS timestamp,
        {agg_expr}                                AS head,
        '{RUN_ID}'                                AS run_id,
        '{sr.METING}'                             AS source_table,
        current_timestamp()                       AS copied_at
    FROM {sr.METING} m
    LEFT JOIN {sr.MEETREEKS} r ON r.id = m.{sr.SERIES_KEY}
    WHERE m.{sr.VALUE_COLUMN} IS NOT NULL
      AND m.{sr.TIME_COLUMN} IS NOT NULL
      {selected_filter}
    GROUP BY m.{sr.SERIES_KEY}, lower(trim(r.bronsysteem)),
             date_trunc('HOUR', m.{sr.TIME_COLUMN})
""")
(copy_df.write.mode("overwrite").option("overwriteSchema", "true")
        .partitionBy("vendor")
        .saveAsTable(f"{OUT}.source_copy_meting"))
print(f"{OUT}.source_copy_meting written")

# COMMAND ----------
# Summary.
display(spark.sql(f"""
    SELECT vendor, count(DISTINCT meetreeks_id) AS series, count(*) AS hourly_rows,
           min(timestamp) AS first_ts, max(timestamp) AS last_ts
    FROM {OUT}.source_copy_meting GROUP BY vendor ORDER BY vendor
"""))
dbutils.jobs.taskValues.set(key="run_id", value=RUN_ID)
dbutils.notebook.exit(RUN_ID)
