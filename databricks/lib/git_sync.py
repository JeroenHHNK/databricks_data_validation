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
   companion note in databricks/README.md.

Nothing here ever writes to `gold_prd`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def probe_push(repo_root: str, branch: str = "main") -> ProbeResult:
    """Prove a round-trip push works, cheaply. Commits a throwaway marker under
    `.probe/` and pushes it, then leaves it (harmless, gitignorable). Returns the
    transport to use. A failure costs seconds, not a whole run.
    """
    if not is_git_worktree(repo_root):
        return ProbeResult(False, "volume",
                           "Not a git worktree; add the repo as a Git folder or use the Volume bridge.")
    marker = Path(repo_root) / ".probe" / "push_probe.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("push probe\n", encoding="utf-8")
    for cmd in (["git", "add", str(marker)],
                ["git", "commit", "-m", "probe: verify control-plane push"],
                ["git", "push", "origin", branch]):
        r = _run(cmd, repo_root)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return ProbeResult(False, "volume",
                               f"`{' '.join(cmd)}` failed: {(r.stderr or r.stdout).strip()[:400]}")
    return ProbeResult(True, "git", "Control-plane push works.")


def commit_and_push(repo_root: str, paths: list[str], message: str,
                    branch: str = "main") -> None:
    """Stage `paths`, commit, and push from the Git folder working tree.

    Raises with a clear message if the push leg fails, pointing at the Volume
    fallback. Staging only the given paths keeps the commit scoped to this run's
    exports and never sweeps in unrelated working-tree changes.
    """
    if not is_git_worktree(repo_root):
        raise RuntimeError(
            f"{repo_root} is not a git worktree. Use write_tree_to_volume() + the "
            "local CLI bridge instead (see databricks/README.md)."
        )
    add = _run(["git", "add", *paths], repo_root)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()}")
    commit = _run(["git", "commit", "-m", message], repo_root)
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        raise RuntimeError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
    push = _run(["git", "push", "origin", branch], repo_root)
    if push.returncode != 0:
        raise RuntimeError(
            "git push failed — the control-plane push leg is not available from this "
            f"job. Fall back to the Volume bridge. Detail: {push.stderr.strip()[:400]}"
        )


def write_tree_to_volume(local_root: str, rel_paths: list[str],
                         volume_base: str = VOLUME_BASE,
                         subdir: str = "git_exchange") -> str:
    """Fallback transport: copy generated files to the `misc_files` Volume,
    preserving their relative layout. A local machine then runs the companion
    bridge (databricks/README.md) to commit & push them.

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
