"""Local validation of the Databricks export, kept in three separate layers.

`data_io` (layers, paths, the write guard), `checks` (v1-v4, rules A/B), `stores` (open,
merge, sync and save the validation store), `validation_store` (the frozen store contract).
Run the checks through `validate.ipynb`; review in `streamlit_app.py`.
"""
