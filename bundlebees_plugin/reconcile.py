"""Crash-safe reconciliation for durable jobs.

The reconciler is the only component that turns a durable dispatch intent into
a launcher attempt. Its central rule is that a dispatch whose outcome is
unknown is never blindly re-POSTed: after an uncertain POST the launcher
journal is consulted, and the task is either adopted (exactly one matching
attempt), left waiting (no matching attempt yet), or escalated (two plausible
attempts, or too many ticks without proof).

A dispatch intent is written atomically under ``<state_dir>/intents`` before
the first launcher call and is kept for audit. A single non-blocking ``flock``
under ``<state_dir>/locks`` serialises whole ticks, so two coordinators cannot
both dispatch the same task. Every task state change goes through
:func:`bundlebees_plugin.job.transition` with ``actor="reconciler"`` and is
saved immediately afterwards.

The launcher is injected. The protocol is intentionally small::

    launcher.idempotent                     # True only for server-side dedupe
    launcher.post(brief, idempotency_key=..) -> attempt id or mapping
    launcher.journal(task_id=.., job_id=.., idempotency_key=..) -> entries
    launcher.verify(attempt_id=.., task=..) -> bool or mapping

Only a launcher that advertises ``idempotent = True`` earns an
``exactly_once`` claim. Without that promise the reconciler still never
double-posts, but it reports ``exactly_once = False``.
"""

from __future__ import annotations

import errno
import inspect
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

try:  # pragma: no cover - fallback for platforms without fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from . import job as jobs

__all__ = [
    "INTENTS_DIR",
    "INTENT_DIR",
    "LOCKS_DIR",
    "LOCK_DIR",
    "RECONCILE_SCHEMA_VERSION",
    "DEFAULT_MAX_UNCERTAIN_TICKS",
    "STATUS_LOCKED",
    "STATUS_NOOP",
    "STATUS_DISPATCHED",
    "STATUS_NOT_SENT",
    "STATUS_RECONCILE_WAIT",
    "STATUS_ADOPTED",
    "STATUS_VERIFIED",
    "STATUS_VERIFY_FAILED",
    "EVIDENCE_PROVEN_PRESENT",
    "EVIDENCE_ADOPTED",
    "EVIDENCE_RETRIED",
    "DispatchNotSent",
    "DispatchUncertain",
    "ReconcileError",
    "TickResult",
    "Launcher",
    "LauncherProtocol",
    "intent_path",
    "intent_dir",
    "lock_path",
    "load_intent",
    "read_intent",
    "save_intent",
    "write_intent",
    "clear_intent",
    "tick",
    "run_tick",
    "reconcile",
    "reconcile_tick",
    "reconcile_job",
    "dispatch_tick",
    "verify_tick",
    "reconcile_once",
    "run",
    "dispatch",
    "verify",
    "dispatch_intent_path",
    "job_intent_path",
    "reconcile_lock_path",
    "job_lock_path",
    "DISPATCH_INTENT_DIR",
    "RECONCILE_LOCK_DIR",
    "load_dispatch_intent",
    "read_dispatch_intent",
    "save_dispatch_intent",
    "write_dispatch_intent",
    "reconcile_one",
    "reconcile_intent_path",
    "reconcile_intent_file",
    "intent_file",
    "intent_for_task",
    "dispatch_lock_path",
    "lock_file",
    "lock_for_job",
]

RECONCILE_SCHEMA_VERSION = 1
DEFAULT_MAX_UNCERTAIN_TICKS = 3

STATUS_LOCKED = "locked"
STATUS_NOOP = "noop"
STATUS_DISPATCHED = "dispatched"
STATUS_NOT_SENT = "not_sent"
STATUS_RECONCILE_WAIT = "reconcile_wait"
STATUS_ADOPTED = "adopted"
STATUS_VERIFIED = "verified"
STATUS_VERIFY_FAILED = "verify_failed"

EVIDENCE_PROVEN_PRESENT = "proven_present"
EVIDENCE_ADOPTED = "adopted"
EVIDENCE_RETRIED = "retried"

INTENTS_DIR = "intents"
INTENT_DIR = INTENTS_DIR
LOCKS_DIR = "locks"
LOCK_DIR = LOCKS_DIR
LOCK_SUFFIX = ".lock"
INTENT_SUFFIX = ".json"
DISPATCH_INTENT_DIR = INTENTS_DIR
RECONCILE_LOCK_DIR = LOCKS_DIR

INTENT_PREPARED = "prepared"
INTENT_SENDING = "sending"
INTENT_NOT_SENT = "not_sent"
INTENT_UNCERTAIN = "uncertain"
INTENT_SENT = "sent"
INTENT_ADOPTED = "adopted"
INTENT_UNCERTAIN_EXHAUSTED = "dispatch_uncertain_exhausted"
INTENT_AMBIGUOUS = "dispatch_ambiguous"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9._-]+")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_FIX_LOCK = "Wait for the current reconciler tick to finish, then retry."
_FIX_INTENT = (
    "Treat this state directory as untrusted; do not edit dispatch intents by hand."
)
_FIX_JOURNAL = (
    "Inspect the launcher journal directly. Do not re-dispatch until the attempt "
    "is proven absent or the ambiguity is resolved by a human."
)


class ReconcileError(Exception):
    """A reconciliation precondition or durable document was refused."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)
        super().__init__(self.message)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


class DispatchNotSent(Exception):
    """The launcher call provably never left the coordinator process.

    A launcher may raise this only before any bytes reach the server. The
    reconciler may safely make a fresh POST on a later tick.
    """


class DispatchUncertain(Exception):
    """The POST may or may not have reached the launcher.

    The intent is kept and the launcher journal is consulted. The exception may
    carry an ``attempt_id`` hint, but the journal remains authoritative.
    """

    def __init__(
        self,
        message: str = "dispatch outcome is uncertain",
        *,
        attempt_id: str | None = None,
        evidence: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id
        self.evidence = evidence


class Launcher(Protocol):
    """The injected launcher protocol understood by the reconciler."""

    idempotent: bool

    def post(self, brief: Mapping[str, Any], **kwargs: Any) -> Any: ...

    def journal(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]: ...

    def verify(self, **kwargs: Any) -> Any: ...


LauncherProtocol = Launcher


class TickResult(dict):
    """A mapping result with attribute access for convenience."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_id(value: Any, what: str = "job_id") -> str:
    text = str(value or "").strip()
    if not text or ".." in text or not _SAFE_ID_RE.match(text):
        raise ReconcileError(
            "reconcile_invalid",
            f"{what} is not a safe durable file name",
            _FIX_INTENT,
        )
    return text


def _safe_part(value: Any) -> str:
    text = _SAFE_PART_RE.sub("-", str(value or "").strip())
    text = text.strip(".-")
    if not text:
        raise ReconcileError(
            "reconcile_invalid",
            "task_id is not a safe durable file name",
            _FIX_INTENT,
        )
    return text[:96]


def _private_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise ReconcileError(
                "reconcile_invalid",
                f"{path.name} must be a private directory",
                _FIX_INTENT,
            )
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path
    except ReconcileError:
        raise
    except OSError as exc:
        raise ReconcileError(
            "reconcile_io",
            f"the {path.name} directory could not be created: {exc}",
            _FIX_INTENT,
        ) from exc


def intent_dir(state_dir: Path | str, job_id: str) -> Path:
    """The directory holding this job's durable dispatch intents."""

    checked = _safe_id(job_id)
    return _private_dir(Path(state_dir) / INTENTS_DIR / checked)


def intent_path(state_dir: Path | str, job_id: str, task_id: str) -> Path:
    """The durable intent path for one task."""

    checked_job = _safe_id(job_id)
    checked_task = _safe_part(task_id)
    directory = _private_dir(Path(state_dir) / INTENTS_DIR / checked_job)
    return directory / f"{checked_task}{INTENT_SUFFIX}"


def lock_path(state_dir: Path | str, job_id: str) -> Path:
    """The whole-tick lock path for one job."""

    checked = _safe_id(job_id)
    directory = _private_dir(Path(state_dir) / LOCKS_DIR)
    return directory / f"{checked}{LOCK_SUFFIX}"


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode)
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


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    try:
        payload = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReconcileError(
            "reconcile_invalid",
            "the dispatch intent is not JSON-serializable",
            _FIX_INTENT,
        ) from exc
    _atomic_write(path, payload + b"\n")


def read_intent(
    state_dir: Path | str, job_id: str, task_id: str
) -> dict[str, Any] | None:
    """Read one durable intent. A missing file is ``None``, not an error."""

    path = intent_path(state_dir, job_id, task_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReconcileError(
            "reconcile_io", "the dispatch intent could not be read", _FIX_INTENT
        ) from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError(
            "intent_tampered", "the dispatch intent is not valid JSON", _FIX_INTENT
        ) from exc
    if not isinstance(document, dict):
        raise ReconcileError(
            "intent_tampered", "the dispatch intent must be an object", _FIX_INTENT
        )
    if str(document.get("job_id")) != _safe_id(job_id):
        raise ReconcileError(
            "intent_tampered", "the dispatch intent names another job", _FIX_INTENT
        )
    if str(document.get("task_id")) != str(task_id):
        raise ReconcileError(
            "intent_tampered", "the dispatch intent names another task", _FIX_INTENT
        )
    return document


load_intent = read_intent


def save_intent(state_dir: Path | str, intent: Mapping[str, Any]) -> Path:
    """Atomically persist one intent and return its path."""

    job_id = _safe_id(intent.get("job_id"))
    task_id = str(intent.get("task_id") or "")
    path = intent_path(state_dir, job_id, task_id)
    _write_json(path, intent)
    return path


write_intent = save_intent


def clear_intent(state_dir: Path | str, job_id: str, task_id: str) -> None:
    try:
        intent_path(state_dir, job_id, task_id).unlink()
    except FileNotFoundError:
        pass


dispatch_intent_path = intent_path
job_intent_path = intent_path
reconcile_intent_path = intent_path
reconcile_intent_file = intent_path
intent_file = intent_path
intent_for_task = intent_path
reconcile_lock_path = lock_path
job_lock_path = lock_path
dispatch_lock_path = lock_path
lock_file = lock_path
lock_for_job = lock_path
load_dispatch_intent = read_intent
read_dispatch_intent = read_intent
save_dispatch_intent = save_intent
write_dispatch_intent = save_intent


class _Lock:
    def __init__(self, fd: int, path: Path, fallback: bool = False) -> None:
        self.fd = fd
        self.path = path
        self.fallback = fallback

    def release(self) -> None:
        if self.fallback:
            try:
                self.path.unlink()
            except OSError:
                pass
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass


def _try_lock(state_dir: Path | str, job_id: str) -> _Lock | None:
    path = lock_path(state_dir, job_id)
    if fcntl is None:  # pragma: no cover - exercised only off POSIX
        try:
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600
            )
        except FileExistsError:
            return None
        os.close(fd)
        return _Lock(-1, path, fallback=True)

    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    except OSError as exc:
        raise ReconcileError(
            "reconcile_io", "the reconciler lock could not be opened", _FIX_LOCK
        ) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise ReconcileError(
            "reconcile_io", "the reconciler lock could not be acquired", _FIX_LOCK
        ) from exc
    return _Lock(fd, path)


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "idempotent"}:
            return True
        if text in {"0", "false", "no", "off", "none", ""}:
            return False
    return None


def _launcher_idempotent(launcher: Any) -> bool:
    if launcher is None:
        return False
    for name in (
        "idempotent",
        "is_idempotent",
        "server_idempotent",
        "idempotent_dispatch",
        "supports_idempotency",
        "supports_server_idempotency",
        "server_side_idempotency",
        "dedupe",
    ):
        try:
            value = getattr(launcher, name)
        except Exception:
            continue
        if value is None:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        parsed = _flag(value)
        if parsed is not None:
            return parsed
    capabilities = getattr(launcher, "capabilities", None)
    if isinstance(capabilities, Mapping):
        for key in ("idempotent", "idempotency", "server_idempotent"):
            if key in capabilities:
                parsed = _flag(capabilities[key])
                if parsed is not None:
                    return parsed
    return False


def _call_supported(
    func: Callable[..., Any],
    values: Mapping[str, Any],
    *,
    leading: Any = None,
) -> tuple[Any, set[str]]:
    """Call ``func`` once, passing only parameters it declares.

    Signature inspection happens before the call so a launcher exception can
    never cause a second POST.
    """

    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        if leading is not None:
            return func(leading), set()
        return func(), set()

    params = list(signature.parameters.values())
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    used: set[str] = set()
    var_kw = any(p.kind is p.VAR_KEYWORD for p in params)
    var_pos = any(p.kind is p.VAR_POSITIONAL for p in params)

    for param in params:
        name = param.name
        if param.kind is param.POSITIONAL_ONLY:
            if name in values:
                args.append(values[name])
                used.add(name)
            elif leading is not None and not args:
                args.append(leading)
            elif param.default is param.empty:
                raise TypeError(f"cannot satisfy positional-only parameter {name!r}")
        elif param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            if name in values:
                kwargs[name] = values[name]
                used.add(name)
            elif param.default is param.empty:
                if (
                    param.kind is param.POSITIONAL_OR_KEYWORD
                    and leading is not None
                    and not args
                    and not kwargs
                ):
                    args.append(leading)
                else:
                    raise TypeError(f"cannot satisfy required parameter {name!r}")
    if var_pos and leading is not None and not args:
        args.append(leading)
    if var_kw:
        for key, value in values.items():
            if key not in used and key not in kwargs:
                kwargs[key] = value
                used.add(key)
    return func(*args, **kwargs), used


def _task_facing_brief(
    job: Mapping[str, Any], task: Mapping[str, Any]
) -> dict[str, Any]:
    goal = job.get("goal") if isinstance(job.get("goal"), Mapping) else {}
    brief = {
        "job_id": job.get("job_id"),
        "task_id": task.get("task_id"),
        "objective": task.get("objective"),
        "assigned_files": list(task.get("lease") or []),
        "lease": list(task.get("lease") or []),
        "depends_on": list(task.get("depends_on") or []),
        "repo": goal.get("repo"),
        "source_rev": goal.get("base_rev"),
        "base_rev": goal.get("base_rev"),
        "contract_path": task.get("contract_path"),
        "contract_sha256": task.get("contract_sha256"),
    }
    for key in ("acceptance", "tests", "branch", "summary"):
        if key in task:
            brief[key] = task[key]
    return brief


def _post(
    launcher: Any,
    job: Mapping[str, Any],
    task: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> Any:
    method = None
    for name in (
        "post",
        "dispatch",
        "create_attempt",
        "create",
        "submit",
        "start_attempt",
        "attempt",
        "post_attempt",
    ):
        candidate = getattr(launcher, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        raise ReconcileError(
            "launcher_invalid",
            "the launcher has no post/dispatch method",
            "Inject the launcher protocol described by bundlebees_plugin.reconcile.",
        )
    brief = _task_facing_brief(job, task)
    task_copy = dict(task)
    key = str(intent.get("idempotency_key") or "")
    raw, _used = _call_supported(
        method,
        {
            "brief": brief,
            "task": task_copy,
            "task_id": task.get("task_id"),
            "job_id": job.get("job_id"),
            "idempotency_key": key,
            "idempotency": key,
            "key": key,
            "request_id": key,
            "idem": key,
            "idempotency_token": key,
            "request_key": key,
            "client_token": key,
            "dedupe_key": key,
            "intent": dict(intent),
            "payload": brief,
            "request": brief,
            "attempt_request": brief,
            "spec": brief,
        },
        leading=brief,
    )
    return raw


def _extract_attempt_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("attempt_id", "id", "attempt", "attemptId"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        for container in ("attempt", "data", "result", "detail"):
            nested = value.get(container)
            if isinstance(nested, Mapping):
                found = _extract_attempt_id(nested)
                if found is not None:
                    return found
        return None
    for key in ("attempt_id", "id", "attempt"):
        found = getattr(value, key, None)
        if isinstance(found, str) and found.strip():
            return found.strip()
    return None


def _entry_field(entry: Any, names: Sequence[str]) -> Any:
    containers: list[Any] = [entry]
    if isinstance(entry, Mapping):
        for container in ("metadata", "brief", "intent", "request", "task"):
            sub = entry.get(container)
            if isinstance(sub, Mapping):
                containers.append(sub)
    else:
        for container in ("metadata", "brief", "intent", "request"):
            sub = getattr(entry, container, None)
            if isinstance(sub, Mapping):
                containers.append(sub)
    for container in containers:
        for name in names:
            if isinstance(container, Mapping):
                if name in container and container[name] not in (None, ""):
                    return container[name]
            else:
                found = getattr(container, name, None)
                if found not in (None, ""):
                    return found
    return None


def _normalize_entries(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [{"attempt_id": raw}]
    if isinstance(raw, Mapping):
        for key in (
            "entries",
            "attempts",
            "journal",
            "items",
            "results",
            "data",
        ):
            value = raw.get(key)
            if isinstance(value, (list, tuple, Mapping)):
                if not (
                    isinstance(value, Mapping)
                    and any(
                        field in value
                        for field in (
                            "attempt_id",
                            "id",
                            "task_id",
                            "idempotency_key",
                        )
                    )
                ):
                    return _normalize_entries(value)
        if any(
            key in raw
            for key in ("attempt_id", "id", "task_id", "idempotency_key")
        ):
            return [raw]
        return [value for value in raw.values()]
    if isinstance(raw, (list, tuple, set)):
        return list(raw)
    return [raw]


def _journal(
    launcher: Any,
    job: Mapping[str, Any],
    task: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> tuple[list[Any], bool, bool]:
    """Return ``(entries, scoped_to_task, journal_available)``."""

    if launcher is None:
        return [], False, False
    source = None
    for name in (
        "journal",
        "list_journal",
        "attempts",
        "list_attempts",
        "attempt_journal",
        "history",
        "journal_entries",
        "get_journal",
    ):
        candidate = getattr(launcher, name, None)
        if candidate is not None:
            source = candidate
            break
    if source is None:
        return [], False, False

    if callable(source):
        try:
            raw, used = _call_supported(
                source,
                {
                    "task_id": task.get("task_id"),
                    "task": task,
                    "job_id": job.get("job_id"),
                    "idempotency_key": intent.get("idempotency_key"),
                    "key": intent.get("idempotency_key"),
                    "idem": intent.get("idempotency_key"),
                    "request_key": intent.get("idempotency_key"),
                    "client_token": intent.get("idempotency_key"),
                    "intent": dict(intent),
                },
                leading=task.get("task_id"),
            )
        except Exception:
            return [], False, False
        scoped = bool({"task_id", "task"} & used)
    else:
        raw = source
        scoped = False
        if isinstance(raw, Mapping) and task.get("task_id") in raw:
            raw = raw[task.get("task_id")]
            scoped = True

    return _normalize_entries(raw), scoped, True


def _matching_entries(
    entries: Iterable[Any],
    *,
    job_id: str,
    task_id: str,
    key: str | None,
    scoped: bool,
) -> list[Any]:
    matches: list[Any] = []
    seen: set[str] = set()
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, Mapping) and not entry:
            continue
        entry_job = _entry_field(entry, ("job_id", "jobId"))
        entry_task = _entry_field(entry, ("task_id", "taskId"))
        entry_key = _entry_field(
            entry,
            (
                "idempotency_key",
                "idempotencyKey",
                "client_request_id",
                "request_id",
                "request_key",
                "idempotency_token",
                "client_token",
                "dedupe_key",
                "key",
            ),
        )
        if entry_job is not None and str(entry_job) != str(job_id):
            continue
        if entry_task is not None and str(entry_task) != str(task_id):
            continue
        if entry_key is not None and key is not None and str(entry_key) != str(key):
            continue
        if entry_task is None and entry_key is None and not scoped:
            continue
        attempt_id = _extract_attempt_id(entry)
        if attempt_id is not None:
            if attempt_id in seen:
                continue
            seen.add(attempt_id)
        matches.append(entry)
    return matches


def _new_intent(
    job: Mapping[str, Any], task: Mapping[str, Any], key: str | None = None
) -> dict[str, Any]:
    now = _utc_now()
    job_id = _safe_id(job.get("job_id"))
    task_id = str(task.get("task_id") or "")
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "kind": "dispatch_intent",
        "job_id": job_id,
        "task_id": task_id,
        "idempotency_key": key or f"{job_id}.{task_id}.{secrets.token_hex(16)}",
        "status": INTENT_PREPARED,
        "uncertain_ticks": 0,
        "attempt_id": None,
        "brief": _task_facing_brief(job, task),
        "created": now,
        "updated": now,
    }


def _save_intent_for_job(
    state_dir: Path,
    intent: dict[str, Any],
    task: dict[str, Any],
    job: dict[str, Any] | None = None,
) -> None:
    intent["updated"] = _utc_now()
    save_intent(state_dir, intent)
    task["intent"] = dict(intent)
    if job is not None:
        _save_job(state_dir, job)


def _record_attempt(
    task: dict[str, Any],
    *,
    attempt_id: str | None,
    state: str,
    evidence: str,
    intent: Mapping[str, Any],
) -> None:
    attempts = task.setdefault("attempts", [])
    if attempt_id is not None:
        for existing in attempts:
            if str(existing.get("attempt_id")) == str(attempt_id):
                return
    attempts.append(
        {
            "attempt_id": attempt_id,
            "state": state,
            "evidence": evidence,
            "idempotency_key": intent.get("idempotency_key"),
            "at": _utc_now(),
        }
    )


def _save_job(state_dir: Path, job: dict[str, Any]) -> None:
    jobs.save_job(state_dir, job)


def _dependencies_met(job: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
    states = {str(t.get("task_id")): t.get("state") for t in job.get("tasks", [])}
    return all(
        states.get(str(dep)) == "accepted" for dep in task.get("depends_on") or []
    )


def _events_since(job: Mapping[str, Any], start: int) -> list[dict[str, Any]]:
    return [dict(event) for event in list(job.get("events") or [])[start:]]


def _apply_transition(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    to_state: str,
    *,
    reason: str,
    evidence: str | None = None,
) -> None:
    jobs.transition(
        job,
        str(task.get("task_id")),
        to_state,
        actor="reconciler",
        reason=reason,
        evidence=evidence,
    )
    _save_job(state_dir, job)


def _launcher_calls_for(launcher: Any) -> int:
    for name in ("calls", "call_count", "calls_made"):
        value = getattr(launcher, name, None)
        if isinstance(value, int):
            return value
        if isinstance(value, (list, tuple)):
            return len(value)
    return 0


def _result(
    *,
    status: str,
    action: str = "none",
    job: Mapping[str, Any] | None = None,
    task: Mapping[str, Any] | None = None,
    mode: str = "dispatch",
    posts: int = 0,
    launcher_calls: int = 0,
    journal_calls: int = 0,
    exact: bool = False,
    evidence: str | None = None,
    reason: str = "",
    transitions: Sequence[Mapping[str, Any]] = (),
    locked: bool = False,
    changed: bool = False,
    ok: bool = True,
    **extra: Any,
) -> TickResult:
    result = TickResult(
        ok=ok,
        status=status,
        action=action,
        mode=mode,
        job_id=job.get("job_id") if job else None,
        task_id=task.get("task_id") if task else None,
        state=task.get("state") if task else None,
        task_state=task.get("state") if task else None,
        attempt_id=extra.pop("attempt_id", None),
        verifier_calls=extra.pop("verifier_calls", 0),
        exactly_once=bool(exact),
        evidence=evidence,
        reason=reason,
        posts=posts,
        launcher_calls=launcher_calls,
        journal_calls=journal_calls,
        transitions=list(transitions),
        events=list(transitions),
        locked=locked,
        changed=changed,
    )
    result.update(extra)
    return result


def _finish_dispatch(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    *,
    attempt_id: str | None,
    retried: bool,
    posts: int,
    events_start: int,
    launcher: Any,
) -> TickResult:
    idem = _launcher_idempotent(launcher)
    if retried:
        evidence = "retried"
    elif idem:
        evidence = "idempotent"
    else:
        evidence = "sent"
    intent["status"] = INTENT_SENT
    intent["attempt_id"] = attempt_id
    intent["evidence"] = evidence
    _save_intent_for_job(state_dir, intent, task, job)
    _record_attempt(
        task,
        attempt_id=attempt_id,
        state="dispatched",
        evidence=evidence,
        intent=intent,
    )
    if task.get("state") == "dispatching":
        _apply_transition(
            state_dir,
            job,
            task,
            "dispatched",
            reason="launcher acknowledged the dispatch",
            evidence=evidence,
        )
    transitions = _events_since(job, events_start)
    return _result(
        status="dispatched",
        action="retry" if retried else "dispatch",
        job=job,
        task=task,
        posts=posts,
        launcher_calls=_launcher_calls_for(launcher),
        exact=idem,
        evidence=evidence,
        reason="dispatch_acknowledged",
        transitions=transitions,
        changed=True,
        attempt_id=attempt_id,
        retried=retried,
        idempotency_key=intent.get("idempotency_key"),
    )


def _escalate(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    *,
    code: str,
    posts: int,
    events_start: int,
    launcher: Any,
    reason: str,
    evidence: str | None = None,
) -> TickResult:
    intent["status"] = code
    intent["evidence"] = evidence or code
    intent["escalated"] = _utc_now()
    _save_intent_for_job(state_dir, intent, task, job)
    if task.get("state") == "dispatching":
        _apply_transition(
            state_dir,
            job,
            task,
            "failed",
            reason=reason or code,
            evidence=evidence or code,
        )
    transitions = _events_since(job, events_start)
    return _result(
        status=code,
        action="escalate",
        job=job,
        task=task,
        posts=posts,
        launcher_calls=_launcher_calls_for(launcher),
        exact=False,
        evidence=evidence or code,
        reason=code,
        transitions=transitions,
        changed=True,
        idempotency_key=intent.get("idempotency_key"),
        fix=_FIX_JOURNAL,
    )


def _adopt(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    *,
    attempt_id: str | None,
    posts: int,
    events_start: int,
    launcher: Any,
    source: str = "journal",
    journal_calls: int = 0,
) -> TickResult:
    idem = _launcher_idempotent(launcher)
    evidence = "proven_present" if idem else "adopted"
    intent["status"] = INTENT_ADOPTED
    intent["attempt_id"] = attempt_id
    intent["evidence"] = evidence
    intent["adopted_at"] = _utc_now()
    _save_intent_for_job(state_dir, intent, task, job)
    _record_attempt(
        task,
        attempt_id=attempt_id,
        state="dispatched",
        evidence=evidence,
        intent=intent,
    )
    if task.get("state") == "dispatching":
        _apply_transition(
            state_dir,
            job,
            task,
            "dispatched",
            reason=f"adopted existing attempt from {source}",
            evidence=evidence,
        )
    transitions = _events_since(job, events_start)
    return _result(
        status="adopted",
        action="adopt",
        job=job,
        task=task,
        posts=posts,
        launcher_calls=_launcher_calls_for(launcher),
        journal_calls=journal_calls,
        exact=idem,
        evidence=evidence,
        reason="adopted_existing_attempt",
        transitions=transitions,
        changed=True,
        attempt_id=attempt_id,
        reconciled="adopted",
        source=source,
        journal_evidence="proven_present" if source == "journal" else None,
        exactly_once_evidence="proven_present" if idem else "adopted",
        idempotency_key=intent.get("idempotency_key"),
    )


def _reconcile(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    *,
    launcher: Any,
    max_uncertain_ticks: int,
    posts: int,
    events_start: int,
    initial_uncertain: bool,
    retried: bool = False,
) -> TickResult:
    status = str(intent.get("status") or INTENT_PREPARED)

    if status in (INTENT_SENT, INTENT_ADOPTED):
        return _adopt(
            state_dir,
            job,
            task,
            intent,
            attempt_id=intent.get("attempt_id"),
            posts=posts,
            events_start=events_start,
            launcher=launcher,
            source="intent",
        )

    if status in (INTENT_UNCERTAIN_EXHAUSTED, INTENT_AMBIGUOUS):
        if task.get("state") == "dispatching":
            _apply_transition(
                state_dir,
                job,
                task,
                "failed",
                reason=status,
                evidence=intent.get("evidence") or status,
            )
        transitions = _events_since(job, events_start)
        return _result(
            status=status,
            action="escalate",
            job=job,
            task=task,
            posts=posts,
            launcher_calls=_launcher_calls_for(launcher),
            exact=False,
            evidence=intent.get("evidence") or status,
            reason=status,
            transitions=transitions,
            changed=bool(transitions),
            idempotency_key=intent.get("idempotency_key"),
        )

    entries, scoped, available = _journal(launcher, job, task, intent)
    journal_calls = 1 if available else 0
    key = intent.get("idempotency_key")
    matches = _matching_entries(
        entries,
        job_id=str(job.get("job_id")),
        task_id=str(task.get("task_id")),
        key=str(key) if key is not None else None,
        scoped=scoped,
    )

    if len(matches) == 1:
        attempt_id = _extract_attempt_id(matches[0]) or intent.get(
            "attempt_hint"
        ) or intent.get("attempt_id")
        return _adopt(
            state_dir,
            job,
            task,
            intent,
            attempt_id=attempt_id,
            posts=posts,
            events_start=events_start,
            launcher=launcher,
            source="journal",
            journal_calls=journal_calls,
        )
    if len(matches) > 1:
        result = _escalate(
            state_dir,
            job,
            task,
            intent,
            code=INTENT_AMBIGUOUS,
            posts=posts,
            events_start=events_start,
            launcher=launcher,
            reason="two or more launcher attempts plausibly match this dispatch",
            evidence=INTENT_AMBIGUOUS,
        )
        result["journal_calls"] = journal_calls
        result["journal_matches"] = len(matches)
        return result

    ticks = int(intent.get("uncertain_ticks") or 0)
    if initial_uncertain:
        exceeded = ticks > int(max_uncertain_ticks)
    else:
        exceeded = ticks >= int(max_uncertain_ticks)
    if exceeded:
        result = _escalate(
            state_dir,
            job,
            task,
            intent,
            code=INTENT_UNCERTAIN_EXHAUSTED,
            posts=posts,
            events_start=events_start,
            launcher=launcher,
            reason=(
                "dispatch outcome stayed uncertain beyond "
                f"max_uncertain_ticks={max_uncertain_ticks}"
            ),
            evidence=INTENT_UNCERTAIN_EXHAUSTED,
        )
        result["journal_calls"] = journal_calls
        result["uncertain_ticks"] = ticks
        return result

    if not initial_uncertain:
        ticks += 1
    intent["status"] = INTENT_UNCERTAIN
    intent["uncertain_ticks"] = ticks
    _save_intent_for_job(state_dir, intent, task, job)
    return _result(
        status="reconcile_wait",
        action="wait",
        job=job,
        task=task,
        posts=posts,
        launcher_calls=_launcher_calls_for(launcher),
        exact=False,
        evidence=None,
        reason="journal_has_no_matching_attempt_yet",
        journal_calls=journal_calls,
        changed=True,
        journal_available=available,
        uncertain_ticks=ticks,
        idempotency_key=intent.get("idempotency_key"),
    )


def _post_intent(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    *,
    launcher: Any,
    max_uncertain_ticks: int,
    retried: bool,
    events_start: int,
) -> TickResult:
    intent["status"] = INTENT_SENDING
    intent["last_post_at"] = _utc_now()
    _save_intent_for_job(state_dir, intent, task, job)
    try:
        raw = _post(launcher, job, task, intent)
    except DispatchNotSent as exc:
        intent["status"] = INTENT_NOT_SENT
        intent["last_error"] = str(exc)
        _save_intent_for_job(state_dir, intent, task, job)
        return _result(
            status="not_sent",
            action="retry",
            job=job,
            task=task,
            posts=1,
            launcher_calls=_launcher_calls_for(launcher),
            exact=False,
            evidence="not_sent",
            reason="launcher proved the request never left the process",
            changed=True,
            retried=True,
            idempotency_key=intent.get("idempotency_key"),
        )
    except DispatchUncertain as exc:
        intent["status"] = INTENT_UNCERTAIN
        intent["uncertain_ticks"] = int(intent.get("uncertain_ticks") or 0) + 1
        intent["last_error"] = str(exc)
        if exc.attempt_id:
            intent["attempt_hint"] = exc.attempt_id
        _save_intent_for_job(state_dir, intent, task, job)
        return _reconcile(
            state_dir,
            job,
            task,
            intent,
            launcher=launcher,
            max_uncertain_ticks=max_uncertain_ticks,
            posts=1,
            events_start=events_start,
            initial_uncertain=True,
            retried=retried,
        )
    except Exception as exc:
        intent["status"] = INTENT_UNCERTAIN
        intent["uncertain_ticks"] = int(intent.get("uncertain_ticks") or 0) + 1
        intent["last_error"] = f"{type(exc).__name__}: {exc}"
        _save_intent_for_job(state_dir, intent, task, job)
        return _reconcile(
            state_dir,
            job,
            task,
            intent,
            launcher=launcher,
            max_uncertain_ticks=max_uncertain_ticks,
            posts=1,
            events_start=events_start,
            initial_uncertain=True,
            retried=retried,
        )

    attempt_id = _extract_attempt_id(raw)
    return _finish_dispatch(
        state_dir,
        job,
        task,
        intent,
        attempt_id=attempt_id,
        retried=retried,
        posts=1,
        events_start=events_start,
        launcher=launcher,
    )


def _begin_or_continue(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    *,
    launcher: Any,
    max_uncertain_ticks: int,
) -> TickResult:
    events_start = len(job.get("events") or [])
    existing = load_intent(
        state_dir, str(job.get("job_id")), str(task.get("task_id"))
    )

    if existing is None and task.get("state") == "dispatching":
        intent = _new_intent(job, task)
        intent["status"] = INTENT_UNCERTAIN
        intent["uncertain_ticks"] = 1
        _save_intent_for_job(state_dir, intent, task, job)
        return _reconcile(
            state_dir,
            job,
            task,
            intent,
            launcher=launcher,
            max_uncertain_ticks=max_uncertain_ticks,
            posts=0,
            events_start=events_start,
            initial_uncertain=True,
        )

    intent = existing or _new_intent(job, task)
    status = str(intent.get("status") or INTENT_PREPARED)
    task["intent"] = dict(intent)

    if status in (INTENT_PREPARED, INTENT_NOT_SENT):
        retried = status == INTENT_NOT_SENT
        wrote_intent = False
        if existing is None:
            _save_intent_for_job(state_dir, intent, task, job)
            wrote_intent = True
        if task.get("state") == "pending":
            try:
                _apply_transition(
                    state_dir,
                    job,
                    task,
                    "dispatching",
                    reason="recorded a durable dispatch intent",
                )
            except BaseException:
                if wrote_intent:
                    clear_intent(
                        state_dir,
                        str(job.get("job_id")),
                        str(task.get("task_id")),
                    )
                raise
        return _post_intent(
            state_dir,
            job,
            task,
            intent,
            launcher=launcher,
            max_uncertain_ticks=max_uncertain_ticks,
            retried=retried,
            events_start=events_start,
        )

    return _reconcile(
        state_dir,
        job,
        task,
        intent,
        launcher=launcher,
        max_uncertain_ticks=max_uncertain_ticks,
        posts=0,
        events_start=events_start,
        initial_uncertain=False,
        retried=False,
    )


def _dispatch_tick(
    state_dir: Path,
    job: dict[str, Any],
    *,
    launcher: Any,
    max_uncertain_ticks: int,
    task_id: str | None,
) -> TickResult:
    if launcher is None:
        return _result(
            ok=False,
            status="launcher_missing",
            action="error",
            job=job,
            exact=False,
            reason="no launcher injected",
            fix="Inject a launcher implementing post/journal/idempotent.",
        )

    tasks = [
        task
        for task in job.get("tasks", [])
        if task_id is None or str(task.get("task_id")) == str(task_id)
    ]

    for task in tasks:
        if task.get("state") == "dispatching":
            return _begin_or_continue(
                state_dir,
                job,
                task,
                launcher=launcher,
                max_uncertain_ticks=max_uncertain_ticks,
            )

    for task in tasks:
        if task.get("state") == "pending" and _dependencies_met(job, task):
            return _begin_or_continue(
                state_dir,
                job,
                task,
                launcher=launcher,
                max_uncertain_ticks=max_uncertain_ticks,
            )

    return _result(
        status="noop",
        action="none",
        job=job,
        task=tasks[0] if len(tasks) == 1 else None,
        exact=False,
        reason="nothing_to_dispatch",
        changed=False,
    )


def _verifier(
    launcher: Any, verifier: Callable[..., Any] | None
) -> Callable[..., Any] | None:
    if callable(verifier):
        return verifier
    if verifier is not None:
        for name in ("verify", "run_verify", "check", "verify_attempt"):
            candidate = getattr(verifier, name, None)
            if callable(candidate):
                return candidate
    if launcher is not None:
        for name in ("verify", "run_verify", "check", "verify_attempt"):
            candidate = getattr(launcher, name, None)
            if callable(candidate):
                return candidate
    return None


def _verifier_ok(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("ok", "passed", "accept", "accepted", "success", "green"):
            if key in value:
                return bool(value[key])
        status = value.get("status")
        if isinstance(status, str):
            return status.lower() in {
                "ok",
                "passed",
                "verified",
                "green",
                "accepted",
            }
        return True
    if isinstance(value, str):
        return value.strip().lower() in {
            "ok",
            "passed",
            "verified",
            "green",
            "accepted",
            "true",
        }
    return bool(value)


def _verifier_evidence(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key in ("evidence", "summary", "detail", "reason"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _run_verifier(
    verifier: Callable[..., Any],
    *,
    job: Mapping[str, Any],
    task: Mapping[str, Any],
    attempt_id: str | None,
) -> Any:
    raw, _used = _call_supported(
        verifier,
        {
            "task": dict(task),
            "attempt_id": attempt_id,
            "task_id": task.get("task_id"),
            "job_id": job.get("job_id"),
            "result": dict(task),
            "evidence": task.get("verify"),
        },
        leading=dict(task),
    )
    return raw


def _latest_attempt_id(task: Mapping[str, Any]) -> str | None:
    attempts = task.get("attempts") or []
    if attempts and isinstance(attempts[-1], Mapping):
        found = attempts[-1].get("attempt_id")
        if isinstance(found, str) and found.strip():
            return found.strip()
    intent = task.get("intent")
    if isinstance(intent, Mapping):
        found = intent.get("attempt_id")
        if isinstance(found, str) and found.strip():
            return found.strip()
    return None


def _is_jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _verify_tick(
    state_dir: Path,
    job: dict[str, Any],
    *,
    launcher: Any,
    verifier: Callable[..., Any] | None,
    task_id: str | None,
) -> TickResult:
    verifier_fn = _verifier(launcher, verifier)
    if verifier_fn is None:
        return _result(
            ok=False,
            status="verifier_missing",
            action="error",
            mode="verify",
            job=job,
            exact=False,
            reason="no verifier injected",
            fix="Pass verifier= or inject a launcher with a verify method.",
        )

    tasks = [
        task
        for task in job.get("tasks", [])
        if task_id is None or str(task.get("task_id")) == str(task_id)
    ]
    selected = None
    for task in tasks:
        if task.get("state") in ("dispatched", "running", "verifying"):
            selected = task
            break
    if selected is None:
        return _result(
            status="noop",
            action="none",
            mode="verify",
            job=job,
            task=tasks[0] if len(tasks) == 1 else None,
            exact=False,
            reason="nothing_to_verify",
            changed=False,
        )

    events_start = len(job.get("events") or [])
    if selected.get("state") == "dispatched":
        _apply_transition(
            state_dir,
            job,
            selected,
            "running",
            reason="reconciler picked up verification",
        )
    if selected.get("state") == "running":
        _apply_transition(
            state_dir,
            job,
            selected,
            "verifying",
            reason="reconciler started verification",
        )

    attempt_id = _latest_attempt_id(selected)
    try:
        raw = _run_verifier(
            verifier_fn, job=job, task=selected, attempt_id=attempt_id
        )
    except Exception as exc:
        selected["verify"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "at": _utc_now(),
        }
        _apply_transition(
            state_dir,
            job,
            selected,
            "rejected",
            reason=f"verifier raised {type(exc).__name__}",
            evidence="verifier_error",
        )
        transitions = _events_since(job, events_start)
        return _result(
            status="verify_failed",
            action="verify",
            mode="verify",
            job=job,
            task=selected,
            exact=False,
            evidence="verifier_error",
            reason="verifier_raised",
            transitions=transitions,
            changed=True,
            verifier_calls=1,
            attempt_id=attempt_id,
        )

    ok = _verifier_ok(raw)
    evidence = _verifier_evidence(raw) or ("verified" if ok else "verify_failed")
    selected["verify"] = {
        "ok": ok,
        "result": raw if _is_jsonable(raw) else None,
        "evidence": evidence,
        "at": _utc_now(),
    }
    if ok:
        _apply_transition(
            state_dir,
            job,
            selected,
            "verified",
            reason="verifier accepted the attempt",
            evidence=evidence,
        )
    else:
        _apply_transition(
            state_dir,
            job,
            selected,
            "rejected",
            reason="verifier rejected the attempt",
            evidence=evidence,
        )
    transitions = _events_since(job, events_start)
    return _result(
        status="verified" if ok else "verify_failed",
        action="verify",
        mode="verify",
        job=job,
        task=selected,
        exact=False,
        evidence=evidence,
        reason="verify_passed" if ok else "verify_failed",
        transitions=transitions,
        changed=True,
        verifier_calls=1,
        attempt_id=attempt_id,
    )


def _normalize_mode(
    value: Any, job: Mapping[str, Any] | None, task_id: str | None
) -> str:
    text = str(value or "").strip().lower()
    if text in ("dispatch", "send", "dispatch_tick", "tick_dispatch"):
        return "dispatch"
    if text in (
        "verify",
        "verification",
        "verify_tick",
        "tick_verify",
        "check",
    ):
        return "verify"
    if text in ("auto", "", "none"):
        if job is None:
            return "dispatch"
        tasks = [
            task
            for task in job.get("tasks", [])
            if task_id is None or str(task.get("task_id")) == str(task_id)
        ]
        for task in tasks:
            if task.get("state") == "dispatching":
                return "dispatch"
        for task in tasks:
            if task.get("state") == "pending" and _dependencies_met(job, task):
                return "dispatch"
        for task in tasks:
            if task.get("state") in ("dispatched", "running", "verifying"):
                return "verify"
        return "dispatch"
    raise ReconcileError(
        "reconcile_invalid",
        f"unknown reconciler mode {value!r}",
        "Use dispatch or verify.",
    )


def tick(
    state_dir: Path | str | Mapping[str, Any],
    job_id: str | Mapping[str, Any] | None = None,
    launcher: Any = None,
    mode: str | None = None,
    *,
    job: dict[str, Any] | None = None,
    verifier: Callable[..., Any] | None = None,
    max_uncertain_ticks: int = DEFAULT_MAX_UNCERTAIN_TICKS,
    task_id: str | None = None,
    **options: Any,
) -> TickResult:
    """Run one crash-safe reconciler tick.

    ``mode`` is ``"dispatch"`` or ``"verify"`` (``None`` chooses from the job
    state). A tick that cannot acquire the per-job lock returns status
    ``"locked"`` before loading the job or touching the launcher.
    """

    if isinstance(state_dir, Mapping):
        first = dict(state_dir)
        if not isinstance(job_id, (str, os.PathLike)):
            raise ReconcileError(
                "reconcile_invalid",
                "state_dir is required when the job is the first argument",
                _FIX_INTENT,
            )
        state_dir = job_id
        job = first
        job_id = first.get("job_id")

    if isinstance(job_id, Mapping):
        job = dict(job_id)
        job_id = job.get("job_id")

    if not isinstance(state_dir, (str, os.PathLike)):
        raise ReconcileError(
            "reconcile_invalid",
            "state_dir must be a filesystem path",
            _FIX_INTENT,
        )
    state_path = Path(state_dir)

    for key in ("op", "operation", "kind", "phase", "tick_type"):
        if key in options and mode is None:
            mode = options.pop(key)
    if "max_uncertain" in options:
        max_uncertain_ticks = int(options.pop("max_uncertain"))
    if "uncertain_limit" in options:
        max_uncertain_ticks = int(options["uncertain_limit"])

    if job is None:
        if job_id is None:
            raise ReconcileError(
                "reconcile_invalid", "job_id or job is required", _FIX_INTENT
            )
        checked_job_id = _safe_id(job_id)
    else:
        if job_id is None:
            job_id = job.get("job_id")
        checked_job_id = _safe_id(job_id)
        if str(job.get("job_id")) != checked_job_id:
            raise ReconcileError(
                "reconcile_invalid",
                "the in-memory job does not match job_id",
                _FIX_INTENT,
            )

    lock = _try_lock(state_path, checked_job_id)
    if lock is None:
        return _result(
            status="locked",
            action="none",
            job=job,
            mode=str(mode or "dispatch"),
            exact=False,
            reason="another reconciler tick holds the job lock",
            locked=True,
            changed=False,
            fix=_FIX_LOCK,
        )

    try:
        if job is None:
            job = jobs.load_job(state_path, checked_job_id)
        chosen_mode = _normalize_mode(mode, job, task_id)
        if chosen_mode == "dispatch":
            return _dispatch_tick(
                state_path,
                job,
                launcher=launcher,
                max_uncertain_ticks=int(max_uncertain_ticks),
                task_id=task_id,
            )
        return _verify_tick(
            state_path,
            job,
            launcher=launcher,
            verifier=verifier,
            task_id=task_id,
        )
    finally:
        lock.release()


def dispatch_tick(
    state_dir: Path | str,
    job_id: str | Mapping[str, Any] | None = None,
    launcher: Any = None,
    *,
    job: dict[str, Any] | None = None,
    max_uncertain_ticks: int = DEFAULT_MAX_UNCERTAIN_TICKS,
    task_id: str | None = None,
    **options: Any,
) -> TickResult:
    return tick(
        state_dir,
        job_id,
        launcher,
        "dispatch",
        job=job,
        max_uncertain_ticks=max_uncertain_ticks,
        task_id=task_id,
        **options,
    )


def verify_tick(
    state_dir: Path | str,
    job_id: str | Mapping[str, Any] | None = None,
    launcher: Any = None,
    *,
    job: dict[str, Any] | None = None,
    verifier: Callable[..., Any] | None = None,
    task_id: str | None = None,
    **options: Any,
) -> TickResult:
    return tick(
        state_dir,
        job_id,
        launcher,
        "verify",
        job=job,
        verifier=verifier,
        task_id=task_id,
        **options,
    )


run_tick = tick
reconcile = tick
reconcile_tick = tick
reconcile_job = tick

reconcile_once = tick
reconcile_one = tick
run = tick


def dispatch(*args: Any, **kwargs: Any) -> TickResult:
    kwargs["mode"] = "dispatch"
    return tick(*args, **kwargs)


def verify(*args: Any, **kwargs: Any) -> TickResult:
    kwargs["mode"] = "verify"
    return tick(*args, **kwargs)
