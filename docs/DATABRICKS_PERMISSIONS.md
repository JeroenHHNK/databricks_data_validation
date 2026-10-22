# Databricks permissions, folders and where the repo lives

What this pipeline is allowed to touch, and where everything sits. Written because the
grants are asymmetric — the data we read and the data we write live in different
catalogs, and getting that wrong fails a job twenty fits in rather than in the first
second.

Workspace: `dbw_hhnkda_prd_05`. Verified 2026-10-20 against the code and the grants
recorded in it.

---

## The rule in one line

**Read from `gold_prd`. Write only to `waterkeringbeheer_prd` — the `jvanruitenbeek`
schema for tables, the `misc_files` volume for files. Never write to `gold_prd`.**

---

## 1. The three places

| | Location | Access | Holds |
|---|---|---|---|
| **Source** | `gold_prd.hl_grondwaterpeilmeting` | **read-only** | the head measurements (`meting`) and eight related tables |
| **Our tables** | `waterkeringbeheer_prd.jvanruitenbeek` | read + write | manifest, fit metrics, EVT roll-ups |
| **Our files** | `/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline` | read + write *(see §4)* | `.pas` models, plots, CSVs, forcing |

All three are addressed by **explicit three-part names** in
[`paths.py`](../src/tarso_pipeline/paths.py), never by a relative name against a current
catalog. That is deliberate — see the `currentCatalog` gotcha in §7.

---

## 2. Source: `gold_prd` is read-only

Grants held by the group:

| Level | Granted | **Not** granted |
|---|---|---|
| Catalog `gold_prd` | `BROWSE`, `USE CATALOG` | — |
| Schema `hl_grondwaterpeilmeting` | `USE SCHEMA`, `SELECT`, `EXECUTE`, `READ VOLUME` | `CREATE TABLE`, `CREATE SCHEMA`, `WRITE VOLUME` |

**Do not attempt to write there** — not a scratch table, not a temp view materialised to
disk, not a volume file. `paths.py` records this at the top of its write-target block so
the constraint travels with the code.

Reading is unrestricted: `SELECT` on any of the nine tables, documented in
[`DATABRICKS_HANDOFF.md` §2.0](DATABRICKS_HANDOFF.md).

---

## 3. Our tables: `waterkeringbeheer_prd.jvanruitenbeek`

✅ **Confirmed working.** A full `CREATE → INSERT → READ → DROP` lifecycle was exercised
against this schema, so table writes are not in doubt.

The schema is named after the user — it is the **personal schema**, the Unity Catalog
equivalent of a home folder. Five tables:

| Table | Written by | Mode | Grain |
|---|---|---|---|
| `pastas_series_verdict` | `exploration/explore_meting.py` *(by hand)* | `overwrite` | one row per source series, with a verdict |
| `pastas_series_manifest` | `ingestion/build_manifest.py` **(task 1)** | `overwrite` | one row per series spanning >= `min_days` |
| `pastas_fit_metrics` | `transformation/fit_pastas_models.py` **(task 2)** | `append` | one row per series × model type × run |
| `pastas_evt_selection` | `transformation/run_extremes.py` **(task 3)** | `append` | one row per model × tail |
| `pastas_extremes_summary` | `transformation/run_extremes.py` **(task 3)** | `append` | one row per model |

**`overwrite` vs `append` matters.** The two `overwrite` tables hold only the latest
state; the three `append` tables accumulate every run. So reading metrics without a
`WHERE run_id = …` gives you every run ever, stacked.

Both schema and catalog are widget-overridable (`output_catalog`, `output_schema`), so
pointing a run at a different personal schema needs no code change.

---

## 4. Our files: the `misc_files` volume

```
/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline
          ^catalog            ^schema     ^volume
```

The repetition is **not a typo** — the schema and the volume inside it are both called
`misc_files`. A Volume path is always
`/Volumes/<catalog>/<schema>/<volume>/…`, so a schema and volume sharing a name
produces a doubled segment.

### ⚠️ `WRITE VOLUME` is a separate grant and is **not yet confirmed**

Table write access does **not** imply volume write access. They are independent grants,
and only the table one has been exercised.

Task 2 therefore **probes it before fitting anything**
([`fit_pastas_models.py:208`](../notebooks/transformation/fit_pastas_models.py#L208)):
it writes `__write_test.txt` to the volume base, deletes it, and raises a named error if
that fails. A missing grant costs you seconds, not a whole run.

If the probe fails, you have two options:

1. Request `WRITE VOLUME` on `waterkeringbeheer_prd.misc_files.misc_files`, or
2. Point the `volume_base` widget at somewhere you can already write.

### Folder layout

```
pastas_pipeline/
├── knmi_forcing/                        ← inputs, uploaded by hand
│   ├── knmi_forcing_249.csv                 observed KNMI forcing (also in the repo)
│   ├── imprex_berkhout_ens01_daily_precipitation.csv   ┐
│   ├── imprex_berkhout_ens01_daily_evaporation.csv     │ 16 members × 2 variables
│   │   …                                               ┘ = 32 files
│   └── bias_correction_run_log.json         how the IMPREX forcing was corrected
│
└── run_yy_mm_dd/                        ← one folder per run, all stages together
    ├── pas_files/                           series_<hash24>_<model>.pas      (task 2)
    ├── results_files/                       …_results.png                    (task 2)
    ├── diagnostics_files/                   …_diagnostics.png                (task 2)
    ├── csv/                                 <series>_return_periods.csv      (task 3)
    ├── pooled_extremes/                     <series>_{dry,wet}.csv           (task 3)
    ├── used_extremes/                       used_extremes_<series>.csv       (task 3)
    ├── return_plots_fitted/                 one PNG per model                (task 3)
    ├── qq_plots/                            one PNG per model                (task 3)
    ├── confidence_plots/                    one PNG per model                (task 3)
    ├── evt_selection.csv                    run roll-up                      (task 3)
    ├── tail_parameters.csv                  run roll-up                      (task 3)
    ├── bias_correction_run_log.json         copied from knmi_forcing/        (task 3)
    └── run_yy_mm_dd_extremes_summary.csv    the run's report card            (task 3)
```

Stage 3 writes **inside** the stage-2 run folder by design: one folder is one complete
run, models and return periods together, and the provenance of the forcing travels with
the numbers.

**Two runs on the same day share a folder** and overwrite each other's artefacts, while
both still append their metrics rows. Set the `run_folder` widget to keep them apart.

Folder names come from [`paths.py`](../src/tarso_pipeline/paths.py)
(`ARTEFACT_DIRS`, `EXTREMES_DIRS`), never from a literal in a notebook, so renaming one
is a single-line change.

---

## 5. Where the repository lives

### On GitHub

`JeroenHHNK/databricks_pastas_pipeline` — **private**. The single source of truth.

### On Databricks: a Git folder

Add it as **Workspace → Create → Git folder**, not by uploading notebooks. A Git folder
means deployment is `git pull` in the UI, and the running code always corresponds to a
commit you can name.

> **Confirm your own path.** A Git folder lands under either
> `/Workspace/Users/<your-email>/<repo>` or `/Workspace/Repos/<your-email>/<repo>`
> depending on workspace configuration. Nothing in this repo pins it, and nothing needs
> to — see below.

Notebooks never hardcode a workspace path. `repo_root()` walks up for `pyproject.toml`,
so the same code runs from a Git folder, a local checkout or a different user's folder
untouched. It deliberately avoids `__file__`, which Databricks leaves undefined because
it `exec`s workspace `.py` files rather than running them as scripts.

To make the package importable in a notebook:

```python
%pip install -e /Workspace/Repos/<user>/<repo>     # or build a wheel and attach it
```

### Your personal workspace folder

`/Workspace/Users/<your-email>/` is writable, and it is where the Git folder and any
scratch notebooks live. **Do not put pipeline data there.** Workspace files are not a
data store: no Delta, no versioning you can query, and nothing outside your own account
can read them reliably. Data belongs in the volume, tables in your schema.

---

## 6. What travels how

Three different mechanisms, deliberately:

| Data | Lives in | Reaches the cluster via | Why |
|---|---|---|---|
| Code | git | Git folder pull | — |
| **Observed KNMI forcing** (~58 KB) | **git** (`data/forcing/knmi_forcing_249.csv`) | Git folder pull | **the cluster has no outbound internet** |
| IMPREX ensemble (32 files) | volume `knmi_forcing/` | already there | too large to commit, and static |
| Models, plots, CSVs | volume `run_yy_mm_dd/` | written by the job | outputs |
| Tables | `jvanruitenbeek` schema | written by the job | outputs |

### Why the forcing is committed

The cluster cannot resolve `www.daggegevens.knmi.nl` — a DNS/egress block, **not** an
authorisation problem; the KNMI API needs no credentials. So the fetch runs on a laptop
and the result is committed:

```bash
python scripts/check_knmi.py --station 249 --backlog-start 1970-01-01
git add data/forcing && git commit -m "Refresh KNMI forcing" && git push
#   then: pull the Git folder in Databricks
```

Committing also pins the data version to the commit that used it. The cost is that the
pipeline **cannot run unattended** — a refresh needs a person. The real fix is cluster
egress, after which `source=api` runs on a schedule and `data/forcing/` can go.

`.gitignore` enforces the split: `*.csv` is ignored, with exceptions only for
`data/forcing/*.csv` and `tests/data/*.csv`. So `data/forcing_imprex/` (32 files, present
locally) stays out of git by default, and no model output can be committed by accident.

---

## 7. Gotchas

**`spark.catalog.currentCatalog()` returns `dbw_hhnkda_prd_05`.** That is the
workspace/metastore name, **not** a catalog you can query. A two-part name resolved
against it will not find anything. Every statement in this repo uses explicit three-part
names for exactly this reason.

**`WRITE VOLUME` ≠ table write.** Covered in §4. Independent grants.

**`gold_prd` has volumes too,** and `READ VOLUME` is granted on them — but still no
`WRITE VOLUME`. Read-only means read-only for files as well as tables.

**Renaming a column changes an `append` table's schema silently.** Those three tables
use `mergeSchema=true`, so a renamed key is *added beside* the old one rather than
erroring — old rows keyed in one column, new rows in the other. After any column rename,
drop the append tables before the next run. The `overwrite` tables heal themselves.

---

## 8. Verifying any of this yourself

Everything above can be checked from a SQL cell. If a claim here and the output of these
disagree, the output is right and this file is stale.

```sql
SELECT current_user(), current_catalog();

-- what we can read
SHOW GRANTS ON CATALOG gold_prd;
SHOW GRANTS ON SCHEMA  gold_prd.hl_grondwaterpeilmeting;

-- what we can write
SHOW GRANTS ON SCHEMA  waterkeringbeheer_prd.jvanruitenbeek;
SHOW GRANTS ON VOLUME  waterkeringbeheer_prd.misc_files.misc_files;   -- the unconfirmed one

-- what is actually there
SHOW TABLES  IN waterkeringbeheer_prd.jvanruitenbeek;
SHOW VOLUMES IN waterkeringbeheer_prd.misc_files;
```

And for the volume, which `SHOW GRANTS` can overstate:

```python
import os
probe = "/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline/__write_test.txt"
os.makedirs(os.path.dirname(probe), exist_ok=True)
open(probe, "w").write("ok"); os.remove(probe)
print("writable")
```

That is the same probe task 2 runs before it fits anything.
