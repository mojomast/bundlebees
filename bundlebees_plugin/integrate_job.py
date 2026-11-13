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

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import revision, verify_local


def _checkpoint(name: str, **payload: Any) -> None:
    """Fire a fault checkpoint for the crash harness, if one is armed.

    Imported lazily and tolerated when absent, exactly like the other lanes'
    checkpoints: the shipped path has no hooks and this is a no-op.
    """

    try:  # noqa: SIM105 - a missing optional module is the normal no-op case
        from . import faults  # noqa: PLC0415
    except ImportError:
        return
    fire = getattr(faults, "checkpoint", None)
    if callable(fire):
        fire(name, **payload)


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


def _wave_number(job: Mapping[str, Any]) -> int:
    """M1 writes "w1"; later waves are integers. Accept both."""

    value = (job.get("integration") or {}).get("wave", 1)
    if isinstance(value, int) and not isinstance(value, bool):
        return max(1, value)
    text = str(value or "").strip().lower().lstrip("w")
    try:
        return max(1, int(text))
    except ValueError:
        return 1


import hashlib as _hashlib


def job_wave_id(job: Mapping[str, Any]) -> str:
    """Wave records are per job and always within the 64-char record limit.

    M1 allows job ids up to 96 characters while wave ids allow 64, so long ids are
    shortened with a digest of the *full* id: unique, deterministic, and stable
    across restarts.
    """
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        raise IntegrateError("bad_job_id", "the job has no id", "Fix the job document.")
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in job_id)
    suffix = f".w{_wave_number(job)}"
    if len(safe) + len(suffix) > 64:
        digest = _hashlib.sha256(job_id.encode()).hexdigest()[:16]
        safe = f"{safe[:44]}.{digest}"
    return f"{safe}{suffix}"


def _legacy_names(job: Mapping[str, Any]) -> list[str]:
    job_id = str(job.get("job_id") or "").strip()
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in job_id)
    return [f"w{_wave_number(job)}", f"{safe}.w{_wave_number(job)}"]


def adopt_legacy_wave(state_dir: Path, *, wave: str, legacy: str, attempts: Sequence[str]) -> dict[str, Any] | None:
    """Copy an old ``wN`` record to its per-job name, only on exact proof.

    Existing evidence is preserved (the legacy file is left in place); adoption
    happens only when the legacy record lists exactly this job's attempts.
    """
    if wave == legacy:
        return None
    target = Path(state_dir) / "waves" / f"{wave}.json"
    source = Path(state_dir) / "waves" / f"{legacy}.json"
    if target.exists() or not source.exists():
        return None
    try:
        record = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = record.get("attempts") if isinstance(record, dict) else None
    if not isinstance(recorded, list) or sorted(str(a) for a in recorded) != sorted(str(a) for a in attempts):
        return None
    body = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)          # atomic: two recoverers see one result
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return record


def integration_ref(job_id: str) -> str:
    """The durable ref that carries the last accepted integration head."""

    return f"refs/bb/integrated/{job_id}"


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

    wave = wave_id or job_wave_id(job)

    # Crash recovery: an accepted wave record already on disk is the result.
    # Restore the durable ref if the crash happened before it was updated, and
    # report recovery so the caller does not count it as a new attempt.
    for legacy in _legacy_names(job):
        adopted = adopt_legacy_wave(state_dir, wave=wave, legacy=legacy, attempts=attempts)
        if adopted is not None:
            break
    existing = _accepted_wave_record(state_dir, wave)
    if existing is not None:
        head = str(existing.get("head_sha") or "")
        if len(head) != 40:
            raise IntegrateError("bad_wave_record", f"accepted wave {wave} has no head sha", "Inspect the wave record.")
        ref_name = integration_ref(str(job.get("job_id") or ""))
        current = _git(["rev-parse", "--verify", "--quiet", ref_name], repo, check=False).stdout.strip()
        if current != head:
            _git(["update-ref", ref_name, head], repo)
        return {
            "ok": True,
            "evidence": f"wave {wave} already accepted at {head[:8]}; recovered without re-integrating",
            "record": existing,
            "integration_ref": ref_name,
            "start_rev": current or head,
            "recovered": True,
        }

    # Later waves build on the last accepted result, not on the original base.
    ref = integration_ref(str(job.get("job_id") or ""))
    ref_check = _git(["rev-parse", "--verify", "--quiet", ref], repo, check=False)
    start_rev = ref_check.stdout.strip() if ref_check.returncode == 0 else base_rev

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

    _git(["worktree", "add", "--detach", str(worktree), start_rev], repo)
    try:
        for attempt_id, sha in shas:
            already = _git(["merge-base", "--is-ancestor", sha, "HEAD"], worktree, check=False)
            if already.returncode == 0:
                continue  # this wave already contains it
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
            wave,
            worktree,
            attempts,
            run_suite,
        )
        if record.get("accepted"):
            head = str(record.get("head_sha") or "")
            if len(head) != 40:
                raise IntegrateError("bad_wave_record", "the accepted wave has no head sha", "Inspect the wave record.")
            # Fault boundary: the accepted wave record is durable, but neither the
            # integration ref nor the job document names it yet. A crash here must
            # be recovered from the record, never by integrating the wave again.
            _checkpoint("wave.after_persist", job_id=str(job.get("job_id") or ""),
                        wave_id=str(record.get("wave_id") or wave), head_sha=head,
                        attempts=list(attempts))
            # Durable ref: the next wave starts here, not at the original base.
            _git(["update-ref", ref, head], repo)
    finally:
        _git(["worktree", "remove", "--force", str(worktree)], repo, check=False)

    accepted = bool(record.get("accepted"))
    head_ref = ref if accepted else None
    return {
        "ok": accepted,
        "evidence": (
            f"wave {record.get('wave_id')} accepted at {str(record.get('head_sha'))[:8]} "
            f"with {len(attempts)} verified commit(s)"
            if accepted
            else f"wave {record.get('wave_id')} rejected: {record.get('problems') or record.get('suite')}"
        ),
        "record": record,
        "integration_ref": head_ref,
        "start_rev": start_rev,
        "recovered": False,
    }


def _accepted_wave_record(state_dir: Path, wave: str) -> dict[str, Any] | None:
    """The accepted wave record, if one was already written for this wave."""

    path = Path(state_dir) / "waves" / f"{wave}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("accepted") is True:
        return data
    return None
