# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Probe the environment
# MAGIC Fail in seconds, not twenty fits in. Verifies, in order:
# MAGIC 1. **Source reads** — `gold_prd.hl_grondwaterpeilmeting` is reachable and read-only.
# MAGIC 2. **Table writes** — the personal schema accepts a create/drop (confirmed working).
# MAGIC 3. **Open DQ failures** — `meting.id` carries a known duplicate-key `critical`.
# MAGIC 4. **git push** — control-plane push works despite the cluster egress block.
# MAGIC 5. **Volume write** — the fallback transport's grant (separate, unconfirmed).
# MAGIC
# MAGIC Run this first. Its last cell prints the transport the export job should use.

# COMMAND ----------
import os
import sys

dbutils.widgets.text("source_table", "gold_prd.hl_grondwaterpeilmeting.meting")
dbutils.widgets.text("output_catalog", "waterkeringbeheer_prd")
dbutils.widgets.text("output_schema", "jvanruitenbeek")
dbutils.widgets.text("repo_root", "")  # blank = walk up for pyproject.toml
dbutils.widgets.text("branch", "main")
dbutils.widgets.text("git_token_secret", "")  # "scope/key" of a PAT for push; blank = origin creds

SOURCE_TABLE = dbutils.widgets.get("source_table")
OUT_CATALOG = dbutils.widgets.get("output_catalog")
OUT_SCHEMA = dbutils.widgets.get("output_schema")
BRANCH = dbutils.widgets.get("branch")


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


def repo_root() -> str:
    override = dbutils.widgets.get("repo_root").strip()
    if override:
        return override
    here = os.getcwd()
    for d in [here, *[str(p) for p in __import__("pathlib").Path(here).parents]]:
        if os.path.exists(os.path.join(d, "pyproject.toml")):
            return d
    return here


ROOT = repo_root()
sys.path.insert(0, os.path.join(ROOT, "databricks_code", "lib"))
import git_sync  # noqa: E402
import source_read  # noqa: E402

print("repo_root:", ROOT)

# COMMAND ----------
# 1 · Source reads (read-only)
print("current_catalog():", spark.sql("SELECT current_catalog()").first()[0],
      "  <- metastore name, NOT a usable catalog (use 3-part names)")
n = spark.sql(f"SELECT count(*) FROM {SOURCE_TABLE}").first()[0]
print(f"{SOURCE_TABLE}: {n:,} rows reachable")
display(spark.sql(f"SHOW GRANTS ON SCHEMA {source_read.SOURCE_SCHEMA}"))

# COMMAND ----------
# 2 · Table writes to the personal schema (confirmed path)
probe_tbl = f"{OUT_CATALOG}.{OUT_SCHEMA}.__probe_tbl"
spark.sql(f"CREATE OR REPLACE TABLE {probe_tbl} AS SELECT 1 AS ok")
assert spark.table(probe_tbl).first()[0] == 1
spark.sql(f"DROP TABLE {probe_tbl}")
print(f"table write OK -> {OUT_CATALOG}.{OUT_SCHEMA}")

# COMMAND ----------
# 3 · Open data-quality failures (check before trusting a run)
display(spark.sql(source_read.dq_failures_sql()))

# COMMAND ----------
# 4 · git push probe (control plane)
git_probe = git_sync.probe_push(ROOT, branch=BRANCH, token=git_token())
print(git_probe)

# COMMAND ----------
# 5 · Volume write probe (fallback transport grant)
vol_probe = git_sync.probe_volume_write()
print(vol_probe)

# COMMAND ----------
# Verdict
transport = "git" if git_probe.ok else ("volume" if vol_probe.ok else "NONE")
print("=" * 60)
print(f"USE TRANSPORT: {transport}")
print(f"  git push : {'OK' if git_probe.ok else 'NO -> ' + git_probe.detail}")
print(f"  volume   : {'OK' if vol_probe.ok else 'NO -> ' + vol_probe.detail}")
print("=" * 60)
if transport == "NONE":
    raise RuntimeError("Neither git push nor Volume write is available — resolve a grant "
                       "before running 02_export_to_git.")
dbutils.jobs.taskValues.set(key="transport", value=transport)
dbutils.notebook.exit(transport)
