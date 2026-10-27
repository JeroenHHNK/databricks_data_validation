# Groundwater data validation — Databricks ↔ git ↔ Streamlit

Replicate groundwater head measurements from the Databricks source, validate them
locally (automated checks in a notebook, then manual review in Streamlit), and push the
quality-checked result back as a Delta table — **without ever touching the source data**.

```
gold_prd.hl_grondwaterpeilmeting (READ-ONLY)  +  gold_prd.hl_cs_objecten.meetlocatie
   │  ① read + clean to hourly, match locations              databricks_code/notebooks/01
   ▼
waterkeringbeheer_prd.jvanruitenbeek.source_copy_*          ② immutable per-well Delta copy
   │  ③ export CSVs into the Git folder                      databricks_code/notebooks/02
   ▼
GitHub ──pull──► local IDE  input_output/output_databricks/
   ▲                │  ④a automated flags v1–v4 + rules A/B   local_validation/validate.ipynb
   │                │  ④b Streamlit manual review (always overrides the automated flags)
   │  push          ▼
   └──── input_output/output_repository/  (reviewed copies, frozen store schema)
   │  ⑤ pull into Databricks, build validated table          databricks_code/notebooks/03
   ▼
waterkeringbeheer_prd.jvanruitenbeek.validated_meting  → further analysis / Tarso
```

The design follows the Pipeline Handover §8. Constraints and grants are in
[`docs/DATABRICKS_PERMISSIONS.md`](docs/DATABRICKS_PERMISSIONS.md); the source schema is in
[`docs/DATABRICKS_HANDOFF.md`](docs/DATABRICKS_HANDOFF.md) §2.0 and the location registry in
[`docs/DATABRICKS_layer_cs_objecten.md`](docs/DATABRICKS_layer_cs_objecten.md).

## Three data layers, kept strictly apart

| Layer | Folder | Git | Role | Written by |
|---|---|---|---|---|
| **1. Source copy** | `input_output/output_databricks/` | tracked | exact per-series copy of Databricks (`time_series_data/<filename>.csv` + `metadata/object_data.csv`) | the Databricks export **only** |
| **2. Derived** | `output_data/validated/`, `output_data/validation_summary.csv` | ignored | automated flags v1–v4 / rules A,B per hour; disposable | `local_validation/validate.ipynb` |
| **3. Validation store** | `input_output/output_repository/<filename>.csv` | tracked | human review decisions, **the asset**; frozen schema `timestamp,raw_data,gecontroleerd,afgekeurd,note` | review app + autoflag, merge-only |

A new export overwrites layer 1, layer 2 is rebuilt, and layer 3 is **merged into, never
replaced**: reviewed rows keep their value, verdict and note; new hours arrive unreviewed.
Every write goes through `data_io.assert_writable()`, which refuses any path inside layer 1.
Source and store join 1:1 on the filename stem; never rename one without the other.

## Repository layout

| Path | What |
|---|---|
| `databricks_code/` | Databricks notebooks + `lib/` + job config: copy and export only |
| `local_validation/` | `validate.ipynb`; `checks.py` (v1–v4, rules A/B), `data_io.py` (layers + paths), `stores.py` (open / merge / sync / save the store), `validation_store.py` (the frozen store contract, ported from on-prem) |
| `streamlit_app.py`, `review_app/` | The review dashboard: Map (landing), Review, Overview |
| `config/settings.yaml` | Paths, thresholds, autoflag switches, `notes_max_len` |
| `tests/` | `python -m pytest tests`: checks, merge contract, layer guard, store sync, RD conversion |
| `input_output/knmi_berkhout.csv` | Hourly KNMI forcing, Berkhout (249), from 2020: `precipitation_mm`, `makkink_mm`. Rebuilt by `local_validation/knmi_forcing.ipynb`; the review app's default overlay |
| `data/forcing/` | Extra KNMI forcing CSVs for the plot overlay (`precipitation_mm`, `makkink_mm`) |

## Validation

Runs locally: open [`local_validation/validate.ipynb`](local_validation/validate.ipynb) and run
all cells (~40 s for ~700 series); its last step folds a new export into the existing
stores. The logic is in
[`local_validation/checks.py`](local_validation/checks.py) (pure pandas, reusable by the
Streamlit app); thresholds are in [`config/settings.yaml`](config/settings.yaml).

**Flag layers**, run in order. Each stores the original value of what it flags in its own
column and sets `head` to NaN, so the next layer only sees what is left:

| | Check | Default |
|---|---|---|
| `v1` | Out of bounds: below the filter bottom or above the top of the well | `eind_filter` / `bovenkant_peilbuis` per series |
| `v2` | Unrealistic step change between two hours | up > +0.30 m, down < −0.05 m |
| `v3` | At the filter bottom: within 0.15 m of `eind_filter` | e.g. `eind_filter` −4.10: −3.95 and −4.05 flagged, −3.92 not |
| `v4` | Statistical outlier (Tukey / IQR) | outside Q1 − 1.5·IQR .. Q3 + 1.5·IQR |

**Autoflag rules**, on the raw value (they act on the review store):

| | Rule | Default |
|---|---|---|
| `rule_a` | Drop ≥ 0.10 m in one hourly step (contiguous hours) | report-only |
| `rule_b` | Value ≤ filter bottom + 0.10 m | applied: `afgekeurd = 1` |

**Manual review** in Streamlit marks each point `gecontroleerd` (reviewed) or `afgekeurd`
(rejected). It always overrides the automated rules; `apply_autoflags()` never touches a row
a human has reviewed.

## The review dashboard

```bash
-m streamlit run streamlit_app.py --server.port 8502
```

- **Map** (landing page): every series with location metadata on a PDOK or OpenStreetMap
  basemap, one marker per well (filters at the same spot are grouped), with each series'
  `sensor_meetlocatie_sleutel` in the hover. Positions come from `rd_x`/`rd_y` (RD New)
  converted to WGS84, which agrees with the stored `lon`/`lat` to within 0.4 m. Colour by
  vendor or by review progress. Click or box-select markers to list their series, then open
  one in Review. The series without a location are listed below the map.
- **Review**: pick a series (filter by vendor, location, or search by name / sensor key /
  code). Opening it merges the current source into its store and applies rule B. The plot
  colours each hour by status (grey unreviewed, green reviewed, red rejected, pink
  auto-rejected awaiting confirmation) and draws the automated checks as hint markers, plus
  `eind_filter` with the v3 band and the top of the well. In the table below, tick
  gecontroleerd / afgekeurd per hour (afgekeurd implies gecontroleerd; notes max 30
  characters), or approve / reject / reset all visible rows. Filter it by status or by
  check, or drag a box on the plot to limit it to a period. **Save** writes the store
  atomically; switching series with unsaved edits asks you to save or discard first.
- **Overview**: progress per vendor (TOTAL recomputed from sums) and per series, with the
  automated-check counts from the last notebook run.

Not ported from on-prem: *Generate Data* (replaced by the Databricks export), *Sensor
Stitching* (each Databricks series is already one `meetreeks`; this also sidesteps the
on-prem issue of stitching writing into the source copy) and the return-period plots (Tarso
repo).

## Quickstart

```bash
# Databricks: add as a Git folder, run the job 00 -> 01 -> 02 (see databricks_code/README.md),
# then commit & push input_output/output_databricks/ from the Git-folder UI
git pull                            # local IDE gets input_output/output_databricks/
python -m pytest tests              # validation logic tests
jupyter lab local_validation/validate.ipynb   # automated flags + store sync
streamlit run streamlit_app.py      # map + manual review -> input_output/output_repository/
git add input_output/output_repository && git commit -m "review" && git push
# Databricks: pull, run 03_validated_to_delta -> validated_meting
```

See [`databricks_code/README.md`](databricks_code/README.md) for the full run order and the
Volume fallback.
