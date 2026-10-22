"""Master validation store: schema, load/save, and the incremental merge.

The store is one CSV per series with columns:
    timestamp, raw_data, gecontroleerd, afgekeurd, note

New raw data is folded in via :func:`merge_new_raw`, which protects the
manual validation (and the validated raw value) of already-reviewed rows.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

COLUMNS = ["timestamp", "raw_data", "gecontroleerd", "afgekeurd", "note"]


def empty_store() -> pd.DataFrame:
    df = pd.DataFrame(columns=COLUMNS)
    return _coerce_types(df)


def ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantee all store columns exist with correct dtypes (back-compat)."""
    df = df.copy()
    if "timestamp" not in df.columns:
        # Support stores/exports that used the index or a 'Time' column.
        if "Time" in df.columns:
            df = df.rename(columns={"Time": "timestamp"})
        else:
            df = df.reset_index().rename(columns={df.index.name or "index": "timestamp"})
    if "raw_data" not in df.columns and "head" in df.columns:
        df = df.rename(columns={"head": "raw_data"})

    for col, default in (("gecontroleerd", 0), ("afgekeurd", 0), ("note", "")):
        if col not in df.columns:
            df[col] = default
    if "raw_data" not in df.columns:
        df["raw_data"] = pd.NA

    df = df[COLUMNS]
    return _coerce_types(df)


def _coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df["raw_data"] = pd.to_numeric(df["raw_data"], errors="coerce")
    for col in ("gecontroleerd", "afgekeurd"):
        df[col] = (
            pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int).clip(0, 1)
        )
    df["note"] = df["note"].fillna("").astype(str)
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def normalize_raw(raw_df: pd.DataFrame, round_freq: str = "1h") -> pd.DataFrame:
    """Turn a raw only_csv frame (Time, head) into timestamp/raw_data at hourly key.

    Timestamps are rounded to ``round_freq`` and duplicates averaged, giving a
    stable merge key across re-exports.
    """
    df = raw_df.copy()

    time_col = "Time" if "Time" in df.columns else df.columns[0]
    val_col = "head" if "head" in df.columns else df.columns[1]

    out = pd.DataFrame({
        "timestamp": pd.to_datetime(df[time_col], errors="coerce"),
        "raw_data": pd.to_numeric(df[val_col], errors="coerce"),
    })
    out = out.dropna(subset=["timestamp"])
    out["timestamp"] = out["timestamp"].dt.round(round_freq)
    out = (
        out.groupby("timestamp", as_index=False)["raw_data"]
        .mean()
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    return out


def merge_new_raw(
    store_df: pd.DataFrame,
    raw_df: pd.DataFrame,
    round_freq: str = "1h",
) -> pd.DataFrame:
    """Merge new raw data into an existing store, protecting reviewed rows.

    - Existing timestamps keep their gecontroleerd/afgekeurd/note. Reviewed rows
      (gecontroleerd==1 or afgekeurd==1) also keep their stored raw_data so a
      validated value is never overwritten; unreviewed rows refresh raw_data
      from the incoming data.
    - New timestamps are appended as unreviewed (gecontroleerd=0, afgekeurd=0).
    """
    store = ensure_columns(store_df)
    incoming = normalize_raw(raw_df, round_freq=round_freq)

    merged = store.merge(
        incoming, on="timestamp", how="outer", suffixes=("", "_new")
    )

    reviewed = (merged["gecontroleerd"].fillna(0) == 1) | (
        merged["afgekeurd"].fillna(0) == 1
    )
    # raw_data: keep stored value for reviewed rows; otherwise prefer incoming.
    incoming_val = merged["raw_data_new"]
    stored_val = merged["raw_data"]
    merged["raw_data"] = incoming_val.where(~reviewed, stored_val)
    # Rows only present in the store (no incoming) fall back to stored value.
    merged["raw_data"] = merged["raw_data"].fillna(stored_val)
    merged = merged.drop(columns=["raw_data_new"])

    return _coerce_types(merged)


def load(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_store()
    df = pd.read_csv(path, encoding="utf-8-sig", encoding_errors="replace")
    return ensure_columns(df)


def save(path: Path, df: pd.DataFrame) -> None:
    """Atomically write the store CSV (temp file + os.replace)."""
    out = ensure_columns(df)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        out.to_csv(tmp_path, index=False, encoding="utf-8-sig")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def migrate_legacy_stores(base_data_dir: Path, validation_store_dir: Path) -> int:
    """Copy dashboard CSVs from the old output_data layout to validation_store_dir.

    Returns the number of files migrated (skips files that already exist at the
    destination).
    """
    from dashboard import data_access

    migrated = 0
    for vendor in data_access.list_vendors(base_data_dir):
        for origin in data_access.list_origins(base_data_dir, vendor):
            old_dir = base_data_dir / vendor / origin / "dashboard"
            if not old_dir.is_dir():
                continue
            for csv_path in old_dir.glob("*.csv"):
                dest = validation_store_dir / vendor / origin / csv_path.name
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(csv_path, dest)
                migrated += 1
    return migrated


def compute_stats(df: pd.DataFrame) -> dict:
    total = len(df)
    reviewed = int((df["gecontroleerd"] == 1).sum())
    rejected = int((df["afgekeurd"] == 1).sum())
    unreviewed = total - reviewed
    return {
        "total": total,
        "reviewed": reviewed,
        "rejected": rejected,
        "unreviewed": unreviewed,
        "pct_reviewed": (reviewed / total * 100) if total else 0.0,
        "pct_rejected": (rejected / total * 100) if total else 0.0,
    }
