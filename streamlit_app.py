"""Groundwater validation dashboard.

Run from the repo root:  streamlit run streamlit_app.py --server.port 8502

Pages: Map (landing: where each series is, with its sensor key), Review (point-by-point
gecontroleerd / afgekeurd), Overview (progress). Reads the source copy
(input_output/output_databricks/) and writes only to the validation store
(input_output/output_repository/).
"""

import streamlit as st

from review_app import nav

st.set_page_config(page_title="Groundwater Validation Dashboard", page_icon=":material/water_drop:",
                   layout="wide")
nav.navigation().run()
