# input_output/output_repository/

Local validation output — the reviewed copies produced **in the IDE / Streamlit**, which
`databricks_code/notebooks/03_validated_to_delta.py` reads back into the
`waterkeringbeheer_prd.jvanruitenbeek.validated_meting` Delta table.

Flow:

```
input_output/output_databricks/   (pulled from Databricks: raw copy)
      │  local_validation/validate.ipynb (automated flags) + manual review (always overrides them)
      ▼
input_output/output_repository/   (reviewed copies, git-tracked) ──push──► Databricks ──03──► validated_meting
```

The data under `input_output/output_databricks/` is **never modified** — review writes its
results here. `03` accepts either a flat `<filename>.csv` (the `filename` from
`output_databricks/metadata/object_data.csv`) or the on-prem `<vendor>/<origin>/<series>.csv`
nesting. The frozen review-store schema is `timestamp, raw_data, gecontroleerd, afgekeurd, note`.
