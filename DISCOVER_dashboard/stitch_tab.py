"""Streamlit UI for the Sensor Stitching tab."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.sensor_stitch import (
    combine_sensors,
    corrections_path,
    find_sensor_groups,
    load_corrections,
    load_sensor_group,
    save_corrections,
    sensor_summary,
)
from dashboard import validation_store

SENSOR_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
]


def _build_stitch_figure(
    sensors: dict[str, pd.DataFrame],
    corrections: dict[str, float],
    combined: pd.DataFrame,
    title: str = "",
) -> go.Figure:
    fig = go.Figure()

    for i, label in enumerate(sorted(sensors.keys())):
        df = sensors[label]
        offset = corrections.get(label, 0.0)
        color = SENSOR_COLORS[i % len(SENSOR_COLORS)]
        suffix = f" ({offset:+.4f} m)" if offset != 0 else ""

        # Raw (uncorrected) as faint
        fig.add_trace(go.Scattergl(
            x=df["timestamp"], y=df["raw_data"],
            mode="lines",
            line=dict(color=color, width=1, dash="dot"),
            opacity=0.3,
            name=f"{label} raw",
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y:.4f} m (raw)<extra></extra>",
        ))

        # Corrected as solid
        fig.add_trace(go.Scattergl(
            x=df["timestamp"], y=df["raw_data"] + offset,
            mode="lines",
            line=dict(color=color, width=1.5),
            name=f"{label}{suffix}",
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y:.4f} m (corrected)<extra></extra>",
        ))

    # Combined line on top
    if not combined.empty:
        fig.add_trace(go.Scattergl(
            x=combined["timestamp"], y=combined["raw_data"],
            mode="lines",
            line=dict(color="black", width=2),
            name="Combined",
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y:.4f} m<br>%{customdata[0]}<extra></extra>",
            customdata=combined[["source_sensor"]].values,
        ))

    fig.update_layout(
        title=title,
        template="plotly_white",
        hovermode="closest",
        legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
        margin=dict(t=70, r=20, b=40, l=60),
        height=520,
        xaxis_title="Time",
        yaxis_title="Head [m NAP]",
    )
    return fig


def render_stitch_tab(
    base_data_dir: Path,
    validation_store_dir: Path,
    round_freq: str = "1h",
) -> None:
    st.header("Sensor Stitching")
    st.caption(
        "Combine multi-sensor piezometer series into a single timeseries. "
        "Set vertical corrections to remove jumps from sensor replacements."
    )

    from dashboard.data_access import list_vendors, list_origins

    col_v, col_o, _ = st.columns([1, 1, 1])
    with col_v:
        vendors = list_vendors(base_data_dir)
        if not vendors:
            st.info("No vendor folders found.")
            return
        vendor = st.selectbox("Vendor", vendors, key="stitch_vendor")

    with col_o:
        origins = list_origins(base_data_dir, vendor)
        if not origins:
            st.warning("No origins found.")
            return
        origin = st.selectbox("Origin", origins, key="stitch_origin")

    only_csv_dir = base_data_dir / vendor / origin / "only_csv"
    groups = find_sensor_groups(only_csv_dir)

    if not groups:
        st.info("No multi-sensor groups found in this origin. "
                "Only series with `_sensor_1`, `_sensor_2`, etc. appear here.")
        return

    group_names = sorted(groups.keys())
    selected_base = st.selectbox(
        "Sensor group",
        group_names,
        key="stitch_group",
    )
    sensor_files = groups[selected_base]

    st.divider()

    # Load sensor data
    sensors = load_sensor_group(sensor_files, round_freq=round_freq)
    summaries = sensor_summary(sensors)

    # Load existing corrections
    corr_path = corrections_path(only_csv_dir, selected_base)
    saved_corrections = load_corrections(corr_path)

    # Sensor info table and correction inputs
    st.subheader("Sensor corrections")

    corrections: dict[str, float] = {}
    cols = st.columns(len(summaries))
    for i, info in enumerate(summaries):
        label = info["label"]
        with cols[i]:
            st.markdown(f"**{label}**")
            st.caption(
                f"{info['rows']} rows\n\n"
                f"{info['start'].strftime('%Y-%m-%d')} → {info['end'].strftime('%Y-%m-%d')}"
            )
            default_val = saved_corrections.get(label, 0.0)
            offset = st.number_input(
                f"Correction (m)",
                value=default_val,
                step=0.001,
                format="%.4f",
                key=f"stitch_corr_{label}",
            )
            corrections[label] = offset

    # Combine
    combined = combine_sensors(sensors, corrections)

    # Plot
    st.subheader("Preview")
    fig = _build_stitch_figure(sensors, corrections, combined, title=selected_base)
    st.plotly_chart(fig, key="stitch_chart", use_container_width=True)

    # Actions
    st.divider()
    col_save_corr, col_save_combined, _ = st.columns([1, 1, 1])

    with col_save_corr:
        if st.button("Save corrections", key="stitch_save_corr"):
            save_corrections(corr_path, corrections)
            st.success(f"Saved → {corr_path.name}")

    with col_save_combined:
        if st.button("Save combined series", type="primary", key="stitch_save_combined"):
            # Save as a raw-format CSV (Time, head) in only_csv/
            combined_csv = only_csv_dir / f"{selected_base}.csv"
            out_df = pd.DataFrame({
                "Time": combined["timestamp"],
                "head": combined["raw_data"],
            })
            out_df.to_csv(combined_csv, index=False)

            # Also create/update the validation store entry
            dashboard_dir = validation_store_dir / vendor / origin
            dashboard_csv = dashboard_dir / f"{selected_base}.csv"

            if dashboard_csv.exists():
                store = validation_store.load(dashboard_csv)
                store = validation_store.merge_new_raw(
                    store,
                    pd.read_csv(combined_csv, encoding="utf-8-sig", encoding_errors="replace"),
                    round_freq=round_freq,
                )
            else:
                raw_df = pd.read_csv(combined_csv, encoding="utf-8-sig", encoding_errors="replace")
                store = validation_store.merge_new_raw(
                    validation_store.empty_store(), raw_df, round_freq=round_freq,
                )

            validation_store.save(dashboard_csv, store)
            save_corrections(corr_path, corrections)

            st.success(
                f"Saved combined series ({len(combined)} rows) → {combined_csv.name}\n\n"
                f"Validation store updated → {dashboard_csv.name}"
            )
