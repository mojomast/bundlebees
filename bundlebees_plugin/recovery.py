"""Durable, evidence-based recovery for a rejected task (``rizzstack.recovery/1``).

A rejection is not a retry licence. The reconciler records *why* an attempt was
rejected as a durable **request**, and a separate, mechanically validated
**decision** authorises exactly one next step. Nothing in this module asks a
model what to do, and nothing dispatches on the strength of a prose note:

* the failure class is derived from evidence codes, never from a caller's
  opinion — a request whose recorded class disagrees with the code that
  produced it is refused (``request_forged``), and a decision that would widen
  the authorization is refused before it is written;
* ``environment`` and ``infrastructure`` never dispatch a worker, whatever the
  decision says (``decision_class_forbidden``);
* ``implementation`` and ``integration`` may retry only inside
  ``envelope.max_attempts_per_task`` (``retry_budget_exhausted``);
* a ``specification`` failure whose acceptance selection is unusable may be
  superseded **once**, and the superseding contract is derived mechanically
  from the current frozen contract plus the authorised ``corrected_tests``
  (``supersede_forbidden``, ``supersession_exhausted``, ``supersede_mismatch``);
* an exhausted or unattributable failure always ends in
  ``approval_gate.state = awaiting_approval`` — never in an accepted job.

The durable layout is one directory per job::

    recovery/<job_id>/<task_id>.<n>.request.json
    recovery/<job_id>/<task_id>.<n>.decision.json
    recovery/<job_id>/<task_id>.<n>.contract.json      # only after a supersede

Requests and decisions are canonical JSON with a SHA-256 digest over the body
excluding the digest field itself, so an edited file is a refusal rather than a
new instruction. Requests are create-only: a repeated tick re-reads the one that
already exists instead of writing a second, which is what makes crash recovery
and repeated ticks idempotent. Decisions are create-only too, and applying one
is idempotent because the *job document* records which request it applied.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import contract as acdd
from . import job as jobs
from .contract import ContractError

SCHEMA = "rizzstack.recovery/1"
DECISION_SCHEMA = "rizzstack.recovery-decision/1"

#: The five failure classes, most specific first. The order is the precedence
#: used when one evidence string names several codes.
CLASSES: tuple[str, ...] = (
    "specification",
    "environment",
    "infrastructure",
    "integration",
    "implementation",
)

#: The only three decisions a record may carry.
DECISIONS: tuple[str, ...] = ("retry", "supersede", "escalate")

#: Recovery directories live beside ``jobs/`` in the plugin state directory.
RECOVERY_DIR = "recovery"

#: The gate state an exhausted budget always produces.
GATE_AWAITING_APPROVAL = "awaiting_approval"
GATE_WAITING = "waiting"

#: Default ceiling for worker attempts per task (the first attempt plus one
#: corrective retry). An authorization may lower or raise the ceiling; a task
#: may only lower it.
DEFAULT_MAX_ATTEMPTS_PER_TASK = 2

#: The class that can never dispatch, and the class that can never retry.
_NO_DISPATCH = frozenset({"environment", "infrastructure"})
_NO_RETRY = frozenset({"specification"})

#: Specification codes that mean "the acceptance selection itself is unusable",
#: which is the only thing a supersession may repair.
SELECTION_CODES = frozenset(
    {
        "test_unresolved",
        "test_ref_unknown",
        "tests_uncollectable",
        "tests_undeclared",
        "acceptance_test_undeclared",
        "acceptance_invalid",
        "acceptance_uncovered",
        "contract_source_rev_malformed",
        "contract_source_rev_unreachable",
        "collection_required",
    }
)

_SPECIFICATION = {
    "test_unresolved",
    "test_ref_unknown",
    "test_not_found",
    "unknown_test",
    "tests_uncollectable",
    "tests_undeclared",
    "acceptance_test_undeclared",
    "acceptance_invalid",
    "acceptance_uncovered",
    "contract_invalid",
    "contract_not_frozen",
    "contract_changed",
    "contract_missing",
    "contract_source_rev_malformed",
    "contract_source_rev_unreachable",
    "collection_required",
    "red_missing",
    "red_invalid",
    "specification_invalid",
    "spec_mismatch",
}
_IMPLEMENTATION = {
    "tests_failed",
    "suite_failed",
    "tests_required",
    "patch_conflict",
    "patch_invalid",
    "commit_scope_mismatch",
    "commit_parent_mismatch",
    "commit_failed",
    "artifact_mismatch",
    "artifact_refused",
    "no_patch",
    "worker_patch_missing",
    "scope_violation",
    "tests_tampered",
    "secrets_found",
    "green_failed",
    "verification_failed",
    "worker_reported_failure",
}
_ENVIRONMENT = {
    "security_tool_missing",
    "sandbox_unavailable",
    "isolation_unavailable",
    "git_error",
    "tool_missing",
    "timeout",
    "verify_timeout",
    "disk_full",
    "permission_denied",
    "python_missing",
    "environment_unavailable",
    "worktree_missing",
    "no_workdir",
    "evidence_unreadable",
}
_INFRASTRUCTURE = {
    "launcher_unavailable",
    "dispatch_rejected",
    "dispatch_uncertain_exhausted",
    "dispatch_ambiguous",
    "attempt_missing",
    "lease_binding_failed",
    "provisioning_failed",
    "provisioning_unbound",
    "receipt_missing",
    "receipt_mismatch",
    "worker_failed",
    "worker_crashed",
    "worker_cancelled",
    "worker_lost",
    "transport_error",
    "network_error",
    "internal_error",
    "job_invalid",
    "job_missing",
    "state_unreadable",
    "unclassified_failure",
}
_INTEGRATION = {
    "integration_failed",
    "integration_error",
    "integrate_failed",
    "merge_conflict",
    "wave_incomplete",
    "suite_conflict",
}

_CODE_TABLE: dict[str, str] = {}
for _name, _class in (
    (_SPECIFICATION, "specification"),
    (_ENVIRONMENT, "environment"),
    (_INFRASTRUCTURE, "infrastructure"),
    (_INTEGRATION, "integration"),
    (_IMPLEMENTATION, "implementation"),
):
    for _code in _name:
        if _code in _CODE_TABLE:  # pragma: no cover - import-time self check
            raise RuntimeError(f"evidence code {_code!r} is mapped twice")
        _CODE_TABLE[_code] = _class

#: Strong phrases used only when the evidence carries no recognised code.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "specification",
        (
            "not in scope.test_files",
            "matches no collected test",
            "no such test",
            "test not found",
            "unknown test",
            "does not exist at",
            "not collected",
            "no red evidence",
            "acceptance criterion",
        ),
    ),
    (
        "environment",
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
        ("launcher", "connection", "network", "unavailable", "worker", "crashed"),
    ),
    ("integration", ("integration", "merge", "wave", "conflict")),
    (
        "implementation",
        ("test failed", "tests failed", "suite failed", "does not apply", "scope", "secret"),
    ),
)

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX_RECOVERY = (
    "Inspect the recorded request and decision under recovery/<job>/; do not hand-edit "
    "either file, and do not re-dispatch a rejected task without a validated decision."
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
# Canonical bytes, digests and durable files
# ---------------------------------------------------------------------------


def canonical_bytes(value: Any) -> bytes:
    """The exact bytes a digest is taken over: sorted keys, no whitespace."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(body: Mapping[str, Any], *excluded: str) -> str:
    drop = set(excluded) or {"request_digest", "decision_digest"}
    payload = {key: value for key, value in body.items() if key not in drop}
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def request_digest(request: Mapping[str, Any]) -> str:
    """SHA-256 over the request body excluding ``request_digest``."""

    return _digest(request, "request_digest")


def decision_digest(decision: Mapping[str, Any]) -> str:
    """SHA-256 over the decision body excluding ``decision_digest``."""

    return _digest(decision, "decision_digest")


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


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = canonical_bytes(dict(value))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _create_only(path: Path, value: Mapping[str, Any]) -> bool:
    """Publish a document only if it does not exist yet (atomic on POSIX)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = canonical_bytes(dict(value))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
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
# Failure classification
# ---------------------------------------------------------------------------


def class_for_code(code: Any) -> str | None:
    """The class a recognised evidence code belongs to, or ``None``."""

    return _CODE_TABLE.get(str(code or "").strip().lower())


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


def _failure(failure_class: str, code: str, evidence: str, rationale: str) -> dict[str, str]:
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

    An unrecognised record is ``infrastructure``/``unclassified_failure``: the
    coordinator could not attribute the failure to the artifact, so it must not
    dispatch a worker on the strength of a guess.
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
            actual = class_for_code(code)
            if actual is None:
                raise RecoveryError(
                    "classification_forged",
                    f"class {name!r} claims evidence code {code!r}, which is not a known code",
                )
            if actual != name:
                raise RecoveryError(
                    "classification_forged",
                    f"class {name!r} disagrees with evidence code {code!r} ({actual})",
                )
            return _failure(name, code, evidence, str(record.get("rationale") or ""))

    tokens = _tokens(record)
    for name in CLASSES:
        for token in tokens:
            if class_for_code(token) == name:
                return _failure(
                    name,
                    token,
                    evidence,
                    f"evidence code {token!r} is recorded as {_article(name)} {name} failure",
                )

    lowered = evidence.lower()
    for name, phrases in _KEYWORDS:
        for phrase in phrases:
            if phrase in lowered:
                return _failure(
                    name,
                    f"unrecognised_{name}",
                    evidence,
                    f"no recognised evidence code; the text names {phrase!r}, which is {_article(name)} {name} symptom",
                )

    declared_ids, test_files = contract_tests(contract)
    if contract is not None:
        for test_id in _test_ids_in(evidence):
            if test_id not in declared_ids and test_id.split("::", 1)[0] in test_files:
                return _failure(
                    "specification",
                    "acceptance_test_undeclared",
                    evidence,
                    f"the evidence names {test_id}, which lives in an approved test file but is not "
                    "declared by the frozen contract: the acceptance selection is unusable",
                )

    return _failure(
        "infrastructure",
        "unclassified_failure",
        evidence,
        "no recognised evidence code and no attributable cause; refusing to dispatch a worker on a guess",
    )


def _normalise_failure(
    failure: Any, contract: Mapping[str, Any] | None
) -> dict[str, str]:
    """Accept a classification or a raw record and always return a valid class."""

    if isinstance(failure, Mapping) and "class" in failure:
        return classify_failure(failure, contract=contract)
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


def _authorized_tests(contract: Mapping[str, Any] | None, task: Mapping[str, Any]) -> list[str]:
    """The test selections a correction may name, derived only from approvals.

    The default is the contract's approved test *files* (plus the task's own
    acceptance tests), so a corrected test may replace an unusable id inside an
    already-approved test file — and may never leave it.
    """

    allowed: set[str] = set()
    _, files = contract_tests(contract)
    allowed.update(files)
    acceptance = task.get("acceptance")
    if isinstance(acceptance, Mapping):
        for item in acceptance.get("tests") or []:
            if isinstance(item, str) and item.strip():
                allowed.add(item.strip())
                allowed.add(item.strip().split("::", 1)[0])
    return sorted(allowed)


def envelope_for(
    job: Mapping[str, Any], task: Mapping[str, Any], authorization: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The bounded envelope this job may act inside.

    An authorization sets the ceiling; the job or the task may only narrow it.
    """

    explicit = _explicit_envelope(authorization)
    ceiling = DEFAULT_MAX_ATTEMPTS_PER_TASK
    if "max_attempts_per_task" in explicit:
        ceiling = _positive_int(explicit.get("max_attempts_per_task"), "max_attempts_per_task")
    for source in (job.get("recovery_policy"), task.get("recovery_policy")):
        if isinstance(source, Mapping) and "max_attempts_per_task" in source:
            narrow = _positive_int(source.get("max_attempts_per_task"), "max_attempts_per_task")
            ceiling = min(ceiling, narrow)
    for source in (job, task):
        value = source.get("max_attempts")
        if value is not None:
            ceiling = min(ceiling, _positive_int(value, "max_attempts"))

    corrections = explicit.get("contract_corrections_allowed", True)
    if not isinstance(corrections, bool):
        raise RecoveryError("envelope_invalid", "contract_corrections_allowed must be a boolean")
    if isinstance(job.get("recovery_policy"), Mapping) and job["recovery_policy"].get("contract_corrections_allowed") is False:
        corrections = False
    if isinstance(task.get("recovery_policy"), Mapping) and task["recovery_policy"].get("contract_corrections_allowed") is False:
        corrections = False

    allowed = explicit.get("corrected_tests_allowed")
    if allowed is None:
        tests = _authorized_tests(task.get("contract") if isinstance(task.get("contract"), Mapping) else None, task)
    else:
        if not isinstance(allowed, list) or not all(isinstance(item, str) and item.strip() for item in allowed):
            raise RecoveryError("envelope_invalid", "corrected_tests_allowed must be a list of test selections")
        tests = sorted({item.strip() for item in allowed})
    return {
        "max_attempts_per_task": int(ceiling),
        "contract_corrections_allowed": bool(corrections),
        "corrected_tests_allowed": list(tests),
    }


def test_authorized(selection: Any, allowed: Sequence[str]) -> bool:
    """True when *selection* names an approved test file or an approved id.

    ``tests/test_a.py::Test::test_x`` is authorised by ``tests/test_a.py`` and by
    the exact id; ``tests/test_b.py::x`` is authorised by neither when only
    ``tests/test_a.py`` was approved.
    """

    if not isinstance(selection, str) or not selection.strip():
        return False
    candidate = selection.strip()
    file_part = candidate.split("::", 1)[0]
    for entry in allowed:
        item = str(entry).strip()
        if not item:
            continue
        if candidate == item or file_part == item:
            return True
        if item.endswith("::") and candidate.startswith(item):
            return True
    return False


def remaining_attempts(request: Mapping[str, Any], task: Mapping[str, Any] | None = None) -> int:
    """Attempts still allowed for this task, floored at zero.

    A live task's attempt list wins; without one (the deterministic adapter only
    has the request) the count recorded when the request was written is used, so
    an exhausted budget is never mistaken for a fresh one.
    """

    try:
        ceiling = int((request.get("envelope") or {}).get("max_attempts_per_task") or 0)
    except (TypeError, ValueError):
        ceiling = 0
    if isinstance(task, Mapping) and "attempts" in task:
        used = len([item for item in (task.get("attempts") or []) if item])
    else:
        try:
            used = int(request.get("attempts_used") or 0)
        except (TypeError, ValueError):
            used = ceiling
    return max(0, ceiling - used)


def attributed(request: Mapping[str, Any]) -> bool:
    """True when the failure was attributed to a recognised evidence code.

    An unattributable record (``unclassified_failure`` or a keyword guess) may
    never escalate on its own: the coordinator waits for a decision instead of
    inventing a verdict.
    """

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    code = str(failure.get("code") or "")
    if code.startswith("unrecognised_") or code == "unclassified_failure":
        return False
    return class_for_code(code) is not None


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
    explicit = job.get("feature_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    try:
        from . import packets  # noqa: PLC0415 - only packet jobs carry a feature id

        raw = packets.read_packet(Path(state_dir), str(job.get("job_id") or ""))
        if raw:
            document = json.loads(raw)
            if isinstance(document, Mapping):
                return str(document.get("feature_id") or "")
    except Exception:  # noqa: BLE001 - a missing packet is not a recovery problem
        return ""
    return ""


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

    if not envelope.get("contract_corrections_allowed"):
        return False
    if str(failure.get("class") or "") != "specification":
        return False
    if str(failure.get("code") or "") not in SELECTION_CODES:
        return False
    if not _contract_block(task).get("sha256"):
        return False
    return not _supersession_history(job, task)


def _existing_requests(state_dir: Path, job_id: str, task_id: str) -> list[dict[str, Any]]:
    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(base.glob(f"{_safe_id(task_id, 'task_id')}.*.request.json")):
        found.append(_read_json(path, "request"))
    found.sort(key=lambda item: int(item.get("number") or 0))
    return found


def list_requests(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """Every durable request for one job, oldest first."""

    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return []
    found = [_read_json(path, "request") for path in sorted(base.glob("*.request.json"))]
    found.sort(key=lambda item: (str(item.get("task_id") or ""), int(item.get("number") or 0)))
    return found


def list_decisions(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    """Every durable decision for one job, oldest first."""

    base = recovery_dir(state_dir, job_id)
    if not base.is_dir():
        return []
    found = [_read_json(path, "decision") for path in sorted(base.glob("*.decision.json"))]
    found.sort(key=lambda item: str(item.get("request_id") or ""))
    return found


def _number_of(request: Mapping[str, Any]) -> int:
    try:
        return int(request.get("number") or 0)
    except (TypeError, ValueError):
        return 0


def find_request(state_dir: Path, request_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Locate a request by its id, without trusting the id's shape for paths."""

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


def load_request(state_dir: Path, request_id: str) -> dict[str, Any]:
    """Read one request and re-prove its digest."""

    located = find_request(state_dir, request_id)
    if located is None:
        raise RecoveryError("request_missing", f"no recovery request {request_id!r} exists", _FIX_RECOVERY)
    _path, request = located
    _check_request(request)
    return request


def _check_request(request: Mapping[str, Any]) -> str:
    if str(request.get("schema") or "") != SCHEMA:
        raise RecoveryError("request_invalid", f"request schema {request.get('schema')!r} is not {SCHEMA!r}")
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
    actual_class = class_for_code(code)
    if actual_class is None:
        raise RecoveryError("request_forged", f"failure code {code!r} is not a known evidence code")
    if actual_class != name:
        raise RecoveryError(
            "request_forged",
            f"failure class {name!r} disagrees with its evidence code {code!r} ({actual_class})",
        )
    contract = request.get("contract")
    if not isinstance(contract, Mapping) or not isinstance(contract.get("sha256"), str):
        raise RecoveryError("request_invalid", "the request carries no contract block")
    envelope = request.get("envelope")
    if not isinstance(envelope, Mapping) or not isinstance(envelope.get("max_attempts_per_task"), int):
        raise RecoveryError("request_invalid", "the request carries no envelope")
    return actual


def _outstanding_question(task_id: str, failure: Mapping[str, Any], envelope: Mapping[str, Any],
                          remaining: int, can_supersede: bool) -> str:
    options: list[str] = []
    if can_supersede:
        options.append("supersede the frozen contract once with an authorised corrected test selection")
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
    """Record (once) the durable recovery request for a rejected attempt.

    Repeated calls for the same attempt and evidence code return the request
    that already exists; a different failure for the same attempt gets a new
    numbered request. Nothing here changes the job or dispatches anything.
    """

    state = Path(state_dir)
    job_id = _safe_id(job.get("job_id"), "job_id")
    task_id = _safe_id(task.get("task_id"), "task_id")
    contract = task.get("contract") if isinstance(task.get("contract"), Mapping) else None
    classified = _normalise_failure(failure, contract)
    envelope = envelope_for(job, task, authorization)
    attempt_id = str((task.get("attempts") or [""])[-1] or "")

    existing = _existing_requests(state, job_id, task_id)
    for request in existing:
        recorded = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
        if (
            str(request.get("attempt_id") or "") == attempt_id
            and str(recorded.get("code") or "") == classified["code"]
            and str(recorded.get("class") or "") == classified["class"]
        ):
            return request

    number = max((_number_of(item) for item in existing), default=0) + 1
    remaining = max(0, int(envelope["max_attempts_per_task"]) - len([a for a in task.get("attempts") or [] if a]))
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "request_id": request_id_for(job_id, task_id, number),
        "number": number,
        "job_id": job_id,
        "feature_id": _feature_id(state, job),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "failure": classified,
        "contract": _contract_block(task),
        "attempts_used": len([item for item in task.get("attempts") or [] if item]),
        "supersession_permitted": supersession_permitted(job, task, classified, envelope),
        "envelope": envelope,
        "outstanding_question": _outstanding_question(
            task_id, classified, envelope, remaining, supersession_permitted(job, task, classified, envelope)
        ),
        "created_at": _iso(_clock(now)),
    }
    body["request_digest"] = request_digest(body)

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
    """The newest request for a task that is still waiting for a decision."""

    job_id = str(job.get("job_id") or "")
    for task in job.get("tasks") or []:
        if str(task.get("state") or "") != "rejected":
            continue
        requests = _existing_requests(Path(state_dir), job_id, str(task.get("task_id") or ""))
        if not requests:
            continue
        request = requests[-1]
        if _decision_for(Path(state_dir), job_id, request) is None:
            return request
    return None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _decision_file(state_dir: Path, job_id: str, request: Mapping[str, Any]) -> Path:
    return decision_path(state_dir, job_id, str(request.get("task_id")), _number_of(request))


def _decision_for(state_dir: Path, job_id: str, request: Mapping[str, Any]) -> dict[str, Any] | None:
    path = _decision_file(state_dir, job_id, request)
    if not path.exists():
        return None
    return _read_json(path, "decision")


def read_decision(state_dir: Path, request: Mapping[str, Any]) -> dict[str, Any] | None:
    """The decision recorded for *request*, if any, with its digest re-proved."""

    job_id = str(request.get("job_id") or "")
    decision = _decision_for(Path(state_dir), job_id, request)
    if decision is None:
        return None
    recorded = str(decision.get("decision_digest") or "")
    actual = decision_digest(decision)
    if not recorded or recorded != actual:
        raise RecoveryError(
            "decision_tampered",
            f"decision for {request.get('request_id')!r} does not hash to its recorded digest",
        )
    return decision


def _decision_body(
    request: Mapping[str, Any], decision: Any, adapter: str, now: Any
) -> dict[str, Any]:
    if isinstance(decision, str):
        decision = {"decision": decision}
    if not isinstance(decision, Mapping):
        raise RecoveryError("decision_invalid", "a decision must be a mapping or a decision name")
    body: dict[str, Any] = {
        "schema": str(decision.get("schema") or DECISION_SCHEMA),
        "request_id": str(decision.get("request_id") or request.get("request_id") or ""),
        "request_digest": str(decision.get("request_digest") or request.get("request_digest") or ""),
        "adapter": str(decision.get("adapter") or adapter or ""),
        "decision": str(decision.get("decision") or "").strip().lower(),
        "corrected_tests": list(decision.get("corrected_tests") or []),
        "supersedes": decision.get("supersedes"),
        "rationale": _text(decision.get("rationale") or ""),
        "decided_at": str(decision.get("decided_at") or "") or _iso(_clock(now)),
    }
    unknown = sorted(set(decision) - set(body) - {"decision_digest"})
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


def _check_decision(state_dir: Path, request: Mapping[str, Any], decision: Mapping[str, Any]) -> None:
    """Every mechanical rule a decision must satisfy, in refusal order."""

    if str(decision.get("request_id") or "") != str(request.get("request_id") or ""):
        raise RecoveryError(
            "decision_mismatch",
            f"the decision names request {decision.get('request_id')!r}, not {request.get('request_id')!r}",
        )
    if str(decision.get("request_digest") or "") != str(request.get("request_digest") or ""):
        raise RecoveryError("decision_mismatch", "the decision does not name this request's digest")
    if str(decision.get("schema") or "") not in {DECISION_SCHEMA, SCHEMA}:
        raise RecoveryError("decision_invalid", f"decision schema {decision.get('schema')!r} is not known")
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

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    failure_class = str(failure.get("class") or "")
    envelope = request.get("envelope") if isinstance(request.get("envelope"), Mapping) else {}
    _job, task = _load_task(state_dir, request)

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
    if not isinstance(corrected, list) or not all(isinstance(item, str) and item.strip() for item in corrected):
        raise RecoveryError("decision_invalid", "corrected_tests must be a list of test selections")
    if corrected and not envelope.get("contract_corrections_allowed"):
        raise RecoveryError(
            "contract_correction_forbidden",
            "this envelope does not allow contract corrections, so no corrected test may be named",
        )
    allowed = envelope.get("corrected_tests_allowed") or []
    unauthorised = [item for item in corrected if not test_authorized(item, allowed)]
    if unauthorised:
        raise RecoveryError(
            "corrected_test_unauthorized",
            "corrected test(s) outside the approved test selection: " + ", ".join(sorted(unauthorised)),
        )

    supersedes = decision.get("supersedes")
    if name == "supersede":
        if not request.get("supersession_permitted"):
            raise RecoveryError("supersede_forbidden", "this request does not permit a contract supersession")
        if failure_class != "specification" or str(failure.get("code")) not in SELECTION_CODES:
            raise RecoveryError("supersede_forbidden", "only an unusable acceptance selection may be superseded")
        if _supersession_history(_job, task):
            raise RecoveryError("supersession_exhausted", "this task already consumed its one supersession")
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
        if declared_sha:
            derived = derive_superseding_contract(
                task, corrected, adapter=str(decision.get("adapter") or "deterministic"),
                decided_at=str(decision.get("decided_at") or ""), contract_id=declared_id or None,
            )
            if (declared_id and str(derived.get("id")) != declared_id) or acdd.check_frozen(derived) != declared_sha:
                raise RecoveryError(
                    "supersede_mismatch",
                    "the declared new contract id/sha256 is not the contract this decision would freeze",
                )
    elif supersedes is not None:
        raise RecoveryError("supersede_forbidden", "only a supersede decision may carry a supersedes block")


def record_decision(
    state_dir: Path,
    request_id: str,
    decision: Any,
    *,
    adapter: str = "deterministic",
    now: Any = None,
) -> dict[str, Any]:
    """Validate and durably record one decision for one request.

    Recording is create-only: a request is decided exactly once. A second
    decision — identical or not — is refused as ``decision_exists``, so a
    replayed command can never look like a second authorisation.
    """

    request = load_request(state_dir, request_id)
    body = _decision_body(request, decision, adapter, now)
    _check_decision(Path(state_dir), request, body)
    body["decision_digest"] = decision_digest(body)

    path = _decision_file(Path(state_dir), str(request.get("job_id") or ""), request)
    if not _create_only(path, body):
        existing = _read_json(path, "decision")
        same = str(existing.get("decision_digest") or "") == body["decision_digest"]
        raise RecoveryError(
            "decision_exists",
            f"{'a duplicate' if same else 'a conflicting'} decision is already recorded for "
            f"{request_id!r}; a request is decided exactly once",
            "Inspect recovery/<job>/<task>.<n>.decision.json; a recorded decision is never overwritten.",
        )
    return body


def deterministic_adapter(request: Mapping[str, Any]) -> dict[str, Any]:
    """The decision a coordinator can reach with no judgement at all.

    Environment and infrastructure failures escalate, a used-up budget
    escalates, an implementation or integration failure retries, and a
    specification failure escalates because correcting an acceptance selection
    needs an authorised replacement this adapter cannot invent.
    """

    failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
    failure_class = str(failure.get("class") or "")
    envelope = request.get("envelope") if isinstance(request.get("envelope"), Mapping) else {}
    limit = int(envelope.get("max_attempts_per_task") or 0)
    left = remaining_attempts(request)
    base = {
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


def _new_contract_id(base_id: str, number: int, job: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    taken = len(_supersession_history(job, task))
    candidate = f"{base_id}-r{number + taken}"
    if len(candidate) > 64:
        candidate = f"{base_id[:52]}-r{number + taken}"
    return candidate


def _test_slots(contract: Mapping[str, Any]) -> list[tuple[int, int]]:
    slots: list[tuple[int, int]] = []
    for index, criterion in enumerate(contract.get("acceptance_criteria") or []):
        if not isinstance(criterion, Mapping) or criterion.get("verification") != "test":
            continue
        for position, _ in enumerate(criterion.get("tests") or []):
            slots.append((index, position))
    return slots


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
    lease or drop a criterion.
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
    body = {key: copy.deepcopy(value) for key, value in dict(base).items() if key != "review"}
    body["supersedes"] = str(base.get("id") or "")
    body["supersedes_sha256"] = base_sha
    if contract_id:
        body["id"] = contract_id
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


def _authorization_or_none(state_dir: Path, job_id: str) -> dict[str, Any] | None:
    try:
        return jobs.load_authorization(Path(state_dir), job_id)
    except Exception:  # noqa: BLE001 - an absent authorization means the default envelope
        return None


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
        str((request.get("contract") or {}).get("id") or "contract"),
        _number_of(request),
        job,
        task,
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

    path = contract_path(Path(state_dir), job_id, str(task.get("task_id")), _number_of(request))
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
    escalation.
    """

    state = Path(state_dir)
    clock = _clock(now)
    stamp = _iso(clock)
    applied: list[dict[str, Any]] = []
    job_id = str(job.get("job_id") or "")

    for task in job.get("tasks") or []:
        task_id = str(task.get("task_id") or "")
        requests = _existing_requests(state, job_id, task_id)
        if not requests:
            continue
        request = requests[-1]
        recorded = task.get("recovery") if isinstance(task.get("recovery"), Mapping) else {}
        decision: dict[str, Any] | None
        try:
            decision = read_decision(state, request)
        except RecoveryError:
            raise
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

        _check_decision(state, request, decision)
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
    """The recovery view of a job: requests, decisions and supersessions."""

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
                        "decision_digest": str(decision.get("decision_digest") or ""),
                    }
                    if decision
                    else None
                ),
            }
        )
    return {"job_id": str(job_id), "requests": rows, "supersessions": _job_supersessions(state, job_id)}


def _job_supersessions(state_dir: Path, job_id: str) -> list[dict[str, Any]]:
    try:
        job = jobs.load_job(Path(state_dir), job_id, recover=False)
    except jobs.JobError:
        return []
    block = job.get("recovery") if isinstance(job.get("recovery"), Mapping) else {}
    return [dict(item) for item in block.get("supersessions") or [] if isinstance(item, Mapping)]


__all__ = [
    "CLASSES",
    "DECISION_SCHEMA",
    "DECISIONS",
    "DEFAULT_MAX_ATTEMPTS_PER_TASK",
    "GATE_AWAITING_APPROVAL",
    "RECOVERY_DIR",
    "RecoveryError",
    "SCHEMA",
    "SELECTION_CODES",
    "apply_ready_decisions",
    "attributed",
    "build_request",
    "canonical_bytes",
    "class_for_code",
    "classify_failure",
    "contract_path",
    "contract_tests",
    "decision_digest",
    "decision_path",
    "derive_superseding_contract",
    "deterministic_adapter",
    "envelope_for",
    "escalate",
    "exhausted",
    "find_request",
    "list_decisions",
    "list_requests",
    "load_request",
    "outstanding",
    "read_decision",
    "record_decision",
    "recovery_dir",
    "remaining_attempts",
    "request_digest",
    "request_id_for",
    "request_path",
    "summarize",
    "supersession_permitted",
    "test_authorized",
]
