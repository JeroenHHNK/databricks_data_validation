# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Validated data -> Delta table
# MAGIC Closes the loop. After the locally-validated copies in `input_output/output_repository/`
# MAGIC have been pushed from the IDE and pulled into this Git folder, reads them, keeps the
# MAGIC approved readings, joins them back to the source copy on `meetreeks_id`, and writes a
# MAGIC quality-checked Delta table for further analysis. The source is still never touched.
# MAGIC
# MAGIC Output: `waterkeringbeheer_prd.jvanruitenbeek.validated_meting` (append, run-tagged).
# MAGIC This table is a natural future source for the Tarso pipeline, which currently ignores
# MAGIC `validatie_status`.
# MAGIC
# MAGIC Review files are matched to a series by name:
# MAGIC * **flat** `output_repository/<filename>.csv` — `filename` from
# MAGIC   `output_databricks/metadata/object_data.csv` (the layout the export uses), or
# MAGIC * **nested** `output_repository/<vendor>/<origin>/<series>.csv` — the on-prem layout,
# MAGIC   kept so migrated review work still loads.
# MAGIC
# MAGIC > **PROVISIONAL** until the local validation toolkit is wired in and fixes the final
# MAGIC > `output_repository/` layout.

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
while not os.path.isdir(os.path.join(ROOT, "databricks_code", "lib")):
    parent = os.path.dirname(ROOT)
    if parent == ROOT:
        raise FileNotFoundError("Could not find databricks_code/lib above the notebook; set the repo_root widget.")
    ROOT = parent
sys.path.insert(0, os.path.join(ROOT, "databricks_code", "lib"))
import source_read as sr  # noqa: E402

VALIDATION_DATA = os.path.join(ROOT, "input_output", "output_repository")
OBJECT_DATA = os.path.join(ROOT, "input_output", "output_databricks", "metadata", "object_data.csv")

# COMMAND ----------
# Name -> meetreeks_id maps: flat `filename` from object_data.csv, nested from the dimension.
import pandas as pd  # noqa: E402

file_to_id = {}
if os.path.exists(OBJECT_DATA):
    od = pd.read_csv(OBJECT_DATA, usecols=["filename", "meetreeks_id", "vendor"])
    file_to_id = {r.filename: (r.meetreeks_id, r.vendor) for r in od.itertuples(index=False)}

dim = spark.table(f"{OUT}.source_copy_meetreeks").toPandas()
key_to_id = {}
for _, r in dim.iterrows():
    sn = sr.name_series(r["meetreeks_id"], r.get("code"), r.get("bronsysteem"), r.get("sensor_sleutel"))
    key_to_id[(sn.vendor, sn.origin, sn.series)] = (r["meetreeks_id"], sn.vendor)
print(f"{len(file_to_id)} flat names, {len(key_to_id)} nested names")

# COMMAND ----------
# Read review CSVs (utf-8-sig; all three verdict columns required).
REQUIRED = {"raw_data", "gecontroleerd", "afgekeurd"}
frames, skipped = [], []
for dirpath, _dirs, files in os.walk(VALIDATION_DATA):
    for fn in files:
        if not fn.endswith(".csv") or fn.lower() == "object_data.csv":
            continue
        path = os.path.join(dirpath, fn)
        rel = os.path.relpath(path, VALIDATION_DATA).replace("\\", "/").split("/")
        stem = os.path.splitext(rel[-1])[0]
        if len(rel) == 1:
            hit = file_to_id.get(stem)
        elif len(rel) >= 3:
            hit = key_to_id.get((rel[0], rel[1], stem))
        else:
            hit = None
        if hit is None:
            skipped.append((path, "no matching series")); continue
        mid, vendor = hit

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
