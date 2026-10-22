# Archive: `DISCOVER_functions/`

The computational core, vendored from the on-prem repo. 7 modules. Imports as
`from functions …`, so it expects to be renamed to `functions`. `validation_functions.py`
is pure pandas/numpy (no filesystem coupling) and is the part most directly reusable.

## Module map

| Module | Lines | Public API | Purpose |
|---|---|---|---|
| `validation_functions.py` | 353 | `load_groundwater_csv()`, `remove_duplicate_and_fill_missing()`, `flag_physical_bounds()`, `flag_unrealistic_step_change()`, `flag_constant_head_runs()`, `flag_constant_head_periods()`, `flag_statistical_outliers()` | The automated-flag algorithms (handover §5). **`remove_duplicate_and_fill_missing(df, freq="1h")`** is the on-prem twin of `source_read.clean_head` — compare the two before implementing. v2 step-change (max_up 0.30 / max_down −0.05), v3 constant-head runs, v4 Tukey/IQR outliers. |
| `load_forcings.py` | 56 | `load_forcings(stressor_dir, prec_name, evap_name)` | Load precipitation/evaporation series (the KNMI overlays used in plots). |
| `load_model.py` | 38 | `load_model(model_path, return_params=False)` | Load a Pastas model from a `.pas` file. |
| `simulate.py` | 69 | `simulate_model(model_path, …)` | Run a loaded Pastas model. |
| `return_periods.py` | 123 | `heads_at_return_periods()`, `_fit_min_models()`, `_fit_max_models()`, `_weibull_T()` | Return-level fitting (min/max families). Overlaps the Tarso repo's stage 3. |
| `plot_functions.py` | 216 | `plot_head_distribution()`, `plot_groundwater_with_flags()` | Reusable plotting helpers. |
| `__init__.py` | 0 | — | Package marker (empty). |

## Mapping to the new structure

- **Reusable as-is:** `validation_functions.py` — it operates on DataFrames, not paths, so
  it carries over unchanged. The v2/v3/v4 thresholds are mirrored in
  [`config/settings.yaml`](../../config/settings.yaml) `flags:`.
- **Check for duplication:** `remove_duplicate_and_fill_missing` vs
  `databricks/lib/source_read.py::clean_head` — both round to the hour, dedup, and insert
  NaN gaps. Decide which is authoritative so cleaning happens once.
- **Pastas / return-period modules** (`load_model`, `simulate`, `return_periods`) belong to
  the modelling/extremes concern (the separate Tarso repo), not to validation — likely out
  of scope here.
