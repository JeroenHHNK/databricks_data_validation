"""Cached loaders shared by the pages. Everything here only reads."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from local_validation import data_io, stores

from .geo import rd_to_wgs84


@st.cache_data
def settings() -> dict:
    return data_io.load_settings()


@st.cache_data(ttl=120, show_spinner=False)
def object_data() -> pd.DataFrame:
    """object_data.csv + `map_lon`/`map_lat` computed from rd_x/rd_y (falling back to
    the stored lon/lat), and `has_location`."""
    meta = data_io.load_object_data(settings())
    for col in ("rd_x", "rd_y", "lon", "lat", "meetlocatie_code", "sensor_sleutel", "cs_naam", "matched", "code"):
        if col not in meta:
            meta[col] = None
    lon, lat = rd_to_wgs84(pd.to_numeric(meta["rd_x"], errors="coerce"),
                           pd.to_numeric(meta["rd_y"], errors="coerce"))
    meta["map_lon"] = pd.Series(lon, index=meta.index).fillna(pd.to_numeric(meta["lon"], errors="coerce"))
    meta["map_lat"] = pd.Series(lat, index=meta.index).fillna(pd.to_numeric(meta["lat"], errors="coerce"))
    meta["has_location"] = meta["map_lon"].notna() & meta["map_lat"].notna()
    return meta


def meta_row(filename: str):
    """The object_data row for one series, as a namedtuple (what `stores` expects)."""
    meta = object_data()
    hit = meta[meta["filename"] == filename]
    return next(hit.itertuples(index=False)) if len(hit) else None


def store_names() -> set[str]:
    """Stems of the saved stores. Not cached: it changes on every save."""
    d = data_io.store_dir(settings())
    return {p.stem for p in d.glob("*.csv")} if d.is_dir() else set()


@st.cache_data(ttl=120, show_spinner="Collecting review progress…")
def overview() -> tuple[pd.DataFrame, int]:
    return stores.overview(settings())


@st.cache_data(show_spinner=False)
def summary() -> pd.DataFrame | None:
    return data_io.load_summary(settings())


@st.cache_data
def stressor_files() -> list[str]:
    """The KNMI notebook's `forcing_file` first (the default overlay), then forcing_dir."""
    main = data_io._path(settings(), "forcing_file")
    d = data_io._path(settings(), "forcing_dir")
    extra = sorted(str(p) for p in d.glob("*.csv")) if d.is_dir() else []
    return ([str(main)] if main.is_file() else []) + [p for p in extra if p != str(main)]


@st.cache_data
def load_stressor_csv(path: str) -> dict:
    """KNMI hourly CSV with `precipitation_mm` + `makkink_mm` (on-prem format)."""
    df = pd.read_csv(path, index_col=0, parse_dates=True, encoding="utf-8-sig",
                     encoding_errors="replace")
    missing = {"precipitation_mm", "makkink_mm"} - set(df.columns)
    if missing:
        return {"error": f"{Path(path).name}: missing columns {', '.join(sorted(missing))}"}
    return {"precipitation": pd.to_numeric(df["precipitation_mm"], errors="coerce").dropna(),
            "evaporation": pd.to_numeric(df["makkink_mm"], errors="coerce").dropna()}


def open_in_review(filename: str) -> None:
    """Jump to the review page with `filename` selected."""
    from . import nav
    st.session_state["review_target"] = filename
    st.switch_page(nav.REVIEW)


def fmt(v, unit: str = "") -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "–"
    return "–" if f != f else f"{f:.2f}{unit}"
