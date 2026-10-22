"""Color-coded Plotly figure for the validation dashboard.

Color priority: afgekeurd -> RED, else gecontroleerd -> GREEN, else GRAY.
Optionally overlays automatic-flag hint markers (v2/v3/v4).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .autoflags import FLAG_COLUMNS, FLAG_LABELS

STATUS_RED = "#D62728"     # afgekeurd (rejected)
STATUS_GREEN = "#2CA02C"   # gecontroleerd (reviewed & ok)
STATUS_GRAY = "#999999"    # unreviewed

# Above this point count, switch to WebGL rendering for responsiveness.
GL_THRESHOLD = 5000


def status_series(df: pd.DataFrame) -> pd.Series:
    """Map each row to 'rejected' / 'reviewed' / 'unreviewed'."""
    status = np.where(
        df["afgekeurd"] == 1, "rejected",
        np.where(df["gecontroleerd"] == 1, "reviewed", "unreviewed"),
    )
    return pd.Series(status, index=df.index)


def build_figure(
    df: pd.DataFrame,
    flag_hints: pd.DataFrame | None = None,
    title: str = "",
    stressor_data: dict | None = None,
) -> go.Figure:
    scatter_cls = go.Scattergl if len(df) > GL_THRESHOLD else go.Scatter
    has_stressors = stressor_data is not None
    fig = go.Figure()

    # Faint connecting line for readability of the overall series shape.
    fig.add_trace(scatter_cls(
        x=df["timestamp"], y=df["raw_data"],
        mode="lines",
        line=dict(color="rgba(120,120,120,0.35)", width=1),
        name="raw_data",
        hoverinfo="skip",
        showlegend=False,
    ))

    # Daily-mean resampled line (the signal that goes into the model).
    ts = df.set_index("timestamp")["raw_data"]
    daily = ts.resample("1D").mean().dropna()
    if not daily.empty:
        fig.add_trace(go.Scatter(
            x=daily.index, y=daily.values,
            mode="lines",
            line=dict(color="rgba(140,140,140,0.6)", width=1.5, dash="dot"),
            name="Daily mean",
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:.4f} m<extra></extra>",
        ))

    status = status_series(df)
    groups = [
        ("unreviewed", STATUS_GRAY, "Unreviewed"),
        ("reviewed", STATUS_GREEN, "Reviewed (gecontroleerd)"),
        ("rejected", STATUS_RED, "Rejected (afgekeurd)"),
    ]
    for key, color, label in groups:
        sub = df[status == key]
        if sub.empty:
            continue
        customdata = np.stack([sub["note"].fillna("").to_numpy()], axis=-1)
        fig.add_trace(scatter_cls(
            x=sub["timestamp"], y=sub["raw_data"],
            mode="markers",
            marker=dict(color=color, size=6),
            name=label,
            customdata=customdata,
            hovertemplate=(
                "%{x|%Y-%m-%d %H:%M}<br>"
                "head: %{y:.4f} m<br>"
                "note: %{customdata[0]}<extra></extra>"
            ),
        ))

    # ---- Automatic-flag hint markers (open symbols above the points) ----
    if flag_hints is not None and not flag_hints.empty:
        merged = df.merge(flag_hints, on="timestamp", how="left")
        symbols = {"v2": "circle-open", "v3": "diamond-open", "v4": "square-open"}
        colors = {"v2": "#FF7F0E", "v3": "#E377C2", "v4": "#8C564B"}
        for col in FLAG_COLUMNS:
            if col not in merged.columns:
                continue
            mask = merged[col].fillna(False).astype(bool)
            if not mask.any():
                continue
            fig.add_trace(go.Scatter(
                x=merged.loc[mask, "timestamp"],
                y=merged.loc[mask, "raw_data"],
                mode="markers",
                marker=dict(color=colors[col], size=11, symbol=symbols[col],
                            line=dict(width=1.5)),
                name=FLAG_LABELS[col],
                hoverinfo="skip",
                opacity=0.8,
            ))

    # ---- Stressor overlay (precipitation bars + evaporation line) ----
    if has_stressors:
        t_min, t_max = df["timestamp"].min(), df["timestamp"].max()
        prec = stressor_data["precipitation"].loc[t_min:t_max].resample("1D").sum()
        evap = stressor_data["evaporation"].loc[t_min:t_max].resample("1D").sum()

        fig.add_trace(go.Bar(
            x=prec.index, y=prec.values,
            marker_color="rgba(65,105,225,0.5)",
            name="Precipitation [mm/d]",
            yaxis="y2",
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:.1f} mm<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=evap.index, y=evap.values,
            mode="lines",
            line=dict(color="rgba(255,140,0,0.8)", width=1.5),
            name="Evaporation [mm/d]",
            yaxis="y2",
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f} mm<extra></extra>",
        ))

        max_precip = prec.max() if len(prec) else 1
        for trace in fig.data:
            if trace.yaxis is None:
                trace.yaxis = "y"

    layout_kw = dict(
        title=title,
        template="plotly_white",
        hovermode="closest",
        legend=dict(orientation="h", y=1.12 if has_stressors else 1.08,
                    x=0.5, xanchor="center"),
        margin=dict(t=80 if has_stressors else 70,
                    r=60 if has_stressors else 20, b=40, l=60),
        height=520,
        xaxis_title="Time",
        yaxis_title="Head [m NAP]",
        uirevision="keep",
    )
    if has_stressors:
        layout_kw["yaxis2"] = dict(
            title="P / E [mm/d]",
            overlaying="y",
            side="right",
            range=[max_precip * 3, 0],
            showgrid=False,
        )
    fig.update_layout(**layout_kw)
    return fig
