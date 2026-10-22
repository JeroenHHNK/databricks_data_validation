"""Return-period ensemble plots for the validation dashboard.

For every series in an origin folder, compute annual extremes, fit a
distribution model, and collect the predicted head at standard return
periods.  Two Plotly figures are returned (drought and wet) that show:

* a light-gray band spanning the full min–max range across all series,
* a darker-gray band for the IQR (25th–75th percentile),
* the selected series as a highlighted coloured line.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from functions.return_periods import _fit_min_models, _fit_max_models

MODEL_CHOICES = ("gev", "gpd", "exp", "empirical")
MODEL_LABELS = {
    "gev": "GEV (Generalised Extreme Value)",
    "gpd": "GPD (Generalised Pareto)",
    "exp": "Exponential",
    "empirical": "Empirical (Weibull)",
}

T_LIST = np.array([1.0, 2.0, 3.0, 5.0, 10.0, 25.0, 50.0, 100.0, 300.0, 1000.0])

MIN_YEARS = 2


def _annual_extremes(csv_path: Path) -> tuple[pd.Series, pd.Series] | None:
    """Return (annual_min, annual_max) head series, or *None* on failure."""
    try:
        df = pd.read_csv(
            csv_path, index_col=0, parse_dates=True,
            encoding="utf-8-sig", encoding_errors="replace",
        )
    except Exception:
        return None
    col = "head" if "head" in df.columns else df.columns[0] if len(df.columns) else None
    if col is None:
        return None
    vals = pd.to_numeric(df[col], errors="coerce").dropna()
    if vals.empty:
        return None
    vals.index = pd.to_datetime(vals.index, errors="coerce")
    vals = vals[~vals.index.isna()]
    annual_min = vals.resample("YE").min().dropna()
    annual_max = vals.resample("YE").max().dropna()
    if len(annual_min) < MIN_YEARS or len(annual_max) < MIN_YEARS:
        return None
    return annual_min, annual_max


def _fit_single(annual_min: pd.Series, annual_max: pd.Series, model: str):
    """Fit one model to one series, return (drought_heads, wet_heads) arrays or None."""
    model_idx = {"exp": 0, "gpd": 1, "gev": 2, "empirical": 6}
    idx = model_idx.get(model)
    if idx is None:
        return None
    try:
        min_results = _fit_min_models(annual_min)
        min_fn = min_results[idx]
        dr = min_fn(T_LIST)
    except Exception:
        dr = None
    try:
        max_results = _fit_max_models(annual_max)
        max_fn = max_results[idx]
        wet = max_fn(T_LIST)
    except Exception:
        wet = None
    return dr, wet


def compute_origin_ensemble(
    only_csv_dir: Path,
    model: str = "gev",
) -> dict:
    """Compute return-period curves for all series in one origin.

    Returns a dict with keys ``series_names``, ``drought`` and ``wet``,
    where each value matrix has shape (n_series, len(T_LIST)).
    """
    csv_files = sorted(only_csv_dir.glob("*.csv"))
    names: list[str] = []
    drought_rows: list[np.ndarray] = []
    wet_rows: list[np.ndarray] = []

    for csv_path in csv_files:
        extremes = _annual_extremes(csv_path)
        if extremes is None:
            continue
        annual_min, annual_max = extremes
        result = _fit_single(annual_min, annual_max, model)
        if result is None:
            continue
        dr, wet = result
        if dr is not None and wet is not None:
            dr_arr = np.asarray(dr, dtype=float)
            wet_arr = np.asarray(wet, dtype=float)
            if not (np.all(np.isfinite(dr_arr)) and np.all(np.isfinite(wet_arr))):
                continue
            # Discard implausible extrapolations (outside -50..+20 m NAP)
            if np.any(np.abs(dr_arr) > 50) or np.any(np.abs(wet_arr) > 50):
                continue
            names.append(csv_path.stem)
            drought_rows.append(dr_arr)
            wet_rows.append(wet_arr)

    return {
        "series_names": names,
        "drought": np.array(drought_rows) if drought_rows else np.empty((0, len(T_LIST))),
        "wet": np.array(wet_rows) if wet_rows else np.empty((0, len(T_LIST))),
    }


def _band_figure(
    matrix: np.ndarray,
    selected_row: np.ndarray | None,
    selected_name: str,
    title: str,
    y_label: str,
    band_color: str,
    line_color: str,
) -> go.Figure:
    fig = go.Figure()
    if matrix.shape[0] == 0:
        fig.update_layout(title=title, annotations=[
            dict(text="Not enough data", showarrow=False, xref="paper", yref="paper", x=0.5, y=0.5)
        ])
        return fig

    p_min = np.nanmin(matrix, axis=0)
    p_max = np.nanmax(matrix, axis=0)
    p25 = np.nanpercentile(matrix, 12.5, axis=0)
    p75 = np.nanpercentile(matrix, 87.5, axis=0)
    p_median = np.nanmedian(matrix, axis=0)

    x_log = T_LIST.tolist()
    x_rev = x_log[::-1]

    # Full range band (min-max)
    fig.add_trace(go.Scatter(
        x=x_log + x_rev,
        y=p_max.tolist() + p_min[::-1].tolist(),
        fill="toself",
        fillcolor="rgba(180,180,180,0.25)",
        line=dict(color="rgba(180,180,180,0)"),
        name="Full range (all series)",
        hoverinfo="skip",
    ))

    # IQR band (75% of values)
    fig.add_trace(go.Scatter(
        x=x_log + x_rev,
        y=p75.tolist() + p25[::-1].tolist(),
        fill="toself",
        fillcolor=f"rgba{_hex_to_rgba(band_color, 0.30)}",
        line=dict(color=f"rgba{_hex_to_rgba(band_color, 0)}"),
        name="75% of series (P12.5–P87.5)",
        hoverinfo="skip",
    ))

    # Median line
    fig.add_trace(go.Scatter(
        x=x_log, y=p_median.tolist(),
        mode="lines",
        line=dict(color="rgba(100,100,100,0.6)", width=1.5, dash="dash"),
        name="Median (all series)",
        hovertemplate="T=%{x:.0f} yr<br>%{y:.3f} m NAP<extra>Median</extra>",
    ))

    # Selected series
    if selected_row is not None:
        fig.add_trace(go.Scatter(
            x=x_log, y=selected_row.tolist(),
            mode="lines+markers",
            line=dict(color=line_color, width=2.5),
            marker=dict(size=7, color=line_color),
            name=selected_name,
            hovertemplate="T=%{x:.0f} yr<br>%{y:.3f} m NAP<extra>" + selected_name + "</extra>",
        ))

    fig.update_layout(
        title=dict(text=title, x=0.5),
        template="plotly_white",
        xaxis=dict(
            title="Return period T [years]",
            type="log",
            dtick=1,
        ),
        yaxis_title=y_label,
        legend=dict(orientation="h", y=-0.18, x=0.5, xanchor="center"),
        height=420,
        margin=dict(t=60, b=80),
        hovermode="x unified",
    )
    return fig


def _hex_to_rgba(hex_color: str, alpha: float) -> tuple:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r, g, b, alpha)


def build_return_period_figures(
    only_csv_dir: Path,
    selected_name: str,
    model: str = "gev",
) -> tuple[go.Figure, go.Figure]:
    """Build the drought and wet return-period ensemble plots.

    Returns (fig_drought, fig_wet).
    """
    ensemble = compute_origin_ensemble(only_csv_dir, model=model)
    names = ensemble["series_names"]

    # Try to find the selected series in the plausible ensemble first.
    selected_idx = None
    for i, n in enumerate(names):
        if n == selected_name:
            selected_idx = i
            break

    dr_selected = ensemble["drought"][selected_idx] if selected_idx is not None else None
    wet_selected = ensemble["wet"][selected_idx] if selected_idx is not None else None

    # If the selected series was filtered out, fit it independently
    # (still show it even if its extrapolation is aggressive).
    if selected_idx is None:
        sel_path = only_csv_dir / f"{selected_name}.csv"
        if sel_path.exists():
            extremes = _annual_extremes(sel_path)
            if extremes is not None:
                result = _fit_single(extremes[0], extremes[1], model)
                if result is not None:
                    dr, wet = result
                    if dr is not None:
                        arr = np.asarray(dr, dtype=float)
                        if np.all(np.isfinite(arr)):
                            dr_selected = arr
                    if wet is not None:
                        arr = np.asarray(wet, dtype=float)
                        if np.all(np.isfinite(arr)):
                            wet_selected = arr

    n_series = len(names)
    model_label = MODEL_LABELS.get(model, model)
    subtitle = f"{n_series} series fitted ({model_label})"

    fig_drought = _band_figure(
        ensemble["drought"],
        dr_selected,
        selected_name,
        title=f"Drought return periods — {subtitle}",
        y_label="Head [m NAP]",
        band_color="#D2691E",
        line_color="#D62728",
    )

    fig_wet = _band_figure(
        ensemble["wet"],
        wet_selected,
        selected_name,
        title=f"Wet return periods — {subtitle}",
        y_label="Head [m NAP]",
        band_color="#4682B4",
        line_color="#1F77B4",
    )

    return fig_drought, fig_wet
