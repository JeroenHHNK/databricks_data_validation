"""Read the Databricks export and write the local validation results.

Input (pulled from git, never modified):
    input_output/output_databricks/metadata/object_data.csv        one row per series
    input_output/output_databricks/time_series_data/<filename>.csv Time,head (hourly)

Output (gitignored, regenerated on every run):
    output_data/validated/<filename>.csv   timestamp,raw_head,head,v1..v4,rule_a,rule_b
    output_data/validation_summary.csv     one row per series: bounds used + count per check

`head` in the output is the value after v1-v4 (NaN where flagged); `raw_head` is the
untouched source value. The manual review (Streamlit) reads these as hints and writes its
verdicts to input_output/output_repository/.

Paths and thresholds come from config/settings.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from . import checks

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = REPO_ROOT / "config" / "settings.yaml"


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def thresholds(settings: dict) -> checks.Thresholds:
    """`checks.Thresholds` from the `flags:` / `autoflag:` sections (missing keys keep
    their defaults)."""
    f, a = settings.get("flags", {}), settings.get("autoflag", {})
    kw = {k: f[k] for k in ("v2_max_up", "v2_max_down", "v3_band", "v3_min_run",
                            "v4_iqr_factor") if k in f}
    kw.update({k: a[k] for k in ("rule_a_drop", "rule_b_margin") if k in a})
    if "frequency" in settings.get("store", {}):
        kw["freq"] = settings["store"]["frequency"]
    return checks.Thresholds(**kw)


def _path(settings: dict, key: str) -> Path:
    return REPO_ROOT / settings[key]


def load_object_data(settings: dict) -> pd.DataFrame:
    return pd.read_csv(_path(settings, "metadata_file"))


def load_series(settings: dict, filename: str) -> pd.Series:
    """One series as an hourly `head` Series indexed by timestamp."""
    df = pd.read_csv(_path(settings, "time_series_dir") / f"{filename}.csv", parse_dates=["Time"])
    return df.set_index("Time")["head"].rename("head")


def validate_row(settings: dict, row, th: checks.Thresholds) -> pd.DataFrame:
    """Validate the series of one `object_data.csv` row."""
    return checks.validate_series(load_series(settings, row.filename),
                                  begin_filter=row.begin_filter, eind_filter=row.eind_filter,
                                  bovenkant_peilbuis=row.bovenkant_peilbuis, th=th)


def summarize(row, result: pd.DataFrame) -> dict:
    return {
        "filename": row.filename, "meetreeks_id": row.meetreeks_id, "vendor": row.vendor,
        "filter_bottom": checks.filter_bottom(row.begin_filter, row.eind_filter),
        "eind_filter": checks.as_float(row.eind_filter),
        "top_of_well": checks.as_float(row.bovenkant_peilbuis),
        "n_hours": len(result),
        **{f"n_{c}": int(result[c].notna().sum()) for c in checks.FLAG_COLUMNS},
        **{f"n_{c}": int(result[c].sum()) for c in checks.RULE_COLUMNS},
        "n_valid_after_flags": int(result["head"].notna().sum()),
    }


def write_validated(settings: dict, filename: str, result: pd.DataFrame) -> Path:
    out_dir = _path(settings, "validated_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = result.copy()
    out[checks.RULE_COLUMNS] = out[checks.RULE_COLUMNS].astype(int)
    path = out_dir / f"{filename}.csv"
    out.to_csv(path)
    return path


def validate_all(settings: dict, th: checks.Thresholds | None = None, *,
                 write: bool = True, limit: int | None = None) -> pd.DataFrame:
    """Validate every series in `object_data.csv`; write the per-series results and the
    summary when `write`. Returns the summary frame."""
    th = th or thresholds(settings)
    meta = load_object_data(settings)
    if limit:
        meta = meta.head(limit)
    if write:   # start clean so a removed series never leaves a stale file behind
        out_dir = _path(settings, "validated_dir")
        for old in out_dir.glob("*.csv") if out_dir.is_dir() else ():
            old.unlink()
    rows = []
    for row in meta.itertuples(index=False):
        result = validate_row(settings, row, th)
        if write:
            write_validated(settings, row.filename, result)
        rows.append(summarize(row, result))
    summary = pd.DataFrame(rows)
    if write:
        path = _path(settings, "validation_summary_file")
        path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(path, index=False)
    return summary
