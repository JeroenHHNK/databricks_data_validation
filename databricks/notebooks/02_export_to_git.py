# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Export the copy -> source_data/
# MAGIC Turns the personal-workspace Delta copy into the `source_data/` CSV layout the
# MAGIC validation toolkit reads, writing it into the Git folder working tree.
# MAGIC
# MAGIC Layout (forward-slashed, git-tracked):
# MAGIC ```
# MAGIC source_data/<vendor>/<origin>/<series>.csv     # Time,head  (hourly, gaps dropped)
# MAGIC source_data/<vendor>/<origin>/OBJECT_DATA.csv   # filter depths + cross-section per series
# MAGIC ```
# MAGIC `transport` controls shipping:
# MAGIC * **`write`** (default) — only write the files; **commit & push manually** from the
# MAGIC   Databricks Git-folder UI. Needs no shell `git`, so it also runs on serverless.
# MAGIC * `git` — also commit & push automatically (uses the `git_token_secret` PAT).
# MAGIC * `volume` — copy to the misc_files Volume for the local CLI bridge.

# COMMAND ----------
import os
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

ROOT = dbutils.widgets.get("repo_root").strip() or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "databricks", "lib"))
import git_sync  # noqa: E402
import source_read as sr  # noqa: E402

SOURCE_DATA = os.path.join(ROOT, "source_data")
print("writing to:", SOURCE_DATA, "| transport:", TRANSPORT)

# COMMAND ----------
# Resolve names from the dimension.
# One row per series with its vendor/origin/series tokens and metadata for OBJECT_DATA.
dim = spark.table(f"{OUT}.source_copy_meetreeks").toPandas()
names = {}
for _, r in dim.iterrows():
    sn = sr.name_series(r["meetreeks_id"], r.get("code"), r.get("bronsysteem"), r.get("sensor_sleutel"))
    names[r["meetreeks_id"]] = (sn, r)
print(len(names), "series to export")

# COMMAND ----------
# Write per-series only_csv files + per-origin OBJECT_DATA.csv.
# ONE global-sorted pass over source_copy_meting (not one query per series): rows
# arrive grouped by meetreeks_id, so we buffer a single series at a time and flush
# it to <vendor>/<origin>/<series>.csv. The table is partitioned by vendor, so a
# per-series .where() can't prune and would rescan millions of rows 693 times;
# this scans once and keeps driver memory bounded to one series.
import pandas as pd  # noqa: E402

ordered = (spark.table(f"{OUT}.source_copy_meting")
                .select("meetreeks_id", "timestamp", "head")
                .orderBy("meetreeks_id", "timestamp"))

written = []
object_rows: dict[tuple[str, str], list[dict]] = {}
missing = 0


def _flush(mid, rows):
    global missing
    if not rows:
        return
    entry = names.get(mid)
    if entry is None:            # series in the copy but not in the dimension
        missing += 1
        return
    sn, meta = entry
    rel = sn.rel_csv
    dst = os.path.join(SOURCE_DATA, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    pd.DataFrame(rows, columns=["Time", "head"]).to_csv(dst, index=False)
    written.append(os.path.join("source_data", *rel.split("/")))
    object_rows.setdefault((sn.vendor, sn.origin), []).append({
        "series": sn.series, "meetreeks_id": mid, "code": meta.get("code"),
        "begin_filter": meta.get("begin_filter"), "eind_filter": meta.get("eind_filter"),
        "bovenkant_peilbuis": meta.get("bovenkant_peilbuis"),
        "peilgebied_code": meta.get("peilgebied_code"),
        "lon": meta.get("lon"), "lat": meta.get("lat"),
    })


cur, buf = None, []
for row in ordered.toLocalIterator():
    mid = row["meetreeks_id"]
    if mid != cur:
        _flush(cur, buf)
        cur, buf = mid, []
    if row["head"] is not None:
        buf.append((row["timestamp"], row["head"]))
_flush(cur, buf)

for (vendor, origin), rows in object_rows.items():
    rel = f"{vendor}/{origin}/OBJECT_DATA.csv"
    dst = os.path.join(SOURCE_DATA, *rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    pd.DataFrame(rows).to_csv(dst, index=False)
    written.append(os.path.join("source_data", *rel.split("/")))

print(f"wrote {len(written)} files under source_data/ "
      f"({len(object_rows)} OBJECT_DATA.csv, {missing} series without a dimension name)")

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
        ROOT, paths=["source_data"],
        message=f"source replication: run {RUN_ID} ({len(names)} series)",
        branch=BRANCH, token=git_token())
    print("committed & pushed source_data/")
elif TRANSPORT == "volume":
    dest = git_sync.write_tree_to_volume(ROOT, written)
    print("copied to Volume for the local bridge:", dest)
    print("Now, on a machine with the Databricks CLI, run the bridge in databricks/README.md")
else:  # "write" (default): leave the files in the working tree; push manually
    print(f"{len(written)} files written to the Git folder working tree under source_data/.")
    print("Nothing pushed. Commit & push from Databricks (Git folder UI) when ready.")

dbutils.notebook.exit(str(len(written)))
