# Archive — the `DISCOVER_*` folders

These documents archive the **vendored on-prem validation toolkit** and the **existing
review data** that currently sit in the repo under `DISCOVER_`-prefixed folders. They are
captured here so the knowledge is preserved and the code can be implemented properly once
the repository structure is definitive (the move to `output_databricks/` + `output_repository/`).

**Status: parked, not wired in.** Nothing in `databricks/` imports these. The code folders
still carry the on-prem import names (`from dashboard …`, `from functions …`,
`from dataval …`) and the `dataval` package + `streamlit_app.py` are missing, so they will
not run as-is.

| Folder | What | Doc |
|---|---|---|
| `DISCOVER_dashboard/` | Streamlit review app + validation store (11 modules) | [DISCOVER_dashboard.md](DISCOVER_dashboard.md) |
| `DISCOVER_functions/` | Validation algorithms + Pastas/forcing/plot helpers (7 modules) | [DISCOVER_functions.md](DISCOVER_functions.md) |
| `DISCOVER_validation_data/` | 325 hand-curated review CSVs (the existing audit trail) | [DISCOVER_validation_data.md](DISCOVER_validation_data.md) |

## When we implement (the checklist these docs feed)

1. Rename `DISCOVER_dashboard` → `dashboard`, `DISCOVER_functions` → `functions`; add the
   missing `dataval/` package and `streamlit_app.py` (handover §3 package map).
2. Repoint data discovery from the old `<vendor>/<origin>/<series>` layout to the new
   `output_databricks/time_series_data/` + `metadata/object_data.csv` (see the mapping notes
   in each doc).
3. Write reviewed copies to `output_repository/`, which `03_validated_to_delta` consumes.
4. Reconcile the metadata schemas — the on-prem `OBJECT_DATA.csv` columns differ from what
   `02` currently emits (see [DISCOVER_validation_data.md](DISCOVER_validation_data.md)).
5. Migrate the existing 325 review CSVs so their curated verdicts are not lost.
