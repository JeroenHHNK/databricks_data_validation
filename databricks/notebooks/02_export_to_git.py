# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Export the copy -> git
# MAGIC Turns the personal-workspace Delta copy into the `source_data/` CSV layout the
# MAGIC validation toolkit reads, writes it into the Git folder working tree, and pushes.
# MAGIC
# MAGIC Layout (forward-slashed, git-tracked):
# MAGIC ```
# MAGIC source_data/<vendor>/<origin>/<series>.csv     # Time,head  (hourly, gaps dropped)
# MAGIC source_data/<vendor>/<origin>/OBJECT_DATA.csv   # filter depths + cross-section per series
# MAGIC ```
# MAGIC Transport is whatever `00_probe_env` returned: `git` (commit+push) or `volume`
# MAGIC (copy to the misc_files Volume for the local CLI bridge to pick up).

# COMMAND ----------
import os
import sys

dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("transport", "git")        # git | volume (from 00_probe_env)
dbutils.widgets.text("branch", "main")
dbutils.widgets.text("run_id", "")
dbutils.widgets.text("repo_root", "")

OUT = f'{dbutils.widgets.get("output_catalog")}.{dbutils.widgets.get("output_schema")}'
TRANSPORT = dbutils.widgets.get("transport")
BRANCH = dbutils.widgets.get("branch")
RUN_ID = dbutils.widgets.get("run_id").strip() or "manual"

ROOT = dbutils.widgets.get("repo_root").strip() or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "databricks", "lib"))
import git_sync  # noqa: E402
import source_read as sr  # noqa: E402

SOURCE_DATA = os.path.join(ROOT, "source_data")
print("writing to:", SOURCE_DATA, "| transport:", TRANSPORT)

# COMMAND ----------
# MAGIC %md ### Resolve names from the dimension
# One row per series with its vendor/origin/series tokens and metadata for OBJECT_DATA.
dim = spark.table(f"{OUT}.source_copy_meetreeks").toPandas()
names = {}
for _, r in dim.iterrows():
    sn = sr.name_series(r["meetreeks_id"], r.get("code"), r.get("bronsysteem"), r.get("sensor_sleutel"))
    names[r["meetreeks_id"]] = (sn, r)
print(len(names), "series to export")

# COMMAND ----------
# MAGIC %md ### Write per-series only_csv files + per-origin OBJECT_DATA.csv
import pandas as pd  # noqa: E402

copy = spark.table(f"{OUT}.source_copy_meting")
written = []
object_rows: dict[tuple[str, str], list[dict]] = {}

for mid, (sn, meta) in names.items():
    pdf = (copy.where(copy.meetreeks_id == mid)
               .select("timestamp", "head").orderBy("timestamp").toPandas())
    if pdf.empty:
        continue
    pdf = pdf.dropna(subset=["head"]).rename(columns={"timestamp": "Time", "head": "head"})
    rel = sn.rel_csv
    dst = os.path.join(SOURCE_DATA, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    pdf.to_csv(dst, index=False)
    written.append(os.path.join("source_data", *rel.split("/")))

    key = (sn.vendor, sn.origin)
    object_rows.setdefault(key, []).append({
        "series": sn.series, "meetreeks_id": mid, "code": meta.get("code"),
        "begin_filter": meta.get("begin_filter"), "eind_filter": meta.get("eind_filter"),
        "bovenkant_peilbuis": meta.get("bovenkant_peilbuis"),
        "peilgebied_code": meta.get("peilgebied_code"),
        "lon": meta.get("lon"), "lat": meta.get("lat"),
    })

for (vendor, origin), rows in object_rows.items():
    rel = f"{vendor}/{origin}/OBJECT_DATA.csv"
    dst = os.path.join(SOURCE_DATA, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    pd.DataFrame(rows).to_csv(dst, index=False)
    written.append(os.path.join("source_data", *rel.split("/")))

print(f"wrote {len(written)} files under source_data/")

# COMMAND ----------
# MAGIC %md ### Ship it
if TRANSPORT == "git":
    git_sync.commit_and_push(
        ROOT, paths=["source_data"],
        message=f"source replication: run {RUN_ID} ({len(names)} series)", branch=BRANCH)
    print("committed & pushed source_data/")
else:
    dest = git_sync.write_tree_to_volume(ROOT, written)
    print("copied to Volume for the local bridge:", dest)
    print("Now, on a machine with the Databricks CLI, run the bridge in databricks/README.md")

dbutils.notebook.exit(str(len(written)))
