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
# MAGIC * `source_copy_meting` — hourly `(meetreeks_id, vendor, timestamp, head)`, run-tagged.
# MAGIC * `source_copy_meetreeks` — the series dimension (code, filter depths, bronsysteem, coords).
# MAGIC
# MAGIC Cleaning is done in Spark SQL (floor to the hour on the UTC twin, mean over duplicate
# MAGIC timestamps) so it scales to all series without pulling rows to the driver.

# COMMAND ----------

# print("cwd:", os.getcwd())
# print("ROOT:", ROOT)
# lib = os.path.join(ROOT, "databricks", "lib")
# print("lib exists:", os.path.isdir(lib))
# print(os.listdir(lib) if os.path.isdir(lib) else os.listdir(ROOT))

# COMMAND ----------

from pathlib import Path

def find_repo_root(marker="databricks/lib"):
    here = Path(os.getcwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / marker).is_dir():
            return candidate
    raise RuntimeError(f"no ancestor of {here} contains {marker}")

ROOT = Path(dbutils.widgets.get("repo_root").strip() or find_repo_root())

lib = ROOT / "databricks" / "lib"
if not lib.is_dir():
    raise RuntimeError(f"{lib} does not exist")
sys.path.insert(0, str(lib))

import source_read as sr  # noqa: E402

# COMMAND ----------

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("freq", "1 hour")          # Spark interval for date_trunc-style floor
dbutils.widgets.text("dedup_method", "mean")    # mean|median|min|max|first|last
dbutils.widgets.text("limit_series", "0")       # 0 = all; set 1 for a first run
dbutils.widgets.text("run_id", "")              # blank = UTC %Y%m%dT%H%M%SZ
dbutils.widgets.text("repo_root", "")           # blank = derive from notebook location

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
DEDUP = dbutils.widgets.get("dedup_method")
LIMIT = int(dbutils.widgets.get("limit_series"))
RUN_ID = dbutils.widgets.get("run_id").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# databricks/lib is a sibling of databricks/notebooks, where this notebook runs from.
_repo_root = dbutils.widgets.get("repo_root").strip()
LIB = Path(_repo_root) / "databricks" / "lib" if _repo_root else Path(os.getcwd()).resolve().parent / "lib"
if not LIB.is_dir():
    raise RuntimeError(f"library directory not found: {LIB} (cwd={os.getcwd()})")
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import source_read as sr  # noqa: E402

AGG = {"mean": "avg", "median": "percentile_approx", "min": "min", "max": "max",
       "first": "first", "last": "last"}.get(DEDUP, "avg")
print("run_id:", RUN_ID, "| dedup:", DEDUP, "| limit:", LIMIT or "all")
print("lib:", LIB)

# COMMAND ----------

# MAGIC %md ### Series dimension -> source_copy_meetreeks (overwrite)
# MAGIC catalog = spark.sql(sr.series_catalog_sql())
# MAGIC (catalog.write.mode("overwrite").option("overwriteSchema", "true")
# MAGIC         .saveAsTable(f"{OUT}.source_copy_meetreeks"))
# MAGIC print(f"{OUT}.source_copy_meetreeks:", catalog.count(), "series")

# COMMAND ----------

# MAGIC %md ### Optional: restrict to the N longest series (fast first runs)
# MAGIC selected_filter = ""
# MAGIC if LIMIT > 0:
# MAGIC     top = spark.sql(f"""
# MAGIC         SELECT {sr.SERIES_KEY} AS meetreeks_id
# MAGIC         FROM {sr.METING}
# MAGIC         WHERE {sr.VALUE_COLUMN} IS NOT NULL
# MAGIC         GROUP BY {sr.SERIES_KEY}
# MAGIC         ORDER BY count(*) DESC
# MAGIC         LIMIT {LIMIT}
# MAGIC     """)
# MAGIC     ids = [r.meetreeks_id for r in top.collect()]
# MAGIC     quoted = ",".join("'" + i + "'" for i in ids)
# MAGIC     selected_filter = f"AND m.{sr.SERIES_KEY} IN ({quoted})"
# MAGIC     print("selected:", ids)

# COMMAND ----------

# MAGIC %md ### Clean to hourly -> source_copy_meting (overwrite, run-tagged)
# MAGIC # Floor the UTC twin to the hour, collapse duplicate hours per series. `vendor` is
# MAGIC # carried from the dimension so the export job and the Delta copy partition cheaply.
# MAGIC agg_expr = (f"{AGG}(m.{sr.VALUE_COLUMN})" if AGG != "percentile_approx"
# MAGIC             else f"percentile_approx(m.{sr.VALUE_COLUMN}, 0.5)")
# MAGIC copy_df = spark.sql(f"""
# MAGIC     SELECT
# MAGIC         m.{sr.SERIES_KEY}                         AS meetreeks_id,
# MAGIC         lower(trim(r.bronsysteem))                AS vendor,
# MAGIC         date_trunc('HOUR', m.{sr.TIME_COLUMN})    AS timestamp,
# MAGIC         {agg_expr}                                AS head,
# MAGIC         '{RUN_ID}'                                AS run_id,
# MAGIC         '{sr.METING}'                             AS source_table,
# MAGIC         current_timestamp()                       AS copied_at
# MAGIC     FROM {sr.METING} m
# MAGIC     LEFT JOIN {sr.MEETREEKS} r ON r.id = m.{sr.SERIES_KEY}
# MAGIC     WHERE m.{sr.VALUE_COLUMN} IS NOT NULL
# MAGIC       AND m.{sr.TIME_COLUMN} IS NOT NULL
# MAGIC       {selected_filter}
# MAGIC     GROUP BY m.{sr.SERIES_KEY}, lower(trim(r.bronsysteem)),
# MAGIC              date_trunc('HOUR', m.{sr.TIME_COLUMN})
# MAGIC """)
# MAGIC (copy_df.write.mode("overwrite").option("overwriteSchema", "true")
# MAGIC         .partitionBy("vendor")
# MAGIC         .saveAsTable(f"{OUT}.source_copy_meting"))

# COMMAND ----------

# MAGIC %md ### Summary
# MAGIC display(spark.sql(f"""
# MAGIC     SELECT vendor, count(DISTINCT meetreeks_id) AS series, count(*) AS hourly_rows,
# MAGIC            min(timestamp) AS first_ts, max(timestamp) AS last_ts
# MAGIC     FROM {OUT}.source_copy_meting GROUP BY vendor ORDER BY vendor
# MAGIC """))
# MAGIC dbutils.jobs.taskValues.set(key="run_id", value=RUN_ID)
# MAGIC dbutils.notebook.exit(RUN_ID)
