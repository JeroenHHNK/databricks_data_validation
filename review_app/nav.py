"""The page objects, defined once so pages can `st.switch_page` to each other."""

from __future__ import annotations

import streamlit as st

from . import map_page, overview_page, review_page

MAP = st.Page(map_page.render, title="Map", icon=":material/map:", url_path="map", default=True)
REVIEW = st.Page(review_page.render, title="Review", icon=":material/fact_check:", url_path="review")
OVERVIEW = st.Page(overview_page.render, title="Overview", icon=":material/table_chart:",
                   url_path="overview")


def navigation():
    return st.navigation([MAP, REVIEW, OVERVIEW])
