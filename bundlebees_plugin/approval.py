"""One-time owner approval for exactly one Bundlebees job.

The job authorization path in :mod:`bundlebees_plugin.job` answers "may this
work happen at all?" by requiring a human to type the authorization at a TTY.
That works when the owner is at the keyboard, and it deliberately does not work
for an AFK coordinator. This module adds the narrow bridge that an application
can use instead: the owner reads a plain-language preview, clicks Approve or
Decline once, and the trusted application signs a receipt with a key the agent
cannot read.

The shape is four documents, none of which is authority by itself:

``job-approvals/requests/<request_id>.json``
    The agent's proposal. It is inert. It carries the full binding and a
    SHA-256 digest over that binding, so a reader can tell an edited preview
    from the one that was shown.
``job-approvals/receipts/<request_id>.json``
    The owner's decision, HMAC-SHA256 signed with
    ``BUNDLEBEES_APPROVAL_SECRET`` read only through Hermes
    ``agent.secret_scope.get_secret``. A receipt that does not verify, has
    expired, or whose binding differs from the authorization is refused.
``job-approvals/consumed/<request_id>.json``
    The one-time consumption marker. Replaying the same consumer is idempotent;
    a second, different consumer is refused.
``authorizations/<job_id>.json``
    The sealed authorization, now carrying ``authority: owner-approval`` and an
    ``approval`` block with the receipt id and digest for audit.

The bridge is intentionally not a second authority system. It authorizes one
trial run and nothing else: no merge, push, release, deploy, campaign
acceptance or integration acceptance is granted here. The key is not a plugin
setting, an environment variable or a file this module creates; when the
Hermes secret scope has no ``BUNDLEBEES_APPROVAL_SECRET`` the path fails closed
with a named installation requirement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .leases import _canonical

#: The only secret this module reads. It belongs to the trusted approval app.
APPROVAL_SECRET_NAME = "BUNDLEBEES_APPROVAL_SECRET"

#: Domain separation for the owner capability a trusted route must present.
OWNER_CAPABILITY_LABEL = b"bundlebees/job-approval-owner-capability/1"

#: A namespace of its own: ``approvals/`` already holds job gate decisions.
APPROVAL_DIR = "job-approvals"
REQUEST_DIR = "requests"
RECEIPT_DIR = "receipts"
CONSUMED_DIR = "consumed"

REQUEST_SCHEMA = "rizzstack.job-approval-request/1"
RECEIPT_SCHEMA = "rizzstack.job-approval-receipt/1"
CONSUMPTION_SCHEMA = "rizzstack.job-approval-consumption/1"

DEFAULT_TTL_S = 900
MAX_TTL_S = 24 * 60 * 60
MIN_KEY_BYTES = 32

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_MAX_DOCUMENT_BYTES = 1_000_000

_FIX_KEY = (
    "Install the owner-held secret BUNDLEBEES_APPROVAL_SECRET in the Hermes "
    "secret scope where the trusted approval application runs. The Bundlebees "
    "agent must not be able to read, mint or proxy it."
)
_FIX_REQUEST = (
    "Ask the owner to review the request again through the trusted application; "
    "do not edit a request or receipt by hand."
)
_FIX_DECISION = (
    "Let the owner decide the exact request that was shown, through the trusted "
    "application path."
)


class ApprovalError(Exception):
    """An approval request, decision or receipt was refused.

    ``code`` is a stable string so a coordinator can tell a stale preview from
    a forged receipt. ``message`` and ``fix`` never contain key material.
    """

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _digest(value: Any) -> str:
    try:
        return hashlib.sha256(_canonical(value)).hexdigest()
    except Exception as exc:  # noqa: BLE001 - every naming failure is the same refusal
        raise ApprovalError(
            "approval_invalid",
            "an approval document is not canonical JSON data",
            _FIX_REQUEST,
        ) from exc


def _sign(body: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical(dict(body)), hashlib.sha256).hexdigest()


def _job_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _JOB_ID_RE.fullmatch(text):
        raise ApprovalError(
            "approval_invalid",
            "job_id must be 1-96 characters of letters, digits, '-', '_' or '.'",
            _FIX_REQUEST,
        )
    return text


def _repo(value: Any) -> str:
    text = str(value or "").strip()
    if text.count("/") != 1 or not all(part.strip() for part in text.split("/")) or any(
        ch.isspace() for ch in text
    ):
        raise ApprovalError("approval_invalid", "repo must be 'owner/name'", _FIX_REQUEST)
    return text


def _rev(value: Any, *, what: str) -> str:
    text = str(value or "").strip().lower()
    if not (_SHA40_RE.fullmatch(text) or _SHA64_RE.fullmatch(text)):
        raise ApprovalError(
            "approval_invalid",
            f"{what} must be a full 40 or 64 character hex commit id",
            _FIX_REQUEST,
        )
    return text


def _scope(values: Iterable[Any]) -> list[str]:
    if isinstance(values, str):
        values = [values]
    globs: list[str] = []
    for entry in values or []:
        text = str(entry or "").strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/") or ".." in Path(text).parts:
            raise ApprovalError(
                "approval_invalid",
                f"scope entry {text!r} is absolute or escapes the repository",
                _FIX_REQUEST,
            )
        while text.startswith("./"):
            text = text[2:]
        globs.append(text)
    if not globs:
        raise ApprovalError(
            "approval_invalid", "an approval request needs at least one scope glob", _FIX_REQUEST
        )
    return sorted(set(globs))


def _gates(values: Sequence[Any]) -> list[str]:
    if isinstance(values, str):
        values = [values]
    gates = sorted(
        {
            "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(gate or "").strip().lower())
            for gate in values or ()
        }
        - {""}
    )
    if not gates:
        raise ApprovalError(
            "approval_invalid", "an approval request needs at least one human gate", _FIX_REQUEST
        )
    return gates


def _budgets(bees: Any, wall_clock_s: Any, extra: Mapping[str, Any] | None) -> dict[str, Any]:
    for name, value in (("bees", bees), ("wall_clock_s", wall_clock_s)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApprovalError(
                "approval_invalid", f"the {name} budget must be an integer", _FIX_REQUEST
            )
    if int(bees) < 1:
        raise ApprovalError(
            "approval_invalid", "the bee budget must be at least 1", _FIX_REQUEST
        )
    if int(wall_clock_s) < 60:
        raise ApprovalError(
            "approval_invalid", "the wall-clock budget must be at least 60 s", _FIX_REQUEST
        )
    if extra is None:
        extra = {}
    if not isinstance(extra, Mapping):
        raise ApprovalError(
            "approval_invalid", "additional budgets must be a mapping", _FIX_REQUEST
        )
    reserved = sorted(set(extra) & {"bees", "wall_clock_s"})
    if reserved:
        raise ApprovalError(
            "approval_invalid",
            "additional budgets may not restate: " + ", ".join(reserved),
            _FIX_REQUEST,
        )
    return {"bees": int(bees), "wall_clock_s": int(wall_clock_s), **dict(extra)}


def _clock(now: Any = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(now, datetime):
        value = now
    else:
        try:
            value = datetime.fromisoformat(str(now).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApprovalError(
                "approval_time_invalid", f"{now!r} is not an ISO-8601 time", _FIX_REQUEST
            ) from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expiry(document: Mapping[str, Any], what: str) -> datetime:
    text = str(document.get("expires_at") or "").strip()
    if not text:
        raise ApprovalError(
            f"{what}_invalid", f"the {what} has no expiry", _FIX_REQUEST
        )
    return _clock(text)


def _request_id(value: Any) -> str:
    text = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(text):
        raise ApprovalError(
            "approval_request_invalid", "request id is not a safe document name", _FIX_REQUEST
        )
    return text


def _subdir(state_dir: Path, name: str) -> Path:
    path = Path(state_dir) / APPROVAL_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def request_path(state_dir: Path, request_id: str) -> Path:
    return _subdir(Path(state_dir), REQUEST_DIR) / f"{_request_id(request_id)}.json"


def receipt_path(state_dir: Path, request_id: str) -> Path:
    return _subdir(Path(state_dir), RECEIPT_DIR) / f"{_request_id(request_id)}.json"


def consumed_path(state_dir: Path, request_id: str) -> Path:
    return _subdir(Path(state_dir), CONSUMED_DIR) / f"{_request_id(request_id)}.json"


def _read_json(path: Path, what: str) -> dict[str, Any]:
    """Read one private JSON object, refusing links and non-regular files."""

    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError as exc:
        raise ApprovalError(
            f"{what}_missing", f"no {what} document exists at {path}", _FIX_REQUEST
        ) from exc
    except OSError as exc:
        raise ApprovalError(
            f"{what}_invalid", f"the {what} document could not be opened safely", _FIX_REQUEST
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ApprovalError(
                f"{what}_invalid", f"the {what} document is not a regular file", _FIX_REQUEST
            )
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(_MAX_DOCUMENT_BYTES + 1)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise ApprovalError(
            f"{what}_invalid", f"the {what} document is implausibly large", _FIX_REQUEST
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApprovalError(
            f"{what}_invalid", f"the {what} document is not valid JSON", _FIX_REQUEST
        ) from exc
    if not isinstance(document, dict):
        raise ApprovalError(
            f"{what}_invalid", f"the {what} document must be a JSON object", _FIX_REQUEST
        )
    return document


def _publish(path: Path, document: Mapping[str, Any]) -> bool:
    """Publish an immutable 0600 document exactly once."""

    from .job import _create_only  # noqa: PLC0415 - job imports this module lazily

    return _create_only(path, _canonical(dict(document)) + b"\n", mode=0o600)


def _existing(path: Path) -> bool:
    return path.exists()


def read_receipt_file(path: Path) -> dict[str, Any]:
    """Read a receipt from any path without trusting it; verification is separate."""

    return _read_json(Path(path), "approval_receipt")


# ---------------------------------------------------------------------------
# The key: Hermes secret scope only, with an explicit test seam
# ---------------------------------------------------------------------------


def _key_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        raise ApprovalError(
            "approval_key_invalid", "the approval key must be bytes or text", _FIX_KEY
        )
    if len(data) < MIN_KEY_BYTES:
        raise ApprovalError(
            "approval_key_weak",
            f"the approval key must be at least {MIN_KEY_BYTES} bytes of secret material",
            _FIX_KEY,
        )
    return data


def approval_key(signing_key: bytes | str | None = None) -> bytes:
    """Return the owner approval key, or fail closed with an installation fix.

    ``signing_key`` exists for tests and for an embedding application that
    already holds the key. Production callers pass nothing: the value comes
    from Hermes ``agent.secret_scope.get_secret`` and is never read from the
    environment, a config file or the coordinator state directory.
    """

    if signing_key is not None:
        return _key_bytes(signing_key)
    try:
        from agent.secret_scope import get_secret
    except Exception as exc:  # noqa: BLE001 - absent scope is a named refusal
        raise ApprovalError(
            "approval_key_unavailable",
            f"the Hermes secret scope is not available to read {APPROVAL_SECRET_NAME}",
            _FIX_KEY,
        ) from exc
    try:
        value = get_secret(APPROVAL_SECRET_NAME)
    except Exception as exc:  # noqa: BLE001 - unscoped secrets fail closed
        raise ApprovalError(
            "approval_key_unavailable",
            f"the owner approval secret {APPROVAL_SECRET_NAME} is not readable in this scope",
            _FIX_KEY,
        ) from exc
    if not value:
        raise ApprovalError(
            "approval_key_unavailable",
            f"the owner approval secret {APPROVAL_SECRET_NAME} is not configured",
            _FIX_KEY,
        )
    return _key_bytes(str(value))


def owner_capability(request_id: str, *, signing_key: bytes | str | None = None) -> str:
    """The per-request capability a trusted owner-facing route must present.

    It is an HMAC over the approval key and the request id, not the key. The
    dashboard computes it after reading ``BUNDLEBEES_APPROVAL_SECRET`` through
    ``agent.secret_scope.get_secret`` and keeps it in memory for the click. It
    is never written into a request, receipt, page or log, and it is useless
    for any other request. A caller that can merely run Python against the
    state directory cannot produce it.
    """

    key = approval_key(signing_key)
    return _capability(key, _request_id(request_id))


def _capability(key: bytes, request_id: str) -> str:
    return hmac.new(
        key,
        OWNER_CAPABILITY_LABEL + b"\x00" + request_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def code_revision(root: Path | None = None) -> str:
    """The Bundlebees checkout's current commit, or ``""`` when unreadable.

    The owner approves one coordinator implementation, not "whatever runs
    later". The comparison happens in :func:`bundlebees_plugin.job.record_authorization`;
    a checkout that cannot name its revision fails closed rather than guessing.
    """

    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = result.stdout.strip().lower()
    if result.returncode != 0 or not _SHA40_RE.fullmatch(text):
        return ""
    return text


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


def _task_specs(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalise the task list exactly the way ``job.create_job`` will.

    A task set is part of what the owner approves, so this reuses the job
    normaliser rather than keeping a second, subtly different copy. The
    coordinator-written keys (state, attempts, binding, ...) are dropped: they
    do not exist yet and are not part of the approval.
    """

    from .job import JobError, _normalize_task  # noqa: PLC0415 - avoids an import cycle

    if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Sequence):
        raise ApprovalError(
            "approval_tasks_invalid", "tasks must be a list of task objects", _FIX_REQUEST
        )
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in tasks:
        if not isinstance(raw, Mapping):
            raise ApprovalError(
                "approval_tasks_invalid", "every task must be an object", _FIX_REQUEST
            )
        try:
            normalized = _normalize_task(raw, for_create=True)
        except JobError as exc:
            raise ApprovalError("approval_tasks_invalid", exc.message, exc.fix) from exc
        spec = {
            key: normalized[key]
            for key in (
                "task_id",
                "objective",
                "lease",
                "depends_on",
                "contract_path",
                "contract_sha256",
                "acceptance",
                "contract",
                "provision",
            )
            if key in normalized
        }
        task_id = str(spec.get("task_id") or "")
        if task_id in seen:
            raise ApprovalError(
                "approval_tasks_invalid", f"duplicate task id {task_id!r}", _FIX_REQUEST
            )
        seen.add(task_id)
        specs.append(spec)
    if not specs:
        raise ApprovalError(
            "approval_tasks_invalid", "an approval request needs at least one task", _FIX_REQUEST
        )
    return specs


def tasks_digest(tasks: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 over the exact normalised task specs the owner approved."""

    return _digest(_task_specs(tasks))


def _recovery(recovery: Mapping[str, Any] | None, *, gates: Sequence[str], scope: Sequence[str]) -> dict[str, Any]:
    if recovery is None:
        return {}
    if not isinstance(recovery, Mapping):
        raise ApprovalError(
            "approval_recovery_invalid", "recovery must be a mapping", _FIX_REQUEST
        )
    from .job import JobError, _recovery_envelope  # noqa: PLC0415 - avoids an import cycle

    try:
        return dict(_recovery_envelope(recovery, gates=gates, scope=scope))
    except JobError as exc:
        raise ApprovalError("approval_recovery_invalid", exc.message, exc.fix) from exc


def _flag(value: Any, what: str) -> Any:
    """A planned coordinator stop/restart is recorded, not interpreted."""

    if value is None:
        raise ApprovalError(
            "approval_invalid", f"the planned coordinator {what} must be stated", _FIX_REQUEST
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        raise ApprovalError(
            "approval_invalid", f"the planned coordinator {what} must not be empty", _FIX_REQUEST
        )
    return text


def binding_for(
    *,
    job_id: str,
    statement: str,
    repo: str,
    base_rev: str,
    scope: Sequence[str],
    tasks: Sequence[Mapping[str, Any]],
    bees: int,
    wall_clock_s: int,
    worker_project: str,
    worker_profile: str,
    code_rev: str,
    coordinator_stop: Any,
    restart: Any,
    budgets: Mapping[str, Any] | None = None,
    gates: Sequence[str] = ("merge",),
    recovery: Mapping[str, Any] | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """The immutable set of facts one owner decision covers.

    Every dimension is explicit. A caller cannot omit the worker
    project/profile, the coordinator code revision or the planned stop/restart
    and have it inferred later: those are exactly the facts that decide what
    the trial run will touch.
    """

    text = str(statement or "").strip()
    if len(text) < 8:
        raise ApprovalError(
            "approval_invalid",
            "the statement must be a real sentence, not a placeholder",
            _FIX_REQUEST,
        )
    base = _rev(base_rev, what="base_rev")
    if len(base) != 40:
        raise ApprovalError(
            "approval_invalid", "base_rev must be a full 40-hex commit", _FIX_REQUEST
        )
    scope_globs = _scope(scope)
    specs = _task_specs(tasks)
    authorized_gates = _gates(gates)
    budget = _budgets(bees, wall_clock_s, budgets)
    envelope = _recovery(recovery, gates=authorized_gates, scope=scope_globs)
    result = {
        "job_id": _job_id(job_id),
        "statement": text,
        "repo": _repo(repo),
        "base_rev": base,
        "scope": scope_globs,
        "tasks": specs,
        "tasks_digest": _digest(specs),
        "budgets": budget,
        "worker": {
            "project": str(worker_project or "").strip(),
            "profile": str(worker_profile or "").strip(),
        },
        "code_rev": _rev(code_rev, what="code_rev"),
        "coordinator": {
            "stop": _flag(coordinator_stop, "stop"),
            "restart": _flag(restart, "restart"),
        },
        "gates": authorized_gates,
        "recovery": envelope,
        "recovery_digest": _digest(envelope),
    }
    if expires_at is not None:
        result["expires_at"] = _iso(_clock(expires_at))
    return result


def binding_digest(binding: Mapping[str, Any]) -> str:
    """SHA-256 over the canonical binding."""

    return _digest(dict(binding))


def request_digest(binding: Mapping[str, Any], expires_at: Any) -> str:
    """The exact digest the owner card displays.

    The expiry is part of the preview identity, not merely metadata: editing
    the request's expiry must invalidate the digest rather than silently
    extending how long the same approval can be used.
    """

    return _digest({"binding": dict(binding), "expires_at": _iso(_clock(expires_at))})


# ---------------------------------------------------------------------------
# Requests (the agent's inert proposal)
# ---------------------------------------------------------------------------


def create_request(
    state_dir: Path,
    *,
    job_id: str,
    statement: str,
    repo: str,
    base_rev: str,
    scope: Sequence[str],
    tasks: Sequence[Mapping[str, Any]],
    bees: int,
    wall_clock_s: int,
    worker_project: str,
    worker_profile: str,
    code_rev: str,
    coordinator_stop: Any,
    restart: Any,
    budgets: Mapping[str, Any] | None = None,
    ttl_s: int = DEFAULT_TTL_S,
    now: Any = None,
    gates: Sequence[str] = ("merge",),
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the one pending request for a job and return it.

    Repeating an identical request returns the existing one, so a retried
    agent turn is safe. A different request for the same job is refused while
    an unexpired, undecided request exists: the owner must never be shown two
    previews and have one of them silently win.
    """

    state = Path(state_dir)
    clock = _clock(now)
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not 1 <= ttl_s <= MAX_TTL_S:
        raise ApprovalError(
            "approval_invalid", f"ttl_s must be an integer from 1 to {MAX_TTL_S}", _FIX_REQUEST
        )
    expires_at = _iso(clock + timedelta(seconds=int(ttl_s)))
    binding = binding_for(
        job_id=job_id,
        statement=statement,
        repo=repo,
        base_rev=base_rev,
        scope=scope,
        tasks=tasks,
        bees=bees,
        wall_clock_s=wall_clock_s,
        worker_project=worker_project,
        worker_profile=worker_profile,
        code_rev=code_rev,
        coordinator_stop=coordinator_stop,
        restart=restart,
        budgets=budgets,
        gates=gates,
        recovery=recovery,
        expires_at=expires_at,
    )
    digest = request_digest(binding, expires_at)
    for existing in _all_requests(state, job_id=binding["job_id"]):
        if _request_spent(state, existing, now=clock):
            continue
        if existing.get("request_digest") == digest and existing.get("binding") == binding:
            # The same proposal, still pending: an idempotent retry.
            return existing
        raise ApprovalError(
            "approval_request_pending",
            f"job {binding['job_id']} already has an undecided approval request "
            f"({existing.get('request_id')}) with different facts",
            "Ask the owner to approve or decline the existing preview, or wait for it to expire.",
        )

    request = {
        "schema": REQUEST_SCHEMA,
        "request_id": f"apr_{secrets.token_hex(12)}",
        "created_at": _iso(clock),
        "expires_at": expires_at,
        "request_digest": digest,
        "binding": binding,
    }
    path = request_path(state, request["request_id"])
    if not _publish(path, request):
        raise ApprovalError(
            "approval_request_exists",
            f"a request document already exists at {path}",
            "Retry with a fresh request id; request ids are generated, not supplied.",
        )
    return request


def _verify_request(document: Mapping[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
    schema = str(document.get("schema") or "")
    if schema != REQUEST_SCHEMA:
        raise ApprovalError(
            "approval_request_invalid", f"unexpected request schema {schema!r}", _FIX_REQUEST
        )
    named = str(document.get("request_id") or "")
    if request_id is not None and named != request_id:
        raise ApprovalError(
            "approval_request_tampered",
            "the request document names a different request",
            _FIX_REQUEST,
        )
    _request_id(named)
    binding = document.get("binding")
    if not isinstance(binding, Mapping):
        raise ApprovalError(
            "approval_request_invalid", "the request has no binding object", _FIX_REQUEST
        )
    if str(document.get("request_digest") or "") != request_digest(binding, document.get("expires_at")):
        raise ApprovalError(
            "approval_request_tampered",
            f"the binding or expiry in request {named} does not match its recorded digest",
            _FIX_REQUEST,
        )
    specs = binding.get("tasks")
    if not isinstance(specs, list) or not specs:
        raise ApprovalError(
            "approval_request_invalid", "the request binding names no tasks", _FIX_REQUEST
        )
    if str(binding.get("tasks_digest") or "") != _digest(specs):
        raise ApprovalError(
            "approval_request_tampered",
            f"the task list in request {named} does not match its task digest",
            _FIX_REQUEST,
        )
    _expiry(document, "approval_request")
    return dict(document)


def load_request(state_dir: Path, request_id: str) -> dict[str, Any]:
    """Load and self-verify one request. A damaged request is a named refusal."""

    request = _read_json(request_path(state_dir, request_id), "approval_request")
    return _verify_request(request, request_id=_request_id(request_id))


def _all_requests(state_dir: Path, *, job_id: str | None = None) -> list[dict[str, Any]]:
    directory = _subdir(Path(state_dir), REQUEST_DIR)
    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        request = _verify_request(_read_json(path, "approval_request"), request_id=path.stem)
        binding = request["binding"]
        if job_id is not None and str(binding.get("job_id")) != str(job_id):
            continue
        found.append(request)
    found.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("request_id"))), reverse=True)
    return found


def _request_spent(state_dir: Path, request: Mapping[str, Any], *, now: datetime) -> bool:
    request_id = str(request.get("request_id") or "")
    if receipt_path(state_dir, request_id).exists():
        return True
    if consumed_path(state_dir, request_id).exists():
        return True
    return _expiry(request, "approval_request") <= now


def list_pending(
    state_dir: Path, *, job_id: str | None = None, now: Any = None
) -> list[dict[str, Any]]:
    """Every undecided, unexpired request, newest first."""

    clock = _clock(now)
    return [
        request
        for request in _all_requests(Path(state_dir), job_id=job_id)
        if not _request_spent(Path(state_dir), request, now=clock)
    ]


def pending_for_job(state_dir: Path, job_id: str, *, now: Any = None) -> dict[str, Any] | None:
    """The one pending request for a job, with ambiguity refused explicitly."""

    pending = list_pending(state_dir, job_id=_job_id(job_id), now=now)
    if len(pending) > 1:
        raise ApprovalError(
            "approval_request_ambiguous",
            f"job {job_id} has {len(pending)} pending approval requests: "
            + ", ".join(str(item.get("request_id")) for item in pending),
            "Decide or discard all but one; never guess which preview the owner saw.",
        )
    return pending[0] if pending else None


def preview(request: Mapping[str, Any]) -> dict[str, Any]:
    """Plain-language lines the trusted application can show the owner.

    This is presentation only. The decision binds the digest, not these
    strings, so a renderer cannot change what is approved by wording it
    differently.
    """

    binding = request.get("binding") if isinstance(request, Mapping) else None
    if not isinstance(binding, Mapping):
        raise ApprovalError(
            "approval_request_invalid", "the request has no binding to preview", _FIX_REQUEST
        )
    tasks = binding.get("tasks") or []
    worker = binding.get("worker") or {}
    coordinator = binding.get("coordinator") or {}
    budgets = binding.get("budgets") or {}
    extra = {name: value for name, value in budgets.items() if name not in {"bees", "wall_clock_s"}}
    recovery = binding.get("recovery") or {}
    gates = binding.get("gates") or []
    task_lines = [
        f"  - {task.get('task_id')}: {task.get('objective') or '(no objective)'} "
        f"[{', '.join(str(path) for path in task.get('lease') or [])}]"
        for task in tasks
        if isinstance(task, Mapping)
    ]
    lines = [
        f"Approve one trial run of job {binding.get('job_id')}?",
        f"Repository: {binding.get('repo')} at {str(binding.get('base_rev'))[:12]}",
        f"Statement: {binding.get('statement')}",
        f"Tasks ({len(tasks)}):",
        *task_lines,
        f"File scope: {', '.join(str(item) for item in binding.get('scope') or [])}",
        f"Budget: {budgets.get('bees')} bee(s), {budgets.get('wall_clock_s')} s wall clock",
        f"Worker: project {worker.get('project') or '(default)'}, "
        f"profile {worker.get('profile') or '(default)'}",
        f"Bundlebees revision: {binding.get('code_rev')}",
        f"Coordinator stop={coordinator.get('stop')} restart={coordinator.get('restart')}",
        f"Human gates still required: {', '.join(str(item) for item in gates)}",
    ]
    if extra:
        lines.append("Other budgets: " + json.dumps(extra, sort_keys=True))
    if recovery:
        lines.append("Recovery ceiling: " + json.dumps(recovery, sort_keys=True))
    lines += [
        f"Decide by {request.get('expires_at')} (request {request.get('request_id')}).",
        "This approves the trial run only: no merge, release, deploy or acceptance.",
    ]
    return {
        "request_id": request.get("request_id"),
        "request_digest": request.get("request_digest"),
        "headline": lines[0],
        "lines": lines,
        "tasks": [
            {"task_id": task.get("task_id"), "objective": task.get("objective")}
            for task in tasks
            if isinstance(task, Mapping)
        ],
    }


# ---------------------------------------------------------------------------
# Decisions (the trusted application's signed receipt)
# ---------------------------------------------------------------------------


def _verify_receipt(
    receipt: Mapping[str, Any],
    *,
    key: bytes,
    now: datetime,
    expected_binding: Mapping[str, Any] | None = None,
    check_expiry: bool = True,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ApprovalError(
            "approval_receipt_invalid", "a receipt must be a JSON object", _FIX_REQUEST
        )
    schema = str(receipt.get("schema") or "")
    if schema != RECEIPT_SCHEMA:
        raise ApprovalError(
            "approval_receipt_invalid", f"unexpected receipt schema {schema!r}", _FIX_REQUEST
        )
    signature = receipt.get("signature")
    if not isinstance(signature, str) or not signature:
        raise ApprovalError(
            "approval_receipt_invalid", "the receipt is not signed", _FIX_REQUEST
        )
    body = {name: value for name, value in receipt.items() if name != "signature"}
    expected = _sign(body, key)
    if not hmac.compare_digest(signature, expected):
        raise ApprovalError(
            "approval_signature_invalid",
            "the receipt signature does not verify with the owner approval key",
            "Refuse the receipt; ask the owner to decide again through the trusted application.",
        )
    binding = receipt.get("binding")
    if not isinstance(binding, Mapping):
        raise ApprovalError(
            "approval_receipt_invalid", "the receipt has no binding object", _FIX_REQUEST
        )
    recorded = str(receipt.get("request_digest") or "")
    if not recorded or recorded != request_digest(binding, receipt.get("expires_at")):
        raise ApprovalError(
            "approval_receipt_tampered",
            "the receipt binding does not match its recorded request digest",
            _FIX_REQUEST,
        )
    specs = binding.get("tasks")
    if not isinstance(specs, list) or not specs:
        raise ApprovalError(
            "approval_receipt_invalid", "the receipt binding names no tasks", _FIX_REQUEST
        )
    if str(binding.get("tasks_digest") or "") != _digest(specs):
        raise ApprovalError(
            "approval_receipt_tampered",
            "the receipt task list does not match its task digest",
            _FIX_REQUEST,
        )
    if str(binding.get("recovery_digest") or "") != _digest(binding.get("recovery") or {}):
        raise ApprovalError(
            "approval_receipt_tampered",
            "the receipt recovery ceiling does not match its digest",
            _FIX_REQUEST,
        )
    decision = str(receipt.get("decision") or "").strip().lower()
    if decision not in {"approved", "declined"}:
        raise ApprovalError(
            "approval_receipt_invalid", "the receipt decision must be approved or declined", _FIX_REQUEST
        )
    if not str(receipt.get("owner") or "").strip():
        raise ApprovalError(
            "approval_receipt_invalid", "the receipt names no owner", _FIX_REQUEST
        )
    expires = _expiry(receipt, "approval_receipt")
    if check_expiry and expires <= now:
        raise ApprovalError(
            "approval_expired",
            f"the owner decision expired at {receipt.get('expires_at')}",
            "Create a fresh request and ask the owner to decide again.",
        )
    if expected_binding is not None:
        if not isinstance(expected_binding, Mapping):
            raise ApprovalError(
                "approval_scope_changed", "the expected binding is not an object", _FIX_REQUEST
            )
        if _canonical(dict(binding)) != _canonical(dict(expected_binding)):
            changed = sorted(
                name
                for name in set(binding) | set(expected_binding)
                if binding.get(name) != expected_binding.get(name)
            )
            raise ApprovalError(
                "approval_scope_changed",
                "the authorization fields differ from the owner-approved binding"
                + (": " + ", ".join(changed) if changed else ""),
                "Refuse; ask the owner to approve the exact facts that will run.",
            )
    return dict(receipt)


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    signing_key: bytes | str | None = None,
    now: Any = None,
    expected_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify a receipt's signature, binding, decision and expiry.

    With ``expected_binding``, the binding must equal it exactly. This is the
    call the authorization path uses before a receipt can satisfy a non-TTY
    authorization, so a receipt for a different job, scope, budget or task set
    is refused rather than narrowed silently.
    """

    key = approval_key(signing_key)
    return _verify_receipt(
        receipt, key=key, now=_clock(now), expected_binding=expected_binding, check_expiry=True
    )


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """SHA-256 of the signed receipt body, used for audit references."""

    body = {name: value for name, value in dict(receipt).items() if name != "signature"}
    return _digest(body)


def load_receipt(
    state_dir: Path, request_id: str, *, signing_key: bytes | str | None = None, now: Any = None
) -> dict[str, Any]:
    """Load the receipt recorded for a request and verify it."""

    receipt = _read_json(receipt_path(state_dir, request_id), "approval_receipt")
    if str(receipt.get("request_id") or "") != str(request_id):
        raise ApprovalError(
            "approval_receipt_mismatch",
            f"the receipt at {receipt_path(state_dir, request_id)} names a different request",
            "Use the receipt written for this exact request id.",
        )
    return verify_receipt(receipt, signing_key=signing_key, now=now)


def record_decision(
    state_dir: Path,
    request_id: str,
    *,
    decision: str,
    expected_digest: str,
    owner: str,
    auth_method: str,
    owner_capability: str | None = None,
    signing_key: bytes | str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Record the owner's one decision as a signed receipt.

    This is the trusted-application path, not an agent command. The caller must
    present the owner capability for this exact request (see
    :func:`owner_capability`) as well as the digest it showed the owner. A
    plain in-process or shell call without that capability is refused with
    ``approval_owner_required`` and writes nothing, even when the secret
    happens to be configured: holding the state directory is not consent.

    Repeating the same decision with the same capability is idempotent, while a
    conflicting decision for an already-decided request is refused even after
    the receipt has been consumed.
    """

    state = Path(state_dir)
    capability = str(owner_capability or "").strip()
    if not capability:
        raise ApprovalError(
            "approval_owner_required",
            "recording a decision requires the owner capability presented by the trusted application",
            "Decide through the owner-facing route; the agent must not record decisions itself.",
        )
    request = load_request(state, request_id)
    key = approval_key(signing_key)
    if not hmac.compare_digest(capability, _capability(key, str(request["request_id"]))):
        raise ApprovalError(
            "approval_owner_capability_invalid",
            "the owner capability does not match this request",
            "Decide through the owner-facing route that displayed this request.",
        )
    digest = str(expected_digest or "").strip()
    if not digest or not hmac.compare_digest(digest, str(request.get("request_digest") or "")):
        raise ApprovalError(
            "approval_scope_changed",
            "the decision does not name the request digest that was shown",
            _FIX_DECISION,
        )
    clock = _clock(now)
    if _expiry(request, "approval_request") <= clock:
        raise ApprovalError(
            "approval_expired",
            f"the approval request expired at {request.get('expires_at')}",
            "Create a fresh request and ask the owner to decide again.",
        )
    verdict = str(decision or "").strip().lower()
    verdict = {
        "approve": "approved",
        "approved": "approved",
        "granted": "approved",
        "decline": "declined",
        "declined": "declined",
        "refused": "declined",
    }.get(verdict, verdict)
    if verdict not in {"approved", "declined"}:
        raise ApprovalError(
            "approval_decision_invalid", "decision must be 'approved' or 'declined'", _FIX_DECISION
        )
    named_owner = str(owner or "").strip()
    if not named_owner:
        raise ApprovalError(
            "approval_owner_name_required", "the decision must name the owner who made it", _FIX_DECISION
        )
    method = str(auth_method or "").strip()
    if not method:
        raise ApprovalError(
            "approval_auth_method_required",
            "the decision must name the trusted application path that authenticated the owner",
            _FIX_DECISION,
        )
    body: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "receipt_id": f"rcp_{secrets.token_hex(12)}",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "binding": request["binding"],
        "decision": verdict,
        "owner": named_owner,
        "auth_method": method,
        "issued_at": _iso(clock),
        "expires_at": request["expires_at"],
    }
    body["signature"] = _sign(body, key)
    path = receipt_path(state, request["request_id"])
    if _publish(path, body):
        return body
    existing = _read_json(path, "approval_receipt")
    verified = _verify_receipt(
        existing, key=key, now=clock, expected_binding=None, check_expiry=False
    )
    same = (
        str(verified.get("decision")) == verdict
        and str(verified.get("owner")) == named_owner
        and str(verified.get("auth_method")) == method
        and str(verified.get("request_digest")) == str(request.get("request_digest"))
        and _canonical(verified.get("binding") or {}) == _canonical(request["binding"])
    )
    if same:
        return verified
    raise ApprovalError(
        "approval_conflict",
        f"request {request['request_id']} already carries a different owner decision",
        "A decision is recorded once. Create a new request for new facts.",
    )


# ---------------------------------------------------------------------------
# One-time consumption
# ---------------------------------------------------------------------------


def consume_receipt(
    state_dir: Path,
    receipt: Mapping[str, Any] | str,
    *,
    signing_key: bytes | str | None = None,
    now: Any = None,
    expected_binding: Mapping[str, Any] | None = None,
    consumer: str = "",
) -> dict[str, Any]:
    """Mark an approved receipt consumed, idempotently for the same consumer.

    ``consumer`` is a stable identity such as a hash of the authorization
    fields. Repeating it succeeds and returns the original marker; a different
    consumer is a replay and is refused. A declined receipt never consumes.
    """

    state = Path(state_dir)
    clock = _clock(now)
    key = approval_key(signing_key)
    if isinstance(receipt, str):
        document = _read_json(receipt_path(state, receipt), "approval_receipt")
    elif isinstance(receipt, Mapping):
        document = dict(receipt)
    else:
        raise ApprovalError(
            "approval_receipt_invalid", "a receipt must be an object or request id", _FIX_REQUEST
        )
    verified = _verify_receipt(
        document, key=key, now=clock, expected_binding=expected_binding, check_expiry=True
    )
    if str(verified.get("decision")) != "approved":
        raise ApprovalError(
            "approval_declined",
            f"the owner declined request {verified.get('request_id')}",
            "Nothing is authorized. Ask for a new request if the work changes.",
        )
    identity = str(consumer or "").strip()
    if not identity:
        raise ApprovalError(
            "approval_consumer_required",
            "a receipt can only be consumed for a named consumer",
            "Pass the identity of the authorization this receipt is being spent on.",
        )
    marker = {
        "schema": CONSUMPTION_SCHEMA,
        "request_id": verified["request_id"],
        "receipt_id": verified["receipt_id"],
        "receipt_digest": receipt_digest(verified),
        "request_digest": verified["request_digest"],
        "consumer": identity,
        "consumed_at": _iso(clock),
    }
    path = consumed_path(state, str(verified["request_id"]))
    if _publish(path, marker):
        return {
            "consumed": True,
            "idempotent": False,
            "receipt": verified,
            "consumption": marker,
        }
    existing = _read_json(path, "approval_consumption")
    if (
        str(existing.get("receipt_digest")) == marker["receipt_digest"]
        and str(existing.get("consumer")) == identity
        and str(existing.get("request_digest")) == marker["request_digest"]
    ):
        return {
            "consumed": True,
            "idempotent": True,
            "receipt": verified,
            "consumption": existing,
        }
    raise ApprovalError(
        "approval_already_consumed",
        f"request {verified['request_id']} was already consumed by another authorization",
        "A receipt authorizes one trial run. Create a fresh request for new work.",
    )


__all__ = [
    "APPROVAL_SECRET_NAME",
    "APPROVAL_DIR",
    "ApprovalError",
    "approval_key",
    "binding_digest",
    "request_digest",
    "binding_for",
    "code_revision",
    "consume_receipt",
    "consumed_path",
    "create_request",
    "list_pending",
    "load_receipt",
    "load_request",
    "owner_capability",
    "pending_for_job",
    "preview",
    "read_receipt_file",
    "receipt_digest",
    "receipt_path",
    "record_decision",
    "request_path",
    "tasks_digest",
    "verify_receipt",
]
