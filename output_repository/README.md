# output_repository/

Local validation output — the reviewed copies produced **in the IDE / Streamlit**, which
`databricks/notebooks/03_validated_to_delta.py` reads back into the
`waterkeringbeheer_prd.jvanruitenbeek.validated_meting` Delta table.

Flow:

```
output_databricks/   (pulled from Databricks: raw source replication)
      │  validate locally (automated flags + manual review)
      ▼
output_repository/    (reviewed copies, git-tracked) ──push──► Databricks ──03──► validated_meting
```

The source under `output_databricks/` is **never modified** — validation writes its results
here. The exact layout (per-series files, metadata, store schema) is finalized when the
local validation toolkit is wired in; the frozen review-store schema is
`timestamp, raw_data, gecontroleerd, afgekeurd, note`.
