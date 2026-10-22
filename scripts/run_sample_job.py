r"""Create (or update) the Databricks replication job and run a small sample.

Drives the proposed architecture end to end, from a laptop, through a real
Databricks **job** whose tasks run this repo's notebooks on an existing cluster:

    probe_env  ->  source_to_personal  ->  validate  ->  export_to_git

`source_to_personal` copies the current source data into the personal schema as
Delta; `validate` runs the automated flags on it; `export_to_git` writes it under `input_output/output_databricks/time_series_data/<series>.csv`
into the repo's Git-folder checkout and pushes it. Scoped to `--limit-series`
(default 3) so the first run is cheap.

Prerequisites (one-time):
  * the repo is on GitHub and added to Databricks as a **Git folder** (its workspace
    path is `--repo-path`, e.g. /Workspace/Users/<you>/databricks_data_validation);
  * a PAT for push stored as a Databricks **secret** (`--git-token-secret scope/key`);
  * an existing all-purpose cluster (`--cluster-id`);
  * ~/.databrickscfg with host + token (or `--profile`).

Example (PowerShell):
    .\.venv\Scripts\python scripts\run_sample_job.py `
        --cluster-id 0101-123456-abcde123 `
        --repo-path /Workspace/Users/j.vanruitenbeek@hhnk.nl/databricks_data_validation `
        --git-token-secret gh/pat --limit-series 3

Reads only; writes only to the personal schema and the repo. Never writes gold_prd.
"""

from __future__ import annotations

import argparse
import sys
import time

JOB_NAME_DEFAULT = "groundwater_source_replication_sample"


def build_tasks(jobs, repo_path: str, cluster_id: str, *, branch: str, limit: int,
                token_secret: str, out_catalog: str, out_schema: str):
    """Four notebook tasks wired with task-value handoffs (transport, run_id)."""
    nb = lambda name, params: jobs.Task(  # noqa: E731
        task_key=name[0],
        existing_cluster_id=cluster_id,
        depends_on=[jobs.TaskDependency(task_key=d) for d in name[1]],
        notebook_task=jobs.NotebookTask(
            notebook_path=f"{repo_path}/databricks_code/notebooks/{name[2]}",
            source=jobs.Source.WORKSPACE,
            base_parameters={**{"repo_root": repo_path,
                                "output_catalog": out_catalog,
                                "output_schema": out_schema}, **params}),
    )
    common_git = {"branch": branch, "git_token_secret": token_secret}
    return [
        nb(("probe_env", [], "00_probe_env"), common_git),
        nb(("source_to_personal", ["probe_env"], "01_source_to_personal"),
           {"limit_series": str(limit), "run_id": ""}),
        nb(("validate", ["source_to_personal"], "02_validate"),
           {"run_id": "{{tasks.source_to_personal.values.run_id}}"}),
        nb(("export_to_git", ["validate"], "03_export_to_git"),
           {**common_git,
            "transport": "{{tasks.probe_env.values.transport}}",
            "run_id": "{{tasks.validate.values.run_id}}"}),
    ]


def upsert_job(w, jobs, name: str, tasks) -> int:
    existing = next((j for j in w.jobs.list(name=name) if j.settings.name == name), None)
    settings = jobs.JobSettings(name=name, tasks=tasks, max_concurrent_runs=1)
    if existing:
        w.jobs.reset(job_id=existing.job_id, new_settings=settings)
        print(f"updated job {name} (id {existing.job_id})")
        return existing.job_id
    created = w.jobs.create(name=name, tasks=tasks, max_concurrent_runs=1)
    print(f"created job {name} (id {created.job_id})")
    return created.job_id


def poll(w, run_id: int, timeout_s: int = 1800):
    from databricks.sdk.service.jobs import RunLifeCycleState

    deadline = time.time() + timeout_s
    while True:
        run = w.jobs.get_run(run_id=run_id)
        state = run.state.life_cycle_state
        print(f"  run {run_id}: {state}")
        if state in (RunLifeCycleState.TERMINATED, RunLifeCycleState.INTERNAL_ERROR,
                     RunLifeCycleState.SKIPPED):
            return run
        if time.time() > deadline:
            raise TimeoutError(f"run {run_id} did not finish within {timeout_s}s")
        time.sleep(15)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cluster-id", required=True, help="existing all-purpose cluster id")
    ap.add_argument("--repo-path", required=True, help="Git-folder workspace path")
    ap.add_argument("--git-token-secret", default="", help="PAT secret as scope/key")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--limit-series", type=int, default=3)
    ap.add_argument("--output-catalog", default="waterkeringbeheer_prd")
    ap.add_argument("--output-schema", default="jvanruitenbeek")
    ap.add_argument("--job-name", default=JOB_NAME_DEFAULT)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--no-run", action="store_true", help="create/update the job but don't run it")
    args = ap.parse_args()

    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service import jobs
    except ImportError:
        print("ERROR: `.\\.venv\\Scripts\\pip install databricks-sdk`", file=sys.stderr)
        return 2
    try:
        w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not authenticate: {exc}", file=sys.stderr)
        return 2

    tasks = build_tasks(jobs, args.repo_path, args.cluster_id, branch=args.branch,
                        limit=args.limit_series, token_secret=args.git_token_secret,
                        out_catalog=args.output_catalog, out_schema=args.output_schema)
    job_id = upsert_job(w, jobs, args.job_name, tasks)
    if args.no_run:
        print("created/updated only (--no-run). Trigger it from the Jobs UI when ready.")
        return 0

    print(f"running job {job_id} with limit_series={args.limit_series} …")
    run = w.jobs.run_now(job_id=job_id)
    final = poll(w, run.run_id)

    from databricks.sdk.service.jobs import RunResultState

    print("\n=== task results ===")
    ok = True
    for t in final.tasks or []:
        res = t.state.result_state
        ok = ok and res == RunResultState.SUCCESS
        print(f"  {t.task_key}: {t.state.life_cycle_state} / {res}")
        try:
            out = w.jobs.get_run_output(run_id=t.run_id)
            if out.notebook_output and out.notebook_output.result:
                print(f"      exit -> {out.notebook_output.result}")
            if out.error:
                print(f"      error -> {out.error}")
        except Exception:  # noqa: BLE001
            pass
    print("\nDONE." if ok else "\nFINISHED WITH FAILURES — see task errors above.")
    if ok:
        print("Now locally: `git pull` to land the sample under input_output/output_databricks/.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
