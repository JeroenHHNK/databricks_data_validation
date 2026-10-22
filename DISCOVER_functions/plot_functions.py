import plotly.graph_objects as go
from plotly.subplots import make_subplots

def plot_head_distribution(dataframe, title="Head Distribution: Histogram + Box Plot"):
    """
    Plot a histogram and a horizontal box plot of the 'head' values
    from the inserted dataframe
    """
    # Drop NaNs
    head_data = dataframe["head"].dropna()

    # Create subplot with 2 rows: box plot on top, histogram below
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.2, 0.8],
        shared_xaxes=True,
        vertical_spacing=0.02
    )

    # 1) Horizontal box plot (on top)
    fig.add_trace(
        go.Box(
            x=head_data,
            boxpoints="outliers",
            orientation="h",
            name="Box Plot",
            marker_color="black",
            line=dict(width=1),
            fillcolor="lightgray",
            opacity=0.8
        ),
        row=1, col=1
    )

    # 2) Histogram (on bottom)
    fig.add_trace(
        go.Histogram(
            x=head_data,
            nbinsx=50,
            name="Histogram",
            marker_color="skyblue",
            opacity=0.75
        ),
        row=2, col=1
    )

    # Layout and styling
    fig.update_layout(
        title=dict(
            text=title,
            y=0.95,
            x=0.5,
            xanchor="center",
            yanchor="top"
        ),
        template="plotly_white",
        showlegend=False,
        margin=dict(t=60, r=30, b=60, l=60),
        height=500
    )

    fig.update_xaxes(title_text="Head (m)", row=2, col=1)
    fig.update_yaxes(title_text="Count", row=2, col=1)

    return fig


import plotly.graph_objects as go
import pandas as pd

def plot_groundwater_with_flags(df, 
                                evap_col=None, 
                                prec_col=None):
    """
    Hydrology-standard groundwater plot:
    - Head (cleaned) + head_raw + daily/3-day/7-day means
        - Flags (v1-v4)
        - Precipitation: downward filled area, blue
        - Evapotranspiration: upward filled area, orange

    Returns: Plotly Figure
    """

    df_plot = df.copy()
    df_plot["Time"] = pd.to_datetime(df_plot["Time"])

    fig = go.Figure()

    # -------------------------------------------
    # MAIN HEAD TIMESERIES
    # -------------------------------------------
    fig.add_trace(go.Scatter(
        x=df_plot["Time"], y=df_plot["head"],
        mode="lines", name="Head (cleaned)",
        line=dict(color="black", width=2)
    ))

    if "head_raw" in df_plot.columns:
        fig.add_trace(go.Scatter(
            x=df_plot["Time"], y=df_plot["head_raw"],
            mode="lines", name="Head (original)",
            line=dict(color="gray", width=1)
        ))

    # Trend lines are based on raw head values when available.
    trend_source_col = "head_raw" if "head_raw" in df_plot.columns else "head"
    trend_label_suffix = ", raw" if trend_source_col == "head_raw" else ""
    head_series = df_plot.set_index("Time")[trend_source_col].sort_index()
    avg_1d = head_series.resample("D").mean()
    avg_3d = head_series.resample("3D").mean()
    avg_7d = head_series.resample("7D").mean()
    rolling_3d = head_series.rolling(window="3D", min_periods=1).mean()
    rolling_7d = head_series.rolling(window="7D", min_periods=1).mean()

    fig.add_trace(go.Scatter(
        x=avg_1d.index,
        y=avg_1d.values,
        mode="lines",
        name=f"Head (daily mean{trend_label_suffix})",
        line=dict(color="darkcyan", width=2, dash="solid")
    ))

    fig.add_trace(go.Scatter(
        x=avg_3d.index,
        y=avg_3d.values,
        mode="lines",
        name=f"Head (3-day avg{trend_label_suffix})",
        line=dict(color="teal", width=2, dash="dashdot")
    ))

    fig.add_trace(go.Scatter(
        x=avg_7d.index,
        y=avg_7d.values,
        mode="lines",
        name=f"Head (7-day avg{trend_label_suffix})",
        line=dict(color="navy", width=2, dash="longdashdot")
    ))

    fig.add_trace(go.Scatter(
        x=rolling_3d.index,
        y=rolling_3d.values,
        mode="lines",
        name=f"Head (3-day rolling mean{trend_label_suffix})",
        line=dict(color="seagreen", width=2, dash="dash")
    ))

    fig.add_trace(go.Scatter(
        x=rolling_7d.index,
        y=rolling_7d.values,
        mode="lines",
        name=f"Head (7-day rolling mean{trend_label_suffix})",
        line=dict(color="royalblue", width=2, dash="dot")
    ))

    # -------------------------------------------
    # FLAG MARKERS (v1–v4)
    # -------------------------------------------
    flag_colors = {"v1": "red", "v2": "darkred", "v3": "tomato", "v4": "firebrick"}

    for col, color in flag_colors.items():
        if col in df_plot.columns:
            mask = df_plot[col].notna()
            fig.add_trace(go.Scatter(
                x=df_plot.loc[mask, "Time"],
                y=df_plot.loc[mask, col],  # ✔ fixed
                mode="markers",
                name=f"{col} flag",
                marker=dict(color=color, size=9, symbol="circle-open"),
                yaxis="y1"
            ))
    # -------------------------------------------
    # HYDROLOGY STANDARD: EVAP & PRECIP
    # -------------------------------------------
    # Precip: downward shaded area
    if prec_col and prec_col in df_plot.columns:
        fig.add_trace(go.Scatter(
            x=df_plot["Time"],
            y=-df_plot[prec_col],  # downward
            name="Precipitation",
            fill="tozeroy",
            line=dict(color="steelblue", width=0),
            fillcolor="rgba(0, 100, 200, 0.5)",
            yaxis="y2"
        ))

    # Evap: upward shaded area
    if evap_col and evap_col in df_plot.columns:
        fig.add_trace(go.Scatter(
            x=df_plot["Time"],
            y=df_plot[evap_col],
            name="Evapotranspiration",
            fill="tozeroy",
            line=dict(color="darkorange", width=0),
            fillcolor="rgba(255, 140, 0, 0.5)",
            yaxis="y2"
        ))

    # -------------------------------------------
    # LAYOUT
    # -------------------------------------------
    fig.update_layout(
        title="Groundwater Head + Hydro-Climatic Inputs",
        xaxis_title="Time",
        yaxis=dict(title="Head [m]", side="left"),
        yaxis2=dict(
            title="Evap (+) / Precip (–) [mm]",
            overlaying="y",
            side="right",
            showgrid=False
        ),
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", y=1.05)
    )

    return fig
