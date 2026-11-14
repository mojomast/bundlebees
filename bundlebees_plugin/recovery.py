"""Durable, evidence-based recovery for a rejected task (``rizzstack.recovery/1``).

A rejection is not a retry licence. The reconciler records *why* an attempt was
rejected as a durable **request**, and a separate, mechanically validated
**decision** authorises exactly one next step. Nothing in this module asks a
model what to do, and nothing dispatches on the strength of a prose note.

The protocol is owned by the pinned workflow checkout, not by this module:

* ``tools/recovery.py`` and ``schemas/recovery.schema.json`` under
  ``WORKFLOW_SRC`` are the only validator and schema used. They are loaded at
  request creation, at decision admission and again when a decision is applied;
  when the checkout is missing, unreadable or not a git checkout, every
  recovery operation fails closed with ``recovery_protocol_unavailable``. A
  recovery that cannot be validated by the protocol it claims is not written
  at all.
* the failure class and code are derived here from recorded evidence, never
  from an adapter's opinion, and are normalised onto the protocol's sixteen
  codes (``test_unresolved`` and its synonyms become ``test_not_found``). The
  original observation is preserved verbatim in the evidence artefact and is
  named in the rationale.
* the envelope is derived from the sealed authorization plus the job and task
  ``recovery_policy`` (which may only narrow it), and is always an integer
  budget: ``contract_corrections_allowed`` is ``0`` or ``1``, never a boolean.
* a request carries exactly the protocol's fields. Nothing volatile is in the
  document, so the digest is stable and a re-read can prove it.
* a decision carries exactly the protocol's fields. It has no digest of its
  own: it binds the request through ``request_digest``. A supersession revises
  the *same* contract id and changes only its reviewed hash.

Durable layout, one directory per job::

    recovery/<job_id>/<task_id>.<n>.request.json
    recovery/<job_id>/<task_id>.<n>.decision.json
    recovery/<job_id>/<task_id>.<n>.<kind>.<sha>.evidence.json
    recovery/<job_id>/<task_id>.<n>.contract.json         # only after a supersede
    recovery/<job_id>/<task_id>.<n>.request.legacy.json   # a pre-conformance label

Requests and decisions are create-only, so a repeated tick re-reads the document
that already exists instead of writing a second one; applying a decision is
idempotent because the *job document* records which request it applied.

Pre-conformance records written before this module spoke the canonical protocol
carry a boolean correction budget, string evidence, a ``number``/``attempts_used``
/``created_at`` block, a decision digest, or a revisioned ``new_contract_id``.
They are never rewritten. They are labelled in a sidecar
(``<record>.legacy.json``) as ``legacy``/``authoritative: false``, and every
load, decision and dispatch path refuses them with ``recovery_record_legacy``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import contract as acdd
from . import job as jobs
from .contract import ContractError

#: The one protocol version this module emits and accepts.
SCHEMA = "rizzstack.recovery/1"
#: A decision is the same protocol version, not a private sub-version.
DECISION_SCHEMA = SCHEMA
#: The durable artefact a request's evidence entries point at.
EVIDENCE_SCHEMA = "rizzstack.recovery-evidence/1"
#: The sidecar that labels a pre-conformance record without editing it.
LEGACY_SCHEMA = "rizzstack.recovery-legacy/1"

#: The pinned workflow checkout that owns the validator.
WORKFLOW_ENV = "WORKFLOW_SRC"
PROTOCOL_RELATIVE = Path("tools") / "recovery.py"
PROTOCOL_SCHEMA_RELATIVE = Path("schemas") / "recovery.schema.json"

#: The five failure classes, most specific first. The order is the precedence
#: used when one evidence string names several codes.
CLASSES: tuple[str, ...] = (
    "specification",
    "environment",
    "infrastructure",
    "integration",
    "implementation",
)

#: The protocol's codes, one class each.
CANONICAL_CODES: dict[str, tuple[str, ...]] = {
    "implementation": ("test_failed", "suite_failed", "lease_violation", "gitleaks_failed"),
    "specification": ("acceptance_uncovered", "test_not_found", "missing_test"),
    "environment": ("toolchain_missing", "dependency_unavailable", "network_blocked"),
    "infrastructure": ("launcher_unavailable", "scheduler_stalled", "storage_full"),
    "integration": ("merge_conflict", "combined_suite_failed", "wave_rejected"),
}

#: Specification codes: every one means "the acceptance selection is unusable",
#: which is the only thing a supersession may repair.
SELECTION_CODES = frozenset(CANONICAL_CODES["specification"])

#: The only three decisions a record may carry.
DECISIONS: tuple[str, ...] = ("retry", "supersede", "escalate")

#: Recovery directories live beside ``jobs/`` in the plugin state directory.
RECOVERY_DIR = "recovery"

#: v1 caps every envelope at five total attempts and one correction.
MAX_ATTEMPTS_CAP = 5
MAX_CORRECTIONS = 1

#: The gate state an exhausted budget always produces.
GATE_AWAITING_APPROVAL = "awaiting_approval"
GATE_WAITING = "waiting"

#: Default ceiling for worker attempts per task (the first attempt plus one
#: corrective retry). An authorization may lower or raise the ceiling; a job or
#: task policy may only lower it, and v1 caps it at five.
DEFAULT_MAX_ATTEMPTS_PER_TASK = 2

#: The class that can never dispatch, and the class that can never retry.
_NO_DISPATCH = frozenset({"environment", "infrastructure"})
_NO_RETRY = frozenset({"specification"})

#: The mechanical components validate; they do not reason. A decision naming one
#: of them is refused so the orchestrator cannot approve its own recovery.
RESERVED_ADAPTERS = frozenset({"bundlebees", "reconciler", "validator"})

#: Internal observation codes, grouped onto the protocol's canonical code that
#: means the same thing. The internal vocabulary is kept because the rest of the
#: plugin records it; the protocol sees only the right-hand side.
_CODE_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # implementation: the patch did not satisfy the approved tests
    ("implementation", "test_failed", (
        "test_failed", "tests_failed", "tests_required", "green_failed", "verification_failed",
        "worker_reported_failure", "artifact_mismatch", "artifact_refused", "no_patch",
        "worker_patch_missing", "patch_invalid", "patch_conflict", "commit_failed",
        "commit_parent_mismatch", "tests_tampered",
    )),
    ("implementation", "suite_failed", ("suite_failed",)),
    ("implementation", "lease_violation", (
        "lease_violation", "scope_violation", "commit_scope_mismatch", "lease_binding_failed",
    )),
    ("implementation", "gitleaks_failed", ("gitleaks_failed", "secrets_found")),
    # specification: the acceptance selection itself is unusable
    ("specification", "test_not_found", (
        "test_not_found", "test_unresolved", "test_ref_unknown", "unknown_test",
        "tests_uncollectable", "collection_required",
    )),
    ("specification", "missing_test", (
        "missing_test", "tests_undeclared", "acceptance_test_undeclared",
    )),
    ("specification", "acceptance_uncovered", (
        "acceptance_uncovered", "acceptance_invalid", "red_missing", "red_invalid",
        "specification_invalid", "spec_mismatch", "contract_invalid", "contract_not_frozen",
        "contract_changed", "contract_missing", "contract_source_rev_malformed",
        "contract_source_rev_unreachable",
    )),
    # environment: the worker host lacked a tool, dependency or route
    ("environment", "toolchain_missing", (
        "toolchain_missing", "security_tool_missing", "tool_missing", "python_missing",
        "git_error", "permission_denied",
    )),
    ("environment", "dependency_unavailable", (
        "dependency_unavailable", "sandbox_unavailable", "isolation_unavailable",
        "environment_unavailable", "worktree_missing", "no_workdir", "evidence_unreadable",
        "timeout", "verify_timeout",
    )),
    ("environment", "network_blocked", ("network_blocked", "network_error")),
    # infrastructure: the launcher, scheduler or storage failed
    ("infrastructure", "launcher_unavailable", (
        "launcher_unavailable", "dispatch_rejected", "dispatch_ambiguous",
        "dispatch_uncertain_exhausted", "attempt_missing", "receipt_missing",
        "receipt_mismatch", "provisioning_failed", "provisioning_unbound",
        "worker_failed", "worker_crashed", "worker_cancelled", "worker_lost",
        "transport_error", "internal_error", "job_invalid", "job_missing",
        "state_unreadable", "unclassified_failure",
    )),
    ("infrastructure", "scheduler_stalled", ("scheduler_stalled",)),
    ("infrastructure", "storage_full", ("storage_full", "disk_full")),
    # integration: verified patches did not combine
    ("integration", "merge_conflict", (
        "merge_conflict", "integration_failed", "integration_error", "integrate_failed",
    )),
    ("integration", "combined_suite_failed", ("combined_suite_failed", "suite_conflict")),
    ("integration", "wave_rejected", ("wave_rejected", "wave_incomplete")),
)

#: ``code -> (canonical class, canonical code)``.
_CODE_TABLE: dict[str, tuple[str, str]] = {}
for _class, _canonical, _codes in _CODE_GROUPS:
    for _code in _codes:
        if _code in _CODE_TABLE:  # pragma: no cover - import-time self check
            raise RuntimeError(f"evidence code {_code!r} is mapped twice")
        _CODE_TABLE[_code] = (_class, _canonical)

#: Strong phrases used only when the evidence carries no recognised code. The
#: canonical code is the least-specific honest member of the guessed class.
_KEYWORDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "specification",
        "test_not_found",
        (
            "not in scope.test_files",
            "matches no collected test",
            "no such test",
            "test not found",
            "unknown test",
            "does not exist at",
            "not collected",
        ),
    ),
    (
        "environment",
        "toolchain_missing",
        (
            "gitleaks",
            "not found on path",
            "sandbox",
            "isolation",
            "timed out",
            "timeout",
            "permission denied",
            "no space",
        ),
    ),
    (
        "infrastructure",
        "launcher_unavailable",
        ("launcher", "connection", "network", "unavailable", "worker", "crashed"),
    ),
    ("integration", "merge_conflict", ("integration", "merge", "wave", "conflict")),
    (
        "implementation",
        "test_failed",
        ("test failed", "tests failed", "suite failed", "does not apply", "scope", "secret"),
    ),
)

#: Evidence kind that satisfies each class. It is derived from the class, never
#: from the adapter's record.
_EVIDENCE_KIND: dict[str, str] = {
    "implementation": "test",
    "specification": "test",
    "environment": "environment",
    "infrastructure": "infrastructure",
    "integration": "conflict",
}

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX_RECOVERY = (
    "Inspect the recorded request and decision under recovery/<job>/; do not hand-edit "
    "either file, and do not re-dispatch a rejected task without a validated decision."
)
_FIX_PROTOCOL = (
    "Check out the pinned workflow revision (tools/recovery.py and schemas/recovery.schema.json) "
    "and set WORKFLOW_SRC to it; recovery is refused while the protocol cannot be loaded."
)
_MAX_TEXT = 2000


class RecoveryError(Exception):
    """A recovery request, decision or application was refused.

    ``code`` is a stable refusal name so a caller can tell a forged
    classification from an exhausted budget.
    """

    def __init__(self, code: str, message: str, fix: str = _FIX_RECOVERY) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# The pinned protocol validator
# ---------------------------------------------------------------------------


_PROTOCOL: dict[str, Any] = {}


def workflow_src() -> Path:
    """The pinned workflow checkout that owns the recovery protocol."""

    raw = os.environ.get(WORKFLOW_ENV, "").strip()
    if not raw:
        raise RecoveryError(
            "recovery_protocol_unavailable",
            f"{WORKFLOW_ENV} is not set, so the pinned recovery validator cannot be loaded",
            _FIX_PROTOCOL,
        )
    path = Path(raw)
    if not path.is_dir():
        raise RecoveryError(
            "recovery_protocol_unavailable",
            f"{WORKFLOW_ENV}={raw!r} is not a directory",
            _FIX_PROTOCOL,
        )
    return path


def _protocol_head(src: Path) -> str:
    """The exact commit the checkout is at; recovery is refused without one."""

    try:
        done = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError(
            "recovery_protocol_unavailable",
            f"the pinned workflow checkout at {src} cannot be resolved: {exc}",
            _FIX_PROTOCOL,
        ) from exc
    ref = done.stdout.strip()
    if done.returncode != 0 or len(ref) != 40 or any(ch not in "0123456789abcdef" for ch in ref):
        raise RecoveryError(
            "recovery_protocol_unavailable",
            f"the pinned workflow checkout at {src} is not a git checkout: {done.stderr.strip()[:200]}",
            _FIX_PROTOCOL,
        )
    return ref


def protocol_validator():
    """Load the protocol validator from the pinned checkout, or fail closed.

    ``WORKFLOW_SRC`` *is* the pin: it must be a git checkout, so the exact
    commit it is at is recorded with the validator and every protocol failure
    points at a revision. The job's ``workflow_ref`` is deliberately not
    consulted — it names the feature-packet protocol revision, which is a
    different contract from the recovery protocol, and conflating the two would
    refuse recovery for every job admitted under an older packet revision.
    """

    src = workflow_src()
    module_path = src / PROTOCOL_RELATIVE
    schema_path = src / PROTOCOL_SCHEMA_RELATIVE
    for required in (module_path, schema_path):
        if not required.is_file():
            raise RecoveryError(
                "recovery_protocol_unavailable",
                f"the pinned workflow protocol is incomplete: {required} is missing",
                _FIX_PROTOCOL,
            )
    try:
        stat = module_path.stat()
    except OSError as exc:  # pragma: no cover - a raced deletion is still unavailable
        raise RecoveryError(
            "recovery_protocol_unavailable", f"{module_path} cannot be inspected: {exc}", _FIX_PROTOCOL
        ) from exc

    head = _protocol_head(src)
    key = f"{module_path}|{stat.st_mtime_ns}|{stat.st_size}|{head}"
    cached = _PROTOCOL.get("module")
    if cached is not None and _PROTOCOL.get("key") == key:
        return cached

    spec = importlib.util.spec_from_file_location("rizzstack_recovery_protocol", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - unloadable file
        raise RecoveryError(
            "recovery_protocol_unavailable", f"cannot load the validator at {module_path}", _FIX_PROTOCOL
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any load failure is unavailable, not a verdict
        raise RecoveryError(
            "recovery_protocol_unavailable",
            f"the pinned validator at {module_path} cannot be loaded: {type(exc).__name__}: {exc}",
            _FIX_PROTOCOL,
        ) from exc
    for name in ("validate_request", "validate_decision", "validate_pair"):
        if not callable(getattr(module, name, None)):
            raise RecoveryError(
                "recovery_protocol_unavailable",
                f"the pinned validator at {module_path} does not expose {name}()",
                _FIX_PROTOCOL,
            )
    supported = tuple(getattr(module, "SUPPORTED", ()) or ())
    if SCHEMA not in supported:
        raise RecoveryError(
            "recovery_protocol_unavailable",
            f"the pinned validator does not implement {SCHEMA}; it supports {supported!r}",
            _FIX_PROTOCOL,
        )
    _PROTOCOL.update(key=key, module=module, commit=head)
    return module


def _pinned(module: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call one pinned validator entry point and translate its refusal.

    The pinned ``RecoveryError`` is a different class; its ``code``/``message``
    are the refusal and are preserved so a caller sees the protocol's own name.
    Anything else is a validator defect: recovery fails closed either way.
    """

    entry = getattr(module, name)
    try:
        return entry(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - both branches refuse; neither dispatches
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            fix = str(getattr(exc, "fix", "") or _FIX_PROTOCOL)
            message = str(getattr(exc, "message", "") or exc)
            raise RecoveryError(code, message, fix) from exc
        raise RecoveryError(
            "recovery_protocol_error",
            f"the pinned {name}() raised {type(exc).__name__}: {exc}",
            _FIX_PROTOCOL,
        ) from exc


# ---------------------------------------------------------------------------
# Canonical bytes, digests and durable files
# ---------------------------------------------------------------------------


def canonical_bytes(value: Any) -> bytes:
    """The exact bytes a digest is taken over: sorted keys, no whitespace."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def request_digest(request: Mapping[str, Any]) -> str:
    """SHA-256 over the request body excluding ``request_digest``."""

    body = {key: value for key, value in request.items() if key != "request_digest"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def decision_digest(decision: Mapping[str, Any]) -> str:
    """SHA-256 over the whole canonical decision, for journaling only.

    A decision carries no digest field: it binds the request through
    ``request_digest``. This helper exists so a coordinator can journal the
    decision it stored, not so the document can prove anything about itself.
    """

    return hashlib.sha256(canonical_bytes(dict(decision))).hexdigest()


def _safe_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 96
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise RecoveryError(
            "identifier_invalid",
            f"{what} {text!r} is not a safe path component",
            "Use letters, digits, '-', '_' and '.' only, at most 96 characters.",
        )
    return text


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    text = str(value if value is not None else "").strip()
    return text[:limit]


def recovery_dir(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / RECOVERY_DIR / _safe_id(job_id, "job_id")


def request_path(state_dir: Path, job_id: str, task_id: str, number: int) -> Path:
    return recovery_dir(state_dir, job_id) / f"{_safe_id(task_id, 'task_id')}.{int(number)}.request.json"


def decision_path(state_dir: Path, job_id: str, task_id: str, number: int) -> Path:
    return recovery_dir(state_dir, job_id) / f"{_safe_id(task_id, 'task_id')}.{int(number)}.decision.json"


def contract_path(state_dir: Path, job_id: str, task_id: str, number: int) -> Path:
    return recovery_dir(state_dir, job_id) / f"{_safe_id(task_id, 'task_id')}.{int(number)}.contract.json"


def request_id_for(job_id: str, task_id: str, number: int) -> str:
    return f"{_safe_id(job_id, 'job_id')}.{_safe_id(task_id, 'task_id')}.{int(number)}"


def _ordinal_from_path(path: Path) -> int:
    """The ``<n>`` in ``<task>.<n>.request.json``; 0 when the name is not one."""

    name = path.name
    for suffix in (".request.json", ".decision.json"):
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            break
    else:
        return 0
    try:
        return int(stem.rsplit(".", 1)[-1])
    except ValueError:
        return 0


def _write_bytes(path: Path, payload: bytes, *, replace: bool) -> bool:
    """Publish *payload* atomically; create-only unless ``replace`` is true.

    Create-only is what makes requests and decisions exactly-once: two ticks
    that observe the same rejection race on ``os.link`` and exactly one wins.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(tmp, path)
            return True
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except OSError:
            try:
                direct = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                return False
            with os.fdopen(direct, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes(path, canonical_bytes(dict(value)), replace=True)


def _create_only(path: Path, value: Mapping[str, Any]) -> bool:
    """Publish a document only if it does not exist yet (atomic on POSIX)."""

    return _write_bytes(path, canonical_bytes(dict(value)), replace=False)


def _read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RecoveryError(f"{what}_missing", f"{what} {path.name} does not exist") from exc
    except OSError as exc:
        raise RecoveryError(f"{what}_unreadable", f"{what} {path.name} cannot be read: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RecoveryError(f"{what}_corrupt", f"{what} {path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise RecoveryError(f"{what}_corrupt", f"{what} {path.name} is not a JSON object")
    return data


# ---------------------------------------------------------------------------
# Pre-conformance records: detect, label, never rewrite
# ---------------------------------------------------------------------------


def legacy_reasons(document: Mapping[str, Any], kind: str) -> list[str]:
    """Why a durable record predates the canonical protocol; empty when it does not.

    Only the markers the pre-conformance producer actually wrote are named. A
    document that is merely wrong (an unknown version, an unknown field, a bad
    digest) is *not* legacy: it is refused by the validator under its own code.
    """

    reasons: list[str] = []
    if kind == "request":
        for key in ("number", "attempts_used", "created_at"):
            if key in document:
                reasons.append(f"request carries the pre-conformance field {key!r}")
        failure = document.get("failure")
        if isinstance(failure, Mapping) and isinstance(failure.get("evidence"), str):
            reasons.append("failure.evidence is a pre-conformance string, not an array of records")
        envelope = document.get("envelope")
        if isinstance(envelope, Mapping):
            value = envelope.get("contract_corrections_allowed")
            if isinstance(value, bool):
                # Booleans were the pre-conformance spelling of the budget. An
                # out-of-range integer is not legacy: it is a shape the protocol
                # refuses, so the validator names it.
                reasons.append(
                    "envelope.contract_corrections_allowed is a pre-conformance boolean, "
                    "not an integer budget"
                )
        if "decision_digest" in document:
            reasons.append("a request can never carry decision_digest")
    else:
        if str(document.get("schema") or "") == "rizzstack.recovery-decision/1":
            reasons.append("decision schema is the pre-conformance 'rizzstack.recovery-decision/1'")
        if "decision_digest" in document:
            reasons.append("decision carries the pre-conformance decision_digest field")
        supersedes = document.get("supersedes")
        if isinstance(supersedes, Mapping):
            contract_id = str(supersedes.get("contract_id") or "")
            new_id = str(supersedes.get("new_contract_id") or "")
            if contract_id and new_id and new_id != contract_id:
                reasons.append(
                    "supersedes.new_contract_id is a pre-conformance revisioned identity: a "
                    "correction revises the same contract"
                )
    return reasons


def is_legacy_request(document: Mapping[str, Any]) -> bool:
    return bool(legacy_reasons(document, "request"))


def is_legacy_decision(document: Mapping[str, Any]) -> bool:
    return bool(legacy_reasons(document, "decision"))


def legacy_label_path(path: Path) -> Path:
    if not path.name.endswith(".json"):
        raise RecoveryError("legacy_label_invalid", f"{path.name} is not a JSON record")
    return path.with_name(path.name[: -len(".json")] + ".legacy.json")


def label_legacy(state_dir: Path, path: Path, document: Mapping[str, Any], kind: str,
                 reasons: Sequence[str] | None = None) -> dict[str, Any]:
    """Archive-label a pre-conformance record without touching it.

    The label is a create-only sidecar naming the record, its digest and every
    reason it is not authoritative. The record bytes are never rewritten: the
    archived document stays exactly what the earlier producer wrote.
    """

    found = list(reasons) if reasons is not None else legacy_reasons(document, kind)
    try:
        record_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RecoveryError("legacy_label_unreadable", f"{path.name} cannot be read: {exc}") from exc
    label = {
        "schema": LEGACY_SCHEMA,
        "kind": kind,
        "record": path.name,
        "record_sha256": record_sha,
        "classification": "legacy",
        "authoritative": False,
        "reasons": sorted(found),
    }
    target = legacy_label_path(path)
    if not _create_only(target, label):
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = label
        if isinstance(existing, dict):
            return existing
    return label


def legacy_records(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """Every archived pre-conformance record for one job, with its label."""

    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.legacy.json")):
        try:
            label = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(label, dict):
            label = dict(label)
            label["label_path"] = f"{RECOVERY_DIR}/{job_id}/{path.name}"
            found.append(label)
    return found


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def class_for_code(code: Any) -> str | None:
    """The canonical class an evidence code belongs to, or ``None``."""

    entry = _CODE_TABLE.get(str(code or "").strip().lower())
    return entry[0] if entry else None


def canonical_code_for(code: Any) -> str | None:
    """The protocol code the observation maps to, or ``None``."""

    entry = _CODE_TABLE.get(str(code or "").strip().lower())
    return entry[1] if entry else None


def _record_text(record: Any) -> str:
    if isinstance(record, str):
        return record
    if isinstance(record, Mapping):
        parts: list[str] = []
        for key in (
            "code",
            "error",
            "failure_code",
            "reason",
            "evidence",
            "detail",
            "message",
            "state",
            "status",
        ):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        return " | ".join(parts)
    if record is None:
        return ""
    return str(record)


def _tokens(record: Any) -> list[str]:
    """Evidence codes named by a record, in order, de-duplicated."""

    out: list[str] = []
    if isinstance(record, Mapping):
        for key in ("code", "error", "failure_code"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                out.append(value.strip().lower())
        for key in ("state", "status"):
            value = record.get(key)
            if isinstance(value, str) and value.strip().lower() in {"failed", "error", "cancelled", "canceled"}:
                out.append("worker_cancelled" if value.strip().lower().startswith("cancel") else "worker_failed")
        for key in ("evidence", "detail", "message", "reason"):
            value = record.get(key)
            if isinstance(value, str):
                out.extend(_leading_token(value))
    else:
        out.extend(_leading_token(_record_text(record)))
    seen: list[str] = []
    for token in out:
        if token and token not in seen:
            seen.append(token)
    return seen


def _leading_token(text: str) -> list[str]:
    """``"tests_failed: ..."`` -> ``["tests_failed"]``; free text -> ``[]``."""

    head = text.strip().splitlines()[0] if text.strip() else ""
    token = head.split(":", 1)[0].strip().lower()
    if token and len(token) <= 64 and all(ch.isalnum() or ch == "_" for ch in token):
        if token[0].isalpha() and token in _CODE_TABLE:
            return [token]
    return []


_TEST_ID = re.compile(r"[A-Za-z0-9_./-]+\.py::[A-Za-z0-9_:\[\].-]+")


def _test_ids_in(text: str) -> list[str]:
    """Every ``path/to/test.py::Test::test_x`` shaped id mentioned in *text*."""

    found: list[str] = []
    for match in _TEST_ID.finditer(text):
        candidate = match.group(0).strip().rstrip(".,;:")
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def contract_tests(contract: Mapping[str, Any] | None) -> tuple[set[str], set[str]]:
    """The declared test ids and the approved test files of a frozen contract."""

    if not isinstance(contract, Mapping):
        return set(), set()
    ids: set[str] = set()
    files: set[str] = set()
    scope = contract.get("scope") if isinstance(contract.get("scope"), Mapping) else {}
    for item in scope.get("test_files") or []:
        if isinstance(item, str) and item.strip():
            files.add(item.strip())
    for criterion in contract.get("acceptance_criteria") or []:
        if not isinstance(criterion, Mapping):
            continue
        for item in criterion.get("tests") or []:
            if isinstance(item, str) and item.strip():
                ids.add(item.strip())
                files.add(item.strip().split("::", 1)[0])
    return ids, files


def _article(name: str) -> str:
    return "an" if name[:1] in "aeiou" else "a"


def _failure(failure_class: str, code: str, evidence: str, rationale: str) -> dict[str, Any]:
    return {
        "class": failure_class,
        "code": code,
        "evidence": _text(evidence),
        "rationale": _text(rationale),
    }


def classify_failure(record: Any, *, contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Attribute a rejection to one of :data:`CLASSES` from its evidence codes.

    ``record`` may be a verify verdict (``{"ok": False, "evidence": "code: why"}``),
    a launcher attempt entry, a plain string, or an already-classified failure
    mapping. The class is never taken on trust: when a mapping already names a
    class, the code must still belong to that class or the classification is
    refused (``classification_forged``).

    The returned code is always one of the protocol's sixteen. The observation
    that produced it is preserved in ``evidence`` and named in ``rationale``, so
    normalising ``test_unresolved`` to ``test_not_found`` hides nothing.

    An unrecognised record is ``infrastructure``/``scheduler_stalled``: the
    coordinator could not attribute the failure, so it must not dispatch a
    worker on the strength of a guess. The protocol has no "unclassified" code;
    a stalled scheduler is the honest description of "no next step could be
    scheduled", and the class keeps the record non-dispatching.
    """

    evidence = _record_text(record)
    if isinstance(record, Mapping):
        declared = record.get("class")
        declared_code = record.get("code")
        if isinstance(declared, str) and declared.strip():
            name = declared.strip().lower()
            if name not in CLASSES:
                raise RecoveryError("classification_forged", f"{name!r} is not one of {', '.join(CLASSES)}")
            code = str(declared_code or "").strip().lower()
            entry = _CODE_TABLE.get(code)
            if entry is None:
                raise RecoveryError(
                    "classification_forged",
                    f"class {name!r} claims evidence code {code!r}, which is not a known code",
                )
            if entry[0] != name:
                raise RecoveryError(
                    "classification_forged",
                    f"class {name!r} disagrees with evidence code {code!r} ({entry[0]})",
                )
            rationale = str(record.get("rationale") or "").strip() or (
                f"evidence code {code!r} is recorded as {_article(name)} {name} failure "
                f"(protocol code {entry[1]})"
            )
            return _failure(name, entry[1], evidence, rationale)

    tokens = _tokens(record)
    for name in CLASSES:
        for token in tokens:
            if class_for_code(token) == name:
                entry = _CODE_TABLE[token]
                return _failure(
                    name,
                    entry[1],
                    evidence,
                    f"evidence code {token!r} is recorded as {_article(name)} {name} failure "
                    f"(protocol code {entry[1]})",
                )

    lowered = evidence.lower()
    for name, code, phrases in _KEYWORDS:
        for phrase in phrases:
            if phrase in lowered:
                return _failure(
                    name,
                    code,
                    evidence,
                    f"no recognised evidence code; the text names {phrase!r}, which is "
                    f"{_article(name)} {name} symptom, so it is recorded as {code}",
                )

    declared_ids, test_files = contract_tests(contract)
    if contract is not None:
        for test_id in _test_ids_in(evidence):
            if test_id not in declared_ids and test_id.split("::", 1)[0] in test_files:
                return _failure(
                    "specification",
                    "missing_test",
                    evidence,
                    f"the evidence names {test_id}, which lives in an approved test file but is not "
                    "declared by the frozen contract: the acceptance selection is unusable (missing_test)",
                )

    return _failure(
        "infrastructure",
        "scheduler_stalled",
        evidence,
        "no recognised evidence code and no attributable cause; the coordinator could not schedule "
        "a next step from this record, so it is recorded as a stalled scheduler and refuses to "
        "dispatch a worker on a guess",
    )


def _normalise_failure(failure: Any, contract: Mapping[str, Any] | None) -> dict[str, Any]:
    """Accept a classification or a raw record and always return a canonical failure."""

    return classify_failure(failure, contract=contract)


# ---------------------------------------------------------------------------
# Envelope and authorization
# ---------------------------------------------------------------------------


def _explicit_envelope(authorization: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(authorization, Mapping):
        return {}
    for key in ("recovery", "recovery_envelope"):
        block = authorization.get(key)
        if isinstance(block, Mapping):
            return dict(block)
    budget = authorization.get("budget")
    if isinstance(budget, Mapping) and "max_attempts_per_task" in budget:
        return {"max_attempts_per_task": budget.get("max_attempts_per_task")}
    if "max_attempts_per_task" in authorization:
        return {"max_attempts_per_task": authorization.get("max_attempts_per_task")}
    return {}


def _positive_int(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RecoveryError("envelope_invalid", f"{what} must be a positive integer, not {value!r}")
    return value


def _correction_budget(value: Any, where: str) -> int | None:
    """Read one correction flag as a 0/1 budget, or ``None`` when it is absent."""

    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and 0 <= value <= MAX_CORRECTIONS:
        return value
    raise RecoveryError(
        "envelope_invalid",
        f"{where}.contract_corrections_allowed must be a boolean or the integer 0 or 1, not {value!r}",
    )


def _authorized_tests(contract: Mapping[str, Any] | None, task: Mapping[str, Any]) -> list[str]:
    """The exact test ids a correction may name, derived only from approvals.

    The protocol requires every corrected test to be *identical* to an entry of
    ``corrected_tests_allowed``: a decision selects from the pre-authorized
    replacements, it does not invent acceptance. Without an explicit list, the
    only selections an approval already names are the contract's declared test
    ids and the task's own acceptance tests — which is why a correction normally
    needs the approver to enumerate its replacements.
    """

    allowed: set[str] = set()
    ids, _files = contract_tests(contract)
    allowed.update(ids)
    acceptance = task.get("acceptance")
    if isinstance(acceptance, Mapping):
        for item in acceptance.get("tests") or []:
            if isinstance(item, str) and item.strip():
                allowed.add(item.strip())
    return sorted(allowed)


def _policy_tests(source: Any, where: str) -> list[str] | None:
    if not isinstance(source, Mapping) or "corrected_tests_allowed" not in source:
        return None
    value = source.get("corrected_tests_allowed")
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise RecoveryError(
            "envelope_invalid", f"{where}.corrected_tests_allowed must be a list of test selections"
        )
    return sorted({item.strip() for item in value})



def _narrow_tests(base: list[str] | None, narrower: list[str] | None) -> list[str] | None:
    """A policy may only narrow the tests an authorization already allows."""

    if base is None:
        return narrower
    if narrower is None:
        return base
    return sorted(set(base) & set(narrower))


def envelope_for(
    job: Mapping[str, Any], task: Mapping[str, Any], authorization: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The bounded envelope this job may act inside.

    The authorization sets the ceiling; the job and the task may only narrow it.
    The result is already in the protocol's types: an integer attempt ceiling
    and an integer 0/1 correction budget. The final narrowing to "this failure
    may actually use a correction" happens in :func:`_final_envelope`.
    """

    explicit = _explicit_envelope(authorization)
    ceiling = DEFAULT_MAX_ATTEMPTS_PER_TASK
    if "max_attempts_per_task" in explicit:
        ceiling = _positive_int(explicit.get("max_attempts_per_task"), "max_attempts_per_task")
    policies = (job.get("recovery_policy"), task.get("recovery_policy"))
    for source in policies:
        if isinstance(source, Mapping) and "max_attempts_per_task" in source:
            narrow = _positive_int(source.get("max_attempts_per_task"), "max_attempts_per_task")
            ceiling = min(ceiling, narrow)
    for source in (job, task):
        value = source.get("max_attempts")
        if value is not None:
            ceiling = min(ceiling, _positive_int(value, "max_attempts"))
    ceiling = max(1, min(int(ceiling), MAX_ATTEMPTS_CAP))

    # The authorization is a hard ceiling; an explicit deny there cannot be
    # overridden. When it says nothing, the job/task policy may grant, because
    # those were approved as part of admitting the job — never an adapter, and
    # never an implicit default.
    authorized = _correction_budget(explicit.get("contract_corrections_allowed"), "authorization.recovery")
    policy_values = [
        _correction_budget(source.get("contract_corrections_allowed"), "recovery_policy")
        for source in policies
        if isinstance(source, Mapping) and "contract_corrections_allowed" in source
    ]
    if authorized == 0:
        corrections = 0
    elif authorized == 1:
        corrections = 0 if any(value == 0 for value in policy_values) else 1
    else:
        if any(value == 0 for value in policy_values):
            corrections = 0
        else:
            corrections = 1 if any(value == 1 for value in policy_values) else 0

    allowed = _policy_tests(explicit, "authorization.recovery")
    allowed = _narrow_tests(allowed, _policy_tests(job.get("recovery_policy"), "recovery_policy"))
    allowed = _narrow_tests(allowed, _policy_tests(task.get("recovery_policy"), "recovery_policy"))
    contract = task.get("contract") if isinstance(task.get("contract"), Mapping) else None
    if allowed is None:
        tests = _authorized_tests(contract, task)
    else:
        tests = list(allowed)
    return {
        "max_attempts_per_task": int(ceiling),
        "contract_corrections_allowed": int(corrections),
        "corrected_tests_allowed": list(tests),
    }


def _final_envelope(
    envelope: Mapping[str, Any], *, permitted: bool, failure_class: str
) -> dict[str, Any]:
    """Narrow the envelope to what *this* failure may actually consume.

    The protocol refuses an envelope that grants a correction the request does
    not permit, and refuses a test list for every class that may not change
    acceptance. The budget is therefore kept only when a supersession is
    permitted; an integration repair may name the pre-authorized tests without
    a correction budget.
    """

    attempts = max(1, min(int(envelope.get("max_attempts_per_task") or 1), MAX_ATTEMPTS_CAP))
    budget = int(envelope.get("contract_corrections_allowed") or 0)
    corrections = 1 if permitted and budget >= 1 else 0
    tests = list(envelope.get("corrected_tests_allowed") or []) if (permitted or failure_class == "integration") else []
    return {
        "max_attempts_per_task": int(attempts),
        "contract_corrections_allowed": int(corrections),
        "corrected_tests_allowed": sorted({str(item) for item in tests}),
    }


def test_authorized(selection: Any, allowed: Sequence[str]) -> bool:
    """True when *selection* is exactly one of the pre-authorized replacements.

    The protocol compares a corrected test to ``corrected_tests_allowed`` by
    identity: a file path does not authorise an arbitrary id inside it, because
    that would let a decision choose acceptance the approver never named.
    """

    if not isinstance(selection, str) or not selection.strip():
        return False
    return selection.strip() in {str(entry).strip() for entry in allowed}


def remaining_attempts(request: Mapping[str, Any], task: Mapping[str, Any] | None = None) -> int:
    """Attempts still allowed for this task, floored at zero.

    A live task's attempt list is the ledger. Without one (the deterministic
    adapter only has the request) the request still proves at least one attempt
    was consumed — the failed attempt it names — so that is the conservative
    lower bound. The authoritative budget check at decision admission always
    uses the task's ledger.
    """

    try:
        ceiling = int((request.get("envelope") or {}).get("max_attempts_per_task") or 0)
    except (TypeError, ValueError):
        ceiling = 0
    if isinstance(task, Mapping) and "attempts" in task:
        used = len([item for item in (task.get("attempts") or []) if item])
    else:
        used = 1
    return max(0, ceiling - used)


def attributed(request: Mapping[str, Any]) -> bool:
    """True when the failure was attributed to a canonical evidence code.

    The protocol has no unclassified code, so an unattributable record is
    recorded as ``scheduler_stalled`` and is attributed: it may be escalated to
    a human (never retried), which is the only honest outcome left.
    """

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    code = str(failure.get("code") or "")
    name = str(failure.get("class") or "")
    return name in CANONICAL_CODES and code in CANONICAL_CODES[name]


def exhausted(request: Mapping[str, Any], task: Mapping[str, Any] | None = None) -> bool:
    """True when no worker attempt may be dispatched under this request."""

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    if str(failure.get("class") or "") in _NO_DISPATCH:
        return True
    return remaining_attempts(request, task) <= 0


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _feature_id(state_dir: Path, job: Mapping[str, Any]) -> str:
    """The feature this task belongs to.

    A workflow-native job carries the packet's feature id. A legacy job has no
    packet, and the canonical request still needs a non-empty identity: the job
    id is the feature carrier then, because one job admits one feature.
    """

    explicit = job.get("feature_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        from . import packets  # noqa: PLC0415 - only packet jobs carry a feature id

        raw = packets.read_packet(Path(state_dir), str(job.get("job_id") or ""))
        if raw:
            document = json.loads(raw)
            if isinstance(document, Mapping):
                value = str(document.get("feature_id") or "").strip()
                if value:
                    return value
    except Exception:  # noqa: BLE001 - a missing packet is not a recovery problem
        pass
    return str(job.get("job_id") or "")


def _contract_block(task: Mapping[str, Any]) -> dict[str, str]:
    contract = task.get("contract") if isinstance(task.get("contract"), Mapping) else {}
    return {
        "id": str(contract.get("id") or task.get("contract_id") or ""),
        "sha256": str(task.get("contract_sha256") or ""),
    }


def _supersession_history(job: Mapping[str, Any], task: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    recorded = task.get("contract_supersession")
    if isinstance(recorded, Mapping):
        found.append(dict(recorded))
    job_block = job.get("recovery") if isinstance(job.get("recovery"), Mapping) else {}
    for entry in job_block.get("supersessions") or []:
        if isinstance(entry, Mapping) and str(entry.get("task_id") or "") == str(task.get("task_id") or ""):
            found.append(dict(entry))
    return found


def supersession_permitted(
    job: Mapping[str, Any], task: Mapping[str, Any], failure: Mapping[str, Any], envelope: Mapping[str, Any]
) -> bool:
    """A supersession is permitted only once, only for an unusable selection."""

    if int(envelope.get("contract_corrections_allowed") or 0) < 1:
        return False
    if str(failure.get("class") or "") != "specification":
        return False
    if str(failure.get("code") or "") not in SELECTION_CODES:
        return False
    if not _contract_block(task).get("sha256"):
        return False
    return not _supersession_history(job, task)


def _scan_requests(
    state_dir: Path, job_id: str, task_id: str
) -> tuple[list[tuple[int, Path, dict[str, Any]]], list[tuple[Path, dict[str, Any]]]]:
    """The canonical requests and the archived pre-conformance requests for a task.

    Legacy records are labelled here, once, and never returned as canonical.
    """

    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return [], []
    canonical: list[tuple[int, Path, dict[str, Any]]] = []
    legacy: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(base.glob(f"{_safe_id(task_id, 'task_id')}.*.request.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        reasons = legacy_reasons(document, "request")
        if reasons:
            label_legacy(state_dir, path, document, "request", reasons)
            legacy.append((path, document))
            continue
        canonical.append((_ordinal_from_path(path), path, document))
    canonical.sort(key=lambda item: item[0])
    return canonical, legacy


def _scan_all_requests(
    state_dir: Path, job_id: str
) -> tuple[list[tuple[str, int, Path, dict[str, Any]]], list[tuple[Path, dict[str, Any]]]]:
    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return [], []
    canonical: list[tuple[str, int, Path, dict[str, Any]]] = []
    legacy: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(base.glob("*.request.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        reasons = legacy_reasons(document, "request")
        if reasons:
            label_legacy(state_dir, path, document, "request", reasons)
            legacy.append((path, document))
            continue
        canonical.append((str(document.get("task_id") or ""), _ordinal_from_path(path), path, document))
    canonical.sort(key=lambda item: (item[0], item[1]))
    return canonical, legacy


def _existing_requests(state_dir: Path, job_id: str, task_id: str) -> list[dict[str, Any]]:
    canonical, _legacy = _scan_requests(state_dir, job_id, task_id)
    return [document for _number, _path, document in canonical]


def list_requests(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """Every canonical durable request for one job, oldest first.

    Archived pre-conformance records are labelled and omitted: they are not
    authoritative and must never be replayed as if they were.
    """

    canonical, _legacy = _scan_all_requests(state_dir, job_id)
    return [document for _task, _number, _path, document in canonical]


def list_decisions(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """Every canonical durable decision for one job, oldest first."""

    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.decision.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        reasons = legacy_reasons(document, "decision")
        if reasons:
            label_legacy(state_dir, path, document, "decision", reasons)
            continue
        found.append(document)
    found.sort(key=lambda item: str(item.get("request_id") or ""))
    return found


def _locate_request(state_dir: Path, request_id: str) -> tuple[Path, dict[str, Any]] | None:
    root = Path(state_dir) / RECOVERY_DIR
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*/*.request.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("request_id") or "") == str(request_id):
            return path, data
    return None


def find_request(state_dir: Path, request_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Locate a canonical request by its id; a legacy one is labelled and refused."""

    located = _locate_request(state_dir, request_id)
    if located is None:
        return None
    path, document = located
    reasons = legacy_reasons(document, "request")
    if reasons:
        label_legacy(Path(state_dir), path, document, "request", reasons)
        raise RecoveryError(
            "recovery_record_legacy",
            f"request {request_id!r} is a pre-conformance record labelled in {legacy_label_path(path).name}; "
            "it is archived as legacy/non-authoritative and cannot be decided or dispatched",
            "Record a new canonical request from the recorded evidence; never rewrite the archived one.",
        )
    return path, document


def request_ordinal(state_dir: Path, request_id: str) -> int:
    """The ``<n>`` in the request's durable filename, or 0 when it is not on disk."""

    located = _locate_request(state_dir, request_id)
    return _ordinal_from_path(located[0]) if located else 0


def load_request(state_dir: Path, request_id: str) -> dict[str, Any]:
    """Read one request, prove its digest and re-validate it against the protocol."""

    located = find_request(state_dir, request_id)
    if located is None:
        raise RecoveryError("request_missing", f"no recovery request {request_id!r} exists", _FIX_RECOVERY)
    _path, request = located
    _admit_request(Path(state_dir), request)
    return request


def _check_request(request: Mapping[str, Any]) -> str:
    """The plugin's own structural checks, before the pinned protocol validator.

    The protocol version is *not* checked here: the pinned validator owns that
    refusal (``recovery_version_unsupported``), and re-implementing it would
    hide which protocol actually refused the document.
    """

    recorded = str(request.get("request_digest") or "")
    actual = request_digest(request)
    if not recorded or recorded != actual:
        raise RecoveryError(
            "request_tampered",
            f"request {request.get('request_id')!r} does not hash to its recorded digest "
            f"({recorded[:12] or 'missing'} vs {actual[:12]})",
        )
    failure = request.get("failure")
    if not isinstance(failure, Mapping):
        raise RecoveryError("request_invalid", "the request carries no failure block")
    name = str(failure.get("class") or "")
    code = str(failure.get("code") or "")
    if name not in CLASSES:
        raise RecoveryError("request_forged", f"failure class {name!r} is not one of {', '.join(CLASSES)}")
    if code not in CANONICAL_CODES[name]:
        raise RecoveryError(
            "request_forged",
            f"failure code {code!r} is not a canonical {name} code; {name} codes are "
            + ", ".join(CANONICAL_CODES[name]),
        )
    evidence = failure.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise RecoveryError("request_invalid", "the request carries no evidence array")
    contract = request.get("contract")
    if not isinstance(contract, Mapping) or not isinstance(contract.get("sha256"), str):
        raise RecoveryError("request_invalid", "the request carries no contract block")
    envelope = request.get("envelope")
    if not isinstance(envelope, Mapping) or not isinstance(envelope.get("max_attempts_per_task"), int):
        raise RecoveryError("request_invalid", "the request carries no envelope")
    return name


def _verify_evidence(state_dir: Path, request: Mapping[str, Any]) -> None:
    """Re-hash every referenced evidence artefact; a changed record is a refusal."""

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    for entry in failure.get("evidence") or []:
        if not isinstance(entry, Mapping):
            raise RecoveryError("recovery_evidence_invalid", "an evidence entry is not an object")
        reference = str(entry.get("reference") or "")
        relative = Path(reference)
        if not reference or relative.is_absolute() or ".." in relative.parts:
            raise RecoveryError(
                "recovery_evidence_invalid",
                f"evidence reference {reference!r} is not a path inside the state directory",
            )
        recorded = str(entry.get("sha256") or "")
        if not recorded:
            continue
        path = Path(state_dir) / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RecoveryError(
                "recovery_evidence_missing",
                f"the evidence record {reference!r} cannot be read: {exc}",
            ) from exc
        actual = hashlib.sha256(payload).hexdigest()
        if actual != recorded:
            raise RecoveryError(
                "recovery_evidence_tampered",
                f"the evidence record {reference!r} hashes to {actual[:12]}, not the recorded {recorded[:12]}",
            )


def _admit_request(state_dir: Path, request: Mapping[str, Any], *, module: Any = None) -> dict[str, Any]:
    """Admit one request: plugin checks, then the pinned protocol, then evidence."""

    _check_request(request)
    loaded = module if module is not None else protocol_validator()
    admitted = _pinned(loaded, "validate_request", request)
    if not isinstance(admitted, Mapping):
        admitted = request
    _verify_evidence(state_dir, request)
    return dict(admitted)


def _conflicted_paths(record: Any) -> list[str]:
    """The conflicted paths a record names, for a ``conflict`` evidence entry."""

    found: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            found.add(value.strip())
        elif isinstance(value, Mapping):
            for key in ("path", "file", "paths"):
                add(value.get(key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    if isinstance(record, Mapping):
        for key in ("paths", "conflicted_paths", "conflicts"):
            add(record.get(key))
        inner = record.get("record")
        if isinstance(inner, Mapping):
            for key in ("paths", "conflicted_paths", "conflicts"):
                add(inner.get(key))
    return sorted(found)


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):  # pragma: no cover - default=str already covers this
        return str(value)


def _evidence_entry(
    state_dir: Path,
    job_id: str,
    task_id: str,
    number: int,
    failure: Mapping[str, Any],
    raw: Any,
) -> dict[str, Any]:
    """Write the durable evidence artefact and return the request's reference to it.

    A reference is not a copy: the artefact is the authoritative record of what
    was observed, hashed and re-hashed on every load. The canonical failure text
    lives in it verbatim, so normalising the code never loses the observation.
    """

    kind = _EVIDENCE_KIND[str(failure["class"])]
    payload_body = {
        "schema": EVIDENCE_SCHEMA,
        "kind": kind,
        "class": str(failure["class"]),
        "code": str(failure["code"]),
        "text": str(failure["evidence"]),
        "record": _jsonable(raw),
    }
    payload = canonical_bytes(payload_body)
    digest = hashlib.sha256(payload).hexdigest()
    name = f"{_safe_id(task_id, 'task_id')}.{int(number)}.{kind}.{digest[:12]}.evidence.json"
    path = recovery_dir(state_dir, job_id) / name
    _write_bytes(path, payload, replace=False)
    entry: dict[str, Any] = {
        "kind": kind,
        "reference": f"{RECOVERY_DIR}/{job_id}/{name}",
        "sha256": digest,
    }
    if kind == "conflict":
        paths = _conflicted_paths(raw)
        if not paths:
            raise RecoveryError(
                "recovery_evidence_incomplete",
                "an integration failure needs a conflict record naming the conflicted paths; "
                "a bare reference is not distinct repair evidence",
                "Record the wave log with the paths that conflicted.",
            )
        entry["paths"] = paths
    return entry


def _outstanding_question(task_id: str, failure: Mapping[str, Any], envelope: Mapping[str, Any],
                          remaining: int, can_supersede: bool) -> str:
    options: list[str] = []
    if can_supersede:
        options.append("supersede the same frozen contract once with an authorised corrected test selection")
    if str(failure.get("class")) not in _NO_DISPATCH:
        options.append(f"retry once within the remaining budget ({remaining} attempt(s) left)")
    options.append("escalate to a human")
    return (
        f"Task {task_id} was rejected as {_article(str(failure.get('class')))} {failure.get('class')} failure "
        f"({failure.get('code')}). Decide whether to {'; '.join(options)}. "
        "Record the decision with `bundlebees job recover --job-id ... --decision-file ...`; "
        "no corrective attempt is dispatched until a validated decision exists."
    )


def build_request(
    state_dir: Path,
    job: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    failure: Any,
    authorization: Mapping[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Record (once) the canonical durable recovery request for a rejected attempt.

    Repeated calls for the same attempt and canonical code return the request
    that already exists; a different failure for the same attempt gets a new
    numbered request. Nothing here changes the job or dispatches anything.
    """

    state = Path(state_dir)
    job_id = _safe_id(job.get("job_id"), "job_id")
    task_id = _safe_id(task.get("task_id"), "task_id")

    # Fail closed before writing anything: no validator, no request.
    module = protocol_validator()

    contract = task.get("contract") if isinstance(task.get("contract"), Mapping) else None
    contract_block = _contract_block(task)
    if not contract_block["id"]:
        # A job that never froze a contract is not an M4 job: there is no
        # contract to name, and inventing one would invent authority. The
        # rejection stands on its own and no corrective attempt is dispatched.
        raise RecoveryError(
            "recovery_not_applicable",
            f"task {task_id} carries no frozen contract, so no rizzstack.recovery/1 request can name one",
            "Reject the task as before; recovery needs a reviewed contract to recover.",
        )
    if not contract_block["sha256"]:
        raise RecoveryError(
            "request_incomplete",
            f"task {task_id} names contract {contract_block['id']!r} but no reviewed sha256",
            "Freeze and pin the contract before a rejection can be recovered.",
        )
    attempt_id = str((task.get("attempts") or [""])[-1] or "").strip()
    if not attempt_id:
        raise RecoveryError(
            "request_incomplete",
            f"task {task_id} records no failed attempt, so no attempt can be named",
            "Record the attempt before the rejection; a recovery never rewrites an attempt.",
        )

    classified = _normalise_failure(failure, contract)
    envelope = envelope_for(job, task, authorization)
    permitted = supersession_permitted(job, task, classified, envelope)
    final_envelope = _final_envelope(envelope, permitted=permitted, failure_class=classified["class"])

    canonical, legacy = _scan_requests(state, job_id, task_id)
    for _number, _path, request in canonical:
        recorded = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
        if (
            str(request.get("attempt_id") or "") == attempt_id
            and str(recorded.get("code") or "") == classified["code"]
            and str(recorded.get("class") or "") == classified["class"]
        ):
            _admit_request(state, request, module=module)
            return request

    highest = max(
        [_ordinal_from_path(path) for path, _document in legacy]
        + [_ordinal_from_path(path) for _number, path, _document in canonical]
        + [0]
    )
    number = highest + 1
    remaining = max(
        0,
        int(final_envelope["max_attempts_per_task"])
        - len([item for item in task.get("attempts") or [] if item]),
    )
    evidence = _evidence_entry(state, job_id, task_id, number, classified, failure)
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "request_id": request_id_for(job_id, task_id, number),
        "job_id": job_id,
        "feature_id": _feature_id(state, job),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "failure": {
            "class": classified["class"],
            "code": classified["code"],
            "evidence": [evidence],
            "rationale": classified["rationale"],
        },
        "contract": contract_block,
        "supersession_permitted": bool(permitted),
        "envelope": final_envelope,
        "outstanding_question": _outstanding_question(
            task_id, classified, final_envelope, remaining, permitted
        ),
    }
    body["request_digest"] = request_digest(body)
    _admit_request(state, body, module=module)

    path = request_path(state, job_id, task_id, number)
    if not _create_only(path, body):
        existing_body = _read_json(path, "request")
        if str(existing_body.get("request_digest") or "") == body["request_digest"]:
            return existing_body
        raise RecoveryError(
            "request_conflict",
            f"a different recovery request already exists at {path.name}",
            _FIX_RECOVERY,
        )
    return body


def outstanding(state_dir: Path, job: Mapping[str, Any]) -> dict[str, Any] | None:
    """The newest canonical request for a task that is still waiting for a decision.

    A rejected task whose only records predate the canonical protocol is a
    refusal, not an idle tick: those records are archived and must never be used
    to dispatch.
    """

    job_id = str(job.get("job_id") or "")
    for task in job.get("tasks") or []:
        if str(task.get("state") or "") != "rejected":
            continue
        task_id = str(task.get("task_id") or "")
        canonical, legacy = _scan_requests(Path(state_dir), job_id, task_id)
        if not canonical:
            if legacy:
                raise RecoveryError(
                    "recovery_record_legacy",
                    f"task {task_id} has only pre-conformance recovery records; they are archived as "
                    "legacy/non-authoritative and no decision may be dispatched from them",
                    "Record a new canonical request from the recorded evidence.",
                )
            continue
        request = canonical[-1][2]
        if _decision_for(Path(state_dir), job_id, request) is None:
            return request
    return None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _decision_file(state_dir: Path, job_id: str, request: Mapping[str, Any]) -> Path:
    task_id = str(request.get("task_id") or "")
    number = request_ordinal(state_dir, str(request.get("request_id") or ""))
    return decision_path(state_dir, job_id, task_id, number)


def _decision_for(state_dir: Path, job_id: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
    path = _decision_file(state_dir, job_id, request)
    if not path.exists():
        return None
    return _read_json(path, "decision")


def _check_decision_shape(request: Mapping[str, Any], decision: Mapping[str, Any]) -> None:
    """Every field the protocol defines, and nothing else, bound to this request."""

    if str(decision.get("request_id") or "") != str(request.get("request_id") or ""):
        raise RecoveryError(
            "decision_mismatch",
            f"the decision names request {decision.get('request_id')!r}, not {request.get('request_id')!r}",
        )
    if str(decision.get("request_digest") or "") != str(request.get("request_digest") or ""):
        raise RecoveryError("decision_mismatch", "the decision does not name this request's digest")
    name = str(decision.get("decision") or "")
    if name not in DECISIONS:
        raise RecoveryError("decision_invalid", f"decision {name!r} is not one of {', '.join(DECISIONS)}")
    if not str(decision.get("adapter") or "").strip():
        raise RecoveryError("decision_invalid", "a decision must name the adapter that produced it")
    if not str(decision.get("decided_at") or "").strip():
        raise RecoveryError("decision_invalid", "a decision must record decided_at")
    if len(str(decision.get("rationale") or "").strip()) < 8:
        raise RecoveryError(
            "decision_invalid",
            "a decision must carry a real rationale (at least 8 characters), not a placeholder",
            "Say why this decision follows from the recorded failure evidence.",
        )
    corrected = decision.get("corrected_tests")
    if not isinstance(corrected, list) or not all(isinstance(item, str) and item.strip() for item in corrected):
        raise RecoveryError("decision_invalid", "corrected_tests must be a list of test selections")
    supersedes = decision.get("supersedes")
    if supersedes is not None and not isinstance(supersedes, Mapping):
        raise RecoveryError("decision_invalid", "supersedes must be null or a contract pair")


def read_decision(state_dir: Path, request: Mapping[str, Any]) -> dict[str, Any] | None:
    """The canonical decision recorded for *request*, if any.

    A decision has no digest of its own; it is proven by re-reading the request
    it binds and by the protocol validation that admission and application
    perform. A pre-conformance decision is labelled and refused.
    """

    job_id = str(request.get("job_id") or "")
    decision = _decision_for(Path(state_dir), job_id, request)
    if decision is None:
        return None
    reasons = legacy_reasons(decision, "decision")
    if reasons:
        path = _decision_file(Path(state_dir), job_id, request)
        label_legacy(Path(state_dir), path, decision, "decision", reasons)
        raise RecoveryError(
            "recovery_record_legacy",
            f"the decision for {request.get('request_id')!r} is a pre-conformance record; it is archived "
            "as legacy/non-authoritative and cannot be applied",
            "Record a canonical decision for a canonical request; never rewrite the archived one.",
        )
    _check_decision_shape(request, decision)
    return decision


def _decision_body(
    request: Mapping[str, Any], decision: Any, adapter: str, now: Any
) -> dict[str, Any]:
    if isinstance(decision, str):
        decision = {"decision": decision}
    if not isinstance(decision, Mapping):
        raise RecoveryError("decision_invalid", "a decision must be a mapping or a decision name")
    corrected = decision.get("corrected_tests")
    if corrected is None:
        corrected = []
    if not isinstance(corrected, list) or not all(isinstance(item, str) and item.strip() for item in corrected):
        raise RecoveryError("decision_invalid", "corrected_tests must be a list of test selections")
    supersedes = decision.get("supersedes")
    body: dict[str, Any] = {
        "schema": str(decision.get("schema") or DECISION_SCHEMA),
        "request_id": str(decision.get("request_id") or request.get("request_id") or ""),
        "request_digest": str(decision.get("request_digest") or request.get("request_digest") or ""),
        "adapter": str(decision.get("adapter") or adapter or ""),
        "decision": str(decision.get("decision") or "").strip().lower(),
        "corrected_tests": [item.strip() for item in corrected],
        "supersedes": dict(supersedes) if isinstance(supersedes, Mapping) else supersedes,
        "rationale": _text(decision.get("rationale") or ""),
        "decided_at": str(decision.get("decided_at") or "") or _iso(_clock(now)),
    }
    # A decision has no vocabulary for widening: any field the protocol does not
    # define — including a digest of its own — is refused.
    unknown = sorted(set(decision) - set(body))
    if unknown:
        raise RecoveryError("decision_invalid", "unexpected decision field(s): " + ", ".join(unknown))
    return body


def _load_task(state_dir: Path, request: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    job_id = str(request.get("job_id") or "")
    try:
        job = jobs.load_job(Path(state_dir), job_id, recover=False)
    except jobs.JobError as exc:
        raise RecoveryError("job_missing", f"job {job_id!r} cannot be loaded: {exc}") from exc
    task_id = str(request.get("task_id") or "")
    for task in job.get("tasks") or []:
        if str(task.get("task_id") or "") == task_id:
            return job, task
    raise RecoveryError("request_invalid", f"job {job_id!r} has no task {task_id!r}")


def _ledger(job: Mapping[str, Any], task: Mapping[str, Any]) -> tuple[int, int]:
    """The durable attempt and correction counts the protocol validates against."""

    attempts = len([item for item in task.get("attempts") or [] if item])
    corrections = len(_supersession_history(job, task))
    return attempts, corrections


def _check_decision(state_dir: Path, request: Mapping[str, Any], decision: Mapping[str, Any],
                    *, job: Mapping[str, Any] | None = None,
                    task: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Every mechanical rule a decision must satisfy, in refusal order."""

    _check_decision_shape(request, decision)
    name = str(decision.get("decision") or "")
    adapter = str(decision.get("adapter") or "")
    if adapter.lower() in RESERVED_ADAPTERS:
        raise RecoveryError("adapter_reserved", f"{adapter!r} is a mechanical component, not an adapter",
                            "The validator never decides; a named agent adapter must.")

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    failure_class = str(failure.get("class") or "")
    envelope = request.get("envelope") if isinstance(request.get("envelope"), Mapping) else {}
    if job is None or task is None:
        job, task = _load_task(state_dir, request)
    assert job is not None and task is not None

    if failure_class in _NO_DISPATCH and name != "escalate":
        raise RecoveryError(
            "decision_class_forbidden",
            f"a {failure_class} failure never dispatches a worker; the only admissible decision is escalate",
        )
    if failure_class in _NO_RETRY and name == "retry":
        raise RecoveryError(
            "decision_class_forbidden",
            f"a {failure_class} failure cannot be repaired by re-dispatching the same contract; "
            "supersede it or escalate",
        )
    if name == "retry" and remaining_attempts(request, task) <= 0:
        raise RecoveryError(
            "retry_budget_exhausted",
            f"task {task.get('task_id')} has used {len(task.get('attempts') or [])} of "
            f"{envelope.get('max_attempts_per_task')} allowed attempts; escalate instead",
        )

    corrected = decision.get("corrected_tests") or []
    allowed = envelope.get("corrected_tests_allowed") or []
    supersedes = decision.get("supersedes")
    if name == "supersede":
        # The permission is checked before the selection: an unpermitted
        # supersession is refused on its authority, not on the test it names.
        if not request.get("supersession_permitted"):
            raise RecoveryError("supersede_forbidden", "this request does not permit a contract supersession")
        if failure_class != "specification" or str(failure.get("code")) not in SELECTION_CODES:
            raise RecoveryError("supersede_forbidden", "only an unusable acceptance selection may be superseded")
        if int(envelope.get("contract_corrections_allowed") or 0) < 1:
            raise RecoveryError(
                "contract_correction_forbidden",
                "this envelope does not allow contract corrections, so no corrected test may be named",
            )
        if _supersession_history(job, task):
            raise RecoveryError("supersession_exhausted", "this task already consumed its one supersession")
    unauthorised = [item for item in corrected if not test_authorized(item, allowed)]
    if unauthorised:
        raise RecoveryError(
            "corrected_test_unauthorized",
            "corrected test(s) outside the pre-authorized selections: " + ", ".join(sorted(unauthorised)),
        )
    if name == "escalate" and corrected:
        raise RecoveryError("corrected_tests_unexpected", "an escalation cannot carry corrected tests",
                            "Propose the correction in the escalation text, not in this field.")
    if name == "retry" and failure_class == "implementation" and corrected:
        raise RecoveryError("corrected_tests_unexpected", "an implementation retry may not change acceptance",
                            "Retry the same contract; corrected tests belong to a supersession.")

    if name == "supersede":
        if not corrected:
            raise RecoveryError("supersede_incomplete", "a supersession must name the corrected test selection")
        if not isinstance(supersedes, Mapping):
            raise RecoveryError("supersede_incomplete", "a supersede decision must carry a supersedes block")
        contract_block = request.get("contract") if isinstance(request.get("contract"), Mapping) else {}
        if str(supersedes.get("contract_id") or "") != str(contract_block.get("id") or ""):
            raise RecoveryError(
                "supersede_mismatch",
                f"the decision supersedes {supersedes.get('contract_id')!r}, the request names "
                f"{contract_block.get('id')!r}",
            )
        if str(supersedes.get("contract_sha256") or "") != str(contract_block.get("sha256") or ""):
            raise RecoveryError(
                "supersede_mismatch",
                "the decision supersedes a different contract digest than the one in the request",
            )
        declared_id = str(supersedes.get("new_contract_id") or "")
        declared_sha = str(supersedes.get("new_contract_sha256") or "")
        if not declared_id or not declared_sha:
            raise RecoveryError(
                "supersede_incomplete",
                "a supersede decision must name the corrected contract id and reviewed hash",
                "A correction revises the same contract id and changes only its hash.",
            )
        derived = derive_superseding_contract(
            task, corrected, adapter=adapter or "deterministic",
            decided_at=str(decision.get("decided_at") or ""), contract_id=declared_id or None,
        )
        if str(derived.get("id")) != declared_id or acdd.check_frozen(derived) != declared_sha:
            raise RecoveryError(
                "supersede_mismatch",
                "the declared new contract id/sha256 is not the contract this decision would freeze",
            )
    elif supersedes is not None:
        raise RecoveryError("supersede_forbidden", "only a supersede decision may carry a supersedes block")
    return job, task


def _admit_pair(state_dir: Path, request: Mapping[str, Any], decision: Mapping[str, Any],
                job: Mapping[str, Any], task: Mapping[str, Any], *, module: Any = None) -> None:
    """Validate a request and its decision as one unit, exactly at apply time."""

    attempts, corrections = _ledger(job, task)
    loaded = module if module is not None else protocol_validator()
    _pinned(
        loaded,
        "validate_pair",
        request,
        decision,
        attempts_used=attempts,
        corrections_used=corrections,
    )


def _admit_decision(state_dir: Path, request: Mapping[str, Any], decision: Mapping[str, Any],
                    job: Mapping[str, Any], task: Mapping[str, Any], *, module: Any = None) -> None:
    """The pinned protocol is the last gate before a decision is recorded."""

    attempts, corrections = _ledger(job, task)
    loaded = module if module is not None else protocol_validator()
    _pinned(
        loaded,
        "validate_decision",
        decision,
        request=request,
        attempts_used=attempts,
        corrections_used=corrections,
    )


def record_decision(
    state_dir: Path,
    request_id: str,
    decision: Any,
    *,
    adapter: str = "deterministic",
    now: Any = None,
) -> dict[str, Any]:
    """Validate and durably record one canonical decision for one request.

    Recording is create-only: a request is decided exactly once. A second
    decision — identical or not — is refused as ``decision_exists``, so a
    replayed command can never look like a second authorisation.
    """

    state = Path(state_dir)
    located = find_request(state, request_id)
    if located is None:
        raise RecoveryError("request_missing", f"no recovery request {request_id!r} exists", _FIX_RECOVERY)
    _path, request = located
    job, task = _load_task(state, request)
    module = protocol_validator()
    _admit_request(state, request, module=module)

    body = _decision_body(request, decision, adapter, now)
    _check_decision(state, request, body, job=job, task=task)
    _admit_decision(state, request, body, job, task, module=module)

    path = _decision_file(state, str(request.get("job_id") or ""), request)
    if not _create_only(path, body):
        prior = _read_json(path, "decision")
        same = canonical_bytes(prior) == canonical_bytes(body)
        raise RecoveryError(
            "decision_exists",
            f"{'a duplicate' if same else 'a conflicting'} decision is already recorded for "
            f"{request_id!r}; a request is decided exactly once",
            "Inspect recovery/<job>/<task>.<n>.decision.json; a recorded decision is never overwritten.",
        )
    return body


def deterministic_adapter(request: Mapping[str, Any], *, task: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The decision a coordinator can reach with no judgement at all.

    Environment and infrastructure failures escalate, a used-up budget
    escalates, an implementation or integration failure retries, and a
    specification failure escalates because correcting an acceptance selection
    needs an authorised replacement this adapter cannot invent.

    ``task`` is optional because a request alone is enough to propose a
    decision; when the caller has the live task it should pass it so the
    attempt ledger decides the budget rather than the request's one known
    failed attempt.
    """

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    failure_class = str(failure.get("class") or "")
    envelope = request.get("envelope") if isinstance(request.get("envelope"), Mapping) else {}
    limit = int(envelope.get("max_attempts_per_task") or 0)
    left = remaining_attempts(request, task)
    base = {
        "schema": SCHEMA,
        "request_id": str(request.get("request_id") or ""),
        "request_digest": str(request.get("request_digest") or ""),
        "adapter": "deterministic",
        "corrected_tests": [],
        "supersedes": None,
    }
    if failure_class in _NO_DISPATCH:
        return {
            **base,
            "decision": "escalate",
            "rationale": (
                f"{_article(failure_class)} {failure_class} failure ({failure.get('code')}) is outside "
                "the artifact's control; "
                "bundlebees never dispatches a worker for it"
            ),
        }
    if failure_class == "specification":
        return {
            **base,
            "decision": "escalate",
            "rationale": (
                f"the specification is unusable ({failure.get('code')}); a corrected acceptance selection "
                "must be authorised by a decision that names the replacement tests"
            ),
        }
    if left <= 0:
        return {
            **base,
            "decision": "escalate",
            "rationale": f"the attempt budget ({limit}) is exhausted; no retry is admissible",
        }
    return {
        **base,
        "decision": "retry",
        "rationale": (
            f"{_article(failure_class)} {failure_class} failure ({failure.get('code')}) is repairable and "
            f"{left} attempt(s) remain within the envelope"
        ),
    }


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


def _new_contract_id(base_id: str) -> str:
    """A supersession revises the same contract: the id never changes."""

    return str(base_id)


def _test_slots(contract: Mapping[str, Any]) -> list[tuple[int, int]]:
    slots: list[tuple[int, int]] = []
    for index, criterion in enumerate(contract.get("acceptance_criteria") or []):
        if not isinstance(criterion, Mapping) or criterion.get("verification") != "test":
            continue
        for position, _ in enumerate(criterion.get("tests") or []):
            slots.append((index, position))
    return slots


def current_selection(contract: Mapping[str, Any]) -> list[str]:
    """The test selections a contract names, in the order a correction replaces."""

    out: list[str] = []
    for index, position in _test_slots(contract):
        out.append(str(contract["acceptance_criteria"][index]["tests"][position]))
    return out


def derive_superseding_contract(
    task: Mapping[str, Any],
    corrected_tests: Sequence[str],
    *,
    adapter: str,
    decided_at: str,
    contract_id: str | None = None,
) -> dict[str, Any]:
    """Freeze the one contract a supersede decision is allowed to produce.

    The base is the task's current frozen contract. Only the test selections
    named by ``corrected_tests`` change, positionally, so the scope, the criteria
    ids and every other field stay byte-identical: a correction cannot widen the
    lease or drop a criterion. The contract *id* stays the same; the
    ``supersedes`` pair and the corrected selection are what change the reviewed
    hash.
    """

    base = task.get("contract")
    if not isinstance(base, Mapping):
        raise RecoveryError("supersede_incomplete", "the task carries no frozen contract to supersede")
    try:
        base_sha = acdd.check_frozen(dict(base))
    except ContractError as exc:
        raise RecoveryError(exc.code, f"the base contract cannot be superseded: {exc.error}", exc.fix) from exc
    slots = _test_slots(base)
    if not slots:
        raise RecoveryError(
            "supersede_incomplete",
            "the base contract names no test criteria, so there is no acceptance selection to correct",
        )
    if len(corrected_tests) != len(slots):
        raise RecoveryError(
            "corrected_tests_mismatch",
            f"the contract has {len(slots)} test selection(s); the decision names {len(corrected_tests)}",
            "Name exactly one replacement for each declared test criterion, in contract order.",
        )
    if [str(item) for item in corrected_tests] == current_selection(base):
        raise RecoveryError(
            "supersede_no_change",
            "the decision names the same tests the failed contract already selects",
            "Correct the selection that made the contract unusable; an unchanged selection is not a revision.",
        )
    body = {key: copy.deepcopy(value) for key, value in dict(base).items() if key != "review"}
    body["supersedes"] = str(base.get("id") or "")
    body["supersedes_sha256"] = base_sha
    if contract_id:
        if str(contract_id) != str(base.get("id") or ""):
            raise RecoveryError(
                "decision_scope_widened",
                "a supersession may not change the contract id",
                "A correction revises the same contract; a different id is a new feature.",
            )
        body["id"] = str(contract_id)
    criteria = body.get("acceptance_criteria")
    assert isinstance(criteria, list)  # validated by check_frozen above
    for (index, position), replacement in zip(slots, corrected_tests):
        criteria[index]["tests"][position] = replacement
    try:
        return acdd.freeze_contract(
            body,
            approved_by=f"recovery:{adapter}",
            approved_at=decided_at or None,
        )
    except ContractError as exc:
        raise RecoveryError(exc.code, f"the corrected contract is not valid: {exc.error}", exc.fix) from exc


# ---------------------------------------------------------------------------
# Application and escalation
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def _clock(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    return _epoch(value)


def escalate(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    request: Mapping[str, Any],
    *,
    reason: str,
    clock: Any = None,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Put a task beyond every further dispatch and require a human.

    Idempotent: a task that already escalated this request is not escalated a
    second time, and a terminal task is never moved again.
    """

    stamp = _iso(_clock(clock))
    task_id = str(task.get("task_id") or "")
    recorded = task.get("recovery") if isinstance(task.get("recovery"), Mapping) else {}
    already = str(recorded.get("request_id") or "") == str(request.get("request_id") or "") and bool(
        recorded.get("escalated")
    )

    transition = ""
    state = str(task.get("state") or "")
    if state == "rejected":
        try:
            jobs.transition(job, task_id, "failed", actor="recovery", reason=reason)
            transition = f"{task_id}->failed"
        except jobs.JobError:
            transition = ""

    gate = job.setdefault("approval_gate", {})
    if isinstance(gate, dict):
        gate["state"] = GATE_AWAITING_APPROVAL

    block = job.setdefault("recovery", {})
    if isinstance(block, dict):
        block["state"] = "needs_approval"
        escalations = block.setdefault("escalations", [])
        if isinstance(escalations, list) and not any(
            isinstance(entry, Mapping)
            and entry.get("request_id") == request.get("request_id")
            for entry in escalations
        ):
            escalations.append(
                {
                    "request_id": str(request.get("request_id") or ""),
                    "task_id": task_id,
                    "code": str((request.get("failure") or {}).get("code") or ""),
                    "reason": _text(reason),
                    "at": stamp,
                }
            )

    if not already:
        job.setdefault("escalations", []).append(
            {
                "at": stamp,
                "task_id": task_id,
                "code": "recovery_escalated",
                "detail": _text(reason),
            }
        )
        _mark_applied(task, str(request.get("request_id") or ""), "escalate", stamp)
        task["recovery"] = {
            "request_id": str(request.get("request_id") or ""),
            "decision": "escalate",
            "adapter": str((decision or {}).get("adapter") or "reconciler"),
            "decided_at": str((decision or {}).get("decided_at") or stamp),
            "applied_at": stamp,
            "corrected_tests": [],
            "supersedes": None,
            "escalated": True,
            "failure": dict((request.get("failure") or {})),
        }
    return {
        "task_id": task_id,
        "request_id": str(request.get("request_id") or ""),
        "decision": "escalate",
        "escalated": True,
        "transition": transition,
        "detail": _text(reason),
        "gate": GATE_AWAITING_APPROVAL,
    }


def _mark_applied(task: dict[str, Any], request_id: str, decision_name: str, stamp: str) -> dict[str, Any]:
    """Append-only proof that one request was applied to this task.

    The job document is the single atomic commit for "applied", so a crash can
    never leave a decision marked applied without the transition that followed
    it, and a restart can never apply the same request twice.
    """

    entries = task.setdefault("recovery_applied", [])
    if not isinstance(entries, list):
        entries = []
        task["recovery_applied"] = entries
    for entry in entries:
        if isinstance(entry, Mapping) and str(entry.get("request_id") or "") == request_id:
            return dict(entry)
    entry = {"request_id": request_id, "decision": decision_name, "at": stamp}
    entries.append(entry)
    return entry


def _was_applied(task: Mapping[str, Any], request_id: str) -> bool:
    for entry in task.get("recovery_applied") or []:
        if isinstance(entry, Mapping) and str(entry.get("request_id") or "") == request_id:
            return True
    recorded = task.get("recovery") if isinstance(task.get("recovery"), Mapping) else {}
    return str(recorded.get("request_id") or "") == request_id


def _mark_retry(task: dict[str, Any], request: Mapping[str, Any], decision: Mapping[str, Any], stamp: str) -> dict[str, Any]:
    """Return the task to ``dispatching`` without ever writing a second intent."""

    attempt_no = len([item for item in task.get("attempts") or [] if item]) + 1
    _mark_applied(task, str(request.get("request_id") or ""), str(decision.get("decision") or ""), stamp)
    task["recovery"] = {
        "request_id": str(request.get("request_id") or ""),
        "decision": str(decision.get("decision") or ""),
        "adapter": str(decision.get("adapter") or ""),
        "decided_at": str(decision.get("decided_at") or ""),
        "applied_at": stamp,
        "attempt_no": attempt_no,
        "corrected_tests": list(decision.get("corrected_tests") or []),
        "supersedes": decision.get("supersedes"),
        "failure": dict((request.get("failure") or {})),
    }
    # No intent file for this attempt exists yet, and the *old* attempt's intent
    # is never reused: the marker makes the dispatch path treat the task as a
    # clean, not-yet-sent request, so exactly one POST can follow.
    task["intent"] = {"status": "not_sent", "attempt_no": attempt_no, "recovery": str(request.get("request_id") or "")}
    return task["recovery"]


def _apply_supersession(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    request: Mapping[str, Any],
    decision: Mapping[str, Any],
    stamp: str,
) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    binding = task.get("binding") if isinstance(task.get("binding"), Mapping) else {}
    corrected = [str(item) for item in decision.get("corrected_tests") or []]
    declared = decision.get("supersedes") if isinstance(decision.get("supersedes"), Mapping) else {}
    new_id = str(declared.get("new_contract_id") or "") or _new_contract_id(
        str((request.get("contract") or {}).get("id") or "contract")
    )
    frozen = derive_superseding_contract(
        task,
        corrected,
        adapter=str(decision.get("adapter") or "deterministic"),
        decided_at=str(decision.get("decided_at") or ""),
        contract_id=new_id,
    )
    try:
        new_sha = acdd.check_frozen(frozen)
    except ContractError as exc:  # pragma: no cover - freeze_contract already proves it
        raise RecoveryError(exc.code, exc.error, exc.fix) from exc
    pinned = str(binding.get("contract_sha256") or "")
    stale_binding: dict[str, Any] = {}
    if pinned and pinned != new_sha:
        provision = task.get("provision")
        if not isinstance(provision, Mapping):
            raise RecoveryError(
                "supersede_binding_conflict",
                "this task is bound to a provisioning receipt that pins the superseded contract, "
                "and it carries no provisioning spec that could renew it",
                "Re-provision the task against the corrected contract before dispatching it.",
            )
        # The workspace is unchanged (only the acceptance selection moved), but the
        # receipt was bound to the old contract: drop the stale binding so the
        # provisioning phase re-binds the corrected digest before any dispatch.
        stale_binding = dict(binding)
        renewed = dict(provision)
        renewed["contract_sha256"] = new_sha
        task["provision"] = renewed
        task["binding"] = None
        task["provisioning"] = None

    ordinal = request_ordinal(Path(state_dir), str(request.get("request_id") or ""))
    path = contract_path(Path(state_dir), job_id, str(task.get("task_id")), ordinal)
    _atomic_write(path, frozen)
    record = {
        "task_id": str(task.get("task_id") or ""),
        "request_id": str(request.get("request_id") or ""),
        "contract_id": str(frozen.get("id") or ""),
        "contract_sha256": new_sha,
        "supersedes": str(frozen.get("supersedes") or ""),
        "supersedes_sha256": str(frozen.get("supersedes_sha256") or ""),
        "path": f"{RECOVERY_DIR}/{job_id}/{path.name}",
        "at": stamp,
        "reprovisioned": bool(stale_binding),
        "stale_binding_contract_sha256": str(stale_binding.get("contract_sha256") or ""),
    }
    # The packet is never touched: the job keeps the digest it was admitted
    # under, and the superseding contract is recorded beside it.
    task["contract"] = frozen
    task["contract_sha256"] = new_sha
    task["contract_path"] = None
    task["contract_supersession"] = record
    acceptance = task.get("acceptance")
    if isinstance(acceptance, Mapping):
        # The acceptance block is the selection the verifier actually runs, so it
        # follows the corrected contract exactly. Only the tests move: criteria,
        # commands and the lease stay as the authorization approved them.
        refreshed = dict(acceptance)
        refreshed["tests"] = list(corrected)
        task["acceptance"] = refreshed
    block = job.setdefault("recovery", {})
    if isinstance(block, dict):
        supersessions = block.setdefault("supersessions", [])
        if isinstance(supersessions, list):
            supersessions.append(dict(record))
    return record


def apply_ready_decisions(state_dir: Path, job: dict[str, Any], *, now: Any = None) -> list[dict[str, Any]]:
    """Apply every validated decision for this job, once.

    A retry or supersede returns the task to ``dispatching`` exactly once; the
    caller saves the job (the atomic commit for "applied") and may then dispatch
    a single corrective attempt. A budget that cannot dispatch anything is
    escalated here, because every admissible decision on such a request is an
    escalation. The pinned protocol validates the pair again before anything
    moves, so a decision edited after it was recorded cannot dispatch.
    """

    state = Path(state_dir)
    clock = _clock(now)
    stamp = _iso(clock)
    applied: list[dict[str, Any]] = []
    job_id = str(job.get("job_id") or "")
    module = protocol_validator()

    for task in job.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        canonical, legacy = _scan_requests(state, job_id, task_id)
        if not canonical:
            if legacy and str(task.get("state") or "") == "rejected":
                raise RecoveryError(
                    "recovery_record_legacy",
                    f"task {task_id} has only pre-conformance recovery records; they are archived as "
                    "legacy/non-authoritative and nothing may be dispatched from them",
                    "Record a new canonical request from the recorded evidence.",
                )
            continue
        request = canonical[-1][2]
        recorded = task.get("recovery") if isinstance(task.get("recovery"), Mapping) else {}
        decision = read_decision(state, request)
        state_name = str(task.get("state") or "")

        if decision is None:
            if state_name == "rejected" and str(recorded.get("request_id") or "") != str(request.get("request_id") or ""):
                if exhausted(request, task) and attributed(request):
                    applied.append(
                        escalate(
                            state,
                            job,
                            task,
                            request,
                            reason=(
                                f"no worker attempt may be dispatched for this request "
                                f"({(request.get('failure') or {}).get('class')} failure, "
                                f"{remaining_attempts(request, task)} attempt(s) left); "
                                "the budget is exhausted and the request needs approval"
                            ),
                            clock=clock,
                        )
                    )
            continue

        already = _was_applied(task, str(request.get("request_id") or ""))
        if already and state_name != "rejected":
            continue
        if state_name != "rejected":
            # A decision for a request whose task already moved on is stale, not
            # an instruction; applying it would dispatch a second attempt.
            continue

        _check_decision(state, request, decision, job=job, task=task)
        _admit_pair(state, request, decision, job, task, module=module)
        name = str(decision.get("decision") or "")
        if name == "escalate":
            applied.append(
                escalate(
                    state,
                    job,
                    task,
                    request,
                    reason=str(decision.get("rationale") or "a validated decision required a human"),
                    clock=clock,
                    decision=decision,
                )
            )
            continue
        if name not in {"retry", "supersede"}:
            raise RecoveryError("decision_invalid", f"decision {name!r} cannot be applied")

        if exhausted(request, task):
            raise RecoveryError(
                "retry_budget_exhausted",
                f"task {task_id} cannot dispatch another attempt under request {request.get('request_id')}",
            )
        supersession = None
        if name == "supersede":
            supersession = _apply_supersession(state, job, task, request, decision, stamp)
        summary = _mark_retry(task, request, decision, stamp)
        jobs.transition(
            job,
            task_id,
            "dispatching",
            actor="recovery",
            reason=(
                f"recovery decision {name} for {request.get('request_id')}"
                + (f"; contract superseded by {supersession['contract_sha256'][:12]}" if supersession else "")
            ),
        )
        applied.append(
            {
                "task_id": task_id,
                "request_id": str(request.get("request_id") or ""),
                "decision": name,
                "attempt_no": summary.get("attempt_no"),
                "supersedes": supersession,
                "transition": f"{task_id}->dispatching",
                "detail": str(decision.get("rationale") or ""),
            }
        )
    return applied


def summarize(state_dir: Path, job_id: str) -> dict[str, Any]:
    """The recovery view of a job: canonical requests, decisions and supersessions."""

    state = Path(state_dir)
    requests = list_requests(state, job_id)
    decisions = list_decisions(state, job_id)
    by_request = {str(item.get("request_id") or ""): item for item in decisions}
    rows: list[dict[str, Any]] = []
    for request in requests:
        decision = by_request.get(str(request.get("request_id") or ""))
        rows.append(
            {
                "request_id": str(request.get("request_id") or ""),
                "ordinal": request_ordinal(state, str(request.get("request_id") or "")),
                "task_id": str(request.get("task_id") or ""),
                "attempt_id": str(request.get("attempt_id") or ""),
                "failure": dict(request.get("failure") or {}),
                "contract": dict(request.get("contract") or {}),
                "supersession_permitted": bool(request.get("supersession_permitted")),
                "envelope": dict(request.get("envelope") or {}),
                "outstanding_question": str(request.get("outstanding_question") or ""),
                "decision": (
                    {
                        "decision": str(decision.get("decision") or ""),
                        "adapter": str(decision.get("adapter") or ""),
                        "decided_at": str(decision.get("decided_at") or ""),
                        "rationale": str(decision.get("rationale") or ""),
                        "corrected_tests": list(decision.get("corrected_tests") or []),
                        "supersedes": decision.get("supersedes"),
                        # A journal value, recomputed; the document has no digest field.
                        "digest": decision_digest(decision),
                    }
                    if decision
                    else None
                ),
            }
        )
    return {
        "job_id": str(job_id),
        "requests": rows,
        "supersessions": _job_supersessions(state, job_id),
        "legacy": legacy_records(state, job_id),
    }


def _job_supersessions(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    try:
        job = jobs.load_job(Path(state_dir), job_id, recover=False)
    except jobs.JobError:
        return []
    block = job.get("recovery") if isinstance(job.get("recovery"), Mapping) else {}
    return [dict(item) for item in block.get("supersessions") or [] if isinstance(item, Mapping)]


__all__ = [
    "CANONICAL_CODES",
    "CLASSES",
    "DECISION_SCHEMA",
    "DECISIONS",
    "DEFAULT_MAX_ATTEMPTS_PER_TASK",
    "EVIDENCE_SCHEMA",
    "GATE_AWAITING_APPROVAL",
    "LEGACY_SCHEMA",
    "MAX_ATTEMPTS_CAP",
    "MAX_CORRECTIONS",
    "RECOVERY_DIR",
    "RecoveryError",
    "SCHEMA",
    "SELECTION_CODES",
    "apply_ready_decisions",
    "attributed",
    "build_request",
    "canonical_bytes",
    "canonical_code_for",
    "class_for_code",
    "classify_failure",
    "contract_path",
    "contract_tests",
    "current_selection",
    "decision_digest",
    "decision_path",
    "derive_superseding_contract",
    "deterministic_adapter",
    "envelope_for",
    "escalate",
    "exhausted",
    "find_request",
    "is_legacy_decision",
    "is_legacy_request",
    "label_legacy",
    "legacy_label_path",
    "legacy_records",
    "legacy_reasons",
    "list_decisions",
    "list_requests",
    "load_request",
    "outstanding",
    "protocol_validator",
    "read_decision",
    "record_decision",
    "recovery_dir",
    "remaining_attempts",
    "request_digest",
    "request_id_for",
    "request_ordinal",
    "request_path",
    "summarize",
    "supersession_permitted",
    "test_authorized",
    "workflow_src",
]
