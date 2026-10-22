# Archive: `DISCOVER_dashboard/`

The Streamlit review application and its persistence layer, vendored from the on-prem
`pastas_machine` repo. 11 modules. Imports internally as `from dashboard …` and
`from dataval._utils …` / `from functions …`, so it expects to be renamed to `dashboard`
and to sit beside `dataval/` and `functions/`.

## Module map

| Module | Lines | Public API | Purpose |
|---|---|---|---|
| `config.py` | 69 | `Settings`, `load_settings()` | Load `config/settings.yaml` with defaults. |
| `data_access.py` | 74 | `SeriesRef`, `list_vendors()`, `list_origins()`, `list_series()` | Discover series on disk by **`<vendor>/<origin>/<series>`**. ⚠️ the main adaptation point — repoint to the new flat `output_databricks/` + metadata layout. |
| `validation_store.py` | 180 | `empty_store()`, `ensure_columns()`, `normalize_raw()`, `merge_new_raw()`, `load()`, `save()`, `migrate_legacy_stores()`, `compute_stats()` | The persistence core. **Frozen schema** `timestamp, raw_data, gecontroleerd, afgekeurd, note`; rounds timestamps to `1h`; merge protects reviewed rows; atomic save (temp + replace). Keep untouched; point its output at `output_repository/`. |
| `plotting.py` | 158 | `status_series()`, `build_figure()` | Color-coded Plotly figure (gray=unreviewed, green=approved, red=rejected). |
| `overview.py` | 117 | `collect_all_stats()`, `compute_folder_summary()`, `append_total_row()` | Aggregate review stats per vendor/origin. |
| `autoflags.py` | 41 | `load_flag_hints()` | Load v2/v3/v4 flag hints from validated CSVs for the overlay. |
| `generate_tab.py` | 252 | `DatasetInfo`, `render_generate_tab()`, `_run_generate()`, `_scan_raw_files()`, … | "Generate Data" tab — triggers conversion from the browser. Tied to the old `input_data/` convert flow; **largely superseded** by the Databricks source copy. |
| `sensor_stitch.py` | 130 | `corrections_path()`, `load_corrections()`, `save_corrections()`, `load_sensor_group()`, `combine_sensors()`, `find_sensor_groups()`, `sensor_summary()` | Multi-sensor stitching (newest sensor wins overlaps; corrections persisted per `only_csv` dir). |
| `stitch_tab.py` | 211 | `render_stitch_tab()`, `_build_stitch_figure()` | Streamlit tab for stitching. |
| `return_period_plots.py` | 276 | `compute_origin_ensemble()`, `build_return_period_figures()`, … | Return-period / extreme-value plots (GEV/Gumbel). Overlaps the Tarso repo's stage 3; likely out of scope for validation. |
| `__init__.py` | 3 | — | Package marker. |

## Mapping to the new structure

- **`data_access.py`** is the key change: today it walks `<vendor>/<origin>/<series>.csv`.
  New input is flat `output_databricks/time_series_data/<series>.csv` keyed by the
  `output_databricks/metadata/object_data.csv` row (`filename`), with `vendor`/`origin` now
  metadata columns rather than folders.
- **`validation_store.py`** stays byte-for-byte (frozen schema); only its target directory
  moves to `output_repository/`.
- **`generate_tab.py`** / `sensor_stitch.py` assume the old convert + `only_csv/` flow;
  revisit whether they are still needed once the source comes from Databricks.
