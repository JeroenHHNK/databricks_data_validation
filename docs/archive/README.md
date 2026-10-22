# Archive — the `DISCOVER_*` folders

These documents archive the **vendored on-prem validation toolkit** and the **existing
review data** that currently sit in the repo under `DISCOVER_`-prefixed folders. They are
captured here so the knowledge is preserved and the code can be implemented properly once
the repository structure is definitive (the move to `input_output/output_databricks/` + `input_output/output_repository/`).

**Status: removed from the working tree, archived here.** The `DISCOVER_*` folders were
deleted from `main` once documented; recover them from git history if needed
(`git show <commit>:DISCOVER_…`, or `git checkout e7e568d -- DISCOVER_validation_data`). They
were never wired in — the code carried the on-prem import names (`from dashboard …`,
`from functions …`, `from dataval …`) and the `dataval` package + `streamlit_app.py` were
missing, so they would not have run as-is.

| Folder | What | Doc |
|---|---|---|
| `DISCOVER_dashboard/` | Streamlit review app + validation store (11 modules) | [DISCOVER_dashboard.md](DISCOVER_dashboard.md) |
| `DISCOVER_functions/` | Validation algorithms + Pastas/forcing/plot helpers (7 modules) | [DISCOVER_functions.md](DISCOVER_functions.md) |
| `DISCOVER_validation_data/` | 325 hand-curated review CSVs (the existing audit trail) | [DISCOVER_validation_data.md](DISCOVER_validation_data.md) |

## When we implement (the checklist these docs feed)

1. Rename `DISCOVER_dashboard` → `dashboard`, `DISCOVER_functions` → `functions`; add the
   missing `dataval/` package and `streamlit_app.py` (handover §3 package map).
2. Repoint data discovery from the old `<vendor>/<origin>/<series>` layout to the new
   `input_output/output_databricks/time_series_data/` + `metadata/object_data.csv` (see the mapping notes
   in each doc).
3. Write reviewed copies to `input_output/output_repository/`, which `04_validated_to_delta` consumes.
4. Reconcile the metadata schemas — the on-prem `OBJECT_DATA.csv` columns differ from what
   `03_export_to_git` emits (see [DISCOVER_validation_data.md](DISCOVER_validation_data.md)).
5. Migrate the existing 325 review CSVs so their curated verdicts are not lost.
