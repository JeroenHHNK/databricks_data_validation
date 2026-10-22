"""Collect validation progress stats across all vendors, origins, and series."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard import data_access, validation_store


@st.cache_data(ttl=120, show_spinner="Loading overview…")
def collect_all_stats(
    base_data_dir_str: str,
    validation_store_dir_str: str,
) -> pd.DataFrame:
    base = Path(base_data_dir_str)
    store_dir = Path(validation_store_dir_str)
    rows: list[dict] = []

    for vendor in data_access.list_vendors(base):
        for origin in data_access.list_origins(base, vendor):
            for ref in data_access.list_series(
                base, vendor, origin, validation_store_dir=store_dir,
            ):
                try:
                    if ref.dashboard_csv.exists():
                        df = validation_store.load(ref.dashboard_csv)
                        stats = validation_store.compute_stats(df)
                        rows.append({
                            "vendor": vendor,
                            "folder": origin,
                            "series": ref.name,
                            "amount_of_rows": stats["total"],
                            "reviewed_rows": stats["reviewed"],
                            "rejected_rows": stats["rejected"],
                            "gecontroleerd_%": stats["pct_reviewed"],
                            "afgekeurd_%": stats["pct_rejected"],
                        })
                    else:
                        n = len(pd.read_csv(
                            ref.only_csv, usecols=[0],
                            encoding="utf-8-sig", encoding_errors="replace",
                        ))
                        rows.append({
                            "vendor": vendor,
                            "folder": origin,
                            "series": ref.name,
                            "amount_of_rows": n,
                            "reviewed_rows": 0,
                            "rejected_rows": 0,
                            "gecontroleerd_%": 0.0,
                            "afgekeurd_%": 0.0,
                        })
                except Exception:
                    continue

    if not rows:
        return pd.DataFrame(columns=[
            "vendor", "folder", "series", "amount_of_rows",
            "reviewed_rows", "rejected_rows", "gecontroleerd_%", "afgekeurd_%",
        ])
    return pd.DataFrame(rows)


def compute_folder_summary(detail_df: pd.DataFrame) -> pd.DataFrame:
    if detail_df.empty:
        return detail_df

    grouped = (
        detail_df
        .groupby(["vendor", "folder"], as_index=False)
        .agg(
            amount_of_rows=("amount_of_rows", "sum"),
            reviewed_rows=("reviewed_rows", "sum"),
            rejected_rows=("rejected_rows", "sum"),
            series_count=("series", "count"),
        )
    )
    grouped["gecontroleerd_%"] = (
        grouped["reviewed_rows"] / grouped["amount_of_rows"] * 100
    ).round(1).fillna(0.0)
    grouped["afgekeurd_%"] = (
        grouped["rejected_rows"] / grouped["amount_of_rows"] * 100
    ).round(1).fillna(0.0)
    return grouped


#: Label used for the combined all-vendor row in the folder summary.
TOTAL_LABEL = "TOTAL"


def append_total_row(summary_df: pd.DataFrame) -> pd.DataFrame:
    """Append one row totalling every folder across all vendors.

    Percentages are recomputed from the summed absolutes rather than averaged,
    so the total reflects the real overall share.
    """
    if summary_df.empty:
        return summary_df

    body = summary_df[summary_df["vendor"] != TOTAL_LABEL]
    rows = int(body["amount_of_rows"].sum())
    reviewed = int(body["reviewed_rows"].sum())
    rejected = int(body["rejected_rows"].sum())

    total = {
        "vendor": TOTAL_LABEL,
        "folder": f"all vendors ({body['vendor'].nunique()})",
        "amount_of_rows": rows,
        "reviewed_rows": reviewed,
        "rejected_rows": rejected,
        "series_count": int(body["series_count"].sum()),
        "gecontroleerd_%": round(reviewed / rows * 100, 1) if rows else 0.0,
        "afgekeurd_%": round(rejected / rows * 100, 1) if rows else 0.0,
    }
    return pd.concat([body, pd.DataFrame([total])], ignore_index=True)
