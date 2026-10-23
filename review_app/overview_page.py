"""Overview: review progress per vendor and per series, plus the automated-check counts.

Series without a store count their source rows at 0 % reviewed. The TOTAL row is
recomputed from summed counts (not averaged). Series that fail to load are counted and
shown, not silently skipped.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from . import common

TOTAL_LABEL = "TOTAL"


def vendor_summary(detail: pd.DataFrame) -> pd.DataFrame:
    g = detail.groupby("vendor", as_index=False).agg(
        series=("filename", "count"), with_store=("has_store", "sum"), rows=("rows", "sum"),
        reviewed=("reviewed", "sum"), rejected=("rejected", "sum"),
        auto_rejected_unconfirmed=("auto_rejected_unconfirmed", "sum"))
    total = g.drop(columns="vendor").sum().to_dict() | {"vendor": TOTAL_LABEL}
    g = pd.concat([g, pd.DataFrame([total])], ignore_index=True)
    g["pct_reviewed"] = (100 * g["reviewed"] / g["rows"].where(g["rows"] > 0)).round(1).fillna(0.0)
    g["pct_rejected"] = (100 * g["rejected"] / g["rows"].where(g["rows"] > 0)).round(1).fillna(0.0)
    return g


def render() -> None:
    st.title("Review progress")
    c1, c2 = st.columns([1, 5])
    if c1.button("Refresh"):
        common.overview.clear()
        common.summary.clear()
        st.rerun()

    detail, failed = common.overview()
    if failed:
        st.warning(f"{failed} series could not be loaded and are not in the tables below.")
    if detail.empty:
        st.info("No series found in object_data.csv.")
        return

    st.subheader("Per vendor")
    st.dataframe(vendor_summary(detail), hide_index=True, width="stretch", column_config={
        "pct_reviewed": st.column_config.NumberColumn("Reviewed %", format="%.1f%%"),
        "pct_rejected": st.column_config.NumberColumn("Rejected %", format="%.1f%%"),
        "with_store": st.column_config.NumberColumn("Stores"),
        "auto_rejected_unconfirmed": st.column_config.NumberColumn("Auto-rejected, to confirm")})

    summary = common.summary()
    if summary is not None:
        flags = [c for c in summary if c.startswith("n_") and c not in ("n_hours", "n_valid_after_flags")]
        detail = detail.merge(summary[["filename", *flags]], on="filename", how="left")
    else:
        st.caption("Run local_validation/validate.ipynb to add the automated-check counts.")

    st.subheader("Per series")
    f1, f2 = st.columns([1, 3])
    vendor = f1.selectbox("Vendor", ["All", *sorted(detail["vendor"].unique())])
    only_started = f2.checkbox("Only series with a validation store")
    view = detail if vendor == "All" else detail[detail["vendor"] == vendor]
    if only_started:
        view = view[view["has_store"]]
    pick = st.dataframe(view, hide_index=True, width="stretch", on_select="rerun",
                        selection_mode="single-row", key="overview_table", column_config={
                            "pct_reviewed": st.column_config.ProgressColumn(
                                "Reviewed %", min_value=0, max_value=100, format="%.1f%%"),
                            "has_store": st.column_config.CheckboxColumn("Store")})
    rows = pick.selection.rows if pick and pick.selection else []
    if rows:
        filename = view.iloc[rows[0]]["filename"]
        if st.button(f"Open {filename} in review →", type="primary"):
            common.open_in_review(filename)
