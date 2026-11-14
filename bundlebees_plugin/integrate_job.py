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

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import revision, sandbox, test_receipts as receipts, verify_local

DEFAULT_SANDBOX_TIMEOUT_S = 900
_MAX_SANDBOX_TIMEOUT_S = 7200
SuiteRunner = Callable[[Path, Sequence[str]], Mapping[str, Any]]


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


def _suite_evidence(
    workdir: Path,
    argv: Sequence[str],
    *,
    runner: SuiteRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
) -> dict[str, Any]:
    """Run the full suite in the sandbox and return revision's evidence shape.

    No child inherits the coordinator environment (``BUNDLEBEES_*``/``HERMES_*``
    are absent because the sandbox builds an exact environment), the cwd is the
    disposable copy of *workdir*, and a missing backend fails closed with
    ``IntegrateError("sandbox_unavailable")``.
    """

    bounded = max(1, min(int(timeout), _MAX_SANDBOX_TIMEOUT_S))
    try:
        if runner is not None:
            evidence = runner(Path(workdir), list(argv))
        else:
            evidence = sandbox.run_in_sandbox(
                Path(workdir),
                list(argv),
                backend=backend,
                allow_unsafe_local=allow_unsafe_local,
                allow_weak=allow_weak,
                timeout=bounded,
            )
    except sandbox.SandboxUnavailable as exc:
        raise IntegrateError(exc.code, exc.error, exc.fix) from exc
    except ValueError as exc:
        raise IntegrateError(
            "invalid_argument",
            f"unknown verify_isolation backend: {exc}",
            "Set verify_isolation to auto, bwrap or firejail (unshare needs the weak override).",
        ) from exc
    if not isinstance(evidence, Mapping):
        raise IntegrateError(
            "sandbox_evidence",
            "the suite runner returned no evidence",
            "Check the sandbox runner and retry.",
        )
    try:
        exit_code = int(evidence.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = 1
    return {
        "exit_code": exit_code,
        "timed_out": bool(evidence.get("timed_out")),
        "tail": str(evidence.get("output_tail") or evidence.get("tail") or "").strip()[-300:],
        "junit": evidence.get("junit"),
        "backend": evidence.get("backend"),
    }


def _test_cache_stats() -> dict[str, int]:
    return {"executions": 0, "hits": 0, "misses": 0}


def _integration_request_lock(
    job: Mapping[str, Any],
    *,
    wave: str,
    attempts: Sequence[str],
    suite_command: Sequence[str],
) -> str:
    payload = {
        "job_id": str(job.get("job_id") or ""),
        "wave": str(wave),
        "attempts": [str(attempt) for attempt in attempts],
        "suite_command": [str(item) for item in suite_command],
    }
    return "integrate-request:" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def _receipt_suite_evidence(receipt: Mapping[str, Any]) -> dict[str, Any]:
    result = receipt.get("result")
    if not isinstance(result, Mapping):
        raise IntegrateError(
            "receipt_corrupt",
            "the stored suite receipt has no complete result",
            "Remove the corrupt receipt only by intending to rerun the suite.",
        )
    try:
        exit_code = int(result.get("exit_code"))
    except (TypeError, ValueError) as exc:
        raise IntegrateError(
            "receipt_corrupt",
            "the stored suite receipt has no exit code",
            "Remove the corrupt receipt only by intending to rerun the suite.",
        ) from exc
    timed_out = result.get("timed_out", False)
    if not isinstance(timed_out, bool):
        raise IntegrateError(
            "receipt_corrupt",
            "the stored suite receipt has an invalid timeout flag",
            "Remove the corrupt receipt only by intending to rerun the suite.",
        )
    tail = result.get("tail", "")
    if tail is None:
        tail = ""
    if not isinstance(tail, str):
        raise IntegrateError(
            "receipt_corrupt",
            "the stored suite receipt has an invalid output tail",
            "Remove the corrupt receipt only by intending to rerun the suite.",
        )
    evidence: dict[str, Any] = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "tail": tail,
        "junit": result.get("junit"),
        "backend": result.get("backend"),
        "receipt": {
            "key": receipt.get("key"),
            "hit": True,
            "duration_s": receipt.get("duration_s"),
        },
    }
    return evidence


def _run_suite_with_receipt(
    workdir: Path,
    argv: Sequence[str],
    *,
    runner: SuiteRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
    receipt_context: Mapping[str, Any],
) -> dict[str, Any]:
    execute = lambda: _suite_evidence(  # noqa: E731 - local closure keeps the call site small
        workdir,
        argv,
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
    )
    stats = receipt_context["stats"]
    state_dir = receipt_context["state_dir"]
    cacheable = bool(receipt_context.get("cacheable", True))
    key_inputs = verify_local._receipt_key_inputs(
        workdir,
        command=argv,
        stage=str(receipt_context["stage"]),
        source_tree_sha256=str(receipt_context["source_tree_sha256"]),
        patch_sha256=str(receipt_context["patch_sha256"]),
        tests=receipt_context.get("tests", ()),
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
    )

    try:
        hit = receipts.lookup_receipt(state_dir, key_inputs, cacheable=cacheable)
    except receipts.ReceiptError as exc:
        raise IntegrateError(exc.code, exc.error, exc.fix) from exc
    if hit is not None:
        verify_local._assert_sandbox_usable(
            runner,
            backend=backend,
            allow_unsafe_local=allow_unsafe_local,
            allow_weak=allow_weak,
        )
        stats["hits"] += 1
        return _receipt_suite_evidence(hit)

    if not cacheable:
        stats["misses"] += 1
        stats["executions"] += 1
        return execute()

    try:
        claim = receipts.claim_receipt(state_dir, key_inputs, cacheable=True)
    except receipts.ReceiptError as exc:
        raise IntegrateError(exc.code, exc.error, exc.fix) from exc
    try:
        try:
            hit = receipts.lookup_receipt(state_dir, key_inputs)
        except receipts.ReceiptError as exc:
            raise IntegrateError(exc.code, exc.error, exc.fix) from exc
        if hit is not None:
            verify_local._assert_sandbox_usable(
                runner,
                backend=backend,
                allow_unsafe_local=allow_unsafe_local,
                allow_weak=allow_weak,
            )
            stats["hits"] += 1
            return _receipt_suite_evidence(hit)

        stats["misses"] += 1
        started = time.monotonic()
        evidence = execute()
        stats["executions"] += 1
        duration = max(0.0, time.monotonic() - started)
        if not evidence.get("timed_out"):
            try:
                receipts.record_receipt(state_dir, key_inputs, evidence, duration)
            except receipts.ReceiptError as exc:
                raise IntegrateError(exc.code, exc.error, exc.fix) from exc
        return evidence
    finally:
        claim.release()


def integrate(
    state_dir: Path,
    job: Mapping[str, Any],
    repo: Path,
    *,
    suite_command: Sequence[str] = ("python", "-m", "pytest", "-q"),
    worktree_root: Path | None = None,
    wave_id: str | None = None,
    runner: SuiteRunner | None = None,
    sandbox_backend: str = "auto",
    sandbox_allow_unsafe_local: bool = False,
    sandbox_allow_weak: bool = False,
    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    cacheable: bool = True,
) -> dict[str, Any]:
    """Build one integrated history from verified commits, then call integrate_wave.

    The full regression suite runs through :func:`_suite_evidence` (or the
    explicitly injected *runner*) so it is sandboxed, bounded and never sees the
    coordinator's environment. An unavailable sandbox fails closed with
    ``IntegrateError("sandbox_unavailable")``. An identical wave request is
    single-flighted and its exact suite invocation is reused from the ledger.
    """

    wave = wave_id or job_wave_id(job)
    attempts = verified_attempts(job)
    try:
        claim = receipts.claim_key(
            state_dir,
            _integration_request_lock(
                job,
                wave=wave,
                attempts=attempts,
                suite_command=suite_command,
            ),
        )
    except receipts.ReceiptError as exc:
        raise IntegrateError(exc.code, exc.error, exc.fix) from exc
    try:
        return _integrate_locked(
            state_dir,
            job,
            repo,
            suite_command=suite_command,
            worktree_root=worktree_root,
            wave_id=wave,
            runner=runner,
            sandbox_backend=sandbox_backend,
            sandbox_allow_unsafe_local=sandbox_allow_unsafe_local,
            sandbox_allow_weak=sandbox_allow_weak,
            sandbox_timeout=sandbox_timeout,
            cacheable=cacheable,
            stats=_test_cache_stats(),
        )
    finally:
        claim.release()


def _integrate_locked(
    state_dir: Path,
    job: Mapping[str, Any],
    repo: Path,
    *,
    suite_command: Sequence[str] = ("python", "-m", "pytest", "-q"),
    worktree_root: Path | None = None,
    wave_id: str | None = None,
    runner: SuiteRunner | None = None,
    sandbox_backend: str = "auto",
    sandbox_allow_unsafe_local: bool = False,
    sandbox_allow_weak: bool = False,
    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    cacheable: bool = True,
    stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    stats = stats if stats is not None else _test_cache_stats()
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
            "test_cache": dict(stats),
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

        combined = json.dumps(
            {
                "start_rev": start_rev,
                "attempts": [[attempt_id, sha] for attempt_id, sha in shas],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        patch_sha256 = hashlib.sha256(
            b"bundlebees-integration-identity:v1\x00" + combined.encode("utf-8")
        ).hexdigest()
        source_tree_sha256 = verify_local._tree_digest(worktree, "HEAD")
        receipt_context = {
            "state_dir": state_dir,
            "stage": "integrate.suite",
            "source_tree_sha256": source_tree_sha256,
            "patch_sha256": patch_sha256,
            "tests": [],
            "cacheable": cacheable,
            "stats": stats,
        }

        def run_suite(workdir: Path) -> dict[str, Any]:
            return _run_suite_with_receipt(
                workdir,
                list(suite_command),
                runner=runner,
                backend=sandbox_backend,
                allow_unsafe_local=sandbox_allow_unsafe_local,
                allow_weak=sandbox_allow_weak,
                timeout=sandbox_timeout,
                receipt_context=receipt_context,
            )

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
        "test_cache": dict(stats),
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
