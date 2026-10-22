# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Validated data -> Delta table
# MAGIC Closes the loop. After the locally-validated copies in `output_repository/` have been
# MAGIC pushed from the IDE and pulled into this Git folder, reads them, keeps the approved
# MAGIC readings, joins them back to the source copy on `meetreeks_id`, and writes a
# MAGIC quality-checked Delta table for further analysis. The source is still never touched.
# MAGIC
# MAGIC Output: `waterkeringbeheer_prd.jvanruitenbeek.validated_meting` (append, run-tagged).
# MAGIC This table is a natural future source for the Tarso pipeline, which currently ignores
# MAGIC `validatie_status`.
# MAGIC
# MAGIC > **PROVISIONAL.** The exact `output_repository/` layout (flat vs nested, and the join
# MAGIC > key) is finalized when the local validation toolkit is wired in. The matching logic
# MAGIC > below still assumes the old `<vendor>/<origin>/<series>.csv` shape and will be
# MAGIC > realigned to the new metadata `filename` join then.

# COMMAND ----------
import os
import sys
from datetime import datetime, timezone

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("keep_only_approved", "true")  # gecontroleerd==1 & afgekeurd!=1
dbutils.widgets.text("run_id", "")
dbutils.widgets.text("repo_root", "")

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
KEEP_APPROVED = dbutils.widgets.get("keep_only_approved").lower() == "true"
RUN_ID = dbutils.widgets.get("run_id").strip() or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

ROOT = dbutils.widgets.get("repo_root").strip() or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "databricks", "lib"))
import source_read as sr  # noqa: E402

VALIDATION_DATA = os.path.join(ROOT, "output_repository")

# COMMAND ----------
# Rebuild the name -> meetreeks_id map from the dimension.
dim = spark.table(f"{OUT}.source_copy_meetreeks").toPandas()
key_to_id = {}
for _, r in dim.iterrows():
    sn = sr.name_series(r["meetreeks_id"], r.get("code"), r.get("bronsysteem"), r.get("sensor_sleutel"))
    key_to_id[(sn.vendor, sn.origin, sn.series)] = r["meetreeks_id"]

# COMMAND ----------
# Read review CSVs (utf-8-sig; all three verdict columns required).
import pandas as pd  # noqa: E402

REQUIRED = {"raw_data", "gecontroleerd", "afgekeurd"}
frames, skipped = [], []
for dirpath, _dirs, files in os.walk(VALIDATION_DATA):
    for fn in files:
        if not fn.endswith(".csv") or fn == "OBJECT_DATA.csv":
            continue
        path = os.path.join(dirpath, fn)
        rel = os.path.relpath(path, VALIDATION_DATA).replace("\\", "/").split("/")
        if len(rel) < 3:
            skipped.append((path, "not <vendor>/<origin>/<series>.csv")); continue
        vendor, origin, series = rel[0], rel[1], os.path.splitext(rel[-1])[0]
        mid = key_to_id.get((vendor, origin, series))
        if mid is None:
            skipped.append((path, "no matching series in dimension")); continue

        df = pd.read_csv(path, encoding="utf-8-sig")
        df.columns = [c.strip().lower() for c in df.columns]
        if not REQUIRED.issubset(df.columns):
            skipped.append((path, "missing verdict columns (treated as raw/unreviewed)")); continue

        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["gecontroleerd"] = pd.to_numeric(df["gecontroleerd"], errors="coerce").fillna(0).astype(int)
        df["afgekeurd"] = pd.to_numeric(df["afgekeurd"], errors="coerce").fillna(0).astype(int)
        if KEEP_APPROVED:
            df = df[(df["gecontroleerd"] == 1) & (df["afgekeurd"] != 1)]
        if df.empty:
            continue
        out = df[["timestamp", "raw_data", "gecontroleerd", "afgekeurd"]].copy()
        out["note"] = df["note"] if "note" in df.columns else None
        out["meetreeks_id"] = mid
        out["vendor"] = vendor
        frames.append(out)

print(f"matched {len(frames)} series; skipped {len(skipped)}")
for p, why in skipped[:20]:
    print("  skip:", why, "->", p)

# COMMAND ----------
# Write validated_meting (append, run-tagged).
if not frames:
    dbutils.notebook.exit("0")
validated = pd.concat(frames, ignore_index=True)
validated = validated.rename(columns={"raw_data": "head"})
validated["run_id"] = RUN_ID

sdf = spark.createDataFrame(validated)
(sdf.write.mode("append").option("mergeSchema", "true")
    .partitionBy("vendor")
    .saveAsTable(f"{OUT}.validated_meting"))

display(spark.sql(f"""
    SELECT vendor, count(DISTINCT meetreeks_id) AS series, count(*) AS approved_rows
    FROM {OUT}.validated_meting WHERE run_id = '{RUN_ID}' GROUP BY vendor ORDER BY vendor
"""))
dbutils.notebook.exit(str(len(validated)))
