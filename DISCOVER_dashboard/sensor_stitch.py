"""Sensor stitching: combine multi-sensor series with user-defined corrections.

Each sensor group (files sharing a base name but with ``_sensor_N`` suffixes)
can have a YAML sidecar storing per-sensor vertical corrections. The stitcher
loads all sensors, applies the corrections, and produces a single combined
timeseries where each timestamp takes the value from the highest-numbered
sensor that has data (i.e. the newest sensor wins in overlap regions).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from dataval._utils import get_series_base, group_sensor_files


def corrections_path(only_csv_dir: Path, base_name: str) -> Path:
    return only_csv_dir / f"{base_name}.corrections.yaml"


def load_corrections(path: Path) -> dict[str, float]:
    """Load per-sensor corrections from a YAML sidecar. Returns empty dict if missing."""
    if not path.exists() or yaml is None:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sensors = data.get("sensors", {})
    return {k: float(v.get("correction_m", 0.0)) for k, v in sensors.items()}


def save_corrections(path: Path, corrections: dict[str, float]) -> None:
    if yaml is None:
        raise RuntimeError("pyyaml is required to save corrections")
    data = {
        "sensors": {
            k: {"correction_m": round(v, 4)} for k, v in sorted(corrections.items())
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def load_sensor_group(
    sensor_files: list[Path],
    round_freq: str = "1h",
) -> dict[str, pd.DataFrame]:
    """Load each sensor CSV into a DataFrame keyed by sensor label (e.g. 'sensor_1')."""
    import re
    sensors: dict[str, pd.DataFrame] = {}
    for p in sorted(sensor_files):
        m = re.search(r"_sensor_(\d+)$", p.stem)
        label = f"sensor_{m.group(1)}" if m else p.stem

        df = pd.read_csv(p, encoding="utf-8-sig", encoding_errors="replace")
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
        sensors[label] = out

    return sensors


def combine_sensors(
    sensors: dict[str, pd.DataFrame],
    corrections: dict[str, float],
) -> pd.DataFrame:
    """Combine multiple sensors into a single timeseries.

    For each timestamp, the highest-numbered sensor with data wins (newest sensor
    takes priority). Corrections are applied before combining.
    """
    if not sensors:
        return pd.DataFrame(columns=["timestamp", "raw_data", "source_sensor"])

    corrected: list[pd.DataFrame] = []
    for label in sorted(sensors.keys()):
        df = sensors[label].copy()
        offset = corrections.get(label, 0.0)
        df["raw_data"] = df["raw_data"] + offset
        df["source_sensor"] = label
        corrected.append(df)

    combined = pd.concat(corrected, ignore_index=True)
    combined = combined.sort_values(["timestamp", "source_sensor"])
    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)
    return combined


def find_sensor_groups(only_csv_dir: Path) -> dict[str, list[Path]]:
    """Find all multi-sensor groups in a directory."""
    all_csvs = sorted(only_csv_dir.glob("*.csv"))
    groups = group_sensor_files(all_csvs)
    return {base: files for base, files in groups.items() if len(files) > 1}


def sensor_summary(
    sensors: dict[str, pd.DataFrame],
) -> list[dict]:
    """Return a summary of each sensor's date range and row count."""
    summaries = []
    for label in sorted(sensors.keys()):
        df = sensors[label]
        ts = df["timestamp"]
        summaries.append({
            "label": label,
            "rows": len(df),
            "start": ts.min(),
            "end": ts.max(),
        })
    return summaries
