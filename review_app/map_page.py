"""Landing page: where the measurement series are, with their sensor key.

One marker per physical location (rd_x/rd_y): a well with several filters is one point,
and its hover lists every series there. Positions come from rd_x/rd_y converted to
WGS84 (`geo.py`), because web basemaps are in Web Mercator. RD is shown in the hover.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from . import common

BASEMAPS = {
    "PDOK BRT achtergrondkaart": "https://service.pdok.nl/brt/achtergrondkaart/wmts/v2_0/"
                                 "standaard/EPSG:3857/{z}/{x}/{y}.png",
    "PDOK luchtfoto": "https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0/"
                      "Actueel_orthoHR/EPSG:3857/{z}/{x}/{y}.jpeg",
    "OpenStreetMap": None,
}
VENDOR_COLOURS = {"fugro": "#1F77B4", "wiertsema": "#FF7F0E"}
PROGRESS_COLOURS = {"not started": "#999999", "in progress": "#FFB000", "complete": "#2CA02C"}


def _progress_by_file() -> pd.Series:
    ov, _ = common.overview()
    if ov.empty:
        return pd.Series(dtype=str)
    state = pd.Series("in progress", index=ov["filename"])
    state[(~ov["has_store"]).to_numpy() | (ov["reviewed"] == 0).to_numpy()] = "not started"
    state[(ov["pct_reviewed"] >= 100).to_numpy()] = "complete"
    return state


def _locations(meta: pd.DataFrame, colour_by: str) -> pd.DataFrame:
    """One row per (rd_x, rd_y) with the series that share it."""
    located = meta[meta["has_location"]].copy()
    located["meetlocatie_code"] = located["meetlocatie_code"].fillna("–")
    located["sensor_sleutel"] = located["sensor_sleutel"].fillna("–")
    if colour_by == "Review progress":
        located["group"] = located["filename"].map(_progress_by_file()).fillna("not started")
    else:
        located["group"] = located["vendor"]
    located["loc_key"] = located["rd_x"].round(2).astype(str) + "," + located["rd_y"].round(2).astype(str)

    def _hover(g: pd.DataFrame) -> str:
        first = g.iloc[0]
        lines = [f"<b>{first['cs_naam'] if pd.notna(first['cs_naam']) else first['code']}</b>",
                 f"RD {first['rd_x']:.0f}, {first['rd_y']:.0f}", ""]
        lines += [f"{r.filename} · {r.meetlocatie_code} · {r.sensor_sleutel}" for r in g.itertuples()]
        return "<br>".join(lines)

    rows = []
    for key, g in located.groupby("loc_key", sort=False):
        # A location takes the "least done" group so unfinished work stands out.
        order = list(PROGRESS_COLOURS) if colour_by == "Review progress" else sorted(g["group"].unique())
        group = next(o for o in order if o in set(g["group"]))
        rows.append({"loc_key": key, "map_lon": g["map_lon"].iloc[0], "map_lat": g["map_lat"].iloc[0],
                     "n_series": len(g), "group": group, "hover": _hover(g)})
    return pd.DataFrame(rows)


def _figure(locs: pd.DataFrame, colour_by: str, basemap: str) -> go.Figure:
    colours = PROGRESS_COLOURS if colour_by == "Review progress" else VENDOR_COLOURS
    fig = go.Figure()
    for group, sub in locs.groupby("group", sort=False):
        fig.add_trace(go.Scattermap(
            lon=sub["map_lon"], lat=sub["map_lat"], mode="markers", name=str(group),
            marker=dict(size=(7 + 2.5 * sub["n_series"].clip(upper=6)).tolist(),
                        color=colours.get(group, "#7F7F7F"), opacity=0.85),
            customdata=sub[["loc_key"]].to_numpy(), text=sub["hover"],
            hovertemplate="%{text}<extra></extra>"))
    tiles = BASEMAPS[basemap]
    map_layout = dict(center=dict(lon=float(locs["map_lon"].mean()), lat=float(locs["map_lat"].mean())),
                      zoom=8.6)
    if tiles:
        map_layout.update(style="white-bg", layers=[dict(
            below="traces", sourcetype="raster", source=[tiles],
            sourceattribution="© PDOK / Kadaster")])
    else:
        map_layout["style"] = "open-street-map"
    fig.update_layout(map=map_layout, height=620, margin=dict(l=0, r=0, t=0, b=0),
                      legend=dict(orientation="h", y=1.0, x=0.0, bgcolor="rgba(255,255,255,0.8)"),
                      uirevision="map")
    return fig


def _selected_keys(event) -> list[str]:
    try:
        pts = event.selection.points
    except AttributeError:
        return []
    keys = []
    for p in pts:
        cd = p.get("customdata")
        if cd:
            keys.append(cd[0] if isinstance(cd, (list, tuple)) else cd)
    return keys


def render() -> None:
    meta = common.object_data()
    st.title("Measurement locations")

    n_loc = int(meta["has_location"].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Series", len(meta))
    c2.metric("With location", n_loc)
    c3.metric("Without location", len(meta) - n_loc)
    c4.metric("Distinct locations", meta.loc[meta["has_location"], ["rd_x", "rd_y"]].drop_duplicates().shape[0])
    if n_loc == 0:
        st.info("No series in object_data.csv carry rd_x/rd_y or lon/lat.")
        return

    o1, o2, o3 = st.columns([1, 1, 2])
    colour_by = o1.radio("Colour by", ["Vendor", "Review progress"], horizontal=True)
    basemap = o2.selectbox("Basemap", list(BASEMAPS))
    o3.caption("Click a marker, or drag a box / lasso, to list its series below. Positions from "
               "rd_x/rd_y (RD New) converted to WGS84; markers grow with the number of filters "
               "at a location.")

    locs = _locations(meta, colour_by)
    event = st.plotly_chart(_figure(locs, colour_by, basemap), width="stretch", key="location_map",
                            on_select="rerun", selection_mode=("points", "box", "lasso"),
                            config={"scrollZoom": True})

    keys = _selected_keys(event)
    located = meta[meta["has_location"]].copy()
    located["loc_key"] = located["rd_x"].round(2).astype(str) + "," + located["rd_y"].round(2).astype(str)
    table = located[located["loc_key"].isin(keys)] if keys else located
    st.subheader(f"Series at the selected location{'s' if len(set(keys)) != 1 else ''}"
                 if keys else "All located series")

    cols = ["filename", "meetlocatie_code", "sensor_sleutel", "code", "vendor", "cs_naam", "compartiment_naam",
            "rd_x", "rd_y", "eind_filter", "bovenkant_peilbuis", "n_rows"]
    cols = [c for c in cols if c in table]
    store_set = common.store_names()
    shown = table[cols].assign(store=table["filename"].isin(store_set).map({True: "✓", False: ""}))
    pick = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun",
                        selection_mode="single-row", key="map_table",
                        column_config={"rd_x": st.column_config.NumberColumn(format="%.0f"),
                                       "rd_y": st.column_config.NumberColumn(format="%.0f")})
    rows = pick.selection.rows if pick and pick.selection else []
    if rows:
        filename = shown.iloc[rows[0]]["filename"]
        if st.button(f"Open {filename} in review →", type="primary"):
            common.open_in_review(filename)
    else:
        st.caption("Select a row to open that series in the review page.")

    with st.expander(f"Series without location metadata ({len(meta) - n_loc})"):
        missing = meta[~meta["has_location"]]
        st.dataframe(missing[[c for c in ("filename", "meetlocatie_code", "sensor_sleutel", "code", "vendor", "matched")
                              if c in missing]], hide_index=True, width="stretch")
