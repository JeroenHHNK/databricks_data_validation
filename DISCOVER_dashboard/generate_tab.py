"""Generate Data tab: match new raw input files to existing validation folders
and regenerate per-sensor CSVs into output_data/ without touching validation_data/.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from dataval._utils import parse_stable_key, match_origin_folder


INPUT_EXTENSIONS = {
    "fugro": ".csv",
    "wiertsema": ".xlsx",
}

VENDORS = ("fugro", "wiertsema")


@dataclass
class DatasetInfo:
    stable_key: str
    vendor: str
    raw_files: list[Path]
    latest_raw: Path
    existing_origin: str | None
    is_new: bool


def _scan_raw_files(input_dir: Path, vendor: str) -> dict[str, list[Path]]:
    """Group raw input files by stable key."""
    ext = INPUT_EXTENSIONS.get(vendor.lower(), ".*")
    vendor_input = input_dir / vendor.capitalize()
    if vendor.lower() == "wiertsema":
        vendor_input = input_dir / "Wiertsema"
    elif vendor.lower() == "fugro":
        vendor_input = input_dir / "Fugro"

    if not vendor_input.is_dir():
        return {}

    groups: dict[str, list[Path]] = {}
    for f in vendor_input.iterdir():
        if f.is_file() and f.suffix.lower() == ext:
            key = parse_stable_key(f.name, vendor)
            groups.setdefault(key, []).append(f)

    for key in groups:
        groups[key].sort(key=lambda p: p.stat().st_mtime, reverse=True)

    return groups


def _build_dataset_infos(
    input_dir: Path,
    output_dir: Path,
    vendor: str,
) -> list[DatasetInfo]:
    groups = _scan_raw_files(input_dir, vendor)
    infos = []
    for key, files in sorted(groups.items()):
        existing = match_origin_folder(key, vendor, output_dir)
        infos.append(DatasetInfo(
            stable_key=key,
            vendor=vendor,
            raw_files=files,
            latest_raw=files[0],
            existing_origin=existing,
            is_new=existing is None,
        ))
    return infos


def _compute_diff_summary(
    info: DatasetInfo,
    output_dir: Path,
) -> dict:
    """Compare new raw file against existing only_csv data to summarise what's new."""
    if info.existing_origin is None:
        return {"status": "new", "message": "New dataset — no existing validation data."}

    only_csv_dir = output_dir / info.vendor / info.existing_origin / "only_csv"
    if not only_csv_dir.is_dir() or not list(only_csv_dir.glob("*.csv")):
        return {"status": "empty", "message": "Existing folder found but contains no series CSVs."}

    existing_csvs = sorted(only_csv_dir.glob("*.csv"))
    first_csv = existing_csvs[0]
    try:
        df = pd.read_csv(first_csv, encoding="utf-8-sig", encoding_errors="replace")
        time_col = [c for c in df.columns if c.lower() in ("time", "timestamp")]
        if time_col:
            dates = pd.to_datetime(df[time_col[0]], errors="coerce").dropna()
            if not dates.empty:
                old_end = dates.max()
                return {
                    "status": "update",
                    "series_count": len(existing_csvs),
                    "last_timestamp": old_end.strftime("%Y-%m-%d %H:%M"),
                    "message": (
                        f"{len(existing_csvs)} existing series. "
                        f"Last data point: {old_end.strftime('%Y-%m-%d %H:%M')}. "
                        f"New raw file may extend beyond this."
                    ),
                }
    except Exception:
        pass

    return {
        "status": "update",
        "series_count": len(existing_csvs),
        "message": f"{len(existing_csvs)} existing series found.",
    }


def _run_generate(info: DatasetInfo, output_dir: Path) -> str:
    """Run the conversion for the selected dataset, writing into the matched folder."""
    vendor = info.vendor.lower()
    target_origin = info.existing_origin or info.stable_key

    if vendor == "fugro":
        from dataval.convert_fugro import process_fugro_csv
        out_base = output_dir / vendor
        target_dir = out_base / target_origin / "only_csv"
        target_dir.mkdir(parents=True, exist_ok=True)

        written = process_fugro_csv(
            info.latest_raw,
            out_base,
        )
        if info.latest_raw.stem != target_origin:
            new_dir = out_base / info.latest_raw.stem / "only_csv"
            old_dir = out_base / target_origin / "only_csv"
            if new_dir.is_dir() and new_dir != old_dir:
                old_dir.mkdir(parents=True, exist_ok=True)
                for csv_file in new_dir.glob("*.csv"):
                    dest = old_dir / csv_file.name
                    csv_file.replace(dest)
                try:
                    (out_base / info.latest_raw.stem / "only_csv").rmdir()
                    (out_base / info.latest_raw.stem).rmdir()
                except OSError:
                    pass
        return f"Generated {len(written)} series CSV(s) into {target_origin}/only_csv/"

    elif vendor == "wiertsema":
        from dataval.convert_wiertsema import process_workbook
        out_base = output_dir / vendor

        for raw_file in info.raw_files:
            process_workbook(raw_file, out_base, origin_name=target_origin)

        csv_count = len(list((out_base / target_origin / "only_csv").glob("*.csv")))
        return f"Generated {csv_count} series CSV(s) from {len(info.raw_files)} file(s) into {target_origin}/only_csv/"

    return "Unknown vendor."


def render_generate_tab(
    input_dir: Path,
    output_dir: Path,
    validation_dir: Path,
) -> None:
    st.header("Generate Data")
    st.caption(
        "Convert raw input files into per-sensor CSVs. New downloads are "
        "automatically matched to existing validation folders by dataset identity."
    )

    col_vendor, _ = st.columns([1, 2])
    with col_vendor:
        vendor = st.selectbox(
            "Source",
            VENDORS,
            format_func=str.capitalize,
            key="gen_vendor",
        )

    infos = _build_dataset_infos(input_dir, output_dir, vendor)

    if not infos:
        st.info(f"No raw input files found in input_data/{vendor.capitalize()}/")
        return

    key_labels = {i.stable_key: i for i in infos}
    col_ds, _ = st.columns([1, 2])
    with col_ds:
        selected_key = st.selectbox(
            "Dataset",
            list(key_labels.keys()),
            key="gen_dataset",
        )
    info = key_labels[selected_key]

    st.divider()

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("Raw input")
        st.markdown(f"**Stable key:** `{info.stable_key}`")
        st.markdown(f"**Latest raw file:** `{info.latest_raw.name}`")
        mod_time = datetime.fromtimestamp(info.latest_raw.stat().st_mtime)
        st.markdown(f"**Modified:** {mod_time.strftime('%Y-%m-%d %H:%M')}")
        if len(info.raw_files) > 1:
            st.markdown(f"**Related files ({len(info.raw_files)}):**")
            for f in info.raw_files:
                st.caption(f"  {f.name}")

    with col_right:
        st.subheader("Existing data")
        if info.existing_origin:
            st.markdown(f"**Matched folder:** `{info.existing_origin}`")
            val_dir = validation_dir / vendor / info.existing_origin
            has_validation = val_dir.is_dir() and list(val_dir.glob("*.csv"))
            if has_validation:
                val_count = len(list(val_dir.glob("*.csv")))
                st.success(f"Validation data found ({val_count} series)")
            else:
                st.warning("No validation data yet for this folder.")

            diff = _compute_diff_summary(info, output_dir)
            st.info(diff["message"])
        else:
            st.markdown("**No existing match found**")
            st.info("This will be treated as a new dataset. A new folder will be "
                    f"created: `{info.stable_key}`")

    st.divider()

    target = info.existing_origin or info.stable_key
    st.markdown(f"**Output folder:** `output_data/{vendor}/{target}/only_csv/`")

    if info.existing_origin:
        st.warning(
            "This will overwrite the per-sensor CSVs in the existing folder. "
            "Validation data in validation_data/ will NOT be touched — "
            "reviewed rows are preserved when you load the series."
        )

    if st.button("Generate", type="primary", key="gen_run"):
        with st.spinner("Converting raw data..."):
            try:
                result = _run_generate(info, output_dir)
                st.success(result)
                st.balloons()
            except Exception as e:
                st.error(f"Generation failed: {e}")
                raise
