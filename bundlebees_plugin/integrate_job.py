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
import re
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import job as jobs, recovery, revision, sandbox, test_receipts as receipts, verify_local

DEFAULT_SANDBOX_TIMEOUT_S = 900
_MAX_SANDBOX_TIMEOUT_S = 7200
SuiteRunner = Callable[[Path, Sequence[str]], Mapping[str, Any]]
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ZERO = "0" * 40


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


def _git(args: Sequence[str], cwd: Path, *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=bundlebees", "-c", "user.email=bees@localhost", *args],
        cwd=str(cwd),
        capture_output=True,
        input=input_text,
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


def _reconcile(wave: str, why: str) -> IntegrateError:
    return IntegrateError("wave_reconciliation_required", f"Wave {wave} requires reconciliation: {why}.",
                          "Reconcile the integration evidence and ref explicitly; preserve the original records.")


def legacy_wave_is_foreign(state_dir: Path, legacy: str, *, job_id: str = "",
                           attempts: Sequence[str] = ()) -> bool:
    """Whether legacy waves/<legacy>.json provably belongs to another job.

    Every job's wave 1 has the bare ``w1`` legacy alias, so a record left by an
    unrelated job or a standalone integration must not refuse this job.  Only
    evidence that can belong here may block: a sealed identity naming another
    job proves it is foreign, and so do recorded attempts that do not overlap
    this job's attempts.  Unreadable, empty or shape-less evidence is never
    called foreign - that still fails closed in the caller.
    """

    state = Path(state_dir)
    stored = _read_evidence(_wave_identity_path(state, legacy), legacy) or {}
    identity = stored.get("identity")
    if not isinstance(identity, Mapping):
        identity = {}
    owner = str(identity.get("job_id") or "").strip()
    if job_id and owner and owner != job_id:
        return True
    record = _read_evidence(state / "waves" / f"{legacy}.json", legacy)
    if not isinstance(record, Mapping):
        return False
    claimed = record.get("attempts")
    if not isinstance(claimed, list):
        return False
    claimed_ids = {str(entry) for entry in claimed if str(entry or "").strip()}
    if not claimed_ids:
        return False
    return not (claimed_ids & {str(entry) for entry in attempts if str(entry or "").strip()})


def adopt_legacy_wave(state_dir: Path, *, wave: str, legacy: str, attempts: Sequence[str],
                      job_id: str = "") -> None:
    """A differently named receipt cannot attest today's wave-bound identity.

    A record that provably belongs to another job is ignored: it is not this
    job's evidence, so it can neither be adopted nor refuse this integration.
    """
    if wave == legacy:
        return None
    target = Path(state_dir) / "waves" / f"{wave}.json"
    source = Path(state_dir) / "waves" / f"{legacy}.json"
    if target.exists():
        return None
    if os.path.lexists(source) or os.path.lexists(_wave_identity_path(state_dir, legacy)):
        if legacy_wave_is_foreign(state_dir, legacy, job_id=job_id, attempts=attempts):
            return None
        raise _reconcile(wave, f"legacy evidence at {legacy} cannot be adopted by attempt name")


def integration_ref(job_id: str) -> str:
    """The durable ref that carries the last accepted integration head."""

    return f"refs/bb/integrated/{job_id}"


def verified_attempts(job: Mapping[str, Any]) -> list[str]:
    """Attempt ids for tasks whose verification is recorded and ok."""

    attempts: list[str] = []
    for task in job.get("tasks", []):
        if task.get("state") not in {"verified", "integrating"}:
            continue
        history = task.get("attempts")
        if not isinstance(history, list) or not history or not revision._safe_attempt_id(history[-1]):
            raise _reconcile(job_wave_id(job), "verified task has no valid latest attempt")
        attempts.append(history[-1])
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


def _read_evidence(path: Path, wave: str) -> dict[str, Any] | None:
    """Only absence is normal. Never follow links or block on a special file."""
    try:
        if path.parent.is_symlink():
            raise _reconcile(wave, f"linked {path.parent.name} directory")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _reconcile(wave, f"non-regular {path.parent.name} record")
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise _reconcile(wave, f"unreadable {path.parent.name} record") from exc
    if not isinstance(data, dict):
        raise _reconcile(wave, f"malformed {path.parent.name} record")
    return data


def _record_digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _wave_pair(state: Path, repo: Path, wave: str, request: dict) -> tuple[dict, dict] | None:
    """Verify the historical evidence before routing a changed request or replaying it."""
    record = _read_evidence(revision.wave_path(state, wave), wave)
    stored = _read_evidence(_wave_identity_path(state, wave), wave)
    if record is None and stored is None:
        return None
    identity = (stored or {}).get("identity")
    result = (stored or {}).get("materialized")
    if (record is None or not isinstance(identity, dict) or identity.keys() != request.keys()
            or stored.get("identity_digest") != _identity_digest(identity)
            or identity.get("job_id") != request["job_id"] or identity.get("wave") != request["wave"]
            or not isinstance(result, dict)):
        raise _reconcile(wave, "missing or inconsistent execution identity")

    head = record.get("head_sha")
    accepted = record.get("accepted")
    inputs = identity.get("attempts")
    if (record.get("wave_id") != wave or result.get("wave_id") != wave
            or type(accepted) is not bool or result.get("accepted") is not accepted
            or not isinstance(head, str) or not _SHA.fullmatch(head) or result.get("head_sha") != head
            or not isinstance(inputs, list) or not inputs
            or not all(isinstance(i, dict) and revision._safe_attempt_id(i.get("attempt_id"))
                       and isinstance(i.get("sha"), str) and _SHA.fullmatch(i["sha"]) for i in inputs)
            or record.get("attempts") != [i["attempt_id"] for i in inputs]
            or len(set(record["attempts"])) != len(inputs)
            or "previous_ref" not in result or result.get("record_sha256") != _record_digest(record)
            or stored.get("binding_sha256") != _binding_digest(stored["identity_digest"], result)):
        raise _reconcile(wave, "wave record does not match its materialized identity")
    if accepted:
        if not revision.suite_passed(record.get("suite")) or record.get("problems") != {}:
            raise _reconcile(wave, "accepted record lacks a passing unblocked suite or has attempt problems")
        parents = [identity["base_rev"], *(i["sha"] for i in inputs)]
        if result.get("previous_ref") is not None:
            parents.append(result["previous_ref"])
        if (_git(["cat-file", "-t", head], repo, check=False).stdout.strip() != "commit"
                or any(not isinstance(p, str) or not _SHA.fullmatch(p)
                       or _git(["merge-base", "--is-ancestor", p, head], repo, check=False).returncode for p in parents)
                or result.get("source_tree_sha256") != verify_local._tree_digest(repo, head)):
            raise _reconcile(wave, "accepted head does not contain the recorded inputs or tested tree")
    return record, stored


def _ref_value(repo: Path, ref: str, wave: str) -> str | None:
    if _git(["symbolic-ref", "-q", ref], repo, check=False).returncode != 1:
        raise _reconcile(wave, "integration ref is not a direct ref")
    check = _git(["rev-parse", "--verify", "--quiet", ref], repo, check=False)
    if check.returncode == 1:
        return None
    if check.returncode or not _SHA.fullmatch(check.stdout.strip()):
        raise _reconcile(wave, "integration ref cannot be read")
    return check.stdout.strip()


@contextmanager
def _hold_tested_head(repo: Path, ref: str, wave: str, head: str):
    """Verify and lock the ref through the signed job save. No update is queued."""
    with subprocess.Popen(
        ["git", "update-ref", "--no-deref", "--stdin"], cwd=repo,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(Path.home())},
    ) as txn:
        # EOF aborts this verify-only transaction on success or exception.
        txn.stdin.write(f"start\nverify {ref} {head}\nprepare\n")
        txn.stdin.flush()
        if (txn.stdout.readline() != "start: ok\n" or txn.stdout.readline() != "prepare: ok\n"
                or _ref_value(repo, ref, wave) != head):
            raise _reconcile(wave, "integration ref no longer identifies the tested head")
        yield


def _pending_ref(handoff: dict, binding: str) -> str:
    return "refs/bb/pending-integration/" + _record_digest({"request": handoff, "binding": binding})


def _publish_head(repo: Path, ref: str, wave: str, result: dict, *, pending: str) -> None:
    current = _ref_value(repo, ref, wave)
    if _ref_value(repo, pending, wave) is not None:
        head = result["head_sha"]
        if current not in (head, result["previous_ref"]):
            raise _reconcile(wave, "integration ref is not at the recorded predecessor")
        # The intent exists only before publication. Consume it in the *same*
        # Git transaction: a later deleted ref is not a pending crash repair.
        txn = f"update {ref} {head} {current or _ZERO}\ndelete {pending} {head}\n"
        if _git(["update-ref", "--no-deref", "--stdin"], repo, input_text=txn, check=False).returncode:
            raise _reconcile(wave, "integration ref or pending publication changed")
        return
    if current != result["head_sha"]:
        raise _reconcile(wave, "no pending publication for the tested head")


def _retain_rejection(state: Path, wave: str, pair: tuple[dict, dict]) -> None:
    """Revision retries may replace a rejected log. Keep the exact original files."""
    directory = state / "wave-history" / wave / _record_digest({"record": pair[0], "sidecar": pair[1]})
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (revision.wave_path(state, wave), _wave_identity_path(state, wave)):
        dest = directory / f"{path.parent.name}.json"
        try:
            os.link(path, dest)
        except FileExistsError:
            if dest.read_bytes() != path.read_bytes():
                raise _reconcile(wave, "conflicting rejection history") from None


def _verified_inputs(state: Path, wave: str, attempts: list[str]) -> list[tuple[str, str]]:
    shas = []
    if len(set(attempts)) != len(attempts):
        raise _reconcile(wave, "duplicate attempt id")
    for attempt in attempts:
        record = _read_evidence(verify_local.record_path(state, attempt), wave)
        if not record or record.get("ok") is not True:
            raise IntegrateError("not_verified", f"{attempt} has no accepted verify record", "Verify it first.")
        sha = record.get("sha")
        if record.get("attempt_id") != attempt or not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise _reconcile(wave, "verification does not name the requested attempt and commit")
        shas.append((attempt, sha))
    return shas


def _binding_digest(digest: str, materialized: Mapping[str, Any]) -> str:
    """Commit the execution identity and its result together, including the ref CAS."""
    return _record_digest({"domain": "bb.integration.binding.v1", "request": digest, "result": materialized})


def _write_wave_identity(
    state_dir: Path,
    wave: str,
    identity: Mapping[str, Any],
    digest: str,
    *,
    materialized: Mapping[str, Any],
) -> None:
    path = _wave_identity_path(state_dir, wave)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "identity": dict(identity),
                "identity_digest": str(digest),
                "materialized": dict(materialized),
                "binding_sha256": _binding_digest(digest, materialized),
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
    # Different requests can compete for the same wave/sidecar slot.
    request_key = "integrate-wave:" + wave
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
    if not _SHA.fullmatch(base_rev):
        raise IntegrateError("bad_base_rev", "the job has no pinned base revision", "Fix the job document.")

    wave = wave_id or job_wave_id(job)

    # The verified commits are inputs to the request identity, so read them
    # before deciding whether an accepted wave is recovery or a stale result.
    shas = _verified_inputs(state_dir, wave, attempts)

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
    ref = integration_ref(str(job.get("job_id") or ""))
    previous_ref = _ref_value(repo, ref, wave)
    pair = _wave_pair(state_dir, repo, wave, identity)
    if pair is None:
        for legacy in _legacy_names(job):
            adopt_legacy_wave(state_dir, wave=wave, legacy=legacy, attempts=attempts,
                              job_id=str(job.get("job_id") or ""))
    elif pair[1]["identity_digest"] != identity_digest:
        original = pair[0]
        wave = _derived_wave_id(wave, identity_digest)
        pair = _wave_pair(state_dir, repo, wave, identity)
        if pair and pair[1]["identity_digest"] != identity_digest:
            raise _reconcile(wave, "derived wave belongs to another execution identity")
        if (not pair or not pair[0]["accepted"]) and original["accepted"] and previous_ref != original["head_sha"]:
            raise _reconcile(wave, "changed request is not starting at the previously accepted head")
    handoff = jobs._integration_request(state_dir, job, repo, identity["wave"], wave, identity_digest)
    # Standalone callers have no sealed job handoff. Their local intent is
    # scoped to this state store, repo and execution request; the same result
    # binding and one-shot Git transaction govern publication in either mode.
    publication = handoff or {"kind": "standalone-integration/1", "state": str(state_dir.resolve()),
                              "repo": str(repo.resolve()), "selected": wave, "identity_digest": identity_digest}
    if pair and pair[0]["accepted"]:
        existing, stored = pair
        head = existing["head_sha"]
        if handoff:
            jobs._confirm_integration(state_dir, handoff, stored["binding_sha256"], fresh=False)
        _publish_head(repo, ref, wave, stored["materialized"],
                      pending=_pending_ref(publication, stored["binding_sha256"]))
        if handoff:
            jobs._select_integration(state_dir, handoff)
        return {
            "ok": True,
            "evidence": f"wave {wave} already accepted at {head[:8]}; recovered without re-integrating",
            "record": existing,
            "integration_ref": ref,
            "start_rev": stored["materialized"].get("previous_ref") or base_rev,
            "recovered": True,
            "request_identity_digest": identity_digest,
            "test_cache": dict(stats),
        }

    # Later waves build on the last accepted result, not on the original base.
    start_rev = previous_ref or base_rev
    if handoff:
        jobs._select_integration(state_dir, handoff)
    if pair:
        _retain_rejection(state_dir, wave, pair)

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
        tested_head = _git(["rev-parse", "HEAD"], worktree).stdout.strip()
        source_tree_sha256 = verify_local._tree_digest(worktree, tested_head)
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
            "wave_id": wave,
            "head_sha": tested_head,
            "source_tree_sha256": source_tree_sha256,
            "previous_ref": previous_ref,
            "accepted": record["accepted"],
            "record_sha256": _record_digest(record),
        }
        _write_wave_identity(
            state_dir, wave, identity, identity_digest, materialized=materialized
        )
        if record.get("accepted"):
            head = tested_head
            binding = _binding_digest(identity_digest, materialized)
            pending = _pending_ref(publication, binding)
            _git(["update-ref", "--no-deref", pending, head, _ZERO], repo)
            if handoff:
                # The coordinator's live execution is the only proof issuer. The
                # recovery reader also independently checks the Git and suite gates.
                jobs._confirm_integration(state_dir, handoff, binding, fresh=True)
            # Fault boundary: the accepted wave record is durable, but neither the
            # integration ref nor the job document names it yet. A crash here must
            # be recovered from the record, never by integrating the wave again.
            _checkpoint("wave.after_persist", job_id=str(job.get("job_id") or ""),
                        wave_id=str(record.get("wave_id") or wave), head_sha=head,
                        attempts=list(attempts))
            # Durable ref: the next wave starts here, not at the original base.
            pair = _wave_pair(state_dir, repo, wave, identity)
            if (not pair or pair[1]["identity_digest"] != identity_digest or pair[1]["materialized"] != materialized
                    or _verified_inputs(state_dir, wave, attempts) != shas):
                raise _reconcile(wave, "integration evidence changed before publication")
            _publish_head(repo, ref, wave, materialized, pending=pending)
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
