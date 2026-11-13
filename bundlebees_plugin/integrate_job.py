"""Wire the reconciler to `integrate_wave`, preserving verified commit identity.

The verified commits live on local `bb-verify/<attempt>` branches whose parent is
the pinned base. `integrate_wave` requires every verified sha to be an ancestor of
HEAD in the workdir it is given, so this module builds one integration worktree at
that base and merges each verified commit into it — no rebase, no cherry-pick, no
SHA rewriting. The sibling commits stay exactly as verified, and become ancestors
of the integrated HEAD.

Only then is `integrate_wave` called, with the same ancestry and suite checks it
has always enforced. Nothing here weakens it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import revision, verify_local


class IntegrateError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def _git(args: Sequence[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=bundlebees", "-c", "user.email=bees@localhost", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(Path.home())},
    )
    if check and proc.returncode != 0:
        raise IntegrateError("git_error", f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}", "Inspect the integration worktree.")
    return proc


def verified_attempts(job: Mapping[str, Any]) -> list[str]:
    """Attempt ids for tasks whose verification is recorded and ok."""

    attempts: list[str] = []
    for task in job.get("tasks", []):
        if task.get("state") not in {"verified", "integrating"}:
            continue
        attempt_id = (task.get("attempts") or [None])[-1]
        if attempt_id:
            attempts.append(str(attempt_id))
    return attempts


def integrate(
    state_dir: Path,
    job: Mapping[str, Any],
    repo: Path,
    *,
    suite_command: Sequence[str] = ("python", "-m", "pytest", "-q"),
    worktree_root: Path | None = None,
    wave_id: str | None = None,
) -> dict[str, Any]:
    """Build one integrated history from verified commits, then call integrate_wave."""

    state_dir = Path(state_dir)
    repo = Path(repo)
    attempts = verified_attempts(job)
    if not attempts:
        raise IntegrateError("nothing_verified", "no verified tasks to integrate", "Verify at least one attempt first.")

    base_rev = str((job.get("goal") or {}).get("base_rev") or "")
    if len(base_rev) != 40:
        raise IntegrateError("bad_base_rev", "the job has no pinned base revision", "Fix the job document.")

    shas: list[tuple[str, str]] = []
    for attempt_id in attempts:
        record = verify_local.read_record(state_dir, attempt_id)
        if not record or record.get("ok") is not True:
            raise IntegrateError("not_verified", f"{attempt_id} has no accepted verify record", "Verify it first.")
        sha = str(record.get("sha") or "")
        if len(sha) != 40:
            raise IntegrateError("bad_verify_record", f"{attempt_id} has no commit sha", "Re-verify it.")
        shas.append((attempt_id, sha))

    root = Path(worktree_root) if worktree_root else repo.parent / ".bb-integrate"
    worktree = root / "wt"
    if worktree.exists():
        _git(["worktree", "remove", "--force", str(worktree)], repo, check=False)
    root.mkdir(parents=True, exist_ok=True)

    _git(["worktree", "add", "--detach", str(worktree), base_rev], repo)
    try:
        for attempt_id, sha in shas:
            # Merge keeps the verified commit itself as an ancestor: no rewrite.
            merged = _git(
                ["merge", "--no-ff", "--no-edit", "-m", f"integrate {attempt_id}", sha],
                worktree,
                check=False,
            )
            if merged.returncode != 0:
                raise IntegrateError(
                    "integration_conflict",
                    f"merging {attempt_id} ({sha[:8]}) conflicted: {merged.stderr.strip()[:300]}",
                    "Resolve the conflict by re-dispatching; do not rewrite verification commits.",
                )

        def run_suite(workdir: Path) -> dict[str, Any]:
            proc = subprocess.run(
                list(suite_command),
                cwd=str(workdir),
                capture_output=True,
                text=True,
            )
            return {"exit_code": proc.returncode, "timed_out": False, "tail": (proc.stdout or proc.stderr)[-300:]}

        record = revision.integrate_wave(
            state_dir,
            wave_id or f"w{int((job.get('integration') or {}).get('wave') or 1)}",
            worktree,
            attempts,
            run_suite,
        )
    finally:
        _git(["worktree", "remove", "--force", str(worktree)], repo, check=False)

    accepted = bool(record.get("accepted"))
    return {
        "ok": accepted,
        "evidence": (
            f"wave {record.get('wave_id')} accepted at {str(record.get('head_sha'))[:8]} "
            f"with {len(attempts)} verified commit(s)"
            if accepted
            else f"wave {record.get('wave_id')} rejected: {record.get('problems') or record.get('suite')}"
        ),
        "record": record,
    }
