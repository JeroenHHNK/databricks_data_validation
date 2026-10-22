# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "6"
# ///
# MAGIC %md
# MAGIC # 02 · Export the copy -> output_databricks/
# MAGIC Turns the personal-workspace Delta copy into a flat per-series CSV layout plus one
# MAGIC combined metadata table, written into the Git folder working tree. Everything pulled
# MAGIC from the Databricks source lives under `output_databricks/`.
# MAGIC
# MAGIC Layout (forward-slashed, git-tracked):
# MAGIC ```
# MAGIC output_databricks/time_series_data/<series>.csv   # Time,head (hourly); one file per series
# MAGIC output_databricks/metadata/object_data.csv        # one row per series: filename, ids, filters, coords
# MAGIC ```
# MAGIC Flat data + a single metadata CSV is easier to loop over than nested vendor/origin
# MAGIC folders: read `output_databricks/metadata/object_data.csv`, and for each row open
# MAGIC `output_databricks/time_series_data/<filename>.csv`.
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

# Repo root: widget if set, otherwise walk up from the notebook until databricks/lib is found.
ROOT = dbutils.widgets.get("repo_root").strip()
if not ROOT:
    nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    p = nb_path if nb_path.startswith("/Workspace") else "/Workspace" + nb_path
    while not os.path.isdir(os.path.join(p, "databricks", "lib")):
        parent = os.path.dirname(p)
        if parent == p or parent in ("", "/"):
            raise FileNotFoundError("Could not find databricks/lib above the notebook; set the repo_root widget.")
        p = parent
    ROOT = p

LIB = os.path.join(ROOT, "databricks", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import git_sync  # noqa: E402
import source_read as sr  # noqa: E402

OUTPUT_DBX = os.path.join(ROOT, "output_databricks")
TS_DIR = os.path.join(OUTPUT_DBX, "time_series_data")
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

# COMMAND ----------
# Write one flat CSV per series + one combined metadata/object_data.csv.
# ONE global-sorted pass over source_copy_meting (not one query per series): rows arrive
# grouped by meetreeks_id, so we buffer a single series at a time and flush it. The table
# is partitioned by vendor, so a per-series .where() can't prune and would rescan millions
# of rows per series; this scans once and keeps driver memory bounded to one series.
import pandas as pd  # noqa: E402

# Clean the output dirs so a rerun never leaves stale series behind.
for d in (TS_DIR, META_DIR):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)

ordered = (spark.table(f"{OUT}.source_copy_meting")
                .select("meetreeks_id", "timestamp", "head")
                .orderBy("meetreeks_id", "timestamp"))

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


def _flush(mid, rows):
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
    written_rel.append(f"output_databricks/time_series_data/{stem}.csv")
    meta_rows.append({
        "filename": stem, "meetreeks_id": mid, "vendor": sn.vendor, "origin": sn.origin,
        "series": sn.series, "code": meta.get("code"),
        "begin_filter": meta.get("begin_filter"), "eind_filter": meta.get("eind_filter"),
        "bovenkant_peilbuis": meta.get("bovenkant_peilbuis"),
        "peilgebied_code": meta.get("peilgebied_code"),
        "lon": meta.get("lon"), "lat": meta.get("lat"),
        "n_rows": len(rows),
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

# One combined metadata table (sorted for a stable, reviewable diff).
meta_df = pd.DataFrame(meta_rows).sort_values(["vendor", "origin", "series"]).reset_index(drop=True)
meta_df.to_csv(os.path.join(META_DIR, "object_data.csv"), index=False)
written_rel.append("output_databricks/metadata/object_data.csv")

print(f"wrote {len(meta_rows)} series to output_databricks/time_series_data/ and "
      f"output_databricks/metadata/object_data.csv "
      f"({missing} series in the copy without a dimension name)")

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
        ROOT, paths=["output_databricks"],
        message=f"source replication: run {RUN_ID} ({len(meta_rows)} series)",
        branch=BRANCH, token=git_token())
    print("committed & pushed output_databricks/")
elif TRANSPORT == "volume":
    dest = git_sync.write_tree_to_volume(ROOT, written_rel)
    print("copied to Volume for the local bridge:", dest)
    print("Now, on a machine with the Databricks CLI, run the bridge in databricks/README.md")
else:  # "write" (default): leave the files in the working tree; push manually
    print(f"{len(written_rel)} files written under output_databricks/.")
    print("Nothing pushed. Commit & push from Databricks (Git folder UI) when ready.")

dbutils.notebook.exit(str(len(meta_rows)))
