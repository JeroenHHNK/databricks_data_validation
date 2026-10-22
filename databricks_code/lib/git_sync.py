"""Move generated files out of Databricks into git — with a probe, because the
cluster has no outbound internet and whether a push succeeds is unverified.

Two transports, chosen at runtime by the probe:

1. **Git-folder push** (preferred). The repo is added as a Databricks Git folder
   (Workspace -> Create -> Git folder). Git operations route through the Databricks
   **control plane**, not the cluster data-plane that the egress block sits on, so
   `git push` *can* work even though the cluster cannot reach KNMI. On recent
   runtimes the Git folder is a real checkout, so the `git` CLI on the driver
   drives it. This is unofficial for automation — the Workspace UI is the fully
   supported path — so `probe_push()` settles it before a run depends on it.

2. **Volume bridge** (fallback). When the probe fails, Databricks writes the files
   to the `misc_files` Volume and a local machine with the Databricks CLI/SDK
   pulls them down and commits/pushes. See `write_tree_to_volume()` and the
   companion note in databricks_code/README.md.

Nothing here ever writes to `gold_prd`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

VOLUME_BASE = "/Volumes/waterkeringbeheer_prd/misc_files/misc_files/pastas_pipeline"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    transport: str  # "git" | "volume"
    detail: str


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def is_git_worktree(repo_root: str) -> bool:
    r = _run(["git", "rev-parse", "--is-inside-work-tree"], repo_root)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _origin_url(repo_root: str) -> str:
    return _run(["git", "remote", "get-url", "origin"], repo_root).stdout.strip()


def _authenticated_url(origin_url: str, token: str) -> str:
    """Inject a PAT into an https origin so the driver can push without relying on
    the workspace Git-credential plumbing. SSH or blank origins are returned as-is.
    The token is never written to git config — it only ever lives in the argv of a
    single `git push <url>` call.
    """
    if token and origin_url.startswith("https://"):
        host_and_path = origin_url[len("https://"):].split("@")[-1]
        return f"https://x-access-token:{token}@{host_and_path}"
    return origin_url


def _redact(text: str, token: str | None) -> str:
    out = (text or "").strip()[:400]
    if token:
        out = out.replace(token, "***").replace("x-access-token:***@", "")
    return out


def _push(repo_root: str, branch: str, token: str | None) -> subprocess.CompletedProcess:
    if token:
        url = _authenticated_url(_origin_url(repo_root), token)
        return _run(["git", "push", url, f"HEAD:{branch}"], repo_root)
    return _run(["git", "push", "origin", branch], repo_root)


def probe_push(repo_root: str, branch: str = "main", token: str | None = None) -> ProbeResult:
    """Prove a push works, cheaply and without polluting the repo. A no-op
    `git push HEAD:<branch>` returns "up to date" when there is nothing to send,
    so it verifies the remote is reachable and the credentials work without
    creating a commit. A failure costs seconds, not a whole run.
    """
    if not is_git_worktree(repo_root):
        return ProbeResult(False, "volume",
                           "Not a git worktree; add the repo as a Git folder or use the Volume bridge.")
    push = _push(repo_root, branch, token)
    if push.returncode != 0:
        return ProbeResult(False, "volume", f"git push: {_redact(push.stderr or push.stdout, token)}")
    return ProbeResult(True, "git", "Push from job works (remote reachable, auth OK).")


def commit_and_push(repo_root: str, paths: list[str], message: str,
                    branch: str = "main", token: str | None = None) -> None:
    """Stage `paths`, commit, and push from the Git folder working tree.

    When `token` is given, pushes to a PAT-authenticated remote URL (robust from a
    Databricks driver); otherwise relies on `origin`'s configured credentials.
    Raises with a redacted message if the push leg fails, pointing at the Volume
    fallback. Staging only the given paths keeps the commit scoped to this run.
    """
    if not is_git_worktree(repo_root):
        raise RuntimeError(
            f"{repo_root} is not a git worktree. Use write_tree_to_volume() + the "
            "local CLI bridge instead (see databricks_code/README.md)."
        )
    add = _run(["git", "add", *paths], repo_root)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()}")
    commit = _run(["git", "commit", "-m", message], repo_root)
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        raise RuntimeError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
    push = _push(repo_root, branch, token)
    if push.returncode != 0:
        raise RuntimeError(
            "git push failed — the push leg is not available from this job. Fall back to "
            f"the Volume bridge. Detail: {_redact(push.stderr or push.stdout, token)}"
        )


def write_tree_to_volume(local_root: str, rel_paths: list[str],
                         volume_base: str = VOLUME_BASE,
                         subdir: str = "git_exchange") -> str:
    """Fallback transport: copy generated files to the `misc_files` Volume,
    preserving their relative layout. A local machine then runs the companion
    bridge (databricks_code/README.md) to commit & push them.

    Requires `WRITE VOLUME`, which is a **separate, unconfirmed grant**
    (DATABRICKS_PERMISSIONS.md §4). Probe it with `probe_volume_write()` first.
    """
    dest_root = os.path.join(volume_base, subdir)
    for rel in rel_paths:
        src = os.path.join(local_root, rel)
        dst = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    return dest_root


def probe_volume_write(volume_base: str = VOLUME_BASE) -> ProbeResult:
    """Write-and-delete a marker on the Volume base. Same probe task 2 of the
    Tarso pipeline runs before it fits anything.
    """
    probe = os.path.join(volume_base, "__write_test.txt")
    try:
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return ProbeResult(True, "volume", f"WRITE VOLUME confirmed at {volume_base}.")
    except Exception as exc:  # noqa: BLE001 - report the grant failure as data
        return ProbeResult(False, "volume", f"WRITE VOLUME denied: {exc}")
