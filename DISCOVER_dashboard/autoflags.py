"""Read automatic quality flags (v2/v3/v4) from a matching validated/ CSV.

These are surfaced as visual hints on the plot only; they never change the
manual gecontroleerd/afgekeurd state.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

FLAG_COLUMNS = ["v2", "v3", "v4"]
FLAG_LABELS = {
    "v2": "Step change (v2)",
    "v3": "Constant head (v3)",
    "v4": "Outlier (v4)",
}


def load_flag_hints(validated_csv: Path | None, round_freq: str = "1h") -> pd.DataFrame:
    """Return a frame of timestamp + one boolean column per present flag.

    Empty frame if there is no validated file. Timestamps are rounded to the
    same key used by the store so they align on merge.
    """
    if validated_csv is None or not validated_csv.exists():
        return pd.DataFrame(columns=["timestamp"])

    df = pd.read_csv(
        validated_csv, index_col=0, parse_dates=True,
        encoding="utf-8-sig", encoding_errors="replace",
    )
    df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[~df.index.isna()]

    out = pd.DataFrame({"timestamp": df.index.round(round_freq)})
    for col in FLAG_COLUMNS:
        if col in df.columns:
            out[col] = df[col].notna().values
    out = out.groupby("timestamp", as_index=False).max()
    return out
