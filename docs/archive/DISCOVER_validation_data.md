# Archive: `DISCOVER_validation_data/`

The **existing hand-curated review state** from the on-prem toolkit — the audit trail of
human verdicts. This is data, not code, and the one `DISCOVER_` folder that must **not** be
lost: its verdicts represent real review work.

## Structure

```
DISCOVER_validation_data/
├── fugro/      8 origins, 212 series
├── wiertsema/  9 origins,  96 series
└── wiertsema/migration_log.json
```

- **325 CSVs** total (308 series files + 17 `OBJECT_DATA.csv`, one per origin), ~152 MB.
- Layout is the old **`<vendor>/<origin>/<series>.csv`** — the structure we are moving away
  from for the Databricks source, but the review files themselves are what carry the verdicts.

## Review-store schema (the frozen one)

Written UTF-8-**BOM** (read with `encoding="utf-8-sig"`):

```csv
timestamp,raw_data,gecontroleerd,afgekeurd,note
2024-04-29 10:00:00,,1,0,
```

`gecontroleerd` = reviewed (0/1), `afgekeurd` = rejected (0/1). This is the frozen schema
`timestamp, raw_data, gecontroleerd, afgekeurd, note`; `03_validated_to_delta` already reads
exactly this shape. Note `raw_data` can be empty (a reviewed-but-null reading).

## `OBJECT_DATA.csv` — richer than what `02` currently emits ⚠️

The on-prem per-origin `OBJECT_DATA.csv` columns are:

```
name_timeseries, bottom_filter, cross_sec_location, top_filter,
n_sensor_files, source_files, is_nap, needs_review, review_reason, manually_edited
```

Our generated `output_databricks/metadata/object_data.csv` instead has
`filename, meetreeks_id, vendor, origin, series, code, begin_filter, eind_filter,
bovenkant_peilbuis, peilgebied_code, lon, lat, n_rows`.

**These must be reconciled** when the toolkit is wired in. Rough correspondence:

| on-prem | ours | note |
|---|---|---|
| `name_timeseries` | `series` / `filename` | the series identifier |
| `top_filter` | `begin_filter` | filter top [m NAP] |
| `bottom_filter` | `eind_filter` | filter bottom [m NAP] |
| `cross_sec_location` | — | dike cross-section code; **not yet sourced** (candidate: derive from `meetlocatie`) |
| `is_nap` | — | assumed true (source `eenheid = m NAP`) |
| `needs_review`, `review_reason`, `manually_edited` | — | curation flags; no source equivalent yet |
| `n_sensor_files`, `source_files` | — | stitching provenance; superseded by `meetreeks_id` |

## When we implement

- **Migrate these verdicts** into `output_repository/` under whatever final layout is chosen
  — do not regenerate from scratch. `migration_log.json` and
  `dashboard/validation_store.py::migrate_legacy_stores()` are the hooks for that.
- Decide whether `output_repository/` keeps the `<vendor>/<origin>/` nesting or goes flat
  (`filename`-keyed) to match `output_databricks/`.
