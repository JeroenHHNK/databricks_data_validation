# Databricks side

The notebooks and helpers that read the read-only source, replicate it per well, and move
it in and out of git. Read [`../DATABRICKS_PERMISSIONS.md`](../DATABRICKS_PERMISSIONS.md)
first — the grants are asymmetric (read `gold_prd`, write only `waterkeringbeheer_prd`).

## Layout

```
notebooks/
  00_probe_env.py          verify grants + git push + Volume write BEFORE a real run
  01_source_to_personal.py read source -> clean to hourly -> per-well Delta copy
  02_export_to_git.py      Delta copy -> source_data/ CSVs -> commit & push (or Volume)
  03_validated_to_delta.py validation_data/ CSVs -> validated_meting Delta table
lib/
  source_read.py           SQL builders + cleaning + the vendor/origin/series naming bridge
  git_sync.py              push probe, commit/push, and the Volume fallback
jobs/workflow_config.json  job: 00 -> 01 -> 02
```

`lib/` is imported flat (`sys.path.insert(0, ".../databricks/lib"); import source_read`),
**not** as a `databricks.*` package — that name belongs to `databricks-sdk`.

## One-time setup

1. **Add the repo as a Git folder.** Workspace -> Create -> Git folder, point it at the
   GitHub repo. This lands under `/Workspace/Users/<you>/…` or `/Workspace/Repos/<you>/…`;
   the notebooks walk up for `pyproject.toml` and don't care which.
2. **Configure Git credentials** (Settings -> Linked accounts / Git integration) with a PAT
   that can push. Git-folder operations route through the Databricks control plane, so they
   are **not** subject to the cluster egress block that stops KNMI downloads.
3. **Run `00_probe_env`.** It prints `USE TRANSPORT: git` or `volume`. Pass that to
   `02_export_to_git`'s `transport` widget (the job wires it automatically via task values).

## The Volume bridge (fallback, when git push is unavailable from the job)

If the push probe fails, `02_export_to_git` copies the generated tree to
`/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline/git_exchange/`.
Then, on a machine that has both the Databricks CLI and a clone of this repo:

```bash
# download the exported tree from the Volume into the local working copy
databricks fs cp -r \
  dbfs:/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline/git_exchange/source_data \
  ./source_data

git add source_data && git commit -m "source replication (via Volume bridge)" && git push
```

This mirrors the pattern the KNMI forcing already uses: a laptop bridges git because the
cluster cannot. It needs the `WRITE VOLUME` grant, which is **separate and unconfirmed** —
`00_probe_env` checks it.

## Run order

| Step | Notebook | Trigger |
|---|---|---|
| ① replicate | `00` -> `01` -> `02` (the job) | on source update, or manually |
| ② validate | local `dataval validate` + Streamlit, then `git push` | human |
| ③ load back | `03_validated_to_delta` | after validation is pushed & pulled |

Start with `limit_series=1` on `01` for a first run, confirm the round trip, then set it to
`0` (all series).
