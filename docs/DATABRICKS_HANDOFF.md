# Handoff: minimal Pastas/Tarso training pipeline on Databricks

> **Amended 2026-10-20.** This began as a pristine specification written before the
> source data was known. Two parts have since been overtaken by reality and are marked
> in place:
>
> - **§2.0 is new** — the actual Unity Catalog source schema
>   (`gold_prd.hl_grondwaterpeilmeting`, ten tables). It **supersedes §2.1**: heads come
>   from a Delta table, not CSV files.
> - **§6 excludes the extreme-value analysis.** That exclusion no longer holds — the
>   EVT subsystem is built and shipped as stage 3. See
>   [`PIPELINE_DATASETS.md` §10](PIPELINE_DATASETS.md).
>
> Everything else still stands. For what the pipeline *produces*, see
> [`PIPELINE_DATASETS.md`](PIPELINE_DATASETS.md).

You are being asked to rebuild a **stripped-down** version of an existing on-premises
Python repository (`pastas_machine`) as a Databricks pipeline. This document is the
complete specification — you do not have access to the original repo, and you do not
need it.

**Scope: load head (groundwater level) CSV files + two meteorological forcing series,
train one Pastas `TarsoModel` per head series, persist the model and its fit statistics.
Nothing else.**

---

## 1. Domain concept in one paragraph

A groundwater monitoring well (a "piezometer", Dutch *peilbuis*) logs water level
("head", in metres relative to Dutch ordnance datum NAP) hourly for typically 2–5 years.
[Pastas](https://pastas.readthedocs.io/) is a Python library for **time series models of
groundwater head**: it fits a transfer-function-noise model that explains the observed
head from meteorological stresses — precipitation and (Makkink reference) evaporation.
A `TarsoModel` (Threshold AutoRegressive Self-exciting Open-loop) is a specific
non-linear Pastas stress model: two linear reservoirs with different drainage levels and
resistances, switching at a threshold. Physically that represents a well which drains to
a ditch above a certain level and only to the deeper subsurface below it. That
non-linearity is why Tarso is the model type of interest here.

The pipeline is **embarrassingly parallel**: each head CSV is an independent model fit.
There are hundreds to thousands of series, each fit taking seconds to a few minutes.
That is the thing worth distributing on Databricks.

---

## 2. Input data contract

### 2.0 The real source: `gold_prd.hl_grondwaterpeilmeting` ✅ current

> **This section supersedes §2.1.** The pipeline does not read head CSVs. Heads come
> from a Unity Catalog Delta table, so there is no format detection, no UTF-8 BOM and no
> `gecontroleerd`/`afgekeurd` filter. §2.1 is kept as the original spec.
>
> Profiled 2026-10-20. **The catalog is read-only**: `BROWSE` / `USE CATALOG` /
> `USE SCHEMA`, and `SELECT` / `EXECUTE` / `READ VOLUME` on the schema. No `CREATE
> TABLE`, no `CREATE SCHEMA`, no `WRITE VOLUME`. All pipeline output goes to
> `waterkeringbeheer_prd.jvanruitenbeek`.
>
> One example value per **column**, not per row — two examples in the same table need
> not come from the same record.

Nine tables, 56 columns. The pipeline currently reads **only `meting`**; the other
eight are unexploited, and four of them carry information the pipeline is currently
missing (see §2.0.6).

#### 2.0.1 `meting` — the measurements (the one table in use)

Grain: one row per reading. ~55 rows per day per series.

| Column | Type | Example | Used? |
|---|---|---|---|
| `id` | `string` | `6bf03c251ebd593e15e757c08b5bb6be248c7abf1707affaa17cacaefd6ca48b` | no — **and not unique, see §2.0.4** |
| `meetreeks_id` | `string` | `5dbba95ccaae4f6d0bb81f0ee336d8e51efe3869627f090a8c7a174f934b420e` | **yes — the pipeline's primary key** |
| `tijdstempel` | `timestamp` | `2026-08-02 19:44:05` | **yes** — local (Europe/Amsterdam) |
| `tijdstempel_utc` | `timestamp` | `2026-08-02 17:44:05` | no — the UTC twin, `+02:00` in the example (CEST) |
| `waarde` | `double` | `1.248` | **yes** — the head, metres NAP |
| `validatie_status` | `int` | `1` | no — ignored by decision; decodable via `validatiestatus` |

#### 2.0.2 `meetreeks` — the series dimension ⭐

**The table that explains `meetreeks_id`.** `meting.meetreeks_id` → `meetreeks.id`.
Holds everything the pipeline currently lacks: a readable name, the filter depths, the
unit and the source system.

| Column | Type | Example | Why it matters |
|---|---|---|---|
| `id` | `string` | `32ab4aec0ac33c238061767bcbce06c2ce066d43baf00ad05429a9c98e16a35a` | = `meting.meetreeks_id` |
| `code` | `string` | `HB28-PB1 ` | **the human-readable well/filter name.** Note the trailing space — `trim()` it |
| `eenheid` | `string` | `m NAP` | **confirms the head unit** |
| `begin_filter` | `decimal(5,2)` | `-3.10` | filter top [m NAP] |
| `eind_filter` | `decimal(5,2)` | `-4.10` | filter bottom [m NAP] |
| `bovenkant_peilbuis` | `decimal(4,1)` | `NULL` | top of standpipe; nullable |
| `bronsysteem` | `string` | `Fugro` | source system |
| `sensor_meetlocatie_sleutel` | `string` | `507080_HB28_GMW_PB1` | sensor/location key |
| `meetlocatie_code` | `string` | `NULL` | |
| `meetlocatie_object_id` | `string` | `NULL` | |
| `object_id` | `string` | `4e132718f2f1b054b07a90598010f7c9aee7d2e73f363dba21321e834a95abdd` | → `object.id` |
| `eigenschap_id` | `string` | `b1556dea32e9d0cdbfed038fd7787275775ea40939c146a64e205bcb349ad02f` | → `eigenschap.id` |
| `validatie_status` | `int` | `1` | |

#### 2.0.3 The other seven tables

**`object`** — the physical object a series belongs to.

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `4e132718f2f1b054b07a90598010f7c9aee7d2e73f363dba21321e834a95abdd` |
| `code` | `string` | `NL-507080_API_GTA507080` |
| `naam` | `string` | `onbekend` |
| `validatie_status` | `int` | `2` |

**`eigenschap`** — property/quantity lookup.

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b` |
| `eenheid_code` | `string` | `N/A` |
| `validatie_status` | `int` | `2` |

**`dijkvak`** — dike section, with a failure probability. The dike-safety context.

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `5e64acaa31e6d347369b281ce4bbce3266cceb02de2b65a291502319d0914112` |
| `segment_id` | `string` | `Beemster_sectie_2_0` |
| `id_meetreeks` | `string` | `NULL` — **the link to `meetreeks.id`, currently unpopulated in the sample** |
| `kans` | `decimal(38,8)` | `100.00000000` (probability / return period) |
| `calculatie_type` | `decimal(5,0)` | `1` |
| `actief` | `int` | `1` |
| `validatie_status` | `int` | `2` |

**`grenswaardes_meetpunten`** — four warning thresholds per measurement point. Directly
comparable to stage 3's return levels.

| Column | Type | Example |
|---|---|---|
| `id` | `string` | `12` (**a plain integer string, not a hash**) |
| `fk_meetpunt` | `string` | `f1ccb771fbc21b9b204a9cc8784a434dead0cc81b0827df8d702675eb4f10d02` |
| `grenswaarde_1` … `_4` | `double` | `0.5`, `1.5`, `2.5`, `4.0` |
| `actief` | `int` | `1` |
| `validatie_status` | `int` | `2` |

**`validatiestatus`** — **decodes the `validatie_status` int** on every other table.

| Column | Type | Example |
|---|---|---|
| `id` | `bigint` | `0` |
| `omschrijving` | `string` | `Niet gevalideerd` ("not validated") |

**`validatieresultaat`** — data-quality check results, Great-Expectations shaped.

| Column | Type | Example |
|---|---|---|
| `schema` | `string` | `hl_grondwaterpeilmeting` |
| `tabel` | `string` | `meting` |
| `kolom` | `string` | `id` |
| `verwachting_type` | `string` | `expect_column_values_to_be_unique` |
| `validatie_type` | `string` | `business` |
| `ernst` | `string` | `critical` |
| `gebruikersbericht` | `string` | `Er bestaan meerdere metingen met hetzelfde id (dubbele primary key).` |
| `pk` | `string` | `NULL` |
| `validatie_tijd` | `timestamp` | `2026-10-20 05:07:14.149404` |

**`validatie_tracking`** — per-record validation audit trail.

| Column | Type | Example |
|---|---|---|
| `pk` | `string` | `2` |
| `tabel_naam` | `string` | `grenswaardes_meetpunten` |
| `record_hash` | `string` | `2a9e00e941d0900888e8e5b318c66ec4` (32 hex = **MD5**, not SHA-256) |
| `validatie_timestamp` | `timestamp` | `2026-10-06 04:07:50.633881` |

#### 2.0.4 Relationships

```
object ──< meetreeks ──< meting
             │   │
             │   └── eigenschap
             └──< dijkvak.id_meetreeks   (NULL in the sample)

grenswaardes_meetpunten.fk_meetpunt ──> a 64-hex key (meetreeks? object? unconfirmed)

validatie_status (int, on most tables) ──> validatiestatus.id
validatieresultaat / validatie_tracking: data-quality side tables, no FK into the model
```

`meetreeks.object_id` and `object.id` carry the **same example value**
(`4e132718…a95abdd`), which confirms that join.

#### 2.0.5 The `id` convention: SHA-256 of the natural key

The 64-hex `id` columns are **SHA-256 of the row's natural business key**, not random
surrogates. Demonstrated on two tables:

| Table | `id` | equals |
|---|---|---|
| `eigenschap` | `6b86b273ff34…875b4b` | `sha256("1")` ✅ |
| `dijkvak` | `5e64acaa31e6…914112` | `sha256("Beemster_sectie_2_0")` = `sha256(segment_id)` ✅ |

So `meetreeks.id` — and therefore the pipeline's `meetreeks_id` — is almost certainly
`sha256(<some natural key of the series>)`. The input is **not yet identified**:
neither `sha256(code)`, `sha256(trim(code))` nor `sha256(sensor_meetlocatie_sleutel)`
reproduces the sampled `id`, but the sampled values may not come from one row. One query
settles it:

```sql
SELECT id, code, sensor_meetlocatie_sleutel, object_id,
       sha2(code, 256)                       = id AS by_code,
       sha2(trim(code), 256)                 = id AS by_trimmed_code,
       sha2(sensor_meetlocatie_sleutel, 256) = id AS by_sensor_key,
       sha2(concat(object_id, code), 256)    = id AS by_object_and_code
FROM gold_prd.hl_grondwaterpeilmeting.meetreeks
LIMIT 20;
```

Knowing the input matters because it makes the key **reproducible**: you could then
derive a series' id from its well code instead of carrying the hash around.

#### 2.0.6 Four things the pipeline should act on

1. **`meetreeks` gives series a readable name.** The pipeline names everything
   `series_<first 24 hex>` because `meting` carries no name. Joining `meetreeks` yields
   `code` (`HB28-PB1`), `begin_filter` / `eind_filter` and `eenheid` — enough to label a
   plot with a well instead of a hash, and to sanity-check that `waarde` really is m NAP.

2. **⚠️ `meting.id` is not unique — and the source says so.** `validatieresultaat`
   carries an open **`critical`** failure: *"Er bestaan meerdere metingen met hetzelfde
   id (dubbele primary key)"*, from `expect_column_values_to_be_unique` on `meting.id`.
   The pipeline never relies on `meting.id`, so it is unaffected — but it confirms the
   source has genuine duplicate rows, which is consistent with the duplicate timestamps
   `clean_head` collapses. **Check this table before trusting a run.**

3. **`validatie_status` is decodable now.** It was ignored because the integers were
   opaque. `validatiestatus` maps them (`0` = `Niet gevalideerd`). Reading that table
   makes "ignore the review status" a measured decision rather than an unavoidable one.

4. **`tijdstempel` is local time; `tijdstempel_utc` is not.** The example pair is
   `19:44:05` local / `17:44:05` UTC — `+02:00`, so the column follows CEST/CET. A local
   timestamp **repeats one hour every October**, making timestamps genuinely ambiguous
   and duplicated at the DST fold. The pipeline indexes on `tijdstempel`
   (`meting.py::TIME_COLUMN`). Daily resampling absorbs most of this, but
   `tijdstempel_utc` is monotonic and would remove the ambiguity for free — a one-line
   change to `TIME_COLUMN`.

Two further opportunities, not defects: **`grenswaardes_meetpunten`** holds four warning
thresholds per point, which stage 3's return levels can be compared against directly
("the T=100 level crosses `grenswaarde_3`"); and **`dijkvak`** ties series to dike
sections with a failure probability (`kans`), which is the dike-safety framing the whole
pipeline ultimately serves.

### 2.1 Head series (one CSV per monitoring well / filter) ❌ superseded by §2.0

Organised as **one folder per survey project**, each folder containing many CSVs.
Folder names look like `83034-2`, `87074-1`, `4424_Hoorn_Zuiderdijk`. File names look
like:

```
83034-2_HB017PB01_HH0114_34_BUKR_GMW_PB1_F-264.csv
```

Two column conventions exist and **must be detected from the columns, never from the
filename or folder**:

**(a) "raw"** — an unvalidated logger export: a timestamp column plus one value column.

```csv
Time,head
2023-02-02 12:00:00,-0.389
2023-02-02 13:00:00,-0.387
```

The time column may also be called `Timestamp`, and the value column name varies
(`head`, `Waterniveau`, …). Rule: use the configured head column name if present,
otherwise **the first numeric column**.

**(b) "validated"** — a human-reviewed series carrying the reviewer's verdict per row.

```csv
timestamp,raw_data,gecontroleerd,afgekeurd,note
2025-01-14 14:00:00,-2.1305,1,1,
2025-01-14 15:00:00,-1.6180,1,0,
```

- `gecontroleerd` = 1 → the value has been reviewed
- `afgekeurd` = 1 → the reviewer **rejected** the value
- `note` = free-text comment, usually empty

A file counts as validated only when **all three** of `raw_data`, `gecontroleerd`,
`afgekeurd` are present; a partial match falls back to raw. Validated files are written
with a UTF-8 BOM, so read them with `encoding="utf-8-sig"` or column matching breaks on
a leading `﻿`. Match column names case-insensitively and strip whitespace.

Default row filter for validated files: **keep `gecontroleerd == 1 & afgekeurd != 1`**.
Rejected rows are **dropped, not NaN-filled** — Pastas handles irregular observation
series with gaps, so nothing is interpolated, backfilled or reindexed to close them. Raw
files have no verdict columns and are never filtered.

### 2.2 Forcing series (shared by all models)

Two CSVs, a date index plus one value column, **daily, in millimetres**:

```csv
date,precipitation_mm
1951-01-01,0.121
1951-01-02,0.000
```

```csv
date,makkink_mm
1951-01-01,0.205
1951-01-02,0.310
```

These come from a KNMI weather station (e.g. station 249, Berkhout) and span decades —
far longer than any head series. Column names are **not** stable across files
(`precipitation_mm`, `makkink_mm`, `evaporation`, …), so the column name must be a
pipeline parameter, not hardcoded. The original repo passes the millimetre series
straight into Pastas, which only rescales the gain parameters; keep that behaviour so
results stay comparable.

### 2.3 Filename constraint (important)

**Pastas model names cannot contain spaces, `+`, `.`, brackets or other punctuation.**
The source files arrive with names like
`83034-2 HB017PB01 HH0114+34_BUKR_GMW_PB1_F-264.csv`. The repo has a pre-step that
renames them on disk with `re.sub(r"[^a-zA-Z0-9_-]", "_", stem)`, collapsing repeated
underscores and stripping leading/trailing ones. In Databricks, **do not rename source
files**; derive the model name by applying that same sanitisation to the file stem. The
model name is the series identifier everywhere downstream.

---

## 3. The core transformation — per-series Tarso training

This is the heart of the pipeline. Reproduce it faithfully. Reference implementation,
condensed from the original `create_tarso_model`:

```python
import pandas as pd
import pastas as ps   # pinned at 1.12.x in the original repo

def train_tarso(series_name: str, head: pd.Series,
                prec: pd.Series, evap: pd.Series,
                tmin_cal=None, tmax_cal=None):
    # head must have a DatetimeIndex, be sorted, and contain no NaN
    if head.first_valid_index() is None:
        raise ValueError(f"No valid head observations in {series_name}")
    head = head.loc[head.first_valid_index():head.last_valid_index()]

    head_min, head_max = head.index.min(), head.index.max()
    tmin = max(pd.to_datetime(tmin_cal), head_min) if tmin_cal else head_min
    tmax = min(pd.to_datetime(tmax_cal), head_max) if tmax_cal else head_max

    # constant=False: TarsoModel brings its own drainage levels, so a Pastas
    # constant would be redundant and unidentifiable.
    ml = ps.Model(head, name=series_name, constant=False)

    sm = ps.TarsoModel(prec, evap,
                       dmin=head.min(), dmax=head.max(),
                       name="Tarso")
    ml.add_stressmodel(sm)

    # Drainage-resistance bounds [days]. Without them the solver wanders into
    # physically meaningless territory and fits fail or become unstable.
    ml.set_parameter("Tarso_a0", pmin=0.0,  pmax=200.0)
    ml.set_parameter("Tarso_a1", pmin=10.0, pmax=200.0)

    ml.solve(tmin=tmin.strftime("%Y-%m-%d"),
             tmax=tmax.strftime("%Y-%m-%d"),
             report=False)

    stats = ml.stats.summary()   # DataFrame indexed by metric name, column "Value"
    return ml, stats
```

Note that `dmin`/`dmax` are taken from the observed head range of that specific series —
they are per-series, not global constants.

Pre-processing applied to the head series **before** this call:

1. Load, format-detect, and (validated only) row-filter as in §2.1.
2. Trim to the first/last valid observation.
3. **Resample to daily means** (`resample("D").mean()`), then `dropna()` — resampling an
   observation series with gaps creates empty buckets, and Pastas must not receive NaN
   rows. Rule and method are parameters (`"D"` / `"mean"` by default).
4. **Skip the series** if fewer than `min_obs` observations survive (default **100**),
   for validated files only. Record the skip as a row in the report with
   `status="skipped"` — a skipped series must not silently disappear.

---

## 4. Output contract

Per series, record **one row** with at least:

| field | meaning |
|---|---|
| `series` | sanitised file stem (= Pastas model name) |
| `model` | `"tarso"` |
| `status` | `successful` / `skipped` / `error` |
| `error` | exception message, empty when successful |
| `first_entry`, `last_entry` | observed head extent (`YYYY-MM-DD`) |
| `length_series` | number of observations used |
| `min_value`, `max_value` | observed head range [m NAP] |
| `rmse`, `sse`, `mae`, `nse`, `evp`, `rsq`, `kge`, `bic`, `aic`, `aicc` | from `ml.stats.summary()`, pulled by index label out of the `"Value"` column, `None` when absent |
| `response_tmax_95` | `ml.get_response_tmax("Tarso", cutoff=0.95)`, `None` on failure |
| `tmin_cal`, `tmax_cal` | calibration window actually used |
| `resample_rule` | e.g. `"D"` |

Also persist the fitted model itself. Pastas serialises to a **`.pas` file** (JSON) via
`ml.to_file(path)` and reloads with `ps.io.load(path)`. Name it
`{series}_tarso_model.pas`. On Databricks write these to a **Unity Catalog Volume**
(`/Volumes/<catalog>/<schema>/<volume>/models/...`), not DBFS root and not the driver's
local disk; alternatively serialise `ml.to_dict()` to JSON into a Delta column if you
prefer everything in the lakehouse. Keep the statistics rows in a **Delta table** — the
natural replacement for the repo's per-run `model_run_<tag>_tarso.csv`.

Every run must be identifiable: a `run_id` column (the repo uses a `yy_mm_dd_HH_MM`
timestamp) plus the parameters that produced it — forcing file names and columns,
resample rule, validation filter, `min_obs`, calibration window, pastas version. Outputs
are append-only per run, never overwritten in place.

---

## 5. Critical: one failure must not kill the run

In the original, every fit is wrapped in `try`/`except` and the exception message lands
in the report row. Hundreds of series are processed and **individual fits do fail** — too
few observations, degenerate head records, non-converging solvers. The Databricks
implementation must preserve this: no single series may abort the job. If you distribute
with a pandas UDF / `mapInPandas` / `ThreadPoolExecutor`, catch inside the worker
function and return the error as data.

Scaling notes:

- Pastas and the scipy solver underneath it are **single-threaded, CPU-bound, pure
  Python/NumPy**. A pandas UDF over a DataFrame of file paths (one row per series) fits
  well; so does `concurrent.futures` on one large driver if the series count is modest.
- The forcing series are small (a few MB) and needed by every task — **broadcast them**
  rather than re-reading per task.
- If you import `matplotlib` at all, force the `Agg` backend. Per §6 you should not need
  it.
- Pin `pastas==1.12.*` (the original requires Python ≥ 3.12) as a cluster library or via
  `%pip install`. Pastas pulls in numpy, pandas, scipy and matplotlib.

---

## 6. Explicitly out of scope — do NOT port these

The source repo is much larger. Everything below exists there and must be **left out**:

- **All other model types.** The original trains nine: `linear_exp`, `linear_gamma`,
  `linear_dblexp` (linear recharge with Exponential / Gamma / DoubleExponential
  response), `flexmodel_gamma`, `flexmodel_dblexp` (Pastas `FlexModel` recharge), three
  `*_soil` variants adding a root-zone soil-moisture stress, and `tarso`. **Only
  `tarso`.** Keep the model-building call shaped so a second type could be added later,
  but ship one.
- **The extreme-value analysis**, which is the repo's actual research goal: driving the
  calibrated models with a 16-member regional climate ensemble (RACMO / IMPREX,
  1951–2000) to obtain ~704 block extremes per series, fitting GEV/Gumbel tail families,
  computing return levels, and labelling each observed timestep as e.g. a 1-in-30-year
  drought. A large subsystem (extremes runners, tail fits, QQ plots, threshold plots,
  return-period summaries). **None of it.**
- **The Streamlit web interface** (~3000 lines: app, results browser, performance editor,
  indicator viewer). **No dashboard of any kind.**
- **All plotting.** `ml.plots.results()`, `ml.plots.diagnostics()`, per-series figure
  PNGs. Return numbers, not pictures.
- **The Brakenhoff et al. (2022) model checklist** (`ps.check.checklist`) and its
  pass/fail aggregation, the "copy best passing model into `accepted_models/`" step, the
  model-performance summary tables, and the wet/dry indicator step.
- **`OBJECT_DATA.csv` / `timeseries_config.csv` metadata sync.** The repo maintains a
  per-folder CSV describing each borehole (filter top/bottom depth, position in the dike
  cross-section, per-series resample rule, chosen EVT family) and joins it into the
  reports. Interesting later, noise now. If you want the hook, allow an optional
  per-series resample rule; otherwise use the global default.
- **Bias correction and soil-moisture stressor derivation.**
- **KNMI API download notebooks.** Forcing CSVs are handed to you as files.
- Input-validation heuristics the repo carries but does not apply during training
  (physical head ceiling 1.5 m NAP, max step +0.30 / −0.05 m per hour, Tukey IQR outlier
  flagging, dry-period band). Listed only so you recognise them if you meet them; do not
  implement.

---

## 7. Suggested Databricks shape

A deliberately small target, mirroring the above:

1. **Discover** — list head CSVs under `/Volumes/.../input_files/<survey>/`, excluding
   any `OBJECT_DATA.csv`. Produce a DataFrame of `(survey, file_path, series_name)`, one
   row per series.
2. **Load forcings** — read the two forcing CSVs into pandas Series; broadcast.
3. **Train** — distribute step 1's rows. Each task loads its head CSV (§2.1), resamples
   (§3), fits Tarso (§3), writes the `.pas` to the models volume, and returns the stats
   row (§4) — including rows for skips and errors.
4. **Persist** — append the collected rows to a Delta table (e.g. `tarso_runs`), tagged
   with `run_id`.

A single notebook or job with these four tasks is an acceptable and preferred
deliverable. Parameterise: input volume path, survey folder(s), forcing file and column
names, resample rule/method, validation filter, `min_obs`, calibration window, `run_id`,
output volume and table.

**Definition of done:** running the job over one survey folder produces one `.pas` file
per successfully trained series and one Delta row per series (successful, skipped or
errored), with fit statistics that look sane — `rsq`/`nse` typically 0.7–0.95 for a
healthy series.
