# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # 02 · Automated validation of the copy
# MAGIC Runs the automated checks on every series in `source_copy_meting` and writes the
# MAGIC result next to it. The copy itself is never modified; the flags land in a new table.
# MAGIC
# MAGIC **Flag layers** run in order on `head`. Each one stores the original of what it flags
# MAGIC in its own column and sets `head` to NaN, so the next layer only sees what is left:
# MAGIC
# MAGIC | Column | Check | Default |
# MAGIC |---|---|---|
# MAGIC | `v1` | Out of bounds: below the filter bottom, or above the top of the well | `eind_filter` / `bovenkant_peilbuis` |
# MAGIC | `v2` | Unrealistic step change between two hours | up > +0.30 m, down < −0.05 m |
# MAGIC | `v3` | At the filter bottom: within 0.15 m of `eind_filter` (inclusive) | ±0.15 m; skipped without `eind_filter` |
# MAGIC | `v4` | Statistical outlier (Tukey / IQR) | outside Q1 − 1.5·IQR .. Q3 + 1.5·IQR |
# MAGIC
# MAGIC **Autoflag rules** look at the raw value (they act on the review store's `raw_data`):
# MAGIC
# MAGIC | Column | Rule | Default |
# MAGIC |---|---|---|
# MAGIC | `rule_a` | Drop ≥ 0.10 m in one hourly step (contiguous hours) | **report-only** |
# MAGIC | `rule_b` | Value ≤ filter bottom + 0.10 m | **applied** (`afgekeurd = 1` in the store) |
# MAGIC
# MAGIC **Manual review** (Streamlit, local) always overrides all of this: a row a human marked
# MAGIC `gecontroleerd` is never touched by autoflag (`validation.apply_autoflags`).
# MAGIC
# MAGIC Writes to `waterkeringbeheer_prd.jvanruitenbeek`:
# MAGIC * `auto_validated_meting` — per hour: `raw_head`, `head` (after v1–v4), `v1`..`v4`, `rule_a`, `rule_b`.
# MAGIC * `auto_validation_summary` — per series: bounds used and how many hours each check flagged.

# COMMAND ----------
# Setup: widgets, resolve the repo + lib dir, import the shared library.
import os
import sys
from pathlib import Path

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("run_id", "")            # blank = the run_id of source_copy_meting
dbutils.widgets.text("repo_root", "")         # blank = derive from notebook location
# Thresholds; defaults mirror config/settings.yaml (flags: / autoflag:).
dbutils.widgets.text("v2_max_up", "0.30")
dbutils.widgets.text("v2_max_down", "-0.05")
dbutils.widgets.text("v3_band", "0.15")
dbutils.widgets.text("v3_min_run", "1")      # >1 = only flag in-band runs this long
dbutils.widgets.text("v4_iqr_factor", "1.5")
dbutils.widgets.text("rule_a_drop", "0.10")
dbutils.widgets.text("rule_b_margin", "0.10")


def find_repo_root(marker: str = "databricks_code/lib") -> Path:
    here = Path(os.getcwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / marker).is_dir():
            return candidate
    raise RuntimeError(f"no ancestor of {here} contains {marker}")


ROOT = Path(dbutils.widgets.get("repo_root").strip() or find_repo_root())
LIB = ROOT / "databricks_code" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import validation as val  # noqa: E402

# The per-series function runs on the workers, which don't have databricks_code/lib on
# their path. Pickling the module by value ships its code along with the function.
from pyspark import cloudpickle  # noqa: E402

cloudpickle.register_pickle_by_value(val)

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
TH = val.Thresholds(
    v2_max_up=float(dbutils.widgets.get("v2_max_up")),
    v2_max_down=float(dbutils.widgets.get("v2_max_down")),
    v3_band=float(dbutils.widgets.get("v3_band")),
    v3_min_run=int(dbutils.widgets.get("v3_min_run")),
    v4_iqr_factor=float(dbutils.widgets.get("v4_iqr_factor")),
    rule_a_drop=float(dbutils.widgets.get("rule_a_drop")),
    rule_b_margin=float(dbutils.widgets.get("rule_b_margin")),
)
RUN_ID = dbutils.widgets.get("run_id").strip() or (
    spark.table(f"{OUT}.source_copy_meting").select("run_id").first().run_id)
print("run_id:", RUN_ID, "| root:", ROOT)
print("thresholds:", TH)

# COMMAND ----------
# Per-series bounds from the dimension: (begin_filter, eind_filter, bovenkant_peilbuis).
dim = (spark.table(f"{OUT}.source_copy_meetreeks")
            .select("meetreeks_id", "begin_filter", "eind_filter", "bovenkant_peilbuis")
            .toPandas())
BOUNDS = {r.meetreeks_id: (r.begin_filter, r.eind_filter, r.bovenkant_peilbuis)
          for r in dim.itertuples(index=False)}
n_filter = sum(1 for b, e, _ in BOUNDS.values() if val.filter_bottom(b, e) is not None)
n_top = sum(1 for *_, t in BOUNDS.values() if val.as_float(t) is not None)
print(f"{len(BOUNDS)} series | filter bottom known: {n_filter} | top of well known: {n_top}")
print("(no filter bottom -> v1 lower bound and rule B are skipped; no top -> v1 upper bound skipped)")

# COMMAND ----------
# Run the checks per series on the workers -> auto_validated_meting (overwrite).
from pyspark.sql import functions as F  # noqa: E402

RESULT_SCHEMA = ("meetreeks_id string, vendor string, timestamp timestamp, raw_head double, "
                 "head double, v1 double, v2 double, v3 double, v4 double, "
                 "rule_a boolean, rule_b boolean")


def _validate(pdf):
    return val.validate_group(pdf, BOUNDS, TH)


result = (spark.table(f"{OUT}.source_copy_meting")
               .select("meetreeks_id", "vendor", "timestamp", "head")
               .groupBy("meetreeks_id")
               .applyInPandas(_validate, schema=RESULT_SCHEMA))
# NaN -> NULL, so count()/IS NULL mean "not flagged" in SQL.
for c in ("raw_head", "head", *val.FLAG_COLUMNS):
    result = result.withColumn(c, F.nanvl(F.col(c), F.lit(None).cast("double")))
result = (result.withColumn("run_id", F.lit(RUN_ID))
                .withColumn("validated_at", F.current_timestamp()))

(result.write.mode("overwrite").option("overwriteSchema", "true")
       .partitionBy("vendor")
       .saveAsTable(f"{OUT}.auto_validated_meting"))
print(f"{OUT}.auto_validated_meting written")

# COMMAND ----------
# Per-series summary -> auto_validation_summary (overwrite), and the totals.
summary = spark.sql(f"""
    SELECT a.meetreeks_id, a.vendor, d.code,
           least(d.begin_filter, d.eind_filter) AS filter_bottom,
           d.bovenkant_peilbuis                 AS top_of_well,
           count(*)            AS n_hours,
           count(a.v1)         AS n_v1,
           count(a.v2)         AS n_v2,
           count(a.v3)         AS n_v3,
           count(a.v4)         AS n_v4,
           count_if(a.rule_a)  AS n_rule_a,
           count_if(a.rule_b)  AS n_rule_b,
           count(a.head)       AS n_valid_after_flags,
           '{RUN_ID}'          AS run_id
    FROM {OUT}.auto_validated_meting a
    LEFT JOIN {OUT}.source_copy_meetreeks d USING (meetreeks_id)
    GROUP BY a.meetreeks_id, a.vendor, d.code, d.begin_filter, d.eind_filter, d.bovenkant_peilbuis
""")
(summary.write.mode("overwrite").option("overwriteSchema", "true")
        .saveAsTable(f"{OUT}.auto_validation_summary"))

display(spark.sql(f"""
    SELECT vendor, count(*) AS series, sum(n_hours) AS hours,
           sum(n_v1) AS v1, sum(n_v2) AS v2, sum(n_v3) AS v3, sum(n_v4) AS v4,
           sum(n_rule_a) AS rule_a_report_only, sum(n_rule_b) AS rule_b_applied,
           round(100.0 * sum(n_valid_after_flags) / sum(n_hours), 1) AS pct_kept_by_v1_v4
    FROM {OUT}.auto_validation_summary GROUP BY vendor ORDER BY vendor
"""))
dbutils.jobs.taskValues.set(key="run_id", value=RUN_ID)
dbutils.notebook.exit(RUN_ID)
