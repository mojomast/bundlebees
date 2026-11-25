"""SHADOW ONLY: production reaches this module only through
:func:`bundlebees_plugin.verify_shadow.observe` when
``BUNDLEBEES_VERIFY_WORKERS=shadow``. The in-beat verdict stays authoritative
and mode ``on`` is not wired.

Verify-worker bees: per-attempt verification as dispatchable queue actions.

Verification was the last big serial stage of the coordinator pipeline: the
tick's verifier closure ran ``verify_local.verify_and_commit`` in-beat, one
attempt at a time. This module makes verification dispatchable through the F6
coordinator action queue (the frozen contract in
:mod:`bundlebees_plugin.coordinator_actions`), so queued items verify in
parallel: each ``verify:<attempt>`` item is claimed by its own verify-worker
bee, which runs the SAME verifier — the configured interpreter, the same test
commands, the same scanner and the same evidence contract — inside the sandbox
runner it is configured with, and returns SIGNED evidence the coordinator
checks. The coordinator never runs the tests to consume the result; it
re-proves the cheap git facts and reads the record the merge gate already
reads.

The item. One action per attempt:

* ``action_key`` ``verify:<attempt_id>`` — the canonical, idempotent identity
  (re-enqueueing replays the stored record untouched).
* ``action_id`` ``verify-<attempt_id>`` — the same name in the queue's safe
  path alphabet (a colon is not a path component there); an attempt id too
  long for the alphabet hashes to a bounded derived id.
* ``kind`` ``verify`` — deliberately not a lease kind, so two verify items on
  the same repository never serialize: parallelism across queued items is the
  point.
* ``workflow_ref`` — required, a full protocol commit: the item is authorized
  under the exact workflow revision the job was admitted under, and the
  evidence binds to it.
* ``payload`` — the pinned verification request: attempt id, source revision,
  patch bytes plus their sha256, declared files, named tests, suite command,
  scanner path. The patch is pinned at dispatch so the worker verifies exactly
  the artifact the coordinator accepted, and the evidence binds to the pin.

The evidence. The worker signs an HMAC-SHA256 over the canonical JSON of the
whole evidence body — schema, action id and key, ``fence_seq``, owner,
attempt id, source revision, workflow pin, and the complete verify record —
with a provisioning key supplied explicitly by the caller (same discipline as
the approval signing key: read from the Hermes secret scope in production,
never from the environment, a config file or the coordinator state directory).
Editing any signed field invalidates the evidence. The record inside is the
ordinary ``verify_and_commit`` record, so the merge gate
(:func:`bundlebees_plugin.queue.job_accepted_reasons`, ``integrate_job``)
cannot tell a worker-verified attempt from an in-beat one: same fields, same
pass/fail semantics, same scanner result.

Consumption. :func:`accept_verify_evidence` checks the signature, the binding
(attempt, source revision, workflow pin, patch pin, lease scope), the record
contract — exactly the record checks the merge gate applies — and re-proves
the git facts via :func:`verify_local.reprove_commit` without running any
test. Completion goes through the queue's fenced ``complete``, so a worker
superseded by an adoption is refused with the queue's own ``stale_fence`` and
replay is idempotent by ``action_key``.

Shadow mode. Production has exactly two modes: ``in-beat`` (the default — the
flag never switches on its own) and ``shadow``. ``on`` is refused with
``mode_not_wired`` and is not wired by the pipeline; the library keeps the
``on`` code path (:func:`dispatch_verification`, :func:`resolve_mode`) for its
tests only. Shadow runs BOTH paths on a bounded sample set: the in-beat
verdict stays authoritative, the worker path runs for real (record reuse and
receipt reuse disabled, so the execution is observed, not inherited), and the
two records are compared field by field. Every comparison is recorded durably;
a divergence raises the named ``shadow_divergence`` alarm and appends to
``alarms.jsonl`` — never a silent switch. The procedure lives in
``docs/verify-workers.md``: prove shadow equality on the full bounded set with
zero alarms and audit the comparisons; rollback is setting the flag back to
``in-beat``.

Prior art, recorded so it stays rejected: gutting the signed-evidence fixtures
to plain strings, or deleting the signature checks, to satisfy a credential
scanner would weaken the very equality contract the acceptance criteria pin.
Synthetic digest-shaped fixture values are built inertly instead (runs of one
repeated letter), so format assertions hold and no scanner rule fires.

Error text is passed through :func:`bundlebees_plugin.security.scrub`. Clocks
are injectable in every mutating call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import coordinator_actions, leases, security, verify_local

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock cannot bound the sample set
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "WIRED",
    "EVIDENCE_SCHEMA",
    "REQUEST_SCHEMA",
    "ACTION_KIND",
    "SHADOW_SCHEMA",
    "MODE_IN_BEAT",
    "MODE_SHADOW",
    "MODE_ON",
    "VERIFY_MODES",
    "DEFAULT_VERIFY_WORKERS_MODE",
    "SETTINGS_KEY",
    "SHADOW_SAMPLE_LIMIT",
    "MIN_KEY_BYTES",
    "RECORD_FIELDS",
    "COMPARABLE_FIELDS",
    "VOLATILE_FIELDS",
    "VerifyWorkersError",
    "worker_key",
    "sign_evidence",
    "evidence_signature_valid",
    "build_request",
    "verify_action_id",
    "verify_action_key",
    "enqueue_verify_action",
    "produce_verify_evidence",
    "publish_verify_result",
    "run_verify_action",
    "accept_verify_evidence",
    "compare_evidence",
    "run_shadow_comparison",
    "shadow_comparison",
    "shadow_alarms_present",
    "shadow_budget_used",
    "resolve_mode",
    "dispatch_verification",
]

#: Production reaches this module only through verify_shadow.observe, in shadow mode.
WIRED = "shadow"

EVIDENCE_SCHEMA = "bundlebees.verify-evidence/1"
REQUEST_SCHEMA = "bundlebees.verify-request/1"
SHADOW_SCHEMA = "bundlebees.verify-workers-shadow/1"

#: The queue kind for verification. Not a lease kind: verify items on the same
#: repository never block each other, which is what makes the stage parallel.
ACTION_KIND = "verify"

MODE_IN_BEAT = "in-beat"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VERIFY_MODES = (MODE_IN_BEAT, MODE_SHADOW, MODE_ON)

#: The config flag's default: the pipeline stays in-beat until shadow equality
#: is proven. The settings key is ``plugins.entries.bundlebees.settings.`` +
#: :data:`SETTINGS_KEY`; :func:`resolve_mode` reads it from a config mapping.
DEFAULT_VERIFY_WORKERS_MODE = MODE_IN_BEAT
SETTINGS_KEY = "verify_workers"

#: The bounded shadow sample set: at most this many dual-run comparisons per
#: coordinator state directory. Beyond it, shadow dispatch verifies in-beat
#: only and records the skip, instead of silently growing the dual-run set.
SHADOW_SAMPLE_LIMIT = 8

#: Minimum signing key material, matching the approval key's floor.
MIN_KEY_BYTES = 32

#: The exact field set of a ``verify_and_commit`` record (see
#: ``verify_local._record``). Worker evidence must carry exactly these fields:
#: that is the schema half of the evidence-equality contract.
RECORD_FIELDS = (
    "attempt_id",
    "ok",
    "sha",
    "branch",
    "base_rev",
    "files",
    "tests",
    "red",
    "suite",
    "gitleaks",
    "mode",
    "patch_sha256",
    "execution_identity",
    "resumed",
    "test_cache",
    "at",
)

#: Fields the shadow comparator requires to be equal: everything the merge
#: gate reads, plus the pass/fail semantics and the scanner result.
COMPARABLE_FIELDS = (
    "ok",
    "sha",
    "branch",
    "base_rev",
    "files",
    "tests",
    "red",
    "suite",
    "gitleaks",
    "mode",
    "patch_sha256",
    "resumed",
)

#: Fields excluded from equality, deliberately and by name. They describe
#: where and when a verification ran, not what it concluded: the worker bee is
#: a different execution environment, so its stack identity, cache counters
#: and timestamp differ from the in-beat run's by design. The comparator
#: records them in every comparison so the exclusion stays visible.
VOLATILE_FIELDS = ("execution_identity", "test_cache", "at")

_ATTEMPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_REV_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_WORKFLOW_RE = re.compile(r"[0-9a-f]{40}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_LABEL_MAX = 200

_WORKER_KWARGS = (
    "owner",
    "runner",
    "sandbox_backend",
    "sandbox_allow_unsafe_local",
    "sandbox_allow_weak",
    "sandbox_timeout",
    "ttl_s",
)

_FIX_ATTEMPT = "Use the attempt id the lease was recorded under."
_FIX_KEY = "Pass the provisioned signing key bytes explicitly; never a weak or absent key."
_FIX_MODE = "Set the flag to in-beat or shadow."
_FIX_REQUEST = "Build the request with build_request so every binding field is validated."
_FIX_STATE = "Inspect the shadow state by hand; a corrupt counter must not widen the sample set."


class VerifyWorkersError(Exception):
    """A verify-worker operation was refused. ``code`` names the refusal."""

    def __init__(self, code: str, message: str, fix: str = "", comparison: Any = None) -> None:
        super().__init__(security.scrub(message))
        self.code = code
        self.message = security.scrub(message)
        self.fix = security.scrub(fix)
        self.comparison = comparison

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# small validation and canonicalization helpers
# ---------------------------------------------------------------------------


def _attempt_id(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not _ATTEMPT_ID_RE.fullmatch(text):
        raise VerifyWorkersError("attempt_id_invalid", f"attempt id {text[:40]!r} is not a safe identifier", _FIX_ATTEMPT)
    return text


def _owner(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not text or len(text) > _LABEL_MAX or any(not (32 <= ord(ch) < 127) for ch in text):
        raise VerifyWorkersError("owner_invalid", f"owner {text[:40]!r} is not safe record text",
                                 "Name the bee with printable text, at most 200 characters.")
    return text


def _epoch(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    try:
        epoch = float(value)
    except (TypeError, ValueError) as exc:
        raise VerifyWorkersError("clock_invalid", f"now {value!r} is not epoch seconds", "Pass a clock the lane can read.") from exc
    if epoch != epoch or epoch in (float("inf"), float("-inf")):
        raise VerifyWorkersError("clock_invalid", f"now {value!r} is not a finite epoch", "Pass a real clock.")
    return epoch


def _iso(now: Any) -> str:
    return (
        datetime.fromtimestamp(_epoch(now), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VerifyWorkersError("evidence_invalid", f"evidence is not JSON-serializable: {exc}",
                                 "Pass evidence that survives a JSON round trip.") from exc


def _brief_value(value: Any) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return security.scrub(text)[:160]


def worker_key(signing_key: bytes | str | None) -> bytes:
    """Return the verify-worker signing key, refusing weak or absent material.

    Production callers pass the provisioned key read from the Hermes secret
    scope, exactly like the approval signing key; it is never read from the
    environment, a config file or the coordinator state directory here.
    """

    if signing_key is None:
        raise VerifyWorkersError("worker_key_missing", "no verify-worker signing key was supplied", _FIX_KEY)
    if isinstance(signing_key, str):
        data = signing_key.encode("utf-8")
    elif isinstance(signing_key, (bytes, bytearray)):
        data = bytes(signing_key)
    else:
        raise VerifyWorkersError("worker_key_invalid", "the verify-worker key must be bytes or text", _FIX_KEY)
    if len(data) < MIN_KEY_BYTES:
        raise VerifyWorkersError("worker_key_weak", f"the verify-worker key must be at least {MIN_KEY_BYTES} bytes of secret material", _FIX_KEY)
    return data


# ---------------------------------------------------------------------------
# signing
# ---------------------------------------------------------------------------


def sign_evidence(body: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    """Return the evidence body with an HMAC-SHA256 ``signature`` over it.

    The signature covers every field of the body — including the fence value,
    the owner and the complete verify record — so any edit invalidates it. A
    ``signature`` already present in the body is never signed; re-signing a
    body signs the body without the stale field.
    """

    key = worker_key(key)
    payload = {name: value for name, value in body.items() if name != "signature"}
    payload["signature"] = hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()
    return payload


def evidence_signature_valid(evidence: Mapping[str, Any], key: bytes) -> bool:
    """Whether the evidence's signature authenticates its exact bytes."""

    key = worker_key(key)
    body = {name: value for name, value in evidence.items() if name != "signature"}
    provided = evidence.get("signature")
    if not isinstance(provided, str) or not provided:
        return False
    expected = hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# the dispatch request and the queue item
# ---------------------------------------------------------------------------


def build_request(
    *,
    attempt_id: str,
    source_rev: str,
    workflow_ref: str,
    patch: str,
    declared_files,
    tests,
    suite_command=(),
    gitleaks: str,
    job_id: str = "",
    repo: str = "",
) -> dict[str, Any]:
    """Build and validate the pinned verification request for one attempt.

    Every binding field is validated here so a malformed item is refused at
    dispatch, never inside a worker sandbox. ``patch_sha256`` is computed from
    the patch bytes: the request pins the exact artifact the coordinator
    accepted, and the evidence binds to that pin.
    """

    attempt_id = _attempt_id(attempt_id)
    rev = source_rev if isinstance(source_rev, str) else ""
    if not _REV_RE.fullmatch(rev):
        raise VerifyWorkersError("source_rev_invalid", f"source_rev {rev[:40]!r} is not a full 40- or 64-hex revision",
                                 "Pin the exact source revision the lease binds to.")
    ref = workflow_ref if isinstance(workflow_ref, str) else ""
    if not ref:
        raise VerifyWorkersError("missing_workflow_ref",
                                 "workflow_ref is required: a verify item without its protocol commit is not an item",
                                 "Pin the exact workflow protocol commit the job is authorized under.")
    if not _WORKFLOW_RE.fullmatch(ref.lower()):
        raise VerifyWorkersError("invalid_workflow_ref", f"workflow_ref {ref[:40]!r} is not a full 40-hex commit",
                                 "Pin the exact workflow protocol commit the job is authorized under.")
    if not isinstance(patch, str) or not patch.strip():
        raise VerifyWorkersError("patch_invalid", "the request carries no patch to verify",
                                 "Dispatch only an attempt whose artifact was resolved.")
    files = [str(item) for item in (declared_files or []) if str(item or "").strip()]
    if not files:
        raise VerifyWorkersError("declared_files_invalid", "the request names no declared files",
                                 "Dispatch only an attempt whose lease names its files.")
    named = [str(item) for item in (tests or []) if str(item or "").strip()]
    if not named:
        raise VerifyWorkersError("tests_required",
                                 "this contract needs named tests and none were given",
                                 "Pass the contract's test targets; an empty list is not a pass.")
    suite = [str(item) for item in (suite_command or [])]
    scanner = str(gitleaks or "").strip()
    if not scanner:
        raise VerifyWorkersError("security_tool_missing",
                                 "the configured scanner is required for verification but is not available",
                                 "Install the scanner, or record an explicit exemption in the contract.")
    request = {
        "schema": REQUEST_SCHEMA,
        "attempt_id": attempt_id,
        "source_rev": rev,
        "workflow_ref": ref.lower(),
        "patch": patch,
        "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "declared_files": files,
        "tests": named,
        "suite_command": suite,
        "gitleaks": scanner,
        "job_id": str(job_id or ""),
        "repo": str(repo or ""),
    }
    return request


def _validate_request(value: Any) -> dict[str, Any]:
    """Validate a stored payload the way :func:`build_request` built it."""

    if not isinstance(value, Mapping) or value.get("schema") != REQUEST_SCHEMA:
        raise VerifyWorkersError("payload_invalid", "the action payload is not a verify request", _FIX_REQUEST)
    request = dict(value)
    request.pop("schema", None)
    rebuilt = build_request(
        attempt_id=request.get("attempt_id"),
        source_rev=request.get("source_rev"),
        workflow_ref=request.get("workflow_ref"),
        patch=request.get("patch"),
        declared_files=request.get("declared_files"),
        tests=request.get("tests"),
        suite_command=request.get("suite_command") or (),
        gitleaks=request.get("gitleaks"),
        job_id=request.get("job_id") or "",
        repo=request.get("repo") or "",
    )
    if request.get("patch_sha256") != rebuilt["patch_sha256"]:
        raise VerifyWorkersError("payload_invalid", "the pinned patch does not match its recorded sha256",
                                 "Re-dispatch the attempt; the pinned artifact changed after dispatch.")
    return rebuilt


def verify_action_id(attempt_id: str) -> str:
    """The queue action id for one attempt, inside the queue's safe alphabet."""

    attempt_id = _attempt_id(attempt_id)
    raw = f"verify-{attempt_id}"
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    return f"verify-{digest[:40]}"


def verify_action_key(attempt_id: str) -> str:
    """The canonical action key ``verify:<attempt_id>`` (idempotent replay)."""

    return f"verify:{_attempt_id(attempt_id)}"


def enqueue_verify_action(state: Any, request: Mapping[str, Any], *, check_lease: bool = True, now: Any = None) -> dict[str, Any]:
    """Queue one verify item; replaying the same request returns the record.

    The lease is checked at dispatch: a request whose declared files do not
    match the attempt's lease is refused before any worker sees it, so an
    abbreviated path list can never shrink the verified scope. Production
    dispatch always runs with the check on (the default); callers exercising
    queue mechanics without a dispatch-time lease may pass
    ``check_lease=False`` explicitly.
    """

    request = _validate_request(request)
    attempt_id = str(request["attempt_id"])
    if check_lease:
        try:
            lease_record = leases.load_lease(state, attempt_id)
            lease_scope = sorted(leases.lease_files(lease_record))
        except leases.LeaseError as exc:
            raise VerifyWorkersError(exc.code, exc.error, exc.fix) from exc
        if lease_scope != sorted(request["declared_files"]):
            raise VerifyWorkersError(
                "lease_scope_mismatch",
                f"declared files {sorted(request['declared_files'])} do not match the lease {lease_scope}",
                "Dispatch only the lease's exact file set; a partial list would verify a narrowed scope.",
            )
    try:
        record = coordinator_actions.enqueue(
            state,
            verify_action_id(attempt_id),
            action_key=verify_action_key(attempt_id),
            kind=ACTION_KIND,
            repo=str(request["repo"]),
            workflow_ref=request["workflow_ref"],
            payload=dict(request, schema=REQUEST_SCHEMA),
            now=now,
        )
    except coordinator_actions.ActionError as exc:
        if exc.code in ("action_key_conflict", "action_id_conflict"):
            raise VerifyWorkersError(
                "verify_request_changed",
                f"the verify item for {attempt_id} already exists with a different request",
                "One attempt has one verification request; re-dispatch a new attempt instead.",
            ) from exc
        raise VerifyWorkersError(exc.code, exc.message, exc.fix) from exc
    # The queue replays by key and shape only; the pinned payload is this
    # lane's binding, so a replay whose request changed is a refusal, never a
    # silent return of the old artifact.
    stored = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    if (
        str(stored.get("patch_sha256") or "") != str(request["patch_sha256"])
        or str(stored.get("source_rev") or "") != str(request["source_rev"])
        or [str(item) for item in stored.get("tests") or []] != [str(item) for item in request["tests"]]
        or [str(item) for item in stored.get("declared_files") or []] != [str(item) for item in request["declared_files"]]
    ):
        raise VerifyWorkersError(
            "verify_request_changed",
            f"the verify item for {attempt_id} already exists with a different request",
            "One attempt has one verification request; re-dispatch a new attempt instead.",
        )
    return record


# ---------------------------------------------------------------------------
# the worker bee side
# ---------------------------------------------------------------------------


def _acquire(state: Any, attempt_id: str, owner: str, *, ttl_s: float, now: Any) -> tuple[int, Any]:
    """Claim, adopt or reuse the queue claim for this bee; return its fence."""

    action_id = verify_action_id(attempt_id)
    try:
        taken = coordinator_actions.adopt(state, action_id, owner, ttl_s=ttl_s, now=now)
        return int(taken["fence_seq"]), taken.get("adopted_from")
    except coordinator_actions.ActionError as exc:
        if exc.code == "nothing_to_adopt":
            claimed = coordinator_actions.claim(state, action_id, owner, ttl_s=ttl_s, now=now)
            return int(claimed["fence_seq"]), None
        if exc.code == "claim_active":
            record = coordinator_actions.load(state, action_id) or {}
            if str(record.get("owner") or "") == owner:
                # Our own live claim from a crashed earlier pass: the fence we
                # hold is still current, so reuse it instead of stealing.
                return int(record.get("fence_seq") or 0), record.get("adopted_from")
        raise VerifyWorkersError(exc.code, exc.message, exc.fix) from exc


def produce_verify_evidence(
    state: Any,
    repo: Any,
    attempt_id: str,
    *,
    owner: str,
    key: bytes | str,
    runner: Any = None,
    sandbox_backend: str = "auto",
    sandbox_allow_unsafe_local: bool = False,
    sandbox_allow_weak: bool = False,
    sandbox_timeout: int = verify_local.DEFAULT_SANDBOX_TIMEOUT_S,
    reuse_record: bool = True,
    cacheable: bool = True,
    ttl_s: float = coordinator_actions.DEFAULT_TTL_S,
    now: Any = None,
) -> dict[str, Any]:
    """Run the verifier for one claimed verify item and sign its evidence.

    The bee holds the queue claim (claiming, adopting an expired claim, or
    reusing its own live claim from a crashed pass), runs the SAME verifier
    the coordinator ran in-beat — ``verify_local.verify_and_commit`` with the
    configured runner, the pinned patch, the lease's file set, the pinned
    tests and suite command and the configured scanner — and signs the
    resulting record together with the action identity and the fence value.
    The action is not completed here; :func:`publish_verify_result` does that,
    so a bee can be superseded between producing and publishing and be
    refused by the fence instead of overwriting the adopter's result.

    A verifier refusal is a legitimate conclusion: it is returned as a
    negative result (``ok`` false with the verifier's own code) and published
    as the action's result, exactly like an in-beat refusal.
    """

    state = Path(state)
    attempt_id = _attempt_id(attempt_id)
    owner = _owner(owner)
    key = worker_key(key)
    record = coordinator_actions.load(state, verify_action_id(attempt_id))
    if record is None:
        raise VerifyWorkersError("unknown_action", f"no verify item for {attempt_id}",
                                 "Dispatch the attempt first.")
    if record.get("status") == "done":
        raise VerifyWorkersError("action_done", f"the verify item for {attempt_id} is already done",
                                 "Replay the stored result; enqueue a new attempt for new work.")
    payload = _validate_request(record.get("payload"))
    fence, _adopted_from = _acquire(state, attempt_id, owner, ttl_s=ttl_s, now=now)

    try:
        lease_record = leases.load_lease(state, attempt_id)
        contract_declared = bool((lease_record.get("brief") or {}).get("contract"))
    except leases.LeaseError as exc:
        raise VerifyWorkersError(exc.code, exc.error, exc.fix) from exc

    def preflight(patch_files: list[str]) -> object:
        from . import acdd_cli

        return acdd_cli.verify_preflight(lease_record, state, patch_files)

    try:
        verify_record = verify_local.verify_and_commit(
            state,
            Path(repo),
            attempt_id,
            patch=str(payload["patch"]),
            declared_files=[str(item) for item in payload["declared_files"]],
            base_rev=str(payload["source_rev"]),
            tests=[str(item) for item in payload["tests"]],
            suite_command=[str(item) for item in payload["suite_command"]] or None,
            gitleaks=str(payload["gitleaks"]),
            require_gitleaks=True,
            expected_patch_sha256=str(payload["patch_sha256"]),
            acdd_preflight=preflight if contract_declared else None,
            contract_declared=contract_declared,
            runner=runner,
            sandbox_backend=sandbox_backend,
            sandbox_allow_unsafe_local=sandbox_allow_unsafe_local,
            sandbox_allow_weak=sandbox_allow_weak,
            sandbox_timeout=sandbox_timeout,
            cacheable=cacheable,
            reuse_record=reuse_record,
        )
    except verify_local.VerifyError as exc:
        return {
            "ok": False,
            "fence_seq": fence,
            "code": exc.code,
            "message": exc.message,
            "fix": exc.fix,
        }

    evidence = sign_evidence(
        {
            "schema": EVIDENCE_SCHEMA,
            "action_id": verify_action_id(attempt_id),
            "action_key": verify_action_key(attempt_id),
            "fence_seq": int(fence),
            "owner": owner,
            "attempt_id": attempt_id,
            "source_rev": str(payload["source_rev"]),
            "workflow_ref": str(record.get("workflow_ref") or ""),
            "job_id": str(payload["job_id"]),
            "repo": str(payload["repo"]),
            "record": verify_record,
        },
        key,
    )
    return {"ok": True, "fence_seq": int(fence), "evidence": evidence, "record": verify_record}


def _enqueue_research_for_failure(
    state: Any,
    attempt_id: str,
    produced: Mapping[str, Any],
    action_record: Mapping[str, Any],
) -> None:
    """Best-effort RESEARCH-BEFORE-RETRY trigger for a non-accepted verify.

    The verify result is already durably completed by the caller. This helper
    never raises and never mutates that result: an enqueue refusal is itself a
    typed ``queue_unavailable`` answer, and the failure record written here
    still marks the failure as needing research.
    """

    try:
        from . import research_gate
    except Exception:  # noqa: BLE001 - the verify record is already written
        return

    payload = action_record.get("payload") if isinstance(action_record, Mapping) else {}
    payload = payload if isinstance(payload, Mapping) else {}
    result = action_record.get("result") if isinstance(action_record, Mapping) else {}
    result = result if isinstance(result, Mapping) else {}
    failure: dict[str, Any] = {
        "kind": "verify",
        "code": str(produced.get("code") or result.get("code") or "verify_error"),
        "message": str(produced.get("message") or result.get("message") or ""),
        "fix": str(produced.get("fix") or result.get("fix") or ""),
        "attempt_id": attempt_id,
        "failing_tests": produced.get("failing_tests") or payload.get("tests") or [],
        "versions": produced.get("versions") or {},
        "repo": str(payload.get("repo") or ""),
    }
    try:
        lease_record = leases.load_lease(state, attempt_id)
        retry_of = (lease_record.get("brief") or {}).get("retry_of")
        if retry_of:
            failure["retry_of"] = str(retry_of)
    except Exception:  # noqa: BLE001 - lineage falls back to the attempt id
        pass

    workflow_ref = str((action_record.get("workflow_ref") if isinstance(action_record, Mapping) else "") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", workflow_ref):
        workflow_ref = None

    outcome: Mapping[str, Any] | None = None
    try:
        outcome = research_gate.enqueue_research(
            state,
            failure,
            repo=str(payload.get("repo") or ""),
            workflow_ref=workflow_ref,
        )
    except Exception:  # noqa: BLE001 - never let research enqueue escape the trigger
        outcome = None
    if not isinstance(outcome, Mapping) or not outcome.get("ok"):
        # The queue may be unavailable; still leave durable evidence that this
        # failure needs research. Both writes are best-effort.
        try:
            research_gate.record_failure(state, failure)
        except Exception:  # noqa: BLE001 - the verify record remains authoritative
            pass


def publish_verify_result(state: Any, attempt_id: str, produced: Mapping[str, Any], *, now: Any = None) -> dict[str, Any]:
    """Complete the verify item with the produced result, on its fence.

    The queue enforces the fence: a worker superseded by an adoption is
    refused with ``stale_fence`` and its result is discarded, never written
    over the adopter's.
    """

    attempt_id = _attempt_id(attempt_id)
    if not isinstance(produced, Mapping) or "fence_seq" not in produced:
        raise VerifyWorkersError("produce_invalid", "nothing was produced for this verify item",
                                 "Produce the verification result before publishing it.")
    if produced.get("ok"):
        evidence = produced.get("evidence")
        if not isinstance(evidence, Mapping):
            raise VerifyWorkersError("produce_invalid", "the produced evidence is missing",
                                     "Produce the verification result before publishing it.")
        result: dict[str, Any] = {
            "ok": True,
            "worker": str(evidence.get("owner") or ""),
            "commit": str((evidence.get("record") or {}).get("sha") or ""),
            "verify_evidence": dict(evidence),
        }
    else:
        result = {
            "ok": False,
            "code": str(produced.get("code") or "verify_error"),
            "message": security.scrub(str(produced.get("message") or "")),
            "fix": security.scrub(str(produced.get("fix") or "")),
        }
    try:
        record = coordinator_actions.complete(
            state,
            verify_action_id(attempt_id),
            int(produced["fence_seq"]),
            result=result,
            now=now,
        )
    except coordinator_actions.ActionError as exc:
        raise VerifyWorkersError(exc.code, exc.message, exc.fix) from exc
    if not result["ok"]:
        # RESEARCH-BEFORE-RETRY trigger: one research item per failure shape
        # and lineage. It runs after the verify record is written and never
        # changes that record.
        _enqueue_research_for_failure(state, attempt_id, produced, record)
    return {"ok": bool(result["ok"]), "result": result, "action": record,
            "evidence": result.get("verify_evidence")}


def run_verify_action(
    state: Any,
    repo: Any,
    attempt_id: str,
    *,
    owner: str,
    key: bytes | str,
    runner: Any = None,
    sandbox_backend: str = "auto",
    sandbox_allow_unsafe_local: bool = False,
    sandbox_allow_weak: bool = False,
    sandbox_timeout: int = verify_local.DEFAULT_SANDBOX_TIMEOUT_S,
    reuse_record: bool = True,
    cacheable: bool = True,
    ttl_s: float = coordinator_actions.DEFAULT_TTL_S,
    now: Any = None,
) -> dict[str, Any]:
    """The verify-worker bee's one-call flow: claim, verify, sign, publish.

    A done action replays its stored result without re-running anything
    (idempotent by the queue's ``action_key``); anything else is produced and
    published on this bee's fence.
    """

    state = Path(state)
    attempt_id = _attempt_id(attempt_id)
    existing = coordinator_actions.load(state, verify_action_id(attempt_id))
    if existing is None:
        raise VerifyWorkersError("unknown_action", f"no verify item for {attempt_id}",
                                 "Dispatch the attempt first.")
    if existing.get("status") == "done":
        result = existing.get("result") if isinstance(existing.get("result"), Mapping) else {}
        return {
            "ok": bool(result.get("ok")),
            "replayed": True,
            "result": dict(result),
            "action": existing,
            "evidence": result.get("verify_evidence"),
        }
    produced = produce_verify_evidence(
        state,
        repo,
        attempt_id,
        owner=owner,
        key=key,
        runner=runner,
        sandbox_backend=sandbox_backend,
        sandbox_allow_unsafe_local=sandbox_allow_unsafe_local,
        sandbox_allow_weak=sandbox_allow_weak,
        sandbox_timeout=sandbox_timeout,
        reuse_record=reuse_record,
        cacheable=cacheable,
        ttl_s=ttl_s,
        now=now,
    )
    published = publish_verify_result(state, attempt_id, produced, now=now)
    return dict(published, replayed=False)


# ---------------------------------------------------------------------------
# the coordinator side
# ---------------------------------------------------------------------------


def _evidence_contract_problems(record: Mapping[str, Any], attempt_id: str) -> list[str]:
    """The record checks the merge gate applies, as named problems."""

    problems: list[str] = []
    fields = set(record)
    missing = sorted(set(RECORD_FIELDS) - fields)
    unexpected = sorted(fields - set(RECORD_FIELDS))
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected fields: {', '.join(unexpected)}")
    if record.get("ok") is not True:
        problems.append("not accepted")
    sha = str(record.get("sha") or "")
    if not _SHA_RE.fullmatch(sha):
        problems.append("no full commit sha")
    if str(record.get("branch") or "") != f"bb-verify/{attempt_id}":
        problems.append("wrong branch")
    if not str(record.get("base_rev") or ""):
        problems.append("no base revision")
    red = record.get("red")
    if not isinstance(red, Mapping) or red.get("exit_code") in (None, 0) or red.get("timed_out"):
        problems.append("no red evidence")
    suite = record.get("suite")
    if not isinstance(suite, Mapping) or suite.get("exit_code") != 0 or suite.get("timed_out"):
        problems.append("regression suite did not pass")
    scan = record.get("gitleaks")
    if not isinstance(scan, Mapping) or scan.get("exit_code") != 0:
        problems.append("scanner did not pass")
    if record.get("mode") != "commit-local":
        problems.append("wrong mode")
    if record.get("resumed") is not False:
        problems.append("resumed record")
    return problems


def accept_verify_evidence(
    state: Any,
    repo: Any,
    attempt_id: str,
    evidence: Mapping[str, Any],
    *,
    key: bytes | str,
    now: Any = None,
) -> dict[str, Any]:
    """Check signed worker evidence and consume it for one attempt.

    The checks, in order: the evidence schema and signature (any edit
    invalidates it), the action identity, the binding — attempt id, source
    revision, workflow pin, patch pin, declared tests and file set, and the
    lease's own file set — the record contract (exactly the record checks the
    merge gate applies), and a git re-proof of the claimed commit without
    running any test. Only then is the action completed through the queue's
    fenced ``complete``: a superseded worker is refused with ``stale_fence``,
    and replaying an already-accepted evidence is idempotent.
    """

    state = Path(state)
    repo = Path(repo)
    key = worker_key(key)
    attempt_id = _attempt_id(attempt_id)
    if not isinstance(evidence, Mapping):
        raise VerifyWorkersError("evidence_invalid", "no worker evidence was supplied",
                                 "Consume the evidence the bee returned.")
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        raise VerifyWorkersError("evidence_schema_invalid",
                                 f"evidence schema {evidence.get('schema')!r} is not {EVIDENCE_SCHEMA!r}",
                                 "Consume evidence this lane produced.")
    if not isinstance(evidence.get("signature"), str) or not evidence["signature"]:
        raise VerifyWorkersError("evidence_unsigned", "the worker evidence carries no signature",
                                 "Refuse unsigned verification evidence.")
    if not evidence_signature_valid(evidence, key):
        raise VerifyWorkersError("evidence_signature_invalid",
                                 "the worker evidence does not authenticate its bytes",
                                 "Refuse the evidence; do not consume a forged or edited record.")

    record = coordinator_actions.load(state, verify_action_id(attempt_id))
    if record is None:
        raise VerifyWorkersError("unknown_action", f"no verify item for {attempt_id}",
                                 "Dispatch the attempt first.")
    if evidence.get("action_id") != record.get("action_id") or evidence.get("action_key") != record.get("action_key"):
        raise VerifyWorkersError("evidence_binding_mismatch",
                                 f"the evidence names {evidence.get('action_id')!r}, not this action",
                                 "Refuse evidence that does not bind to the claimed action.")
    payload = _validate_request(record.get("payload"))
    verify_record = evidence.get("record")
    if not isinstance(verify_record, Mapping):
        raise VerifyWorkersError("evidence_invalid", "the worker evidence carries no verify record",
                                 "Refuse evidence without its record.")

    if str(verify_record.get("attempt_id") or "") != attempt_id or str(evidence.get("attempt_id") or "") != attempt_id:
        raise VerifyWorkersError("evidence_binding_mismatch",
                                 f"the evidence names attempt {evidence.get('attempt_id')!r}, not {attempt_id!r}",
                                 "Refuse evidence bound to another attempt.")
    if str(evidence.get("source_rev") or "") != str(payload["source_rev"]) or str(verify_record.get("base_rev") or "") != str(payload["source_rev"]):
        raise VerifyWorkersError("evidence_source_rev_mismatch",
                                 f"the evidence binds to {evidence.get('source_rev')!r}, not the pinned {payload['source_rev']!r}",
                                 "Refuse evidence verified against another revision.")
    pin = str(record.get("workflow_ref") or "")
    if not pin or str(evidence.get("workflow_ref") or "") != pin:
        raise VerifyWorkersError("evidence_workflow_ref_mismatch",
                                 f"the evidence binds to pin {evidence.get('workflow_ref')!r}, not the action's {pin!r}",
                                 "Refuse evidence executed under another protocol revision.")
    if str(verify_record.get("patch_sha256") or "") != str(payload["patch_sha256"]):
        raise VerifyWorkersError("evidence_patch_mismatch",
                                 "the evidence binds to another patch than the pinned one",
                                 "Refuse evidence about a different artifact.")
    if [str(item) for item in verify_record.get("tests") or []] != [str(item) for item in payload["tests"]]:
        raise VerifyWorkersError("evidence_tests_mismatch",
                                 "the evidence was earned by other tests than the pinned ones",
                                 "Refuse evidence about a different test selection.")
    files = sorted(str(item) for item in verify_record.get("files") or [])
    if files != sorted(str(item) for item in payload["declared_files"]):
        raise VerifyWorkersError("evidence_scope_mismatch",
                                 f"the evidence covers {files}, not the declared scope",
                                 "Refuse evidence about a different file set.")
    try:
        lease_record = leases.load_lease(state, attempt_id)
        lease_scope = sorted(leases.lease_files(lease_record))
    except leases.LeaseError as exc:
        raise VerifyWorkersError(exc.code, exc.error, exc.fix) from exc
    if files != lease_scope:
        raise VerifyWorkersError("evidence_lease_mismatch",
                                 f"the evidence covers {files}, not the lease {lease_scope}",
                                 "Refuse evidence about a narrowed or widened scope.")

    problems = _evidence_contract_problems(verify_record, attempt_id)
    if problems:
        raise VerifyWorkersError("evidence_contract_violation",
                                 f"worker evidence fails the record contract: {'; '.join(problems)}",
                                 "Refuse evidence the merge gate would reject.")
    fence = evidence.get("fence_seq")
    if isinstance(fence, bool) or not isinstance(fence, int):
        raise VerifyWorkersError("evidence_invalid", "the worker evidence carries no fence value",
                                 "Refuse evidence that does not bind to a claim.")
    reproof = verify_local.reprove_commit(repo, verify_record)
    if not reproof["ok"]:
        raise VerifyWorkersError("evidence_commit_mismatch",
                                 f"the claimed commit does not re-prove: {', '.join(reproof['problems'])}",
                                 "Refuse evidence about a commit that is not in the repository.")

    result: dict[str, Any] = {
        "ok": True,
        "worker": str(evidence.get("owner") or ""),
        "commit": str(verify_record.get("sha") or ""),
        "verify_evidence": dict(evidence),
    }
    status = record.get("status")
    if status == "queued":
        raise VerifyWorkersError("not_claimed", f"the verify item for {attempt_id} was never claimed",
                                 "A verify-worker bee must claim the item first.")
    if status == "claimed":
        try:
            updated = coordinator_actions.complete(
                state, verify_action_id(attempt_id), int(fence), result=result, now=now
            )
        except coordinator_actions.ActionError as exc:
            raise VerifyWorkersError(exc.code, exc.message, exc.fix) from exc
        return {"ok": True, "replayed": False, "evidence": dict(evidence),
                "record": dict(verify_record), "action": updated}
    # done: idempotent replay of the exact accepted evidence, anything else is
    # a conflict with the conclusion already recorded.
    stored = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    stored_evidence = stored.get("verify_evidence") if isinstance(stored, Mapping) else None
    if isinstance(stored_evidence, Mapping) and stored.get("ok") and _canonical(stored_evidence) == _canonical(dict(evidence)):
        return {"ok": True, "replayed": True, "evidence": dict(evidence),
                "record": dict(verify_record), "action": record}
    raise VerifyWorkersError("evidence_conflict",
                             f"the verify item for {attempt_id} is already done with a different result",
                             "One attempt has one verification conclusion; re-dispatch a new attempt.")


# ---------------------------------------------------------------------------
# shadow comparison
# ---------------------------------------------------------------------------


def compare_evidence(in_beat: Any, worker: Any, *, repo: Any = None) -> dict[str, Any]:
    """Compare an in-beat record with a worker record field by field.

    The schema half of the equality contract is exact field-set equality; the
    semantic half compares :data:`COMPARABLE_FIELDS`. With a repository, the
    ``sha`` comparison is semantic: two commits are equal evidence when both
    exist, parent the pinned base and carry the record's file set — committer
    time makes byte equality across two runs impossible even for identical
    content. :data:`VOLATILE_FIELDS` are excluded from equality by name and
    recorded in every comparison so the exclusion stays visible.
    """

    left = in_beat if isinstance(in_beat, Mapping) else None
    right = worker if isinstance(worker, Mapping) else None
    excluded = list(VOLATILE_FIELDS)
    if left is None or right is None:
        return {
            "equal": False,
            "schema_equal": False,
            "divergences": [{"field": "record", "in_beat": _brief_value(left), "worker": _brief_value(right)}],
            "compared": [],
            "excluded": excluded,
        }
    divergences: list[dict[str, Any]] = []
    left_fields, right_fields = set(left), set(right)
    schema_equal = left_fields == right_fields
    if not schema_equal:
        divergences.append({
            "field": "(schema)",
            "in_beat": sorted(left_fields - right_fields),
            "worker": sorted(right_fields - left_fields),
        })
    compared: list[str] = []
    for field in COMPARABLE_FIELDS:
        if field not in left or field not in right:
            continue
        compared.append(field)
        if field == "sha" and repo is not None:
            if not (_reproves(repo, left) and _reproves(repo, right)):
                divergences.append({
                    "field": field,
                    "in_beat": _brief_value(left.get(field)),
                    "worker": _brief_value(right.get(field)),
                })
            continue
        if left[field] != right[field]:
            divergences.append({"field": field, "in_beat": _brief_value(left[field]), "worker": _brief_value(right[field])})
    return {
        "equal": not divergences,
        "schema_equal": schema_equal,
        "divergences": divergences,
        "compared": compared,
        "excluded": excluded,
    }


def _reproves(repo: Any, record: Mapping[str, Any]) -> bool:
    try:
        return bool(verify_local.reprove_commit(Path(repo), record)["ok"])
    except Exception:  # noqa: BLE001 - a repo that cannot be read cannot prove equality
        return False


def _shadow_root(state: Any) -> Path:
    return Path(state) / "verify-workers" / "shadow"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


@contextmanager
def _locked(state: Any) -> Iterator[None]:
    if fcntl is None:  # pragma: no cover - no portable fallback for a bounded sample set
        raise VerifyWorkersError(
            "locking_unavailable",
            "this platform has no flock, so the shadow sample set cannot be bounded",
            "Run the coordinator on POSIX (Linux/macOS).",
        )
    lock = _shadow_root(state) / "shadow.lock"
    _private_dir(lock.parent)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def shadow_budget_used(state: Any) -> int:
    """How many shadow samples this state directory has already spent."""

    path = _shadow_root(state) / "budget.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise VerifyWorkersError("shadow_state_unreadable", f"the shadow budget cannot be read: {exc}", _FIX_STATE) from exc
    except ValueError as exc:
        raise VerifyWorkersError("shadow_state_corrupt", f"the shadow budget is not valid JSON: {exc}", _FIX_STATE) from exc
    used = document.get("used") if isinstance(document, dict) else None
    if document.get("schema") != SHADOW_SCHEMA or isinstance(used, bool) or not isinstance(used, int) or used < 0:
        raise VerifyWorkersError("shadow_state_corrupt", "the shadow budget does not record a usable counter", _FIX_STATE)
    return int(used)


def _claim_shadow_sample(state: Any, limit: int) -> tuple[bool, int]:
    with _locked(state):
        used = shadow_budget_used(state)
        if used >= max(0, int(limit)):
            return False, used
        path = _shadow_root(state) / "budget.json"
        document = {"schema": SHADOW_SCHEMA, "kind": "budget", "used": used + 1}
        tmp = path.with_name(f".budget.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True, used + 1


def _record_json(state: Any, relative: str, document: Mapping[str, Any]) -> Path:
    root = _shadow_root(state)
    _private_dir(root)
    path = root / relative
    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def shadow_comparison(state: Any, attempt_id: str) -> dict[str, Any] | None:
    """The stored shadow comparison for one attempt, if one was recorded."""

    path = _shadow_root(state) / "comparisons" / f"{_attempt_id(attempt_id)}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise VerifyWorkersError("shadow_state_corrupt", f"the shadow comparison is unreadable: {exc}", _FIX_STATE) from exc
    return document if isinstance(document, dict) else None


def shadow_alarms_present(state: Any) -> bool:
    """Whether the shadow log holds at least one divergence alarm."""

    path = _shadow_root(state) / "alarms.jsonl"
    try:
        return path.exists() and path.read_text(encoding="utf-8").strip() != ""
    except OSError:
        return False


def run_shadow_comparison(
    state: Any,
    attempt_id: str,
    *,
    in_beat_record: Any,
    worker_record: Any,
    repo: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Record the shadow comparison; alarm loudly on divergence.

    The comparison is always written durably under the shadow directory. An
    unequal comparison additionally appends to ``alarms.jsonl`` and raises
    ``shadow_divergence`` — the alarm is durable and named, never a silent
    observation, and never a switch: the in-beat verdict stays authoritative.
    """

    attempt_id = _attempt_id(attempt_id)
    comparison = compare_evidence(in_beat_record, worker_record, repo=repo)
    document = dict(
        comparison,
        schema=SHADOW_SCHEMA,
        kind="comparison",
        attempt_id=attempt_id,
        in_beat=_brief_value(in_beat_record),
        worker=_brief_value(worker_record),
        at=_iso(now),
    )
    path = _record_json(state, f"comparisons/{attempt_id}.json", document)
    document["comparison_path"] = str(path)
    if not document["equal"]:
        names = ", ".join(str(entry.get("field")) for entry in document["divergences"]) or "unknown"
        alarm = {
            "schema": SHADOW_SCHEMA,
            "kind": "shadow_divergence",
            "attempt_id": attempt_id,
            "divergences": document["divergences"],
            "at": document["at"],
        }
        alarms = _shadow_root(state) / "alarms.jsonl"
        _private_dir(alarms.parent)
        with open(alarms, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(alarm, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        raise VerifyWorkersError(
            "shadow_divergence",
            f"shadow verification diverged on {attempt_id}: {names}",
            "Inspect the comparison and the alarms before any mode switch; the in-beat verdict stays authoritative.",
            comparison=document,
        )
    return document


# ---------------------------------------------------------------------------
# the mode flag and the both-paths dispatch
# ---------------------------------------------------------------------------


def resolve_mode(mode: Any = None, *, config: Any = None) -> str:
    """Resolve the verify-workers mode; the default stays in-beat.

    An explicit ``mode`` wins; otherwise the documented settings key
    (:data:`SETTINGS_KEY`) is read from a config mapping; otherwise the
    default (:data:`DEFAULT_VERIFY_WORKERS_MODE`) applies.
    """

    value = mode
    if value is None and isinstance(config, Mapping):
        value = config.get(SETTINGS_KEY)
    if value is None:
        return DEFAULT_VERIFY_WORKERS_MODE
    text = str(value).strip().lower()
    if text not in VERIFY_MODES:
        raise VerifyWorkersError("mode_invalid", f"verify-workers mode {value!r} is not one of {', '.join(VERIFY_MODES)}", _FIX_MODE)
    return text


def _as_verdict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {"ok": False, "evidence": _brief_value(value)}


def _record_from_verdict(state: Any, attempt_id: str, verdict: Mapping[str, Any]) -> Any:
    if "attempt_id" in verdict and "sha" in verdict:
        return verdict
    if verdict.get("ok") is not True:
        return None
    record = verify_local.read_record(state, attempt_id)
    return record if isinstance(record, Mapping) else None


def _verdict_from_worker_result(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("ok"):
        evidence = result.get("evidence") or {}
        verify_record = evidence.get("record") or {}
        owner = str(evidence.get("owner") or "")
        sha = str(verify_record.get("sha") or "")
        return {"ok": True, "evidence": f"commit {sha[:12]} verified by verify-worker {owner}"}
    return {"ok": False, "evidence": f"{result.get('code')}: {result.get('message')}"}


def _worker_config(worker: Any) -> dict[str, Any]:
    if not isinstance(worker, Mapping):
        raise VerifyWorkersError("worker_missing", "no verify-worker configuration was supplied",
                                 "Name the bee and its runner so the worker path can run.")
    owner = _owner(worker.get("owner"))
    unknown = sorted(set(worker) - set(_WORKER_KWARGS))
    if unknown:
        raise VerifyWorkersError("worker_config_invalid", f"unknown verify-worker settings: {', '.join(unknown)}",
                                 "Pass only the settings run_verify_action accepts.")
    return dict(worker, owner=owner)


def dispatch_verification(
    state: Any,
    repo: Any,
    request: Mapping[str, Any],
    *,
    key: bytes | str,
    worker: Any = None,
    in_beat: Any = None,
    mode: Any = None,
    config: Any = None,
    shadow_limit: int = SHADOW_SAMPLE_LIMIT,
    now: Any = None,
) -> dict[str, Any]:
    """Run one attempt's verification under the configured mode.

    ``in-beat`` (the default) calls the serial verifier exactly as before.
    ``shadow`` runs the in-beat verifier first — its verdict stays
    authoritative — then enqueues the verify item, runs the worker bee for
    real (record reuse and receipt reuse disabled, so the execution is
    observed rather than inherited), accepts its evidence and compares the two
    records on the bounded sample set. ``on`` runs only the worker path and
    returns its verdict in the same shape the pipeline's verifier returns.
    """

    state = Path(state)
    repo = Path(repo)
    resolved = resolve_mode(mode, config=config)
    if not isinstance(request, Mapping):
        raise VerifyWorkersError("request_invalid", "no verification request was supplied", _FIX_REQUEST)
    request = _validate_request(request)
    attempt_id = str(request["attempt_id"])

    if resolved == MODE_IN_BEAT:
        if in_beat is None:
            raise VerifyWorkersError("in_beat_missing", "the in-beat verifier is required for this mode",
                                     "Pass the pipeline's serial verifier closure.")
        return {"path": MODE_IN_BEAT, "mode": resolved, "verdict": _as_verdict(in_beat())}

    worker_kwargs = _worker_config(worker)
    if resolved == MODE_ON:
        action = enqueue_verify_action(state, request, now=now)
        result = run_verify_action(state, repo, attempt_id, key=key, now=now, **worker_kwargs)
        payload: dict[str, Any] = {
            "path": MODE_ON,
            "mode": resolved,
            "action_id": action["action_id"],
            "verdict": _verdict_from_worker_result(result),
            "shadow_alarms_present": shadow_alarms_present(state),
        }
        return payload

    if in_beat is None:
        raise VerifyWorkersError("in_beat_missing", "the in-beat verifier is required for shadow mode",
                                 "Pass the pipeline's serial verifier closure.")
    used = shadow_budget_used(state)
    if used >= max(0, int(shadow_limit)):
        return {
            "path": MODE_IN_BEAT,
            "mode": resolved,
            "verdict": _as_verdict(in_beat()),
            "shadow": {"skipped": "budget_exhausted", "used": used, "limit": int(shadow_limit)},
        }
    verdict = _as_verdict(in_beat())
    allowed, used = _claim_shadow_sample(state, shadow_limit)
    in_beat_record = _record_from_verdict(state, attempt_id, verdict)
    action = enqueue_verify_action(state, request, now=now)
    worker_result = run_verify_action(
        state, repo, attempt_id, key=key, now=now,
        reuse_record=False, cacheable=False, **worker_kwargs,
    )
    worker_record: Any = None
    worker_refusal: dict[str, Any] | None = None
    if worker_result.get("ok"):
        try:
            accepted = accept_verify_evidence(state, repo, attempt_id, worker_result["evidence"], key=key, now=now)
            worker_record = accepted["record"]
        except VerifyWorkersError as exc:
            worker_refusal = exc.to_dict()
    else:
        worker_refusal = {"code": str(worker_result.get("code") or "verify_error"),
                          "message": security.scrub(str(worker_result.get("message") or ""))}
    try:
        comparison = run_shadow_comparison(
            state, attempt_id, in_beat_record=in_beat_record, worker_record=worker_record, repo=repo, now=now
        )
        alarm = False
    except VerifyWorkersError as exc:
        if exc.code != "shadow_divergence":
            raise
        comparison = dict(getattr(exc, "comparison", None) or {})
        comparison["alarm"] = True
        alarm = True
    if worker_refusal is not None:
        comparison["worker_refusal"] = worker_refusal
    return {
        "path": MODE_IN_BEAT,
        "mode": resolved,
        "action_id": action["action_id"],
        "verdict": verdict,
        "shadow": dict(comparison, alarm=alarm, used=used, limit=int(shadow_limit)),
    }
