"""Discovery of vendors, origins, and series; location of raw + validated files.

The dashboard reads raw series from the pipeline output at
``<base>/{vendor}/<origin>/only_csv/*.csv`` and persists manual validation to a
parallel ``<base>/{vendor}/<origin>/dashboard/<series>.csv`` store.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

VENDORS = ("fugro", "wiertsema")


@dataclass
class SeriesRef:
    """A single per-sensor series and its associated file locations."""

    vendor: str
    origin: str
    name: str            # file stem (series identifier)
    only_csv: Path       # raw source file
    dashboard_csv: Path  # master validation store (may not exist yet)
    validated_csv: Path | None  # automatic-flag file, if present


def list_vendors(base_data_dir: Path) -> list[str]:
    return [v for v in VENDORS if (base_data_dir / v).is_dir()]


def list_origins(base_data_dir: Path, vendor: str) -> list[str]:
    vendor_dir = base_data_dir / vendor
    if not vendor_dir.is_dir():
        return []
    origins = [
        d.name
        for d in vendor_dir.iterdir()
        if d.is_dir() and (d / "only_csv").is_dir()
    ]
    return sorted(origins)


def list_series(
    base_data_dir: Path,
    vendor: str,
    origin: str,
    validation_store_dir: Path | None = None,
) -> list[SeriesRef]:
    origin_dir = base_data_dir / vendor / origin
    only_csv_dir = origin_dir / "only_csv"
    if not only_csv_dir.is_dir():
        return []

    if validation_store_dir is not None:
        dashboard_dir = validation_store_dir / vendor / origin
    else:
        dashboard_dir = origin_dir / "dashboard"
    validated_dir = origin_dir / "validated"

    refs: list[SeriesRef] = []
    for csv_path in sorted(only_csv_dir.glob("*.csv")):
        name = csv_path.stem
        validated_path = validated_dir / f"{name}.csv"
        refs.append(
            SeriesRef(
                vendor=vendor,
                origin=origin,
                name=name,
                only_csv=csv_path,
                dashboard_csv=dashboard_dir / f"{name}.csv",
                validated_csv=validated_path if validated_path.exists() else None,
            )
        )
    return refs
