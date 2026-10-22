# Databricks side

The notebooks and helpers that read the read-only source, replicate it per well, and move it
in and out of git. Validation does **not** run here — it runs locally on the export
(`../local_validation/validate.ipynb`). Read
[`../docs/DATABRICKS_PERMISSIONS.md`](../docs/DATABRICKS_PERMISSIONS.md) first — the grants
are asymmetric (read `gold_prd`, write only `waterkeringbeheer_prd`).

## Layout

```
notebooks/
  00_probe_env.py          verify grants + git push + Volume write BEFORE a real run
  01_source_to_personal.py read source -> clean to hourly -> per-well Delta copy;
                           all meetreeks columns + hl_cs_objecten location match
  02_export_to_git.py      Delta copy -> input_output/output_databricks/ CSVs -> write (or commit & push / Volume)
  03_validated_to_delta.py input_output/output_repository/ CSVs -> validated_meting Delta table
lib/
  source_read.py           SQL builders (incl. the cs_objecten match) + cleaning + naming bridge
  git_sync.py              push probe, commit/push, and the Volume fallback
jobs/workflow_config.json  job: 00 -> 01 -> 02
```

`lib/` is imported flat (`sys.path.insert(0, ".../databricks_code/lib"); import source_read`).

## Tables written (all in `waterkeringbeheer_prd.jvanruitenbeek`)

| Table | By | Grain |
|---|---|---|
| `source_copy_meetreeks` | `01` | one row per series: all `meetreeks` columns + `matched`, `match_key`, `cs_*`, coordinates |
| `source_copy_meting` | `01` | hourly `meetreeks_id, vendor, timestamp, head` |
| `validated_meting` | `03` | approved hours after human review |

### The `hl_cs_objecten` match

`01` tries four keys from `meetreeks` to `hl_cs_objecten.meetlocatie`, in order, and the
first that hits wins (LEFT join: unmatched series stay in):

1. `l.id = r.meetlocatie_object_id`
2. `l.code = r.meetlocatie_code`
3. `l.code = trim(r.code)` (`meetreeks.code` carries trailing spaces)
4. `l.id = r.object_id` (a guess — points at the groundwater schema's own `object` table)

Its diagnostics cell prints `SHOW GRANTS`, how often the link columns are filled, the match
rate of each key on its own, and whether `meetlocatie.id = sha256(code)`. The dimension then
says per series `matched` = `matched` | `no match found in cs_objecten`, plus `match_key` and
`cs_n_candidates` (> 1 = that key found several locations; the lowest id was taken). If
`hl_cs_objecten` is not readable, every series is `no match found in cs_objecten` and the run
continues.

## One-time setup

1. **Add the repo as a Git folder.** Workspace -> Create -> Git folder, point it at the
   GitHub repo. This lands under `/Workspace/Users/<you>/…` or `/Workspace/Repos/<you>/…`;
   the notebooks walk up for `databricks_code/lib` and don't care which.
2. **Configure Git credentials** (Settings -> Linked accounts / Git integration) with a PAT
   that can push. Git-folder operations route through the Databricks control plane, so they
   are **not** subject to the cluster egress block that stops KNMI downloads.
3. **Run `00_probe_env`** (optional). It checks grants + whether a push would work. `02`
   defaults to `transport=write` (write files, **push manually** from the Git-folder UI);
   set `transport=git` only if you want `02` to auto-commit & push via the `git_token_secret` PAT.

## The Volume bridge (fallback, when git push is unavailable from the job)

If the push probe fails, `02_export_to_git` copies the generated tree to
`/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline/git_exchange/`.
Then, on a machine that has both the Databricks CLI and a clone of this repo:

```bash
# download the exported tree from the Volume into the local working copy
databricks fs cp -r \
  dbfs:/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline/git_exchange/input_output/output_databricks \
  ./input_output/output_databricks

git add input_output/output_databricks && git commit -m "source replication (via Volume bridge)" && git push
```

This mirrors the pattern the KNMI forcing already uses: a laptop bridges git because the
cluster cannot. It needs the `WRITE VOLUME` grant, which is **separate and unconfirmed** —
`00_probe_env` checks it.

## Run order

| Step | Notebook | Trigger |
|---|---|---|
| ① replicate | `00` -> `01` -> `02` (the job) | on source update, or manually |
| ② validate + review | local `validate.ipynb`, then Streamlit, then `git push` | human |
| ③ load back | `03_validated_to_delta` | after the review is pushed & pulled |

Start with `limit_series=1` on `01` for a first run, confirm the round trip, then set it to
`0` (all series). Running the notebooks by hand works too — run them in number order.
