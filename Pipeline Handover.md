# Groundwater Data Validation Pipeline

Handover document — current architecture and proposed Databricks migration

- [What the pipeline does](#overview)
- [Current architecture](#architecture)
- [Package & module map](#packages)
- [Data flow & directory structure](#data-flow)
- [Validation logic (the core)](#validation-logic)
- [Streamlit dashboard](#dashboard)
- [CLI commands reference](#cli)
- [Proposed Databricks pipeline](#databricks)
- [Migration implementation steps](#migration-steps)

## 1. What the pipeline does

This is a **groundwater timeseries validation toolkit** for measurement wells monitored by two vendors: **Fugro** (CSV exports from Vista Data Vision) and **Wiertsema** (multi-sheet Excel workbooks). It compares hourly groundwater head readings (in meters above NAP datum) against KNMI precipitation and evaporation data, applies automated quality flags, generates interactive HTML plots, produces Excel summary reports, and provides a Streamlit dashboard for manual point-by-point human review.

The domain context is dike monitoring in the Noord-Holland region (HHNK water authority). Each measurement well has one or more physical sensors that are periodically replaced; the pipeline stitches these into logical timeseries and tracks sensor metadata (filter depth, cross-section location on the dike transect).

## 2. Current architecture

┌─────────────────────┐ ┌──────────────────────────┐ │ input_data/ │ │ input_stressors/ │ │ ├── Fugro/\*.csv │ │ └── Berkhout\_\*.txt │ │ └── Wiertsema/\*.xlsx │ (raw KNMI download) │ └────────┬────────────┘ └────────────┬─────────────┘ │ │ convert-fugro convert-knmi convert-wiertsema │ │ ▼ ▼ knmi_249_berkhout_hourly.csv ┌────────────────────┐ knmi\_\*\_daily_precipitation.csv │ output_data/ │ knmi\_\*\_daily_evaporation.csv │ ├── fugro/ │ │ │ └── \<origin>/ │◄──── validate ────┐ │ │ ├── only_csv/ (per-sensor CSVs) │ │ ├── validated/ (CSVs + v2/v3/v4 flags) │ │ └── figures/ (interactive HTML plots) │ └── wiertsema/ │ │ └── (same) │ report ────► validation_report\_\*.xlsx └────────┬───────────┘ │ ▼ ┌────────────────────┐ ┌────────────────────────────┐ │ validation_data/ │◄─────│ Streamlit Dashboard │ │ ├── \<vendor>/ │ │ (streamlit_app.py) │ │ │ └── \<origin>/ │ │ Manual review: reviewed / │ │ │ ├── \*.csv │ │ rejected per point │ │ │ └── OBJECT_DATA │ │ │ └── ... │ └────────────────────────────┘ └────────────────────┘

### Key design decisions

- **output_data/** is ephemeral and gitignored — it is fully rebuilt from vendor deliveries. The converters never clear old files, so you must wipe the vendor dirs for a clean rebuild.
- **validation_data/** is version-controlled — it holds the manual review state (gecontroleerd/afgekeurd per timestamp) and OBJECT_DATA.csv with hand-curated sensor metadata. These survive a rebuild.
- **input_stressors/** holds KNMI weather data (precipitation, Makkink evaporation) used as overlays in all plots and as context for the reviewer.
- The store schema is: `timestamp, raw_data, gecontroleerd (0/1), afgekeurd (0/1), note`. Timestamps are rounded to the hour for a stable merge key.

## 3. Package & module map

| Package | Module | Purpose |
| --- | --- | --- |
| `dataval/` | `cli.py` | Typer CLI entry point — all commands register here |
| `convert_fugro.py` | Parse Vista Data Vision CSVs → per-sensor `only_csv/` files |  |
| `convert_wiertsema.py` | Parse multi-sheet Excel workbooks → per-sensor `only_csv/` files |  |
| `convert_knmi.py` | Parse raw KNMI .txt → hourly/daily stressor CSVs |  |
| `validate.py` | Run v2/v3/v4 flags, generate Plotly HTML plots |  |
| `object_data.py` | Parse sensor metadata from filenames (filter depth, cross-section), build OBJECT_DATA.csv |  |
| `autoflag.py` | Rule A (sudden drops) + Rule B (below filter bottom) auto-rejection |  |
| `_utils.py` | Shared helpers: repo root, filename sanitization, stable key parsing, sensor file grouping |  |
| `dashboard/` | `config.py` | Load `config/settings.yaml` with defaults |
| `data_access.py` | List vendors/origins/series, build SeriesRef objects |  |
| `validation_store.py` | Load/save/merge validation CSVs (the core persistence layer) |  |
| `plotting.py` | Build the interactive Plotly figure with color-coded review status |  |
| `autoflags.py` | Load v2/v3/v4 flag hints from validated/ CSVs for the dashboard overlay |  |
| `overview.py` | Aggregate review statistics across all series |  |
| `sensor_stitch.py` | Multi-sensor stitching UI tab |  |
| `functions/` | `validation_functions.py` | Core validation algorithms: duplicate removal, step change, constant head, IQR outliers |
| `load_forcings.py` | Load precipitation/evaporation forcing data |  |
| `load_model.py` | Load Pastas groundwater models |  |
| `plot_functions.py` | Reusable Plotly plotting helpers |  |

### Dependencies

Python 3.12+, managed with Poetry. Key packages: `pandas`, `openpyxl`, `plotly`, `streamlit`, `typer`, `pastas`, `seaborn`, `pyyaml`, `xarray`.

## 4. Data flow & directory structure

```
input_data/
  Fugro/                    Raw Fugro CSV files (Vista Data Vision exports)
  Wiertsema/                Raw Wiertsema Excel workbooks
input_stressors/
  Berkhout_*.txt            Raw KNMI hourly download
  knmi_249_berkhout_hourly.csv              Converted (used by validate)
  knmi_249_berkhout_daily_precipitation.csv
  knmi_249_berkhout_daily_evaporation.csv
output_data/                                EPHEMERAL — rebuilt from vendor data
  fugro/<origin>/only_csv/                  Per-sensor CSVs
  fugro/<origin>/validated/                 CSVs with v2/v3/v4 flag columns
  fugro/<origin>/figures/                   Interactive HTML plots
  wiertsema/<origin>/...                    Same structure
  validation_report_fugro.xlsx
  validation_report_wiertsema.xlsx
validation_data/                            VERSION-CONTROLLED
  <vendor>/<origin>/<series>.csv           Manual review state
  <vendor>/<origin>/OBJECT_DATA.csv         Sensor metadata (filter depths, cross-section)
config/
  settings.yaml                             Dashboard configuration
```

### Origin naming

- **Fugro** origins are derived from the CSV filename: `4424-260484_HHW_normaal_Uur` (strips the date/export suffix, keeps the interval).
- **Wiertsema** origins use a canonical `NNNNN-N` key extracted from the workbook filename (e.g. `86349-1`).

## 5. Validation logic

Validation runs sequentially on each sensor's hourly timeseries. Each step operates on `head` as modified by the previous step (NaN where already flagged), stores the original value in a dedicated column, and sets flagged rows to NaN.

| Flag | Rule | Default thresholds |
| --- | --- | --- |
| `v2` | Unrealistic step change — hourly Δhead exceeds limits | max_up = +0.30 m/h, max_down = −0.05 m/h |
| `v3` | Constant head runs — values in the bottom band for too many consecutive hours | band = hmin + 0.15 m, min run = 96 steps |
| `v4` | Statistical outliers — Tukey / IQR method (Q1 − 1.5×IQR, Q3 + 1.5×IQR) | factor = 1.5 |

Before validation, `remove_duplicate_and_fill_missing` rounds timestamps to the hour, deduplicates, and inserts NaN rows for gaps. A `head_raw` column preserves the original readings.

### Autoflag rules (post-review layer)

| Rule | Logic | Default behavior |
| --- | --- | --- |
| **Rule A** | Drop ≥ 0.10 m in one hourly interval (contiguous data only) | Report-only (disagreed with human reviewers on most hits) |
| **Rule B** | Value ≤ bottom_filter + 0.10 m (physically impossible or unreliable) | Applied to validation store as `afgekeurd = 1` |

Rule A is skipped at sensor stitch boundaries (a step at a sensor changeover is usually recalibration). Rows already marked `gecontroleerd = 1` by a human are never overwritten.

## 6. Streamlit dashboard

The dashboard (`streamlit_app.py`) provides four tabs:

- **Overview** — Aggregate review statistics (rows, % reviewed, % rejected) per vendor/origin.
- **Validation** — Point-by-point review: toggle gecontroleerd/afgekeurd, add notes (max 30 chars), see color-coded plot (gray=unreviewed, green=approved, red=rejected).
- **Generate Data** — Trigger data conversion from the browser.
- **Sensor Stitching** — Visualize multi-sensor series overlap and stitching behavior.

### Store persistence

The validation store is one CSV per sensor series. The merge logic protects reviewed rows: when new raw data arrives, already-reviewed timestamps keep their status, notes, and validated `raw_data` value; new timestamps are appended as unreviewed. Saves are atomic (temp file + `os.replace`).

## 7. CLI commands reference

```
dataval convert-knmi    <raw .txt>          # KNMI weather → stressor CSVs
dataval convert-fugro   <input_dir>         # Fugro CSV → per-sensor only_csv/
dataval convert-wiertsema <input_dir>       # Wiertsema Excel → per-sensor only_csv/
dataval validate        <dataset>           # Run v2/v3/v4 + generate plots
dataval plot            <dataset>           # Re-plot from validated CSVs
dataval report          <dataset>           # Generate Excel validation report
dataval clean-figures   [--apply]           # Remove stale figures/ dirs
dataval build-object-data                   # Derive OBJECT_DATA.csv from filenames
dataval autoflag        [--apply]           # Auto-reject rule A/B candidates
dataval migrate-wiertsema [--apply]         # Migrate folder names to stable keys
```

All paths resolve relative to the repository root (detected by `pyproject.toml` or `.git`). The entry point is `dataval.cli:app` (Typer).

## 8. Proposed Databricks pipeline

The goal is to replicate this validation workflow on the Databricks platform, replacing the local file-based input with Databricks source tables, and using Git for synchronization between environments.

DATABRICKS PIPELINE LOCAL IDE / STREAMLIT ═══════════════════ ════════════════════ ┌──────────────────┐ │ Source Delta │ ① Read source data │ Tables │ per measurement well │ (Fugro/Wiertsema) │ └────────┬─────────┘ │ ▼ ┌──────────────────┐ │ Personal │ ② Write copy of source │ Workspace │ data per well to │ (Delta tables) │ personal workspace └────────┬─────────┘ │ git push ③ Push CSV/parquet exports │ to remote repository ▼ ┌──────────────────┐ git pull │ Remote Git Repo │ ────────────────► ┌──────────────────┐ │ (Azure DevOps / │ │ Local working │ │ GitHub) │ ◄──────────────── │ copy (IDE) │ └──────────────────┘ git push │ │ │ ④ Validate data │ │ via Streamlit / │ │ CLI / browser │ │ │ │ validation_data/ │ │ (gecontroleerd / │ │ afgekeurd) │ └────────┬─────────┘ │ git push │ ▼ ┌──────────────────┐ │ Remote Git Repo │ │ (validated data) │ └────────┬─────────┘ │ git pull │ ▼ ┌──────────────────┐ │ Databricks Job │ ⑤ Create validated │ Personal │ Delta table in │ Workspace │ personal workspace └──────────────────┘

### Pipeline stages explained

### 1 Source data ingestion

replaces `input_data/` folder + `convert-fugro` / `convert-wiertsema` commands

Instead of placing raw vendor files in `input_data/` and running the converters, read directly from the Databricks source Delta tables. The conversion logic (date parsing, column normalization, sensor detection for Wiertsema multi-sheet workbooks) either moves into a Databricks notebook or is preserved as a Python wheel installed in the workspace.

### 2 Personal workspace copy

new Replication layer for data provenance

For each measurement well, write a copy of the source data to your personal Databricks workspace as a Delta table. This gives you an immutable snapshot tied to a point in time. Export the per-sensor timeseries as CSV files (matching the current `only_csv/` structure) so Git can track them.

The source data is never modified. Every downstream operation works on copies.

### 3 Git synchronization (source → IDE)

new Two-way sync via remote repository

From Databricks, push the exported CSVs to the remote Git repository. On the local IDE, pull to get a working copy that exactly matches the Databricks source. This replaces the manual file-drop workflow and guarantees that the local environment always has a faithful copy of the pipeline's source data.

### 4 Validation in IDE / Streamlit

unchanged Existing validation tooling

The core validation workflow is unchanged:

- Run `dataval validate` for automated flags (v2/v3/v4)
- Run `dataval autoflag` for rule A/B auto-rejection
- Use the Streamlit dashboard for manual point-by-point review
- All review state lives in `validation_data/` (version-controlled)

The logging and validation store schema (`timestamp, raw_data, gecontroleerd, afgekeurd, note`) remain identical, so the audit trail is preserved.

### 5 Validated data → Databricks Delta table

new Closing the loop

After validation, push the `validation_data/` directory upstream. Pull it into Databricks and run a job that creates a new Delta table in the personal workspace containing only the validated data. This table can be used for further analysis (Pastas modeling, return period calculations, etc.) with confidence that every reading has been quality-checked.

## 9. Migration implementation steps

### What can be reused as-is

- **All validation logic** — `functions/validation_functions.py` is pure pandas/numpy, no filesystem coupling.
- **Validation store** — `dashboard/validation_store.py` (load/save/merge) works on DataFrames; file I/O is isolated in `load()` and `save()`.
- **Object data parsing** — `dataval/object_data.py` filename parsers are pure string operations.
- **Autoflag rules** — `dataval/autoflag.py` operates on DataFrames.
- **Streamlit dashboard** — entirely unchanged for the local validation step.
- **KNMI stressor handling** — `dataval/convert_knmi.py` and the Makkink evaporation calculation.

### What needs to be built

| Component | Description | Complexity |
| --- | --- | --- |
| Source reader notebook | Read from Databricks source Delta tables, apply the same column normalization as `convert_fugro` / `convert_wiertsema`, write per-sensor CSVs to personal workspace | Medium |
| Git export job | Export the personal workspace Delta tables as CSVs matching the `only_csv/` layout, commit & push to the remote repository | Low |
| Validated data import job | Read the `validation_data/` CSVs from the repo, join with the source data, write a validated Delta table to the personal workspace | Low |
| KNMI stressor sync | Either keep `input_stressors/` in the repo (current approach works), or pull KNMI data into a Delta table and export | Low |
| Workflow orchestration | Databricks workflow or job that chains: source read → personal copy → git push. Triggered on source data updates. | Medium |

### Important constraints

**Never touch the source data.** The entire design guarantees that the Databricks source tables are read-only from this pipeline's perspective. All validation state lives in Git-tracked CSVs, and the validated Delta table is a separate artifact in the personal workspace.

- **Store format stability** — The validation store schema must remain `timestamp, raw_data, gecontroleerd, afgekeurd, note` with timestamps rounded to the configured frequency (default `1h`). Changing this breaks merge compatibility with existing review work.
- **OBJECT_DATA.csv** lives in `validation_data/` alongside the review CSVs. It contains hand-curated corrections to sensor metadata (filter depths, cross-section codes) that survive automated rebuilds. It must travel with the repo.
- **Protected origins** — `autoflag.py` has a hardcoded set of origins whose validation stores are manually curated and must never be auto-written.
- **Sensor stitching** — Multi-sensor series are stitched chronologically (newest sensor wins overlaps). Rule A is skipped at stitch boundaries. This logic needs to carry over to the Databricks validated table.

### Suggested repository structure for Databricks

```
databricks/
  notebooks/
    01_source_to_personal.py      # Read source tables → personal workspace
    02_export_to_git.py           # Export only_csv/ layout → git push
    03_validated_to_delta.py       # Read validation_data/ → validated Delta table
  jobs/
    workflow_config.json           # Databricks workflow definition
dataval/                           # Existing package (installed as wheel)
dashboard/                         # Existing package
functions/                         # Existing package
validation_data/                   # Git-tracked validation state
config/
  settings.yaml
streamlit_app.py
```