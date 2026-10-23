"""Color-coded review figure, ported from the on-prem `dashboard/plotting.py`.

Status colours: afgekeurd by a human -> RED, afgekeurd by a rule but not yet
confirmed -> PINK, gecontroleerd -> GREEN, else GRAY. The automated checks (v1-v4, rule
A) are drawn as open hint markers; they never change the store. The filter bottom (with
the v3 band) and the top of the well are drawn as reference lines.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from local_validation import checks
from local_validation.stores import store_status

STATUS_STYLE = {   # status -> (colour, legend label)
    "unreviewed": ("#999999", "Unreviewed"),
    "reviewed": ("#2CA02C", "Reviewed (gecontroleerd)"),
    "auto-rejected": ("#F7A1A1", "Auto-rejected, unconfirmed"),
    "rejected": ("#D62728", "Rejected (afgekeurd)"),
}
HINT_STYLE = {     # check -> (symbol, colour, label)
    "v1": ("x-thin-open", "#17BECF", "v1 out of bounds"),
    "v2": ("circle-open", "#FF7F0E", "v2 step change"),
    "v3": ("diamond-open", "#E377C2", "v3 at filter bottom"),
    "v4": ("square-open", "#8C564B", "v4 outlier"),
    "rule_a": ("triangle-down-open", "#BCBD22", "rule A drop (report-only)"),
}
GL_THRESHOLD = 5000  # above this many points switch to WebGL


def flag_hints(flags: pd.DataFrame) -> pd.DataFrame:
    """timestamp + one boolean column per check, from a `validate_series` result."""
    out = pd.DataFrame({"timestamp": flags.index})
    for col in checks.FLAG_COLUMNS:
        out[col] = flags[col].notna().to_numpy()
    for col in checks.RULE_COLUMNS:
        out[col] = flags[col].astype(bool).to_numpy()
    return out


def build_figure(store: pd.DataFrame, hints: pd.DataFrame | None = None, *, title: str = "",
                 eind_filter: float | None = None, top_of_well: float | None = None,
                 v3_band: float = 0.15, stressor_data: dict | None = None,
                 show_hints: tuple[str, ...] = tuple(HINT_STYLE)) -> go.Figure:
    scatter_cls = go.Scattergl if len(store) > GL_THRESHOLD else go.Scatter
    fig = go.Figure()
    fig.add_trace(scatter_cls(
        x=store["timestamp"], y=store["raw_data"], mode="lines",
        line=dict(color="rgba(120,120,120,0.35)", width=1),
        hoverinfo="skip", showlegend=False, name="raw_data"))

    daily = store.set_index("timestamp")["raw_data"].resample("1D").mean().dropna()
    if not daily.empty:
        fig.add_trace(go.Scatter(
            x=daily.index, y=daily.values, mode="lines", name="Daily mean",
            line=dict(color="rgba(140,140,140,0.6)", width=1.5, dash="dot"),
            hovertemplate="%{x|%Y-%m-%d}<br>%{y:.4f} m<extra></extra>"))

    status = store_status(store)
    for key, (colour, label) in STATUS_STYLE.items():
        sub = store[status == key]
        if sub.empty:
            continue
        fig.add_trace(scatter_cls(
            x=sub["timestamp"], y=sub["raw_data"], mode="markers", name=label,
            marker=dict(color=colour, size=6),
            customdata=np.stack([sub["note"].fillna("").to_numpy()], axis=-1),
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>head: %{y:.4f} m<br>"
                          "note: %{customdata[0]}<extra></extra>"))

    if hints is not None and not hints.empty:
        merged = store[["timestamp", "raw_data"]].merge(hints, on="timestamp", how="left")
        for col in show_hints:
            if col not in merged:
                continue
            mask = merged[col].fillna(False).astype(bool)
            if not mask.any():
                continue
            symbol, colour, label = HINT_STYLE[col]
            fig.add_trace(scatter_cls(
                x=merged.loc[mask, "timestamp"], y=merged.loc[mask, "raw_data"],
                mode="markers", name=f"{label} ({int(mask.sum())})", hoverinfo="skip",
                marker=dict(color=colour, size=11, symbol=symbol, line=dict(width=1.5)),
                opacity=0.8))

    if eind_filter is not None:
        fig.add_hrect(y0=eind_filter - v3_band, y1=eind_filter + v3_band, line_width=0,
                      fillcolor="#E377C2", opacity=0.08, layer="below")
        fig.add_hline(y=eind_filter, line=dict(color="#E377C2", dash="dash", width=1),
                      annotation_text="eind_filter", annotation_position="bottom left")
    if top_of_well is not None:
        fig.add_hline(y=top_of_well, line=dict(color="#17BECF", dash="dot", width=1),
                      annotation_text="top of well", annotation_position="top left")

    has_stressors = stressor_data is not None and not store.empty
    if has_stressors:
        t0, t1 = store["timestamp"].min(), store["timestamp"].max()
        prec = stressor_data["precipitation"].loc[t0:t1].resample("1D").sum()
        evap = stressor_data["evaporation"].loc[t0:t1].resample("1D").sum()
        fig.add_trace(go.Bar(x=prec.index, y=prec.values, yaxis="y2",
                             marker_color="rgba(65,105,225,0.5)", name="Precipitation [mm/d]",
                             hovertemplate="%{x|%Y-%m-%d}<br>%{y:.1f} mm<extra></extra>"))
        fig.add_trace(go.Scatter(x=evap.index, y=evap.values, yaxis="y2", mode="lines",
                                 line=dict(color="rgba(255,140,0,0.8)", width=1.5),
                                 name="Evaporation [mm/d]",
                                 hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f} mm<extra></extra>"))
        max_p = float(prec.max()) if len(prec) and prec.max() > 0 else 1.0

    layout = dict(
        title=title, template="plotly_white", hovermode="closest", height=540,
        legend=dict(orientation="h", y=1.14, x=0.5, xanchor="center"),
        margin=dict(t=90, r=60 if has_stressors else 20, b=40, l=60),
        xaxis_title="Time", yaxis_title="Head [m NAP]",
        uirevision="keep", dragmode="zoom")
    if has_stressors:
        layout["yaxis2"] = dict(title="P / E [mm/d]", overlaying="y", side="right",
                                range=[max_p * 3, 0], showgrid=False)
    fig.update_layout(**layout)
    return fig
