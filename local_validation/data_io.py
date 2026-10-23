"""Paths, settings and file access for the three data layers.

The layers are kept strictly apart (agent_streamlit_prompt.md §2):

| Layer | Folder | Git | Written by |
|---|---|---|---|
| 1. Source copy | `input_output/output_databricks/` | tracked (transport from Databricks) | the Databricks export **only** |
| 2. Derived     | `output_data/validated/`, `output_data/validation_summary.csv` | ignored, disposable | `validate.ipynb` |
| 3. Validation store | `input_output/output_repository/<filename>.csv` | tracked, merge-only | review app + autoflag |

Layer 1 is read-only for everything in this repo: every write goes through
`assert_writable()`, which refuses any path inside it. A fresh export overwrites layer 1,
layer 2 is regenerated, and layer 3 is merged into (`stores.py`), never replaced.

Paths and thresholds come from config/settings.yaml; a missing file or key falls back to
`DEFAULTS`, so the app still starts.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import checks

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = REPO_ROOT / "config" / "settings.yaml"

DEFAULTS = {
    "databricks_output_dir": "input_output/output_databricks",
    "time_series_dir": "input_output/output_databricks/time_series_data",
    "metadata_file": "input_output/output_databricks/metadata/object_data.csv",
    "validated_dir": "output_data/validated",
    "validation_summary_file": "output_data/validation_summary.csv",
    "repository_output_dir": "input_output/output_repository",
    "forcing_dir": "data/forcing",
    "notes_max_len": 30,
    "store": {"frequency": "1h"},
    "flags": {},
    "autoflag": {"rule_a_apply": False, "rule_b_apply": True},
}


class SourceWriteError(PermissionError):
    """Raised when code tries to write into the read-only source copy (layer 1)."""


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    settings = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        import yaml

        with open(path, encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
    except (OSError, ImportError):
        loaded = {}
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(settings.get(k), dict):
            settings[k].update(v)
        elif v is not None:
            settings[k] = v
    return settings


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


def round_freq(settings: dict) -> str:
    return settings.get("store", {}).get("frequency", "1h")


def _path(settings: dict, key: str) -> Path:
    p = Path(settings[key])
    return p if p.is_absolute() else REPO_ROOT / p


# --- Layer boundaries ---------------------------------------------------------
def source_dir(settings: dict) -> Path:
    return _path(settings, "databricks_output_dir")


def store_dir(settings: dict) -> Path:
    return _path(settings, "repository_output_dir")


def assert_writable(settings: dict, path: Path) -> Path:
    """Refuse any write into the source copy (layer 1). Returns the resolved path."""
    target, src = Path(path).resolve(), source_dir(settings).resolve()
    if target == src or src in target.parents:
        raise SourceWriteError(f"refusing to write into the read-only source copy: {target}")
    return target


# --- Layer 1: source copy (read-only) ------------------------------------------
def load_object_data(settings: dict) -> pd.DataFrame:
    return pd.read_csv(_path(settings, "metadata_file"))


def series_csv(settings: dict, filename: str) -> Path:
    return _path(settings, "time_series_dir") / f"{filename}.csv"


def load_series(settings: dict, filename: str) -> pd.Series:
    """One series as an hourly `head` Series indexed by timestamp."""
    df = pd.read_csv(series_csv(settings, filename), parse_dates=["Time"],
                     encoding="utf-8-sig", encoding_errors="replace")
    return df.set_index("Time")["head"].rename("head")


def load_source_frame(settings: dict, filename: str) -> pd.DataFrame:
    """The source series as the store expects it (`Time,head`). Values are rounded to
    1e-6 m: Spark's averaging differs by ~1e-15 m between exports, which would otherwise
    rewrite every unreviewed store row on each re-import."""
    df = pd.read_csv(series_csv(settings, filename), encoding="utf-8-sig",
                     encoding_errors="replace")
    df["head"] = pd.to_numeric(df["head"], errors="coerce").round(6)
    return df


# --- Layer 2: derived (disposable) ---------------------------------------------
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
    out_dir = assert_writable(settings, _path(settings, "validated_dir"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = result.copy()
    out[checks.RULE_COLUMNS] = out[checks.RULE_COLUMNS].astype(int)
    path = out_dir / f"{filename}.csv"
    out.to_csv(path)
    return path


def load_summary(settings: dict) -> pd.DataFrame | None:
    path = _path(settings, "validation_summary_file")
    return pd.read_csv(path) if path.exists() else None


def validate_all(settings: dict, th: checks.Thresholds | None = None, *,
                 write: bool = True, limit: int | None = None) -> pd.DataFrame:
    """Validate every series in `object_data.csv`; write the per-series results and the
    summary when `write`. Returns the summary frame."""
    th = th or thresholds(settings)
    meta = load_object_data(settings)
    if limit:
        meta = meta.head(limit)
    if write:   # start clean so a removed series never leaves a stale file behind
        out_dir = assert_writable(settings, _path(settings, "validated_dir"))
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
        path = assert_writable(settings, _path(settings, "validation_summary_file"))
        path.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(path, index=False)
    return summary
