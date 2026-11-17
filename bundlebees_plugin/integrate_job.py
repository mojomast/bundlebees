"""Wire the reconciler to `integrate_wave`, preserving verified commit identity.

The verified commits live on local `bb-verify/<attempt>` branches whose parent is
the pinned base. `integrate_wave` requires every verified sha to be an ancestor of
HEAD in the workdir it is given, so this module builds one integration worktree at
that base and merges each verified commit into it — no rebase, no cherry-pick, no
SHA rewriting. The sibling commits stay exactly as verified, and become ancestors
of the integrated HEAD.

Only then is `integrate_wave` called, with the same ancestry and suite checks it
has always enforced. Nothing here weakens it.

A rejected wave is attributed causally: the failing test selectors the suite
reported are correlated with the attempts whose leases cover the failing files,
and the result names the offending attempt (or states explicitly that the
evidence is ambiguous and names none). The result carries the structured
attribution and a recovery-ready failure record, so the recovery request the
failure earns names the patch that caused it, not just the failing suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import recovery, revision, sandbox, test_receipts as receipts, verify_local

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


def _failed_case_ids(evidence: Mapping[str, Any]) -> list[str]:
    """The failing test ids a sandbox run's per-case results named, bounded."""

    cases = evidence.get("cases")
    if not isinstance(cases, (list, tuple)):
        return []
    found = sorted({
        str(case.get("id"))
        for case in cases
        if isinstance(case, Mapping)
        and isinstance(case.get("id"), str)
        and case.get("id").strip()
        and str(case.get("outcome") or "").strip().lower() in {"failed", "error"}
    })
    return found[:200]


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
    ``IntegrateError("sandbox_unavailable")``. The failing test ids the run
    reported are kept: causal attribution needs them, not just the exit code.
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
    exit_code = evidence.get("exit_code")
    return {
        "exit_code": exit_code if type(exit_code) is int else 1,
        "timed_out": bool(evidence.get("timed_out")),
        "blocking": bool(evidence.get("blocking")),
        "tail": str(evidence.get("output_tail") or evidence.get("tail") or "").strip()[-300:],
        "junit": evidence.get("junit"),
        "backend": evidence.get("backend"),
        "failed_tests": _failed_case_ids(evidence),
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


def _identity_digest(identity: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(identity), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(b"bundlebees-integration-identity:v1\x00" + payload).hexdigest()


def _integration_request_identity(
    repo: Path,
    *,
    job: Mapping[str, Any],
    wave: str,
    base_rev: str,
    shas: Sequence[tuple[str, str]],
    suite_command: Sequence[str],
    runner: SuiteRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
) -> dict[str, Any]:
    """The complete identity of a request to validate one integration wave.

    The accepted-wave record is historical evidence for *this* identity only.
    The attempt commit shas, base tree, exact suite argv, installed runtime,
    allowed environment and sandbox policy all participate, so a changed
    command/runtime/policy is a new validation request rather than recovery.
    """

    return {
        "job_id": str(job.get("job_id") or ""),
        "wave": str(wave),
        "base_rev": str(base_rev),
        "source_tree_sha256": verify_local._tree_digest(repo, base_rev),
        "attempts": [
            {"attempt_id": str(attempt_id), "sha": str(sha)}
            for attempt_id, sha in shas
        ],
        "command": [str(item) for item in suite_command],
        "stack": verify_local._stack_identity(),
        "dependency_environment": verify_local._installed_dependency_identity(),
        "environment": verify_local._environment_policy(runner),
        "sandbox": verify_local._sandbox_policy(
            runner,
            backend=backend,
            allow_unsafe_local=allow_unsafe_local,
            allow_weak=allow_weak,
            timeout=timeout,
        ),
    }


def _derived_wave_id(wave: str, digest: str) -> str:
    """A distinct, safe wave id for a changed request on the same wave number."""

    candidate = f"{wave}.{digest[:16]}"
    if len(candidate) <= 64:
        return candidate
    stable = hashlib.sha256(wave.encode("utf-8")).hexdigest()[:8]
    return f"{wave[:40]}.{stable}.{digest[:12]}"[:64]


def _wave_identity_path(state_dir: Path, wave: str) -> Path:
    # Deliberately outside waves/: callers enumerate waves/*.json as wave
    # records, and this sidecar is identity evidence, not another wave.
    return Path(state_dir) / "wave-identities" / f"{wave}.json"


def _read_wave_identity(state_dir: Path, wave: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_wave_identity_path(state_dir, wave).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_wave_identity(
    state_dir: Path,
    wave: str,
    identity: Mapping[str, Any],
    digest: str,
    *,
    materialized: Mapping[str, Any] | None = None,
) -> None:
    path = _wave_identity_path(state_dir, wave)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "identity": dict(identity),
                "identity_digest": str(digest),
                "materialized": dict(materialized or {}),
            },
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        raise IntegrateError(
            "receipt_io",
            f"the integration identity could not be written: {exc}",
            "Check that the state directory is writable and retry.",
        ) from exc


def _receipt_suite_evidence(receipt: Mapping[str, Any]) -> dict[str, Any]:
    result = receipt.get("result")
    if not isinstance(result, Mapping):
        raise IntegrateError(
            "receipt_corrupt",
            "the stored suite receipt has no complete result",
            "Remove the corrupt receipt only by intending to rerun the suite.",
        )
    exit_code = result.get("exit_code")
    if type(exit_code) is not int:
        raise IntegrateError(
            "receipt_corrupt",
            "the stored suite receipt has no integer exit code",
            "Remove the corrupt receipt only by intending to rerun the suite.",
        )
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
        "blocking": bool(result.get("blocking")),
        "tail": tail,
        "junit": result.get("junit"),
        "backend": result.get("backend"),
        "receipt": {
            "key": receipt.get("key"),
            "hit": True,
            "duration_s": receipt.get("duration_s"),
        },
    }
    failed = result.get("failed_tests")
    evidence["failed_tests"] = (
        [str(item) for item in failed] if isinstance(failed, (list, tuple)) else []
    )
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


def _failing_test_ids(suite: Any) -> list[str]:
    """The failing selectors a rejected wave's suite evidence names.

    Classified regressions first (the CLI's pre-existing/environmental split),
    then the suite's own failing ids, then per-case results, and finally the
    output tail: attribution is only as good as the evidence, and a tail that
    names no selector means no attribution, never a guess.
    """

    if not isinstance(suite, Mapping):
        return []
    failures = suite.get("failures")
    if isinstance(failures, Mapping):
        named = failures.get("failed_tests")
        if isinstance(named, (list, tuple)) and named:
            return [str(item) for item in named if str(item).strip()][:200]
    failed = suite.get("failed_tests")
    if isinstance(failed, (list, tuple)) and failed:
        return [str(item) for item in failed if str(item).strip()][:200]
    cases = suite.get("cases")
    if isinstance(cases, (list, tuple)):
        found = _failed_case_ids({"cases": cases})
        if found:
            return found
    return recovery.test_ids_in(str(suite.get("tail") or ""))[:200]


def _attribution_summary(attribution: Mapping[str, Any]) -> str:
    """One sentence about the cause: the offender, or why none is named."""

    if attribution.get("attributed"):
        return (
            f"; offending attempt {attribution.get('attempt_id')} (its lease covered "
            f"{recovery._listing(attribution.get('files'))}) failed "
            f"{recovery._listing(attribution.get('tests'))}"
        )
    reason = str(attribution.get("reason") or "").strip()
    return f"; no offending attempt named: {reason}" if reason else ""


def _rejection_evidence(
    record: Mapping[str, Any], attribution: Mapping[str, Any] | None
) -> str:
    """A rejected wave's evidence, headed by its cause, not just the suite.

    The leading code is the canonical integration code the failure belongs to,
    so the recovery classification names the mechanism (a patch set the wave
    rejected, or a combined suite the wave broke), and the attribution sentence
    names the patch that caused it — or says explicitly that none can be named.
    """

    head = "wave_rejected" if record.get("problems") else "combined_suite_failed"
    text = (
        f"{head}: wave {record.get('wave_id')} rejected: "
        f"{record.get('problems') or record.get('suite')}"
    )
    if isinstance(attribution, Mapping):
        summary = _attribution_summary(attribution)
        if summary:
            text = f"{text}{summary}"
    return text


def _integration_failure_record(
    record: Mapping[str, Any],
    failed_tests: Sequence[str],
    attribution: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The recovery-ready failure record for a rejected wave.

    ``recovery.build_request`` consumes it as ``failure=``: the code classifies
    the mechanism, ``failed_tests`` keeps the selectors attributable, and the
    attribution rides with it so the durable request names the cause.
    """

    body: dict[str, Any] = {
        "code": "wave_rejected" if record.get("problems") else "combined_suite_failed",
        "evidence": _rejection_evidence(record, attribution),
        "wave_id": str(record.get("wave_id") or ""),
        "failed_tests": list(failed_tests),
    }
    if attribution is not None:
        body["attribution"] = dict(attribution)
    return body


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
    request_key = _integration_request_lock(
        job, wave=wave, attempts=attempts, suite_command=suite_command
    )
    try:
        claim = receipts.claim_key(state_dir, request_key)
    except receipts.ReceiptError as exc:
        raise IntegrateError(exc.code, exc.error, exc.fix) from exc
    try:
        return _integrate_locked(
            state_dir,
            job,
            repo,
            request_key=request_key,
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
    request_key: str,
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

    # The verified commits are inputs to the request identity, so read them
    # before deciding whether an accepted wave is recovery or a stale result.
    shas: list[tuple[str, str]] = []
    for attempt_id in attempts:
        record = verify_local.read_record(state_dir, attempt_id)
        if not record or record.get("ok") is not True:
            raise IntegrateError("not_verified", f"{attempt_id} has no accepted verify record", "Verify it first.")
        sha = str(record.get("sha") or "")
        if len(sha) != 40:
            raise IntegrateError("bad_verify_record", f"{attempt_id} has no commit sha", "Re-verify it.")
        shas.append((attempt_id, sha))

    identity = _integration_request_identity(
        repo,
        job=job,
        wave=wave,
        base_rev=base_rev,
        shas=shas,
        suite_command=suite_command,
        runner=runner,
        backend=sandbox_backend,
        allow_unsafe_local=sandbox_allow_unsafe_local,
        allow_weak=sandbox_allow_weak,
        timeout=sandbox_timeout,
    )
    identity_digest = _identity_digest(identity)

    # Crash recovery: an accepted wave record already on disk is the result,
    # but only for the execution identity it was earned under.  A changed
    # command/runtime/policy is a new validation request and must not inherit
    # the old accepted evidence.
    for legacy in _legacy_names(job):
        adopted = adopt_legacy_wave(state_dir, wave=wave, legacy=legacy, attempts=attempts)
        if adopted is not None:
            break
    existing = _accepted_wave_record(state_dir, wave)
    if existing is not None:
        stored = _read_wave_identity(state_dir, wave)
        stored_digest = str((stored or {}).get("identity_digest") or "")
        if stored_digest and stored_digest != identity_digest:
            wave = _derived_wave_id(wave, identity_digest)
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
            "request_identity_digest": identity_digest,
            "test_cache": dict(stats),
        }

    # Later waves build on the last accepted result, not on the original base.
    ref = integration_ref(str(job.get("job_id") or ""))
    ref_check = _git(["rev-parse", "--verify", "--quiet", ref], repo, check=False)
    start_rev = ref_check.stdout.strip() if ref_check.returncode == 0 else base_rev

    root = Path(worktree_root) if worktree_root else repo.parent / ".bb-integrate"
    # Only the owner of this request lock may reclaim its abandoned checkout.
    # Independent state stores hold independent locks, even for identical keys.
    namespace = json.dumps([str(state_dir.resolve()), str(repo.resolve()), request_key])
    worktree = root.resolve() / ("wt-" + hashlib.sha256(namespace.encode()).hexdigest())
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
        materialized: dict[str, Any] = {
            "wave_id": str(record.get("wave_id") or wave),
            "head_sha": str(record.get("head_sha") or ""),
            "accepted": bool(record.get("accepted")),
        }
        if materialized["head_sha"]:
            materialized["source_tree_sha256"] = verify_local._tree_digest(
                worktree, str(materialized["head_sha"])
            )
        _write_wave_identity(
            state_dir, wave, identity, identity_digest, materialized=materialized
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
    attribution = None
    failed_tests: list[str] = []
    if not accepted:
        # Causal regression attribution: which attempt's patch broke the wave.
        failed_tests = _failing_test_ids(record.get("suite"))
        attribution = recovery.attribute_regressions(
            state_dir,
            job,
            failed_tests=failed_tests,
            attempts=[attempt_id for attempt_id, _sha in shas],
        )
    return {
        "ok": accepted,
        "evidence": (
            f"wave {record.get('wave_id')} accepted at {str(record.get('head_sha'))[:8]} "
            f"with {len(attempts)} verified commit(s)"
            if accepted
            else _rejection_evidence(record, attribution)
        ),
        "record": record,
        "integration_ref": head_ref,
        "start_rev": start_rev,
        "recovered": False,
        "request_identity_digest": identity_digest,
        "attribution": attribution,
        "failure": (
            None if accepted else _integration_failure_record(record, failed_tests, attribution)
        ),
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
