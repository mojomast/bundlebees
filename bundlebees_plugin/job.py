"""Durable job documents and human authorizations.

Chapter 22 of the method: an HMAC proves integrity, not consent, so authority
lives in a separate document written through an interactive path, and a job may
only narrow it. Everything here is plain JSON on disk, signed with the same key
the lease records use, written atomically and readable after a crash.

Refusal codes are stable strings, because a coordinator that cannot tell "the
file was edited" from "the scope was widened" cannot respond correctly.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .leases import LeaseError, _canonical, _ensure_key, _load_key, _sign, _utc_now

JOB_SCHEMA_VERSION = 1

AUTH_DIR = "authorizations"
JOBS_DIR = "jobs"
APPROVALS_DIR = "approvals"

#: Task states, in the order a healthy task walks them.
TASK_STATES: tuple[str, ...] = (
    "pending",
    "dispatching",
    "dispatched",
    "running",
    "verifying",
    "verified",
    "integrating",
    "accepted",
)

#: Sideways and terminal states, each reachable only from the states listed.
TASK_SIDE_STATES: dict[str, tuple[str, ...]] = {
    "rejected": ("dispatching", "dispatched", "running", "verifying", "integrating"),
    "superseded": ("pending", "dispatched", "rejected"),
    "failed": tuple(TASK_STATES) + ("rejected",),
}

_LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("dispatching", "failed"),
    "dispatching": ("dispatched", "rejected", "failed"),
    "dispatched": ("running", "rejected", "superseded", "failed"),
    "running": ("verifying", "rejected", "failed"),
    "verifying": ("verified", "rejected", "failed"),
    "verified": ("integrating", "failed"),
    "integrating": ("accepted", "rejected", "failed"),
    "accepted": (),
    "rejected": ("dispatching", "superseded", "failed"),
    "superseded": (),
    "failed": (),
}

_FIX_TAMPERED = (
    "The file was edited outside the coordinator, or the key changed. Restore it "
    "from the recorded copy; do not hand-edit job or authorization documents."
)
_FIX_TRANSITION = "Only move a task along the documented state machine."
_FIX_SCOPE = "Narrow the lease to a path the authorization names."
_FIX_AUTH = "Ask the approver for an authorization that covers this work."


class JobError(Exception):
    """A job document, authorization or transition was refused."""

    def __init__(self, code: str, message: str, fix: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _subdir(state_dir: Path, name: str) -> Path:
    path = state_dir / name
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def authorization_path(state_dir: Path, job_id: str) -> Path:
    return _subdir(state_dir, AUTH_DIR) / f"{_job_id(job_id)}.json"


def job_path(state_dir: Path, job_id: str) -> Path:
    return _subdir(state_dir, JOBS_DIR) / f"{_job_id(job_id)}.json"


def approval_path(state_dir: Path, job_id: str, gate: str) -> Path:
    return _subdir(state_dir, APPROVALS_DIR) / f"{_job_id(job_id)}.{_slug(gate)}.json"


def _job_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 96:
        raise JobError("job_invalid", "job_id must be 1-96 characters", _FIX_AUTH)
    if not all(ch.isalnum() or ch in "-_." for ch in text):
        raise JobError(
            "job_invalid",
            "job_id may contain only letters, digits, '-', '_' and '.'",
            _FIX_AUTH,
        )
    return text


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise JobError("job_invalid", "gate name must not be empty", _FIX_AUTH)
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in text)


# ---------------------------------------------------------------------------
# Signed, atomically replaced documents
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
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


def _seal(body: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    document = dict(body)
    return {"body": document, "mac": _sign(document, key)}


def _unseal(path: Path, key: bytes, what: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise JobError(f"{what}_missing", f"{what} document does not exist: {path}", _FIX_AUTH) from exc
    except OSError as exc:
        raise JobError(f"{what}_invalid", f"{what} document could not be read", _FIX_TAMPERED) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise JobError(f"{what}_invalid", f"{what} document is not valid JSON", _FIX_TAMPERED) from exc
    if not isinstance(document, dict):
        raise JobError(f"{what}_invalid", f"{what} document must be an object", _FIX_TAMPERED)
    body = document.get("body")
    mac = document.get("mac")
    if not isinstance(body, dict) or not isinstance(mac, str):
        raise JobError(f"{what}_invalid", f"{what} document is not sealed", _FIX_TAMPERED)
    expected = _sign(body, key)
    if not hmac.compare_digest(mac, expected):
        raise JobError(f"{what}_tampered", f"{what} document failed its MAC check", _FIX_TAMPERED)
    return body


# ---------------------------------------------------------------------------
# Authorizations — written only through an interactive path
# ---------------------------------------------------------------------------


def record_authorization(
    state_dir: Path,
    *,
    job_id: str,
    statement: str,
    repo: str,
    base_rev: str,
    scope: Sequence[str],
    bees: int,
    wall_clock_s: int,
    gates: Sequence[str] = ("merge",),
    require_tty: bool = True,
    interactive: bool | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Record one human authorization. Immutable once written.

    ``interactive`` defaults to whether stdin is a TTY. A coordinator running
    from cron has no TTY and therefore cannot mint authority for itself.
    """

    is_tty = sys.stdin.isatty() if interactive is None else bool(interactive)
    if require_tty and not is_tty:
        raise JobError(
            "authorization_not_interactive",
            "an authorization must be typed at a terminal",
            "Have the approver run `bundlebees job authorize` themselves.",
        )

    text = str(statement or "").strip()
    if len(text) < 8:
        raise JobError(
            "authorization_incomplete",
            "the statement of intent must be a real sentence, not a placeholder",
            "Record what the approver actually asked for.",
        )
    globs = _normalize_scope(scope)
    if not globs:
        raise JobError("authorization_incomplete", "an authorization needs at least one scope glob", _FIX_AUTH)
    if not str(repo or "").strip() or "/" not in str(repo):
        raise JobError("authorization_incomplete", "repo must be 'owner/name'", _FIX_AUTH)
    if not _is_sha(base_rev):
        raise JobError("authorization_incomplete", "base_rev must be a full 40-hex commit", _FIX_AUTH)
    if int(bees) < 1:
        raise JobError("authorization_incomplete", "the bee budget must be at least 1", _FIX_AUTH)
    if int(wall_clock_s) < 60:
        raise JobError("authorization_incomplete", "the wall-clock budget must be at least 60 s", _FIX_AUTH)

    path = authorization_path(state_dir, job_id)
    if path.exists():
        raise JobError(
            "authorization_exists",
            "an authorization already exists for this job id",
            "Authorize a new job id; recorded authorizations are immutable.",
        )

    key = _ensure_key(state_dir)
    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "authorization",
        "job_id": _job_id(job_id),
        "auth_id": secrets.token_hex(8),
        "statement": text,
        "repo": str(repo).strip(),
        "base_rev": str(base_rev).strip().lower(),
        "scope": globs,
        "budget": {"bees": int(bees), "wall_clock_s": int(wall_clock_s)},
        "gates": sorted({_slug(gate) for gate in gates}) or ["merge"],
        "by": by or _current_user(),
        "at": _utc_now(),
    }
    _atomic_write(path, _canonical(_seal(body, key)) + b"\n", mode=0o400)
    return body


def load_authorization(state_dir: Path, job_id: str) -> dict[str, Any]:
    try:
        key = _load_key(state_dir)
    except LeaseError as exc:
        # No key means nothing was ever authorized here; that is a missing
        # authorization, not a tampered one.
        raise JobError(
            "authorization_missing",
            f"no authorization exists for job {job_id!r}",
            _FIX_AUTH,
        ) from exc
    return _unseal(authorization_path(state_dir, job_id), key, "authorization")


def record_approval(
    state_dir: Path,
    *,
    job_id: str,
    gate: str,
    decision: str,
    statement: str,
    require_tty: bool = True,
    interactive: bool | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Record a gate decision. Only a TTY can write one."""

    is_tty = sys.stdin.isatty() if interactive is None else bool(interactive)
    if require_tty and not is_tty:
        raise JobError(
            "approval_not_interactive",
            "a gate decision must be typed at a terminal",
            "Have the approver run `bundlebees job approve` themselves.",
        )
    verdict = str(decision or "").strip().lower()
    if verdict not in {"granted", "refused"}:
        raise JobError("approval_invalid", "decision must be 'granted' or 'refused'", _FIX_AUTH)

    path = approval_path(state_dir, job_id, gate)
    if path.exists():
        raise JobError(
            "approval_exists",
            "this gate already has a recorded decision",
            "A gate is decided once; start a new job for new work.",
        )

    key = _ensure_key(state_dir)
    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "approval",
        "job_id": _job_id(job_id),
        "gate": _slug(gate),
        "decision": verdict,
        "statement": str(statement or "").strip(),
        "by": by or _current_user(),
        "at": _utc_now(),
    }
    _atomic_write(path, _canonical(_seal(body, key)) + b"\n", mode=0o400)
    return body


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def create_job(
    state_dir: Path,
    *,
    job_id: str,
    tasks: Sequence[Mapping[str, Any]],
    summary: str = "",
) -> dict[str, Any]:
    """Create a job that narrows an existing authorization."""

    authorization = load_authorization(state_dir, job_id)
    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "job",
        "job_id": _job_id(job_id),
        "authorization": {
            "ref": f"{AUTH_DIR}/{_job_id(job_id)}.json",
            "auth_id": authorization["auth_id"],
        },
        "goal": {
            "summary": str(summary or authorization["statement"]),
            "repo": authorization["repo"],
            "base_rev": authorization["base_rev"],
        },
        "tasks": [_normalize_task(task) for task in tasks],
        "integration": {"wave": "w1", "state": "pending", "evidence": None},
        "approval_gate": {
            "required": True,
            "gates": list(authorization["gates"]),
            "state": "waiting",
        },
        "injected_failures": [],
        "events": [],
    }
    _enforce_narrowing(body, authorization)

    path = job_path(state_dir, job_id)
    if path.exists():
        raise JobError(
            "job_exists",
            "a job already exists for this id",
            "Pick a new job id; the recorded job is the record.",
        )
    save_job(state_dir, body)
    return body


def load_job(state_dir: Path, job_id: str) -> dict[str, Any]:
    try:
        key = _load_key(state_dir)
    except LeaseError as exc:
        raise JobError(
            "job_missing",
            f"no job exists for id {job_id!r}",
            _FIX_AUTH,
        ) from exc
    return _unseal(job_path(state_dir, job_id), key, "job")


def save_job(state_dir: Path, job: Mapping[str, Any]) -> None:
    key = _ensure_key(state_dir)
    _atomic_write(job_path(state_dir, job["job_id"]), _canonical(_seal(job, key)) + b"\n")


def transition(
    job: dict[str, Any],
    task_id: str,
    to_state: str,
    *,
    actor: str,
    reason: str = "",
    evidence: str | None = None,
) -> dict[str, Any]:
    """Move one task forward, enforcing the state machine and dependencies."""

    task = _task(job, task_id)
    current = task.get("state", "pending")
    if to_state not in _LEGAL_TRANSITIONS.get(current, ()):
        raise JobError(
            "job_bad_transition",
            f"task {task_id} cannot move from {current!r} to {to_state!r}",
            _FIX_TRANSITION,
        )
    if to_state == "dispatching":
        _require_dependencies_accepted(job, task)

    task["state"] = to_state
    if evidence:
        task["evidence"] = evidence
    job.setdefault("events", []).append(
        {
            "at": _utc_now(),
            "task_id": task_id,
            "transition": f"{current}->{to_state}",
            "actor": str(actor or "reconciler"),
            "reason": str(reason or ""),
        }
    )
    return job


def _task(job: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for task in job.get("tasks", []):
        if task.get("task_id") == task_id:
            return task  # type: ignore[return-value]
    raise JobError("job_invalid", f"job has no task {task_id!r}", _FIX_TRANSITION)


def _require_dependencies_accepted(job: Mapping[str, Any], task: Mapping[str, Any]) -> None:
    states = {t.get("task_id"): t.get("state") for t in job.get("tasks", [])}
    unmet = [dep for dep in task.get("depends_on") or [] if states.get(dep) != "accepted"]
    if unmet:
        raise JobError(
            "task_dependency_unmet",
            f"task {task.get('task_id')!r} depends on unfinished task(s): {', '.join(unmet)}",
            "Wait for the dependency to be accepted, or remove it from the plan.",
        )


# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------


def _normalize_task(task: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise JobError("job_invalid", "every task needs a task_id", _FIX_TRANSITION)
    lease = _normalize_scope(task.get("lease") or [])
    if not lease:
        raise JobError("job_invalid", f"task {task_id} needs at least one leased path", _FIX_SCOPE)
    depends_on = task.get("depends_on") or []
    if not isinstance(depends_on, (list, tuple)):
        raise JobError("job_invalid", f"task {task_id} depends_on must be a list", _FIX_TRANSITION)
    return {
        "task_id": task_id,
        "objective": str(task.get("objective") or ""),
        "lease": lease,
        "depends_on": [str(dep) for dep in depends_on],
        "contract_path": task.get("contract_path"),
        "contract_sha256": task.get("contract_sha256"),
        "acceptance": task.get("acceptance"),
        "contract": task.get("contract"),
        "provision": task.get("provision"),
        "binding": task.get("binding"),
        "provisioning": task.get("provisioning"),
        "state": str(task.get("state") or "pending"),
        "attempts": list(task.get("attempts") or []),
        "verify": None,
        "intent": None,
    }


def _normalize_scope(scope: Iterable[Any]) -> list[str]:
    globs: list[str] = []
    for entry in scope or []:
        text = str(entry or "").strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/") or ".." in Path(text).parts:
            raise JobError(
                "lease_out_of_scope",
                f"scope entry {text!r} is absolute or escapes the repository",
                _FIX_SCOPE,
            )
        while text.startswith("./"):
            text = text[2:]
        globs.append(text)
    return sorted(set(globs))


def _enforce_narrowing(job: Mapping[str, Any], authorization: Mapping[str, Any]) -> None:
    if job["goal"]["repo"] != authorization["repo"]:
        raise JobError(
            "authorization_widened",
            f"job repo {job['goal']['repo']!r} differs from the authorized repo {authorization['repo']!r}",
            _FIX_AUTH,
        )
    if job["goal"]["base_rev"] != authorization["base_rev"]:
        raise JobError(
            "authorization_widened",
            "job base_rev differs from the authorized base_rev",
            _FIX_AUTH,
        )
    budget = authorization["budget"]
    if len(job["tasks"]) > int(budget["bees"]):
        raise JobError(
            "authorization_widened",
            f"job has {len(job['tasks'])} tasks but the authorization allows {budget['bees']} bees",
            _FIX_AUTH,
        )
    for task in job["tasks"]:
        outside = [path for path in task["lease"] if not _covered(path, authorization["scope"])]
        if outside:
            raise JobError(
                "lease_out_of_scope",
                f"task {task['task_id']} leases path(s) outside the authorized scope: {', '.join(outside)}",
                _FIX_SCOPE,
            )


def _covered(path: str, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in globs)


def _is_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text.lower())


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def job_digest(job: Mapping[str, Any]) -> str:
    """A stable digest other documents can reference."""

    return hashlib.sha256(_canonical(dict(job))).hexdigest()
