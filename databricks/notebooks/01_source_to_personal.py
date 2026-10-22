# Databricks notebook source
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
import os
import sys
from datetime import datetime, timezone

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("freq", "1 hour")          # Spark interval for date_trunc-style floor
dbutils.widgets.text("dedup_method", "mean")    # mean|median|min|max|first|last
dbutils.widgets.text("limit_series", "0")        # 0 = all; set 1 for a first run
dbutils.widgets.text("run_id", "")               # blank = UTC %Y%m%dT%H%M%SZ
dbutils.widgets.text("repo_root", "")

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
DEDUP = dbutils.widgets.get("dedup_method")
LIMIT = int(dbutils.widgets.get("limit_series"))
RUN_ID = dbutils.widgets.get("run_id").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

ROOT = dbutils.widgets.get("repo_root").strip() or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "databricks", "lib"))
import source_read as sr  # noqa: E402

AGG = {"mean": "avg", "median": "percentile_approx", "min": "min", "max": "max",
       "first": "first", "last": "last"}.get(DEDUP, "avg")
print("run_id:", RUN_ID, "| dedup:", DEDUP, "| limit:", LIMIT or "all")

# COMMAND ----------
# MAGIC %md ### Series dimension -> source_copy_meetreeks (overwrite)
catalog = spark.sql(sr.series_catalog_sql())
(catalog.write.mode("overwrite").option("overwriteSchema", "true")
        .saveAsTable(f"{OUT}.source_copy_meetreeks"))
print(f"{OUT}.source_copy_meetreeks:", catalog.count(), "series")

# COMMAND ----------
# MAGIC %md ### Optional: restrict to the N longest series (fast first runs)
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
# MAGIC %md ### Clean to hourly -> source_copy_meting (overwrite, run-tagged)
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

# COMMAND ----------
# MAGIC %md ### Summary
display(spark.sql(f"""
    SELECT vendor, count(DISTINCT meetreeks_id) AS series, count(*) AS hourly_rows,
           min(timestamp) AS first_ts, max(timestamp) AS last_ts
    FROM {OUT}.source_copy_meting GROUP BY vendor ORDER BY vendor
"""))
dbutils.jobs.taskValues.set(key="run_id", value=RUN_ID)
dbutils.notebook.exit(RUN_ID)
