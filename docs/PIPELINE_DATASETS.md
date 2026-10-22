# Pipeline datasets

Every dataset the Tarso pipeline **reads, holds in memory or writes**: its columns,
types and meaning.

> Renamed from `DATA_CATALOG.md` (2026-10-20). "Data catalog" read as if it described
> Unity Catalog — the `gold_prd` source. It never did. The source schema is documented
> in [`DATABRICKS_HANDOFF.md` §2.0](DATABRICKS_HANDOFF.md); this file is about **our**
> datasets.

## ⚠️ Section status

This file was written against the **original CSV-file design**, before the pipeline was
pointed at `gold_prd.hl_grondwaterpeilmeting.meting`. Roughly half of it no longer
describes running code. Verified against the code on 2026-10-20:

| § | Subject | Status |
|---|---|---|
| 1 | Input: head CSV, "raw" | ❌ **superseded** — heads come from the `meting` Delta table. `read_head_csv` still exists in `io.py` but **no notebook calls it**. |
| 2 | Input: head CSV, "validated" | ❌ **superseded** — same. The `gecontroleerd`/`afgekeurd` filter was replaced by `validatie_status`, which is currently ignored. |
| 3 | Input: forcing CSVs | ✅ **current** — `data/forcing/knmi_forcing_249.csv`. |
| 3a | `knmi_daily_forcing` Delta table | ⚠️ **never built** — marked *proposed*, and `notebooks/ingestion/knmi_forcing.py` does not exist. The cluster has no egress, so the forcing is committed to the repo instead. The KNMI API facts and the conversion rules in this section are accurate and still used by `scripts/check_knmi.py`. |
| 4 | `LoadedHead` / `PreparedHead` | ❌ **superseded** — `LoadedHead` is unused; **`prepare_head` and `PreparedHead` no longer exist**. Cleaning is `clean.py::clean_head` → `CleanedSeries`. |
| 5 | Discovery DataFrame | ❌ **superseded** — replaced by `pastas_series_manifest`, keyed on `meetreeks_id`, not `(survey, file_path)`. |
| 6 | Delta table `tarso_runs` | ❌ **superseded** — **no code writes `tarso_runs`.** The real table is `pastas_fit_metrics`; see §6a. |
| 7 | Output: model files | ❌ **superseded** — path and filename both changed; see §3a's artefact tree. |
| 8 | Job parameters | ❌ **superseded** — the real widgets are listed in §8a. |
| 9 | Fixed model constants | ✅ **current** — matches `fit.py::build_model`. |
| 10 | Stage 3: extremes | ✅ **current**. |

Superseded sections are kept because they record the spec the pipeline was built from,
and because §6 is still the only written description of several fit statistics. They are
**not** a description of what runs today.

**Legend**

- *Source* column: **spec** = stated in `DATABRICKS_HANDOFF.md`; **proposed** = a design
  choice of this repo that the spec leaves open (change it freely).
- *pandas* is the dtype as pandas 3.x produces it from `pd.read_csv` / our loaders
  (checked against the code in `src/tarso_pipeline`). *Spark* / *Delta* is the type to
  use when the data is a Spark DataFrame or Delta table.
- Timestamps are timezone-naive. Water levels are **metres relative to NAP**; forcings
  are **millimetres per day**.

## Overview of the data flow

**Current (Delta source):**

```
gold_prd.hl_grondwaterpeilmeting.meting
  --manifest_sql--> pastas_series_manifest        (task 1, every series >= 365d)
      --series_rows_sql--> pandas --clean_head--> CleanedSeries
                                                      |
data/forcing/knmi_forcing_249.csv --load_forcing--> 2 x pd.Series
                                                      |
                                                 fit_series (task 2)
                                                      +--> run_yy_mm_dd/pas_files/*.pas
                                                      +--> pastas_fit_metrics
                                                              |
                 data/forcing_imprex/ (16 members) --> run_extremes (task 3)
                                                      +--> run_yy_mm_dd/csv/*.csv
                                                      +--> pastas_evt_selection
                                                      +--> pastas_extremes_summary
```

Everything joins on **`meetreeks_id`**.

**Superseded (CSV source) — what §§1, 2, 4, 5, 6 describe:**

```
head CSV (raw | validated) --read_head_csv--> LoadedHead --prepare_head--> PreparedHead
                                                                              |
forcing CSVs (prec, evap) --load_forcing--> 2 x pd.Series ----> train_tarso --+--> .pas file
                                                                              +--> stats row --> Delta `tarso_runs`
discovery DataFrame (survey, file_path, series_name) drives the distribution.
```

---

## 1. Input: head CSV, "raw" format ❌ superseded

One CSV per monitoring well/filter, in a folder per survey. Unvalidated logger export.
Hourly, typically 2-5 years.

| Column | CSV example | pandas | Spark | Nullable | Notes |
|---|---|---|---|---|---|
| `Time` (or `Timestamp`) | `2023-02-02 12:00:00` | `str` -> `datetime64` | `timestamp` | no | Name varies. Candidates matched case-insensitively: `time`, `timestamp`, `datetime`, `date`, `tijd`; otherwise column 0. |
| `head` (name varies, e.g. `Waterniveau`) | `-0.389` | `float64` | `double` | rows with NaN are dropped | Configured head column name if present, otherwise the **first numeric column**. Metres NAP. |

Raw files have no verdict columns and are **never row-filtered**.

## 2. Input: head CSV, "validated" format ❌ superseded

Human-reviewed series. Written with a **UTF-8 BOM**, read with `encoding="utf-8-sig"`.
A file is validated only if **all three** of `raw_data`, `gecontroleerd`, `afgekeurd`
are present (case-insensitive, whitespace stripped); a partial match falls back to raw.

| Column | CSV example | pandas | Spark | Nullable | Notes |
|---|---|---|---|---|---|
| `timestamp` | `2025-01-14 14:00:00` | `str` -> `datetime64` | `timestamp` | no | Time column. |
| `raw_data` | `-2.1305` | `float64` | `double` | rows with NaN are dropped | Head in metres NAP. This is the value column. |
| `gecontroleerd` | `1` | `int64` | `int` | treated as not-checked if NaN | 1 = value has been reviewed. |
| `afgekeurd` | `0` | `int64` | `int` | treated as not-rejected if NaN | 1 = reviewer **rejected** the value. |
| `note` | *(usually empty)* | `float64` (all-NaN) or `str` | `string` | yes | Free-text reviewer comment. **Not used** by the pipeline. |

**Row filter (default):** keep `gecontroleerd == 1 and afgekeurd != 1`. Rejected rows are
**dropped, not NaN-filled or interpolated**.

## 3. Input: forcing CSVs (two files, shared by all models) ✅ current

Daily, millimetres, decades long (e.g. KNMI station 249 Berkhout, from 1951). Passed to
Pastas in mm unchanged. **Column names are pipeline parameters**, not hardcoded.

| File | Column | CSV example | pandas | Spark | Notes |
|---|---|---|---|---|---|
| precipitation | `date` | `1951-01-01` | `str` -> `datetime64` | `date` | Index. Loader finds it via the same time-column candidates. |
| precipitation | `precipitation_mm` (varies) | `0.121` | `float64` | `double` | mm/day. Name set by the `prec_column` parameter. |
| evaporation | `date` | `1951-01-01` | `str` -> `datetime64` | `date` | Index. |
| evaporation | `makkink_mm` (varies, e.g. `evaporation`) | `0.205` | `float64` | `double` | Makkink reference evaporation, mm/day. Set by `evap_column`. |

`load_forcing()` returns a `pd.Series`: `float64`, `DatetimeIndex` (`datetime64[us]`),
sorted, duplicate dates removed (first kept), `name` = the column name.

## 3a. Source: Delta table `knmi_daily_forcing` ⚠️ never built

The forcing CSVs of section 3 can also be produced from KNMI directly. Written by
`notebooks/ingestion/knmi_forcing.py` via `src/tarso_pipeline/knmi.py`; column names
match section 3 so `load_forcing()` reads an export of this table unchanged.

**Natural key `(station, date)`**, upserted with a `MERGE` — re-running never duplicates.

| Column | pandas | Spark / Delta | Nullable | Notes |
|---|---|---|---|---|
| `station` | `Int64` | `int` | no | KNMI station number. Default **249 Berkhout**, the station named in section 3. |
| `date` | `datetime64[ns]` | `date` | no | Observation day. |
| `precipitation_mm` | `float64` | `double` | yes | Daily sum, mm. KNMI `RH` ÷ 10. |
| `makkink_mm` | `float64` | `double` | yes | Makkink reference evaporation, mm. KNMI `EV24` ÷ 10. |
| `ingested_at` | — | `timestamp` | no | When the row was last written. Not part of the key. |

**Source API.** `POST https://www.daggegevens.knmi.nl/klimatologie/daggegevens` with
`start`/`end` as `YYYYMMDD`, `vars=RH:EV24`, `stns=<station>`. The response is CSV:
comment lines prefixed `#`, the last of which (`# STN,YYYYMMDD,   RH, EV24`) carries the
column names in KNMI's own order — so the header is parsed, never assumed.

**Conversions applied on ingest.**

| KNMI | Meaning | Stored as |
|---|---|---|
| `RH` / `EV24` in 0.1 mm | — | ÷ 10 → mm |
| `RH = -1` | measured, below 0.05 mm | `0.0` (a dry day, **not** NULL) |
| blank field | missing | `NULL` |

**Incremental refresh.** `max(date)` for the station is the watermark; the next window
is `[watermark + 1 − lookback_days, today]`, clamped to `backlog_start` (2020-01-01).
When the table is current the window is empty and **no HTTP request is made**. The
`lookback_days` overlap (default 2) re-fetches recently published days, which KNMI
revises as they are validated.

**Publication lag.** KNMI runs 1–2 days behind, so a run on day *D* normally stores
through *D−2*. The notebook's coverage check compares stored days against
`datediff(max, min) + 1`; a non-zero gap is a genuinely missing KNMI day, which matters
because Pastas wants an unbroken daily forcing series.

**Committed forcing file `data/forcing/knmi_forcing_<station>.csv`.** The cluster has no
outbound internet, so the fetch runs locally and the result is **committed to the repo**;
a Git folder pull delivers it. Written by `write_export`, read by `read_export` (for the
`MERGE`) or by `load_forcing` (for training). Columns are exactly the four above
**without `ingested_at`**, dates ISO `YYYY-MM-DD`, missing values empty; ~58 KB for
2020-to-date. Locate it with `tarso_pipeline.paths.forcing_path(station)` rather than a
hardcoded `/Workspace/...` path.

One file carries both variables — `load_forcing(path, column)` selects which — so
section 3's "two files" is satisfied by two calls on one file:

```python
precipitation = load_forcing(forcing_path(249), "precipitation_mm")
evaporation   = load_forcing(forcing_path(249), "makkink_mm")
```

`read_export` returns the same frame `fetch_daily` would, so the ingestion notebook's
`MERGE` is one code path for all three `source` modes (`repo` / `api` / `file`) and
re-merging stored rows is a no-op.

**Decisions recorded 2026-10-14.** Head series run from 2022 onwards, so KNMI station
249 (from 1999-03-23) provides ~8300 days before them and the full 3650-day warmup fits.
`validatie_status` is **not** used as a filter: every row with a `waarde` is fitted,
whatever its review status. Series are named `series_<first 24 hex of meetreeks_id>`; the full id stays on every
metrics row, so a downstream join on the prefix recovers the rest.

Artefacts are written per run, grouped by kind rather than by series:

```
/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline/
    run_yy_mm_dd/
        pas_files/          series_<hash24>_<model>.pas
        results_files/      series_<hash24>_<model>_results.png
        diagnostics_files/  series_<hash24>_<model>_diagnostics.png
```

The model type is in each filename because the folders are shared across series, so
without it a `recharge` run would overwrite the `tarso` one. All three paths are
recorded on the metrics row (`model_path`, `plot_path`, `diagnostics_path`).
Two runs on the same day share a folder and overwrite each other's artefacts while
keeping both metrics rows; the `run_folder` widget overrides the name to avoid that.

> **Hourly is not an option for evaporation.** KNMI's `uurgegevens` product has no
> Makkink: its `PRCP` set is `DR:RH` (duration + hourly sum). `EV24` is daily-only.

## 4. In-memory objects (Python, no Spark) ❌ superseded

### `LoadedHead` (output of `read_head_csv`)

| Field | Python type | Notes |
|---|---|---|
| `series` | `pd.Series[float64]`, `DatetimeIndex`, name `"head"` | Sorted, no NaN. |
| `format` | `HeadFormat` enum (`"raw"` / `"validated"`) | Detected from columns only. |
| `n_rows_read` | `int` | Data rows in the file. |
| `n_rejected` | `int` | Rows dropped by the verdict filter (0 for raw). |

### `PreparedHead` (output of `prepare_head`)

| Field | Python type | Notes |
|---|---|---|
| `series` | `pd.Series[float64]`, `DatetimeIndex` | Trimmed, resampled, `dropna()`d. Input to Pastas. |
| `skipped` | `bool` | True when validated and fewer than `min_obs` observations. |
| `skip_reason` | `str` | Empty when not skipped. |

## 5. Discovery DataFrame (step 1 of the job) ❌ superseded

One row per head CSV; `OBJECT_DATA.csv` is excluded. **Source: spec.**

| Column | pandas | Spark | Nullable | Example |
|---|---|---|---|---|
| `survey` | `str` | `string` | no | `83034-2` (the folder name) |
| `file_path` | `str` | `string` | no | `/Volumes/cat/schema/vol/input_files/83034-2/83034-2_HB017PB01_..._F-264.csv` |
| `series_name` | `str` | `string` | no | `83034-2_HB017PB01_HH0114_34_BUKR_GMW_PB1_F-264` (= `sanitize_name(file stem)`) |

## 6. Output: Delta table `tarso_runs` ❌ superseded — never written; see §6a

Append-only; never overwritten in place. Fields in the "Source: spec" rows are required
by section 4 of the handoff; "proposed" rows are the run-identification parameters the
spec asks for without fixing names.

### 6.1 Identity and status

| Column | pandas | Spark / Delta | Nullable | Source | Notes |
|---|---|---|---|---|---|
| `run_id` | `str` | `string` | no | spec | `yy_mm_dd_HH_MM` timestamp, e.g. `26_10_13_14_04`. |
| `series` | `str` | `string` | no | spec | Sanitised file stem = Pastas model name. |
| `model` | `str` | `string` | no | spec | Always `"tarso"`. |
| `status` | `str` | `string` | no | spec | `successful` / `skipped` / `error`. |
| `error` | `str` | `string` | yes | spec | Exception message or skip reason; empty/null when successful. |

### 6.2 Observed data summary

| Column | pandas | Spark / Delta | Nullable | Source | Notes |
|---|---|---|---|---|---|
| `first_entry` | `str` | `string` | yes | spec | `YYYY-MM-DD`, first observation. |
| `last_entry` | `str` | `string` | yes | spec | `YYYY-MM-DD`, last observation. |
| `length_series` | `int` | `int` | yes | spec | Observations **used** (after resampling). For skips: the count that triggered the skip. |
| `min_value` | `float` | `double` | yes | spec | Observed head minimum, m NAP. |
| `max_value` | `float` | `double` | yes | spec | Observed head maximum, m NAP. |
| `span_days` | `int` | `int` | yes | proposed | Calendar days from first to last observation. What the `min_days` filter tests. |

### 6.3 Fit statistics

From `ml.stats.summary()`, a DataFrame indexed by metric name with a single column
`"Value"`; pulled by index label, **`None` when absent**. All null for skipped/error rows.

| Column | pandas | Spark / Delta | Nullable | Source | Meaning |
|---|---|---|---|---|---|
| `rmse` | `float` | `double` | yes | spec | Root mean squared error [m]. |
| `sse` | `float` | `double` | yes | spec | Sum of squared errors [m²]. |
| `mae` | `float` | `double` | yes | spec | Mean absolute error [m]. |
| `nse` | `float` | `double` | yes | spec | Nash-Sutcliffe efficiency (healthy: ~0.7-0.95). |
| `evp` | `float` | `double` | yes | spec | Explained variance percentage. |
| `rsq` | `float` | `double` | yes | spec | R² (healthy: ~0.7-0.95). |
| `kge` | `float` | `double` | yes | spec | Kling-Gupta efficiency. |
| `bic` | `float` | `double` | yes | spec | Bayesian information criterion. |
| `aic` | `float` | `double` | yes | spec | Akaike information criterion. |
| `aicc` | `float` | `double` | yes | spec | Corrected AIC. |
| `response_tmax_95` | `float` | `double` | yes | spec | `ml.get_response_tmax("Tarso", cutoff=0.95)` [days]; `None` on failure. |

### 6.4 Calibration and processing parameters

| Column | pandas | Spark / Delta | Nullable | Source | Notes |
|---|---|---|---|---|---|
| `tmin_cal` | `str` | `string` | yes | spec | Calibration start actually used, `YYYY-MM-DD`. |
| `tmax_cal` | `str` | `string` | yes | spec | Calibration end actually used, `YYYY-MM-DD`. |
| `resample_rule` | `str` | `string` | no | spec | e.g. `"D"`. |
| `resample_method` | `str` | `string` | no | proposed | e.g. `"mean"`. |
| `head_format` | `str` | `string` | yes | proposed | `raw` / `validated` as detected. |
| `n_rejected` | `int` | `int` | yes | proposed | Rows dropped by the validation filter. |

### 6.5 Run identification (spec asks for these; names proposed)

| Column | pandas | Spark / Delta | Nullable | Notes |
|---|---|---|---|---|
| `survey` | `str` | `string` | no | Survey folder name. |
| `prec_file` | `str` | `string` | no | Precipitation forcing file name. |
| `prec_column` | `str` | `string` | no | Column used. |
| `evap_file` | `str` | `string` | no | Evaporation forcing file name. |
| `evap_column` | `str` | `string` | no | Column used. |
| `validation_filter` | `bool` | `boolean` | no | Whether the verdict filter was applied. |
| `min_obs` | `int` | `int` | no | Minimum observations (default 100, **validated files only**). |
| `min_days` | `int` | `int` | no | Minimum span in days (default 365, **every format**). 0 disables. |
| `tmin_cal_param` | `str` | `string` | yes | Requested calibration start (parameter), null if none. |
| `tmax_cal_param` | `str` | `string` | yes | Requested calibration end (parameter), null if none. |
| `pastas_version` | `str` | `string` | no | e.g. `1.12.0`. |
| `model_path` | `str` | `string` | yes | Path of the `.pas` file; null when not successful. |

## 5a. Output: Delta table `pastas_series_manifest` ✅ current

`waterkeringbeheer_prd.jvanruitenbeek.pastas_series_manifest`. Written by
`notebooks/ingestion/build_manifest.py` (job task 1). Replaces §5.

**`mode("overwrite")` + `overwriteSchema`** — it holds only the latest selection, never
a history. Grain: **one row per selected series** -- every series whose valued span
reaches `min_days` (365) and which has at least `min_obs` (100) values. The `limit`
widget caps it for a smoke test (`0` = no cap, the default).

| Column | Spark | Notes |
|---|---|---|
| `meetreeks_id` | `string` | **The key.** 64-char hash, straight from `meting.meetreeks_id`. |
| `n_rows` | `bigint` | Rows in the source for this series, values and nulls alike. |
| `n_values` | `bigint` | Rows with a non-null `waarde`. |
| `first_value_at` | `timestamp` | First timestamp **carrying a value**, not the first row. |
| `last_value_at` | `timestamp` | Last timestamp carrying a value. |
| `span_days` | `int` | `datediff` of the two above. What `min_days` tests. |
| `run_id` | `string` | `make_run_id()` → `yy_mm_dd_HH_MM`. |
| `min_days`, `min_obs` | `int` | Thresholds this selection used. |
| `source_table` | `string` | Fully qualified source, for provenance. |
| `manifest_created_at` | `timestamp` | `current_timestamp()`. |

## 6a. Output: Delta table `pastas_fit_metrics` ✅ current

`waterkeringbeheer_prd.jvanruitenbeek.pastas_fit_metrics`. Written by
`notebooks/transformation/fit_pastas_models.py` (job task 2). **Replaces §6's
`tarso_runs`**, which no code writes.

Schema is generated from `fit_metrics.FIT_METRICS_SCHEMA`, so the table and the row
cannot drift apart. **`mode("append")` + `mergeSchema`.**

**Primary key: `(run_id, meetreeks_id, model_type)`** — logical only, Delta enforces
nothing. `meetreeks_id` alone is not unique: each series is fitted with every type in
`model_types` (default `tarso` **and** `recharge`), and every run appends a fresh
generation of rows. Grain: **one row per series × model type × run** -- so an
uncapped run over N manifest series writes `N × len(model_types)` rows.

> ⚠️ Do not join this to `pastas_series_manifest` on `run_id`. The manifest is
> overwritten so it holds one `run_id`, and the two stages build `run_id` by different
> means — task 1 calls `make_run_id()` (`26_10_20_14_04`), task 2 builds its own UTC
> stamp (`20261020T140412Z`). Join on `meetreeks_id`.

### 6a.1 Identity and status

| Column | Spark | Notes |
|---|---|---|
| `run_id` | `string` | Widget, or a UTC `%Y%m%dT%H%M%SZ` stamp. |
| `meetreeks_id` | `string` | The 64-char source key. |
| `series_name` | `string` | `series_<first 24 hex>`. A label for filenames, **not** a key. |
| `model_type` | `string` | `tarso` or `recharge`. |
| `success` | `boolean` | Replaces §6's three-valued `status`; a skip is a failure with a reason. |
| `error_message` | `string` | Exception or guard message; null on success. |

### 6a.2 Solver and calibration window

| Column | Spark | Notes |
|---|---|---|
| `nfev` | `int` | Solver function evaluations. |
| `nobs` | `int` | Observations Pastas actually used. |
| `noise` | `boolean` | Noise model active. |
| `tmin`, `tmax` | `timestamp` | Calibration window used, after trimming. |
| `freq`, `freq_obs` | `string` | Model and observation frequency. |
| `warmup_days` | `int` | Warmup requested (default 3650). |
| `warmup_available_days` | `int` | Warmup the forcing could actually supply. |
| `calibration_trimmed` | `boolean` | True when `tmax` was pulled back to the last forcing day. |
| `days_trimmed_from_end` | `int` | How many days that cost — the KNMI publication lag. |

### 6a.3 Fit statistics

Read field-by-field from the Pastas API, never parsed from `fit_report()`. Each read is
guarded, so a statistic this Pastas version does not expose lands as `NULL` rather than
losing the row.

| Column | Spark | Notes |
|---|---|---|
| `evp` | `double` | Explained variance percentage. |
| `r2` | `double` | `model.stats.rsq`. Healthy: ~0.7–0.95. |
| `rmse` | `double` | Root mean squared error [m]. |
| `aic`, `aicc`, `bic` | `double` | Information criteria. |
| `obj` | `double` | `solver.obj_func`. |
| `interp` | `boolean` | Simulation interpolated onto the observations. |

> Narrower than §6.3, which also lists `sse`, `mae`, `nse` and `kge`. Those come from
> `ml.stats.summary()`; this table reads individual API methods instead. Add them to
> `FIT_METRICS_SCHEMA` and `extract_metrics` if you want them.

### 6a.4 What was actually fitted — the cleaning audit

Every count from `clean.py::clean_head`, so a surprising fit can be traced to the
cleaning rather than the solver. This has no equivalent in §6.

| Column | Spark | Notes |
|---|---|---|
| `n_raw_rows` | `int` | Rows returned by `series_rows_sql`. |
| `n_missing_value` | `int` | Dropped for a null `waarde`. |
| `n_duplicate_timestamps` | `int` | Rows sharing a timestamp with another. **Typically large** — ~55 rows/day collapse to 1. |
| `n_after_dedup` | `int` | Rows left once duplicates were collapsed. |
| `dedup_method` | `string` | How duplicates were collapsed (default `mean`). |
| `resample_rule`, `resample_method` | `string` | Default `D` / `mean`. |
| `span_days` | `int` | Span of the **cleaned** series. |
| `first_entry`, `last_entry` | `timestamp` | Extent of the cleaned series. |
| `min_value`, `max_value` | `double` | Observed head range [m NAP] — also Tarso's `dmin`/`dmax`. |

### 6a.5 Provenance

| Column | Spark | Notes |
|---|---|---|
| `parameters` | `string` | Fitted optimal parameters as sorted JSON. |
| `model_path` | `string` | The `.pas` file. |
| `plot_path`, `diagnostics_path` | `string` | The two PNGs; null if plotting failed, which never fails the fit. |
| `forcing_station` | `string` | KNMI station, e.g. `249`. |
| `forcing_units` | `string` | `m` or `mm` — **recorded because it rescales the gain parameters**, making the two conventions incomparable. |
| `pastas_version` | `string` | e.g. `1.12.0`. |
| `fitted_at` | `timestamp` | Wall clock at row creation. |

## 7. Output: model files ❌ superseded

| Item | Value |
|---|---|
| Format | Pastas `.pas` (JSON), written with `ml.to_file(path)`, reloaded with `ps.io.load(path)` |
| Name | `{series}_tarso_model.pas` |
| Location | Unity Catalog Volume: `/Volumes/<catalog>/<schema>/<volume>/models/...` (not DBFS root, not driver-local disk) |
| Alternative | `ml.to_dict()` as JSON string in a Delta `string` column |

## 8. Job parameters (all configurable) ❌ superseded — see §8a

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `input_volume_path` | string | none | Root containing survey folders. |
| `surveys` | list[string] | none | Survey folder(s) to process. |
| `prec_file`, `evap_file` | string | none | Forcing CSV paths. |
| `prec_column`, `evap_column` | string | none | Forcing column names. |
| `head_column` | string | `head` | Preferred raw value column; falls back to first numeric. |
| `resample_rule` | string | `D` | pandas offset alias. |
| `resample_method` | string | `mean` | One of `mean, median, min, max, first, last, sum`. |
| `validation_filter` | bool | `true` | Apply `gecontroleerd == 1 & afgekeurd != 1`. |
| `min_obs` | int | `100` | Minimum observations. Validated files only. |
| `min_days` | int | `365` | Minimum series span in days. Applies to every format; 0 disables. A Tarso fit needs a full seasonal cycle. |
| `tmin_cal`, `tmax_cal` | string (date) | none | Optional calibration window. |
| `run_id` | string | `yy_mm_dd_HH_MM` now | Identifies the run. |
| `output_volume_path` | string | none | Where `.pas` files go. |
| `output_table` | string | `tarso_runs` | Fully qualified `catalog.schema.table`. |

## 8a. Job parameters, as the notebooks actually declare them ✅ current

Replaces §8. These are the `dbutils.widgets` of task 2
(`notebooks/transformation/fit_pastas_models.py`); a blank text widget means "derive it".

| Widget | Default | Notes |
|---|---|---|
| `source_root` | *(blank)* | Repo root; blank walks up for `pyproject.toml`. |
| `source_table` | `gold_prd.hl_grondwaterpeilmeting.meting` | Read-only source. |
| `manifest_table` | `…jvanruitenbeek.pastas_series_manifest` | Written by task 1. |
| `metrics_table` | `…jvanruitenbeek.pastas_fit_metrics` | This task's output. |
| `volume_base` | `/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline` | Where run folders go. |
| `forcing_dir` | *(blank)* | Blank = `<volume_base>/knmi_forcing`. |
| `station` | `249` | KNMI station (Berkhout). |
| `forcing_units` | `m` | `m` or `mm`. **Changes the gain parameters.** |
| `warmup_days` | `3650` | Pastas warmup. |
| `model_types` | `tarso,recharge` | Comma-separated. Drives the row multiplier. |
| `limit_series` | `0` | Cap on manifest series to fit; **`0` = the whole manifest**. **Set `1` for a first run.** Longest-first, so a cap is a prefix, not a sample. |
| `run_folder` | *(blank)* | Blank = `run_yy_mm_dd`. Set it to stop two same-day runs overwriting artefacts. |
| `dedup_method` | `mean` | How to collapse duplicate timestamps. |
| `resample_rule` / `resample_method` | `D` / `mean` | |
| `min_obs` | `100` | Minimum observations **after** cleaning. |
| `min_days` | `365` | Minimum span after cleaning. |
| `run_id` | *(blank)* | Blank = UTC `%Y%m%dT%H%M%SZ`. |
| `write_metrics` | `true` | `false` dry-runs the fit without writing. |
| `save_plots` | `true` | |

Nothing here matches §8: there is no `input_volume_path`, no `surveys`, no `prec_file`
or `evap_column`, and no `validation_filter`. Those belonged to the CSV design.

## 9. Fixed model constants (from the spec) ✅ current

| Item | Value |
|---|---|
| Stress model name | `Tarso` |
| `ps.Model(..., constant=False)` | no constant |
| `TarsoModel` `dmin` / `dmax` | per series: `head.min()` / `head.max()` |
| `Tarso_a0` bounds | `pmin=0.0`, `pmax=200.0` |
| `Tarso_a1` bounds | `pmin=10.0`, `pmax=200.0` |
| Solve window | `tmin`/`tmax` as `YYYY-MM-DD` strings, clipped to the head extent |
| `report` | `False` |
| `matplotlib` backend | `Agg` if imported at all |


## 10. Stage 3: extremes and return periods ✅ current

Drives each calibrated `.pas` with the 16 IMPREX ensemble members and reports return
levels. Written by `notebooks/transformation/run_extremes.py`, into the **same dated
run folder** as the models it read.

### Identity

`series_name` is the truncated name the `.pas` filename carries; `meetreeks_id` is the
**full 64-character source key**, recovered by joining `pastas_fit_metrics` on
`series_name`. A model with no metrics row falls back to the truncated name rather
than being dropped, and the summary says how many fell back.

### The pooled sample

16 members x 44 hydrological years (April-March, 1956-1999) = **704 blocks per tail**.
800 means the partial-block filter is not running; 16 440 means the members were
concatenated in time instead of pooled at the extremes.

### Trimming

`exclude_first` defaults to **450**, leaving 254 of 704 (`lam = 0.361`). A T-year
event is then the `1 / (lam * T)` quantile, not `1 / T`. **The smallest computable
return period is `1 / lam = 2.77`, so T=2 is reported as null** rather than invented.

### Artefacts, inside `run_yy_mm_dd/`

| Path | Contents |
|---|---|
| `csv/<series>_return_periods.csv` | T x {`dr_`,`wet_`} x {`exp`,`gpd`,`gev`,`emp`}, metres NAP |
| `pooled_extremes/<series>_{dry,wet}.csv` | every pooled extreme with `member`, `year`, date |
| `used_extremes/used_extremes_<series>.csv` | each extreme with rank and a `used` flag |
| `return_plots_fitted/`, `qq_plots/`, `confidence_plots/` | one PNG per model |
| `evt_selection.csv` | chosen family per tail, with shape, SE, verdict, review flag |
| `tail_parameters.csv` | shape/loc/scale per family per tail |
| `<run>_extremes_summary.csv` | one row per model: the run's report card |
| `bias_correction_run_log.json` | copied from the IMPREX folder: how the forcing was corrected |

Roll-ups also append to `waterkeringbeheer_prd.jvanruitenbeek.pastas_evt_selection`
and `.pastas_extremes_summary`.

### Reading the result

`gpd` is fitted and reported but **never recommended**: a generalised Pareto belongs
on threshold exceedances, not block maxima. Quote **T=10** (stable to centimetres) and
**T=100** (to ~0.1 m). **T=1000 is extrapolation**: 704 blocks reach an empirical
return period of ~705 years, so the families disagree there by 0.3 m and sometimes
much more.

A `degenerate` verdict on a wet tail means the model's `dmax` ceiling is capping the
head, not that the fit failed.
