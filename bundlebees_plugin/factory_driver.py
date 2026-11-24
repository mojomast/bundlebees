"""Minimal production coordinator driver for the local factory.

One driver pass connects the durable modules in the order the coordinator uses
them::

    machine queue -> landing bridge -> event inbox -> work scheduler

The driver never calls a model.  A tick is injected (the CLI can load one with
``--tick module:callable``); event handling is deterministic and only records
eligible work.  Idle passes do no inference and write nothing.

Wake requests are coalesced per session through a durable cursor/generation
record, so arrivals while a turn is active are not lost and each session has at
most one active turn.
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import hashlib
import importlib
import inspect
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:  # POSIX advisory locks; Linux is the target platform
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock has no claim protocol
    fcntl = None  # type: ignore[assignment]

from . import beat
from . import beat_reconciliation
from . import launcher_adapter
from . import bee_events
from . import board_stock
from . import campaign as campaign_mod
from . import job_cli
from . import hermes_adapter
from . import event_consumer
from . import job as jobs
from . import landing_bridge
from . import leases
from . import liveness
from . import queue as queue_mod
from . import work_scheduler
from .security import child_env, scrub

__all__ = [
    "DEFAULT_SWEEP_INTERVAL_S",
    "FactoryDriver",
    "FactoryError",
    "FactoryService",
    "InboxEventWatcher",
    "WakeCoalescer",
    "action_id_for_event",
    "deterministic_handler",
    "load_tick",
    "main",
    "read_dispatch_approval",
    "read_shared_cap",
    "record_dispatch_approval",
    "run_once",
    "run_pass",
    "startup_reconcile",
    "wake_action_id",
    "work_id_for_event",
]

WAKE_SCHEMA = "bundlebees.factory-wake/1"
WAKE_ACTION_SCHEMA = "bundlebees.factory-wake-action/1"
ACTION_SCHEMA = "bundlebees.factory-action/1"
WAKE_DIR = "wakes"
WAKE_ACTION_DIR = "wake-actions"
ACTION_DIR = "actions"
WORK_RETRY_DIR = "work-retries"
JOB_LOCK_DIR = "job-locks"
DISPATCH_APPROVAL_DIR = "dispatch-approvals"
DISPATCH_APPROVAL_SCHEMA = "bundlebees.factory-dispatch-approval/1"
DEFAULT_SESSION = "coordinator"
DEFAULT_WAKE_TTL_S = 900.0
DEFAULT_WAKE_BACKOFF_S = 5.0
MAX_WAKE_BACKOFF_S = 300.0
MAX_WORK_ATTEMPTS = 2
#: The event wait is bounded even when inotify exists, so a missed or coalesced
#: filesystem notification never stalls the loop forever.
DEFAULT_SERVICE_INTERVAL_S = 30.0
#: Polling fallback used only when inotify is unavailable in this sandbox.
DEFAULT_SERVICE_POLL_S = 0.25
#: The backstop reconciliation sweep runs at most this often (30 minutes).
DEFAULT_SWEEP_INTERVAL_S = 1800
SWEEP_LAST_FILENAME = "sweep-last.json"
_TERMINAL_TASK_STATES = frozenset({"accepted", "rejected", "failed", "superseded"})


class FactoryError(Exception):
    """The driver could not complete a pass or a durable wake operation."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Durable file helpers
# ---------------------------------------------------------------------------


def _state_path(state_dir: Any) -> Path:
    return Path(state_dir)


def _factory_dir(state_dir: Any) -> Path:
    return _state_path(state_dir) / "factory"


def _raw_session(value: Any, default: str = DEFAULT_SESSION) -> str:
    """The exact Hermes session identity, never rewritten for storage.

    The gateway routes on this value, so it must survive a slot name containing
    ``/``, spaces, ``@`` or non-ASCII text.  Only :func:`_safe_session` may turn
    it into a filesystem component.
    """

    text = str(value if value is not None else "").strip()
    if not text:
        text = default
    return text


def _safe_session(value: Any) -> str:
    """A storage-safe filename component for one raw session identity."""

    text = _raw_session(value)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if len(text) <= 160 and all(ch in allowed for ch in text) and text not in {".", ".."}:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"session-{digest}"


def wake_action_id(session: Any, generation: Any, subscriber: Any = None) -> str:
    """A stable identity for one session generation's gateway wake.

    It is derived only from the raw session, the durable generation and the
    subscriber key.  Process, turn and retry identity never enter the seed, so a
    restart or a bounded retry resolves to the same durable action record.
    """

    seed = json.dumps(
        {
            "session": _raw_session(session),
            "generation": int(generation or 0),
            "subscriber": _safe_session(subscriber) if subscriber not in (None, "") else "",
        },
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return "wake_" + hashlib.sha256(seed).hexdigest()[:24]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise FactoryError("state_corrupt", f"{path} is unreadable: {exc}", "Inspect it by hand.") from exc
    if not isinstance(value, dict):
        raise FactoryError("state_corrupt", f"{path} is not a JSON object", "Inspect it by hand.")
    return value


def _approval_key(work_id: Any) -> str:
    text = str(work_id if work_id is not None else "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if len(text) <= 96 and text and text not in {".", ".."} and all(ch in allowed for ch in text):
        return text
    return "work-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def dispatch_approval_path(state_dir: Any, work_id: Any) -> Path:
    return _factory_dir(state_dir) / DISPATCH_APPROVAL_DIR / f"{_approval_key(work_id)}.json"


def record_dispatch_approval(
    state_dir: Any,
    work_id: Any,
    *,
    job_id: Any = "",
    scope: Any = (),
    approved: bool = True,
    by: str = "owner",
    now: Any = None,
) -> dict[str, Any]:
    """Record the durable pre-approval one scheduler item needs before dispatch.

    The record is inert unless ``approved`` is exactly ``True`` and its scope
    is a readable list.  The service refuses to dispatch anything else.
    """

    identifier = _approval_key(work_id)
    if isinstance(scope, (list, tuple, set)):
        globs: Any = [str(item) for item in scope if str(item).strip()]
    elif scope is None:
        globs = []
    else:
        # Preserve an explicitly malformed scope so the reader can fail closed.
        globs = scope
    body = {
        "schema": DISPATCH_APPROVAL_SCHEMA,
        "work_id": str(work_id if work_id is not None else ""),
        "approval_key": identifier,
        "job_id": str(job_id or ""),
        "approved": bool(approved),
        "scope": globs,
        "approved_by": str(by or ""),
        "approved_at": _iso(now),
    }
    _write_json(dispatch_approval_path(state_dir, work_id), body)
    return body


def read_dispatch_approval(state_dir: Any, work_id: Any) -> dict[str, Any] | None:
    """Read one pre-approval record; unreadable state is a typed refusal."""

    path = dispatch_approval_path(state_dir, work_id)
    try:
        body = _read_json(path)
    except FactoryError as exc:
        raise FactoryError(
            "approval_unreadable",
            f"the dispatch approval for {work_id!r} is unreadable: {exc.message}",
            "Repair or remove the approval record; the driver never guesses consent.",
        ) from exc
    if body is None:
        return None
    if str(body.get("schema") or "") != DISPATCH_APPROVAL_SCHEMA:
        raise FactoryError(
            "approval_unreadable",
            f"the dispatch approval for {work_id!r} is not schema {DISPATCH_APPROVAL_SCHEMA!r}",
            "Rewrite the approval with record_dispatch_approval.",
        )
    stored = str(body.get("work_id") or "")
    if stored and stored != str(work_id):
        raise FactoryError(
            "approval_unreadable",
            f"the dispatch approval at {path} names {stored!r}, not {work_id!r}",
            "Refuse it; an approval never carries to another work item.",
        )
    return body


def read_shared_cap(state_dir: Any = None) -> int:
    """Read the shared bee cap (12) from the single authority for it.

    ``grants.GLOBAL_MAX_BEES`` is the shared cap.  A missing, non-integer or
    non-positive value is not a reason to dispatch without a bound.
    """

    from . import grants as grants_mod

    value = getattr(grants_mod, "GLOBAL_MAX_BEES", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FactoryError(
            "cap_unreadable",
            f"the shared bee cap is not a readable positive integer: {value!r}",
            "Restore grants.GLOBAL_MAX_BEES; the service never dispatches without a cap.",
        )
    return int(value)


def _scope_allows(scope: Sequence[str], lease_files: Sequence[str]) -> bool:
    return all(
        any(fnmatch.fnmatch(str(path), str(pattern)) for pattern in scope)
        for path in lease_files
    )


_pid_alive = liveness.pid_alive


def _iso(now: Any = None) -> str:
    epoch = time.time()
    if now is not None:
        if callable(now):
            now = now()
        if isinstance(now, (int, float)):
            epoch = float(now)
        elif isinstance(now, str):
            return now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _clock(now: Any = None) -> float:
    if now is None:
        return time.time()
    if callable(now):
        now = now()
    if isinstance(now, (int, float)):
        return float(now)
    if isinstance(now, str):
        try:
            from datetime import datetime, timezone

            return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return time.time()
    return time.time()


def _origin_branch_view(repo: Path, timeout_s: float = 30.0) -> set[str]:
    """Return the branch names on origin, raising so the sweep reports the failure."""

    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            # Never hand the driver's environment (launcher token, provider
            # keys) to git, ssh or credential helpers, and never wait on a prompt.
            env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git ls-remote timed out after {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise RuntimeError(f"git could not be started: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:200]
        raise RuntimeError(f"git ls-remote failed ({proc.returncode}): {detail}")
    branches: set[str] = set()
    for line in proc.stdout.splitlines():
        _, _, ref = line.partition("\t")
        name = ref.strip()
        if name.startswith("refs/heads/"):
            branch = name[len("refs/heads/"):]
            if branch:
                branches.add(branch)
    return branches


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# Wake coalescing
# ---------------------------------------------------------------------------


class WakeCoalescer:
    """Durable per-session cursor/generation with one active turn.

    The record keeps the raw Hermes session identity (the value the gateway
    routes on) while the filename is a storage-safe key.  A session whose id
    contains ``/`` or spaces is therefore still one durable record and never
    escapes ``factory/wakes``.

    ``request_wake`` increments the generation even while a turn is running.
    ``finish_turn`` advances the cursor only to the generation the turn started
    from, so later arrivals remain pending for exactly one next turn.
    """

    def __init__(self, state_dir: Any, *, ttl_s: float = DEFAULT_WAKE_TTL_S) -> None:
        self.root = _factory_dir(state_dir) / WAKE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_s = float(ttl_s)

    def key_for(self, session: Any) -> str:
        """The storage-safe filename component for a raw session identity."""

        return _safe_session(session)

    def _path(self, session: Any) -> Path:
        return self.root / f"{self.key_for(session)}.json"

    def _lock_path(self, session: Any) -> Path:
        return self.root / f"{self.key_for(session)}.lock"

    def _load(self, session: Any) -> dict[str, Any]:
        name = _raw_session(session)
        record = _read_json(self._path(name)) or {}
        return {
            "schema": WAKE_SCHEMA,
            "session": name,
            "session_key": self.key_for(name),
            "subscriber": str(record.get("subscriber") or ""),
            "generation": int(record.get("generation") or 0),
            "cursor": int(record.get("cursor") or 0),
            "active": record.get("active") if isinstance(record.get("active"), Mapping) else None,
            "coalesced": int(record.get("coalesced") or 0),
            "updated_at": str(record.get("updated_at") or ""),
            "retry_after_epoch": float(record.get("retry_after_epoch") or 0.0),
            "retry_attempts": int(record.get("retry_attempts") or 0),
            "last_error": str(record.get("last_error") or ""),
            "last_error_code": str(record.get("last_error_code") or ""),
        }

    def _save(self, session: Any, record: Mapping[str, Any]) -> dict[str, Any]:
        name = _raw_session(record.get("session") or session)
        body = dict(record)
        body["schema"] = WAKE_SCHEMA
        body["session"] = name
        body["session_key"] = self.key_for(name)
        body["pending"] = int(body.get("generation") or 0) > int(body.get("cursor") or 0)
        body["active_turn"] = bool(body.get("active"))
        _write_json(self._path(name), body)
        return dict(body)

    def _expired(self, active: Mapping[str, Any], clock: float) -> bool:
        if not _pid_alive(active.get("pid")):
            return True
        started = active.get("started_epoch")
        if not isinstance(started, (int, float)):
            # A live pid is the authority; without a readable start time there
            # is no evidence the turn itself expired.
            return False
        return clock - float(started) >= self.ttl_s

    def request_wake(
        self, session: Any, *, now: Any = None, subscriber: Any = None
    ) -> dict[str, Any]:
        name = _raw_session(session)
        stamp = _iso(now)
        clock = _clock(now)
        with _locked(self._lock_path(name)):
            record = self._load(name)
            record["generation"] = int(record["generation"]) + 1
            record["coalesced"] = int(record["coalesced"]) + 1
            record["updated_at"] = stamp
            record["last_wake_epoch"] = clock
            if subscriber not in (None, ""):
                record["subscriber"] = _safe_session(subscriber)
            return self._save(name, record)

    def begin_turn(self, session: Any, *, now: Any = None) -> dict[str, Any]:
        name = _raw_session(session)
        stamp = _iso(now)
        clock = _clock(now)
        with _locked(self._lock_path(name)):
            record = self._load(name)
            active = record.get("active")
            if isinstance(active, Mapping) and not self._expired(active, clock):
                return {
                    "started": False,
                    "reason": "active_turn",
                    "session": name,
                    "generation": int(record["generation"]),
                    "cursor": int(record["cursor"]),
                    "active": dict(active),
                }
            retry_after = float(record.get("retry_after_epoch") or 0.0)
            if retry_after > clock:
                return {
                    "started": False,
                    "reason": "backoff",
                    "session": name,
                    "generation": int(record["generation"]),
                    "cursor": int(record["cursor"]),
                    "retry_after": _iso(retry_after),
                }
            if int(record["generation"]) <= int(record["cursor"]):
                return {
                    "started": False,
                    "reason": "no_wake",
                    "session": name,
                    "generation": int(record["generation"]),
                    "cursor": int(record["cursor"]),
                }
            abandoned = dict(active) if isinstance(active, Mapping) else None
            generation = int(record["generation"])
            turn = {
                "turn": "turn-" + uuid.uuid4().hex,
                "generation": generation,
                "action_id": wake_action_id(name, generation, record.get("subscriber")),
                "pid": os.getpid(),
                "started_at": stamp,
                "started_epoch": clock,
            }
            record["active"] = turn
            record["retry_after_epoch"] = 0
            record["retry_after"] = ""
            record["updated_at"] = stamp
            self._save(name, record)
            return {
                "started": True,
                "session": name,
                "generation": generation,
                "cursor": int(record["cursor"]),
                "turn": turn["turn"],
                "active": turn,
                "recovered": abandoned is not None,
                "abandoned": abandoned,
            }

    def finish_turn(self, session: Any, turn: Any, *, now: Any = None) -> dict[str, Any]:
        name = _raw_session(session)
        stamp = _iso(now)
        with _locked(self._lock_path(name)):
            record = self._load(name)
            active = record.get("active") if isinstance(record.get("active"), Mapping) else None
            turn_id = str(turn)
            if active is None or str(active.get("turn")) != turn_id:
                return {
                    "finished": False,
                    "reason": "turn_mismatch",
                    "session": name,
                    "generation": int(record["generation"]),
                    "cursor": int(record["cursor"]),
                    "pending": int(record["generation"]) > int(record["cursor"]),
                }
            started_generation = int(active.get("generation") or 0)
            record["cursor"] = max(int(record["cursor"]), started_generation)
            record["active"] = None
            record["coalesced"] = max(0, int(record["generation"]) - int(record["cursor"]))
            record["retry_after_epoch"] = 0
            record["retry_after"] = ""
            record["retry_attempts"] = 0
            record["last_error"] = ""
            record["last_error_code"] = ""
            record["updated_at"] = stamp
            saved = self._save(name, record)
            return {
                "finished": True,
                "session": name,
                "generation": int(saved["generation"]),
                "cursor": int(saved["cursor"]),
                "pending": bool(saved["pending"]),
                "coalesced": int(saved["coalesced"]),
                "turn": turn_id,
            }

    def release_turn(
        self,
        session: Any,
        turn: Any,
        *,
        now: Any = None,
        backoff_s: float = 0.0,
        error: str = "",
        code: str = "",
    ) -> dict[str, Any]:
        """Clear an active turn without advancing the cursor (delivery failed).

        Releasing a turn is not itself a retry or a backoff: the default call
        leaves the wake immediately due.  Only a caller that knows the gateway
        failed asks for a bounded ``backoff_s``; that explicitly schedules the
        next attempt so repeated failures cannot spin.
        """

        name = _raw_session(session)
        stamp = _iso(now)
        clock = _clock(now)
        try:
            delay = max(0.0, float(backoff_s or 0.0))
        except (TypeError, ValueError):
            delay = 0.0
        with _locked(self._lock_path(name)):
            record = self._load(name)
            active = record.get("active") if isinstance(record.get("active"), Mapping) else None
            released = False
            if active is not None and str(active.get("turn")) == str(turn):
                record["active"] = None
                released = True
            # Schedule only when there is no live turn left to own the retry.
            if delay > 0 and (released or active is None):
                delay = min(delay, MAX_WAKE_BACKOFF_S)
                attempts = int(record.get("retry_attempts") or 0) + 1
                record["retry_attempts"] = attempts
                record["retry_after_epoch"] = clock + delay
                record["retry_after"] = _iso(clock + delay)
            if error:
                record["last_error"] = str(error)[:500]
            if code:
                record["last_error_code"] = str(code)[:120]
            record["updated_at"] = stamp
            saved = self._save(name, record)
        retry_after = float(saved.get("retry_after_epoch") or 0.0)
        saved["released"] = released
        saved["pending"] = int(saved["generation"]) > int(saved["cursor"])
        saved["retry_after"] = _iso(retry_after) if retry_after > clock else ""
        saved["next_attempt_at"] = saved["retry_after"]
        saved["due"] = bool(saved["pending"] and retry_after <= clock)
        return saved

    def commit_generation(self, session: Any, generation: Any, *, now: Any = None) -> dict[str, Any]:
        """Advance the cursor to a generation whose remote effect is recorded.

        Used to reconcile a wake action that was delivered before the process
        died: the next pass commits that generation without sending again.
        """

        name = _raw_session(session)
        stamp = _iso(now)
        target = int(generation or 0)
        with _locked(self._lock_path(name)):
            record = self._load(name)
            committed = int(record["cursor"]) < target
            record["cursor"] = max(int(record["cursor"]), target)
            active = record.get("active") if isinstance(record.get("active"), Mapping) else None
            if active is not None and int(active.get("generation") or 0) <= target:
                record["active"] = None
            record["coalesced"] = max(0, int(record["generation"]) - int(record["cursor"]))
            record["retry_after_epoch"] = 0
            record["retry_after"] = ""
            record["retry_attempts"] = 0
            record["last_error"] = ""
            record["last_error_code"] = ""
            record["updated_at"] = stamp
            saved = self._save(name, record)
        return {
            "committed": committed,
            "session": name,
            "generation": int(saved["generation"]),
            "cursor": int(saved["cursor"]),
            "pending": bool(saved["pending"]),
        }

    def status(self, session: Any, *, now: Any = None) -> dict[str, Any]:
        name = _raw_session(session)
        record = self._load(name)
        clock = _clock(now)
        active = record.get("active") if isinstance(record.get("active"), Mapping) else None
        active_live = active is not None and not self._expired(active, clock)
        abandoned = active is not None and not active_live
        retry_after = float(record.get("retry_after_epoch") or 0.0)
        pending = int(record["generation"]) > int(record["cursor"])
        record["pending"] = pending
        record["active_turn"] = active_live
        record["abandoned"] = abandoned
        record["abandoned_turn"] = abandoned
        record["recoverable"] = bool(abandoned and pending)
        record["retry_attempts"] = int(record.get("retry_attempts") or 0)
        record["retry_after"] = _iso(retry_after) if retry_after > clock else ""
        record["next_attempt_at"] = record["retry_after"]
        record["due"] = bool(pending and not active_live and retry_after <= clock)
        return record

    def due(self, session: Any, *, now: Any = None) -> bool:
        return bool(self.status(session, now=now)["due"])

    def sessions(self) -> list[str]:
        """Every raw session identity with a durable wake record."""

        found: list[str] = []
        for path in sorted(self.root.glob("*.json")):
            if not path.is_file():
                continue
            record = _read_json(path) or {}
            raw = record.get("session")
            if isinstance(raw, str) and raw:
                found.append(raw)
        return sorted(set(found))


# ---------------------------------------------------------------------------
# Inbox wake-up
# ---------------------------------------------------------------------------


class InboxEventWatcher:
    """Wake on a landing written under ``events/inbox/<subscriber>/``.

    Linux inotify is used when the running kernel and libc expose it.  The
    watcher also returns immediately when an inbox JSON file is already
    pending, so a record written before the loop started is never missed.  On
    a platform without inotify it degrades to a short bounded poll instead of
    pretending the filesystem can notify it.
    """

    _MASK = 0x00000002 | 0x00000008 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400

    def __init__(
        self,
        state_dir: Any,
        *,
        interval_s: float = DEFAULT_SERVICE_POLL_S,
        subscribers: Sequence[str] | None = None,
    ) -> None:
        self.state = _state_path(state_dir)
        self.interval_s = max(0.01, float(interval_s))
        self.subscribers = list(subscribers) if subscribers is not None else None
        self._fd = -1
        self._libc: Any = None
        self._add_watch: Any = None
        self._watches: set[str] = set()
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes = [ctypes.c_int]
            init.restype = ctypes.c_int
            add = libc.inotify_add_watch
            add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            add.restype = ctypes.c_int
            flags = getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = int(init(flags))
            if fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1 refused")
            self._libc = libc
            self._add_watch = add
            self._fd = fd
        except Exception:  # noqa: BLE001 - any missing inotify means poll
            self._fd = -1
            self._libc = None
            self._add_watch = None
        self._refresh_watches()

    @property
    def uses_inotify(self) -> bool:
        return self._fd >= 0

    def _inbox_root(self) -> Path:
        return self.state / event_consumer.EVENTS_DIR / event_consumer.INBOX_DIR

    def _candidate_dirs(self) -> list[Path]:
        root = self._inbox_root()
        found: list[Path] = [root]
        if root.is_dir():
            for child in sorted(root.iterdir(), key=lambda item: item.name):
                if child.is_dir() and not child.name.startswith("."):
                    found.append(child)
        return found

    def _refresh_watches(self) -> None:
        if self._fd < 0 or self._add_watch is None:
            return
        for directory in self._candidate_dirs():
            if not directory.is_dir():
                continue
            key = str(directory)
            if key in self._watches:
                continue
            result = self._add_watch(
                self._fd, key.encode("utf-8"), ctypes.c_uint32(self._MASK)
            )
            if result >= 0:
                self._watches.add(key)

    def _read_events(self) -> bool:
        if self._fd < 0:
            return False
        saw = False
        while True:
            try:
                chunk = os.read(self._fd, 4096)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            saw = True
        if saw:
            self._refresh_watches()
        return saw

    def _pending_event(self) -> bool:
        for directory in self._candidate_dirs():
            if not directory.is_dir():
                continue
            try:
                for path in directory.iterdir():
                    if path.is_file() and path.suffix == ".json" and not path.name.startswith("."):
                        return True
            except OSError:
                continue
        return False

    def wait(self, timeout: float | None = None) -> bool:
        """Block until an inbox event arrives, the timeout elapses or poll fires."""

        if self._fd < 0:
            time.sleep(self.interval_s if timeout is None else min(self.interval_s, max(0.0, timeout)))
            return True
        if self._pending_event():
            return True
        self._refresh_watches()
        if self._pending_event():
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select([self._fd], [], [], remaining)
            except (OSError, ValueError):
                return self._pending_event()
            if ready:
                self._read_events()
                return True
            if deadline is None or time.monotonic() >= deadline:
                return False

    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = -1
        self._watches.clear()

    def __enter__(self) -> "InboxEventWatcher":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - defensive fd cleanup
        self.close()


# ---------------------------------------------------------------------------
# Deterministic event handler
# ---------------------------------------------------------------------------


def _event_data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    data = event.get("data")
    return data if isinstance(data, Mapping) else {}


def _event_id(event: Mapping[str, Any]) -> str:
    for key in ("id", "event_id", "eventId"):
        value = event.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _session_of(event: Mapping[str, Any], default: str = DEFAULT_SESSION) -> str:
    """The raw session identity; storage safety is applied only to filenames."""

    for key in ("session", "session_id", "sessionId"):
        value = event.get(key)
        if value not in (None, ""):
            return _raw_session(value, default)
    coordinator = _event_data(event).get("coordinator")
    if isinstance(coordinator, Mapping):
        for key in ("session", "session_id"):
            value = coordinator.get(key)
            if value not in (None, ""):
                return _raw_session(value, default)
    return _raw_session(default)


def _slug(value: Any, limit: int = 60) -> str:
    text = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in str(value))
    text = text.strip("-._") or "event"
    return text[:limit]


def work_id_for_event(event: Mapping[str, Any]) -> str:
    """A stable scheduler id for the verification work an event makes eligible."""

    data = _event_data(event)
    attempt = str(data.get("attempt_id") or event.get("attempt_id") or "").strip()
    if attempt:
        return "ws_verify_" + _slug(attempt)
    seed = json.dumps(dict(event), sort_keys=True, default=str).encode("utf-8")
    return "ws_verify_" + hashlib.sha256(seed).hexdigest()[:24]


def _event_lease_files(event: Mapping[str, Any]) -> list[str]:
    data = _event_data(event)
    lease = data.get("lease") if isinstance(data.get("lease"), Mapping) else {}
    files = lease.get("assigned_files") if isinstance(lease, Mapping) else None
    if files is None:
        files = event.get("assigned_files")
    if not isinstance(files, (list, tuple, set)):
        return []
    return [str(item) for item in files if isinstance(item, str) and item]


def action_id_for_event(event: Mapping[str, Any], subscriber: Any = None) -> str:
    """A stable identity for one event's accepted work.

    It is derived from the subscriber, event id and attempt, never from a
    process, turn or retry counter, so a replay after a crash resolves to the
    same durable action record rather than a second effect.
    """

    data = _event_data(event)
    event_id = _event_id(event)
    attempt = str(data.get("attempt_id") or event.get("attempt_id") or "").strip()
    seed = json.dumps(
        {
            "subscriber": _safe_session(subscriber) if subscriber not in (None, "") else "",
            "event_id": event_id,
            "attempt_id": attempt,
            "work_id": work_id_for_event(event),
        },
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return "act_" + hashlib.sha256(seed).hexdigest()[:24]


def _action_path(state: Path, action_id: str) -> Path:
    return state / "factory" / ACTION_DIR / f"{action_id}.json"


def _action_lock_path(state: Path, action_id: str) -> Path:
    return state / "factory" / ACTION_DIR / ".locks" / f"{action_id}.lock"


def deterministic_handler(
    state_dir: Any,
    event: Mapping[str, Any],
    *,
    now: Any = None,
    subscriber: Any = None,
) -> dict[str, Any]:
    """Record the verification work an event makes eligible. No model call.

    The handler is deliberately idempotent at the action level.  A durable
    action record is written under the action lock once the scheduler item and
    its job mapping exist.  A process killed after that effect but before the
    inbox acknowledgement is written therefore replays into the same action,
    not a second scheduler item or a second verification tick.
    """

    state = _state_path(state_dir)
    work_id = work_id_for_event(event)
    lease_files = _event_lease_files(event)
    title_attempt = str(_event_data(event).get("attempt_id") or _event_id(event) or work_id)
    action_id = action_id_for_event(event, subscriber)
    action_path = _action_path(state, action_id)
    with _locked(_action_lock_path(state, action_id)):
        stored = _read_json(action_path)
        if stored is not None and stored.get("status") == "recorded":
            result = dict(stored.get("result") or {})
            result["accepted"] = True
            result["created"] = False
            result["duplicate_action"] = True
            result["action_id"] = action_id
            result["action_state"] = "recorded"
            try:
                result["eligible"] = [
                    entry["work_id"] for entry in work_scheduler.ready_items(state)
                ]
            except Exception:  # noqa: BLE001 - a read-only summary must not fail the replay
                result.setdefault("eligible", [])
            return result

        existing_ids = {
            entry["work_id"] for entry in work_scheduler.list_items(state)
        }
        created = work_id not in existing_ids
        try:
            item = work_scheduler.add_item(
                state,
                work_id=work_id,
                title=f"Verify landing {title_attempt}",
                kind="tests",
                priority=0,
                lease_files=lease_files,
                now=now,
            )
        except work_scheduler.SchedulerError as exc:
            if exc.code != "work_exists":
                raise
            item = next(
                (entry for entry in work_scheduler.list_items(state) if entry["work_id"] == work_id),
                {"work_id": work_id},
            )
            created = False
        mapping = state / "factory" / "work-jobs"
        mapping.mkdir(parents=True, exist_ok=True)
        map_path = mapping / f"{work_id}.json"
        map_body = {
            "work_id": work_id,
            "job_id": str(_event_data(event).get("job_id") or ""),
            "attempt_id": str(_event_data(event).get("attempt_id") or ""),
            "event_id": _event_id(event),
            "action_id": action_id,
        }
        if not map_path.exists():
            _write_json(map_path, map_body)

        ready = [entry["work_id"] for entry in work_scheduler.ready_items(state)]
        result = {
            "accepted": True,
            "event_id": _event_id(event),
            "work_id": work_id,
            "job_id": str(_event_data(event).get("job_id") or ""),
            "attempt_id": str(_event_data(event).get("attempt_id") or ""),
            "created": bool(created),
            "duplicate_action": False,
            "action_id": action_id,
            "action_state": "recorded",
            "eligible": ready,
        }
        _write_json(
            action_path,
            {
                "schema": ACTION_SCHEMA,
                "action_id": action_id,
                "status": "recorded",
                "subscriber": _safe_session(subscriber) if subscriber not in (None, "") else "",
                "event_id": _event_id(event),
                "work_id": work_id,
                "job_id": result["job_id"],
                "attempt_id": result["attempt_id"],
                "result": result,
                "updated_at": _iso(now),
            },
        )
        return result


def record_eligible_work(
    state_dir: Any,
    event: Mapping[str, Any],
    *,
    now: Any = None,
    subscriber: Any = None,
) -> dict[str, Any]:
    return deterministic_handler(state_dir, event, now=now, subscriber=subscriber)


# ---------------------------------------------------------------------------
# Startup repair
# ---------------------------------------------------------------------------


def _job_ids(state: Path, queue: Any) -> list[str]:
    found: set[str] = set()
    jobs_dir = state / jobs.JOBS_DIR
    if jobs_dir.is_dir():
        for path in jobs_dir.glob("*.json"):
            if path.is_file():
                found.add(path.stem)
    try:
        for item in queue.list_items():
            if getattr(item, "job_id", ""):
                found.add(str(item.job_id))
    except Exception:  # noqa: BLE001 - a broken queue must not stop job repair
        pass
    try:
        for record in queue_mod.QueueStore(state).active():
            if record.get("job_id"):
                found.add(str(record["job_id"]))
    except Exception:  # noqa: BLE001
        pass
    return sorted(found)


def _event_attempt(event: Any) -> str:
    data = event.get("data") if isinstance(event, Mapping) else None
    if isinstance(data, Mapping) and data.get("attempt_id"):
        return str(data["attempt_id"])
    return ""


def _dir_attempts(directory: Path) -> set[str]:
    attempts: set[str] = set()
    if not directory.is_dir():
        return attempts
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        attempt = _event_attempt(event)
        if attempt:
            attempts.add(attempt)
    return attempts


def _outbox_attempts(state: Path, launcher_outbox: Any = None) -> set[str]:
    attempts = _dir_attempts(state / event_consumer.EVENTS_DIR / bee_events.OUTBOX_DIR)
    if launcher_outbox is not None:
        source = Path(launcher_outbox)
        attempts.update(_dir_attempts(source))
        attempts.update(_dir_attempts(source / landing_bridge.ACK_DIR))
    return attempts


def _lease_for(state: Path, attempt_id: str) -> dict[str, Any] | None:
    try:
        return leases.load_lease(state, attempt_id)
    except Exception:  # noqa: BLE001 - an unreadable lease is not a repairable event
        return None


def _repair_terminal_without_event(
    state: Path,
    job: Mapping[str, Any],
    *,
    launcher_outbox: Any = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return []
    goal = job.get("goal") if isinstance(job.get("goal"), Mapping) else {}
    feature_id = str(job.get("feature_id") or goal.get("feature_id") or job_id)
    existing = _outbox_attempts(state, launcher_outbox)
    created: list[dict[str, Any]] = []
    for task in job.get("tasks") or []:
        if not isinstance(task, Mapping):
            continue
        task_state = str(task.get("state") or "")
        if task_state not in _TERMINAL_TASK_STATES:
            continue
        task_id = str(task.get("task_id") or "")
        binding = task.get("binding") if isinstance(task.get("binding"), Mapping) else {}
        for attempt in task.get("attempts") or []:
            attempt_id = str(attempt)
            if not attempt_id or attempt_id in existing:
                continue
            lease = _lease_for(state, attempt_id) or {}
            lease_sha = (
                lease.get("brief_sha256")
                or task.get("lease_sha256")
                or task.get("contract_sha256")
                or binding.get("contract_sha256")
            )
            source_rev = lease.get("source_rev") or binding.get("source_rev") or goal.get("base_rev")
            assigned = lease.get("assigned_files") or task.get("lease") or []
            if not lease_sha or not source_rev:
                continue
            try:
                event = bee_events.build_event(
                    attempt_id=attempt_id,
                    state=task_state,
                    transition_id="factory-reconcile",
                    feature_id=feature_id,
                    job_id=job_id,
                    task_id=task_id,
                    coordinator="factory-driver",
                    backend="factory-driver",
                    lease_sha256=str(lease_sha),
                    source_rev=str(source_rev),
                    assigned_files=assigned,
                    result_available=False,
                    now=now,
                )
                result = bee_events.record_landing(state, event)
            except Exception:  # noqa: BLE001 - one unrepairable attempt must not stop the pass
                continue
            existing.add(attempt_id)
            if result.get("created"):
                created.append({"job_id": job_id, "task_id": task_id, "attempt_id": attempt_id, "event_id": event.get("id")})
    return created


def startup_reconcile(
    state_dir: Any,
    queue: Any,
    *,
    launcher_outbox: Any = None,
    now: Any = None,
    tick: Any = None,
) -> dict[str, Any]:
    """Repair crash residue through the durable modules, before the first pass.

    It never ticks a job: adoption belongs to the regular queue pass, and ticking
    here as well would drive the same claim twice on every restart. The ``tick``
    keyword is accepted for compatibility with older callers and ignored.
    """

    state = _state_path(state_dir)
    report: dict[str, Any] = {
        "adopted_waves": [],
        "terminal_events": [],
        "delivered": 0,
        "abandoned_claims": [],
        "repair": None,
        "errors": [],
    }

    for job_id in _job_ids(state, queue):
        try:
            job = jobs.load_job(state, job_id, recover=False)
            if jobs.recover_integration(state, job, persist=True):
                report["adopted_waves"].append(job_id)
        except Exception as exc:  # noqa: BLE001 - one damaged job must not stop startup
            report["errors"].append({"job_id": job_id, "error": type(exc).__name__})

    for job_id in _job_ids(state, queue):
        try:
            job = jobs.load_job(state, job_id, recover=False)
            report["terminal_events"].extend(
                _repair_terminal_without_event(state, job, launcher_outbox=launcher_outbox, now=now)
            )
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"job_id": job_id, "error": type(exc).__name__})

    try:
        delivery = bee_events.deliver_pending(state)
        report["delivered"] = int(delivery.get("delivered") or 0)
        report["delivery_failed"] = int(delivery.get("failed") or 0)
    except Exception as exc:  # noqa: BLE001 - delivery is retried by the next pass
        report["errors"].append({"delivery": type(exc).__name__})

    try:
        active = queue_mod.QueueStore(state).active()
    except Exception as exc:  # noqa: BLE001
        active = []
        report["errors"].append({"queue": type(exc).__name__})
    abandoned = [str(record.get("feature_id")) for record in active if not _pid_alive(record.get("pid"))]
    report["abandoned_claims"] = abandoned

    # Adoption is the regular queue pass's job. Running run_once here as well
    # would tick the same claim twice on every restart.
    report["repair"] = None

    return report


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _discover_subscribers(state: Path) -> list[str]:
    found: set[str] = set()
    inbox = state / event_consumer.EVENTS_DIR / event_consumer.INBOX_DIR
    if inbox.is_dir():
        for child in inbox.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                found.add(child.name)
    subscribers = state / event_consumer.EVENTS_DIR / bee_events.SUBSCRIBERS_DIR
    if subscribers.is_dir():
        for path in subscribers.glob("*.json"):
            if path.is_file():
                found.add(path.stem)
    return sorted(found)


#: How much landed history the sweep reads per repository (merged PRs, commits on main).
LANDED_PR_LIMIT = 400
LANDED_COMMIT_PAGES = 3
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9][A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+?(?:\.git)?/?$")


def _origin_owner(repo: Path, timeout_s: float) -> str:
    """The GitHub owner of the driver's checkout (``git remote get-url origin``)."""

    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=str(repo), capture_output=True, text=True,
        timeout=timeout_s, env=child_env({"GIT_TERMINAL_PROMPT": "0"}), stdin=subprocess.DEVNULL,
    )
    match = _GITHUB_REMOTE_RE.search((proc.stdout or "").strip()) if proc.returncode == 0 else None
    if not match:
        raise RuntimeError("the checkout's origin is not a GitHub remote")
    return match.group(1)


def _gh_path() -> str:
    found = shutil.which("gh")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "gh"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    raise RuntimeError("the gh CLI is not installed")


def _gh_json(argv: Sequence[str], timeout_s: float) -> Any:
    proc = subprocess.run(
        [_gh_path(), *argv], capture_output=True, text=True, timeout=timeout_s,
        env=child_env({"GIT_TERMINAL_PROMPT": "0"}), stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh failed ({proc.returncode}): {(proc.stderr or '').strip()[:160]}")
    return json.loads(proc.stdout or "null")


def landed_view_for(repo: Path, *, timeout_s: float = 60.0, gh_json: Any = None) -> Any:
    """Read-only landed-work view: merged PRs and recent commits on main, per project.

    ``project`` is an attempt's ``brief.project`` (a repository name under the
    driver checkout's GitHub owner). Any read failure raises, so the sweep
    reports that repository's view as unavailable and keeps flagging its
    attempts; nothing is written anywhere.
    """

    reader = gh_json if gh_json is not None else (lambda argv: _gh_json(argv, timeout_s))
    owner: list[str] = []

    def view(project: str) -> dict[str, Any]:
        if not owner:
            owner.append(_origin_owner(Path(repo), timeout_s))
        slug = f"{owner[0]}/{project}"
        prs = reader(["pr", "list", "--repo", slug, "--state", "merged", "--limit", str(LANDED_PR_LIMIT),
                      "--json", "number,headRefName,title,body"])
        if not isinstance(prs, list):
            raise RuntimeError("gh pr list returned no list")
        commits: list[Any] = []
        for page in range(1, LANDED_COMMIT_PAGES + 1):
            batch = reader(["api", f"repos/{slug}/commits?sha=main&per_page=100&page={page}",
                            "--jq", "[.[] | {sha: .sha, message: .commit.message}]"])
            if not isinstance(batch, list):
                raise RuntimeError("gh api commits returned no list")
            commits.extend(batch)
            if len(batch) < 100:
                break
        return beat_reconciliation.landed_index(prs, commits)

    return view


def sweep_views_for(client: Any, repo: Path, *, timeout_s: float = 30.0) -> dict[str, Any]:
    """The production sweep views: the launcher through the reconcile adapter.

    ``LauncherClient`` has no ``list_attempts()``; the sweep reads it through
    :class:`launcher_adapter.LauncherAdapter`. No PR view is wired (none is
    cheaply available to the driver), so ``open_prs`` is omitted and the sweep
    reports ``pr_view: "unavailable"`` instead of flagging PR gaps it cannot see.
    """

    repo_path = Path(repo)
    return {
        "launcher": launcher_adapter.LauncherAdapter(client),
        "origin_branches": lambda: sorted(_origin_branch_view(repo_path, timeout_s)),
        "landed": landed_view_for(repo_path),
    }


def _without_pr_view(report: dict[str, Any]) -> dict[str, Any]:
    """Move pushed-without-PR detections aside when there is no PR view."""

    report["pr_view"] = "unavailable"
    kept: list[Any] = []
    unverified: list[Any] = []
    for item in report.get("detected") or []:
        if isinstance(item, Mapping) and item.get("reason") == beat_reconciliation.REASON_RESUME_PR:
            unverified.append(dict(item, note="pr_view_unavailable"))
        else:
            kept.append(item)
    report["detected"] = kept
    report["unverified"] = unverified
    counts = report.get("counts")
    if isinstance(counts, dict):
        counts["detected"] = len(kept)
        counts["unverified"] = len(unverified)
    return report


class FactoryDriver:
    """One coordinator loop over durable queue, landing and scheduler state."""

    def __init__(
        self,
        state_dir: Any,
        queue: Any,
        *,
        tick: Any = None,
        launcher_outbox: Any = None,
        landing_client: Any = None,
        launcher_client: Any = None,
        landing_export_limit: int = landing_bridge.DEFAULT_EXPORT_LIMIT,
        subscribers: Sequence[str] | None = None,
        now: Any = None,
        wake_ttl_s: float = DEFAULT_WAKE_TTL_S,
        startup: bool = True,
        max_work_attempts: int = MAX_WORK_ATTEMPTS,
        campaign_id: str | None = None,
        sweep_views: Mapping[str, Any] | None = None,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
    ) -> None:
        self.state = _state_path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.queue = queue if hasattr(queue, "list_items") else queue_mod.JsonQueue(Path(queue))
        self._tick = tick if tick is not None else mechanical_tick(self.state)
        # A tick that cannot dispatch or verify must never claim queue work.
        self.observation_only = bool(getattr(self._tick, "observation_only", False))
        self.tick = self._serialized_tick(self._tick)
        self.campaign_id = str(campaign_id).strip() if campaign_id else ""
        if self.campaign_id:
            _ = campaign_mod.load_campaign(self.state, self.campaign_id)
        self.launcher_outbox = Path(launcher_outbox) if launcher_outbox is not None else None
        self.landing_client = landing_client if landing_client is not None else launcher_client
        self.landing_export_limit = max(1, int(landing_export_limit))
        self.subscribers = list(subscribers) if subscribers is not None else None
        self.now = now
        self.wake = WakeCoalescer(self.state, ttl_s=wake_ttl_s)
        self.max_work_attempts = max(1, int(max_work_attempts))
        self.sweep_views = sweep_views if isinstance(sweep_views, Mapping) else None
        self.sweep_interval_s = max(0.0, float(sweep_interval_s))
        self._startup_done = not startup
        self.startup_report: dict[str, Any] | None = None
        self._driven_this_pass: set[str] = set()

    # -- durable job serialisation -----------------------------------------

    def _job_lock_path(self, job_id: str) -> Path:
        safe = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in "-_.")
        if not safe or safe != str(job_id):
            safe = "job-" + hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:24]
        return self.state / "factory" / JOB_LOCK_DIR / f"{safe}.lock"

    def _serialized_tick(self, core: Any) -> Any:
        """Serialise every tick for one job across processes and passes.

        The durable queue is still authoritative: a live claim held by another
        process is never ticked by this one.  The per-job flock closes the gap
        where two scheduler items name the same job.
        """

        def tick(job_id: str) -> dict[str, Any]:
            with _locked(self._job_lock_path(str(job_id))):
                owner = self._live_queue_owner(str(job_id))
                if owner is not None:
                    return {
                        "status": "deferred",
                        "action": "claimed_elsewhere",
                        "detail": f"job {job_id!r} is claimed by live pid {owner}",
                    }
                result = core(job_id)
                return result if isinstance(result, dict) else {"status": str(result)}

        tick.observation_only = bool(getattr(core, "observation_only", False))  # type: ignore[attr-defined]
        return tick

    def _live_queue_owner(self, job_id: str) -> int | None:
        try:
            records = queue_mod.QueueStore(self.state).active()
        except Exception:  # noqa: BLE001 - a broken queue is handled by the queue stage
            return None
        for record in records:
            if str(record.get("job_id") or "") != job_id:
                continue
            pid = record.get("pid")
            try:
                number = int(pid)
            except (TypeError, ValueError):
                continue
            if number != os.getpid() and _pid_alive(number):
                return number
        return None

    def _owner_for(self, work_id: str) -> str:
        return f"factory-driver:{os.getpid()}:{work_id}"

    def _is_our_owner(self, owner: Any) -> bool:
        return str(owner or "").startswith("factory-driver:")

    def _recover_work_claims(self) -> list[str]:
        """Release work claimed by a driver process that no longer exists."""

        released: list[str] = []
        try:
            items = work_scheduler.list_items(self.state)
        except Exception:  # noqa: BLE001
            return released
        for item in items:
            if item.get("status") != "claimed":
                continue
            owner = str(item.get("owner") or "")
            if not self._is_our_owner(owner):
                continue
            parts = owner.split(":", 2)
            try:
                pid = int(parts[1])
            except (IndexError, ValueError):
                pid = 0
            if pid == os.getpid() or _pid_alive(pid):
                continue
            try:
                work_scheduler.release(
                    self.state, item["work_id"], owner=owner, now=self.now
                )
                released.append(str(item["work_id"]))
            except Exception:  # noqa: BLE001
                continue
        return released

    # -- bounded work retry ledger -----------------------------------------

    def _work_retry_path(self, work_id: str) -> Path:
        safe = "".join(ch for ch in str(work_id) if ch.isalnum() or ch in "-_.")
        if not safe or safe != str(work_id):
            safe = "work-" + hashlib.sha256(str(work_id).encode("utf-8")).hexdigest()[:24]
        return self.state / "factory" / WORK_RETRY_DIR / f"{safe}.json"

    def _retry_state(self, work_id: str) -> dict[str, Any]:
        return _read_json(self._work_retry_path(work_id)) or {}

    def _record_work_failure(self, work_id: str, exc: BaseException) -> dict[str, Any]:
        path = self._work_retry_path(work_id)
        with _locked(path.with_suffix(".lock")):
            record = _read_json(path) or {}
            attempts = int(record.get("attempts") or 0) + 1
            exhausted = attempts >= self.max_work_attempts
            body = {
                "schema": "bundlebees.factory-work-retry/1",
                "work_id": work_id,
                "attempts": attempts,
                "max_attempts": self.max_work_attempts,
                "exhausted": exhausted,
                "error": type(exc).__name__,
                "code": str(getattr(exc, "code", "") or type(exc).__name__),
                "message": str(getattr(exc, "message", "") or str(exc))[:500],
                "fix": str(getattr(exc, "fix", "") or "Inspect the worker tick and retry or escalate."),
                "updated_at": _iso(self.now),
            }
            _write_json(path, body)
        return body

    def _clear_work_failure(self, work_id: str) -> None:
        try:
            self._work_retry_path(work_id).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def subscribers_for_pass(self) -> list[str]:
        if self.subscribers is not None:
            return [_safe_session(name) for name in self.subscribers]
        discovered = _discover_subscribers(self.state)
        return discovered or [DEFAULT_SESSION]

    def _ensure_startup(self) -> dict[str, Any] | None:
        if self._startup_done:
            return None
        self._startup_done = True
        self.startup_report = startup_reconcile(
            self.state,
            self.queue,
            launcher_outbox=self.launcher_outbox,
            now=self.now,
        )
        recovered = self._recover_work_claims()
        if recovered:
            self.startup_report["released_work_claims"] = recovered
        return self.startup_report

    def _event_handler(self, subscriber: str) -> Any:
        def handler(event: Mapping[str, Any]) -> dict[str, Any]:
            # Inbox handling is short and must not hold a model turn open. A
            # busy session is deferred, not turned into a handler failure: the
            # event is acknowledged as durable work and the wake coalescer keeps
            # the pending generation until a safe boundary.
            session = _session_of(event, subscriber)
            wake = self.wake.request_wake(
                session, now=self.now, subscriber=subscriber
            )
            result = deterministic_handler(
                self.state, event, now=self.now, subscriber=subscriber
            )
            wake_events = self.state / "factory" / "wake-events"
            wake_events.mkdir(parents=True, exist_ok=True)
            event_path = wake_events / f"{self.wake.key_for(session)}.json"
            _write_json(event_path, dict(event))
            result["session"] = session
            result["session_key"] = self.wake.key_for(session)
            result["subscriber"] = subscriber
            result["generation"] = int(wake.get("generation") or 0)
            result["deferred"] = bool(wake.get("active"))
            result["turn_active"] = bool(wake.get("active"))
            return result
        return handler

    def _drive_work(
        self, work_id: str, job_id: str, *, max_active: int | None = None
    ) -> dict[str, Any]:
        """Claim one scheduler item, then run the protected job tick."""

        if not work_id:
            try:
                return {"tick": self.tick(job_id)}
            except Exception as exc:  # noqa: BLE001
                failure = self._record_work_failure(job_id, exc)
                return {
                    "work_state": "failed",
                    "failure": failure,
                    "tick": {"status": "error", "error": type(exc).__name__},
                }
        if work_id in self._driven_this_pass:
            return {"work_state": "already_driven_this_pass"}
        retry = self._retry_state(work_id)
        if retry.get("exhausted"):
            return {"work_state": "failed", "failure": retry}
        owner = self._owner_for(work_id)
        try:
            work_scheduler.claim(
                self.state,
                work_id,
                owner,
                str(self.state),
                now=self.now,
                max_active=max_active,
            )
        except work_scheduler.SchedulerError as exc:
            if exc.code == "work_done":
                self._clear_work_failure(work_id)
                return {"work_state": "done"}
            return {
                "work_state": "skipped",
                "failure": {
                    "work_id": work_id,
                    "code": exc.code,
                    "message": exc.message,
                    "fix": exc.fix,
                },
            }
        self._driven_this_pass.add(work_id)
        try:
            tick_result = self.tick(job_id)
        except Exception as exc:  # noqa: BLE001 - one job must not stop the pass
            failure = self._record_work_failure(work_id, exc)
            if failure.get("exhausted"):
                try:
                    work_scheduler.release(
                        self.state, work_id, owner=owner, now=self.now
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {
                "work_state": "retryable" if not failure.get("exhausted") else "failed",
                "failure": failure,
                "tick": {"status": "error", "error": type(exc).__name__},
            }
        try:
            done = queue_mod.job_accepted(self.state, job_id)
        except Exception:  # noqa: BLE001
            done = False
        completed = done or str(tick_result.get("action") or "") in {
            "ready_for_review",
            "accepted",
        }
        if completed:
            jobs.release_repo_grant_for_job(self.state, job_id)
            try:
                work_scheduler.complete(self.state, work_id, owner=owner, now=self.now)
                self._clear_work_failure(work_id)
                return {"work_state": "done", "tick": tick_result}
            except Exception as exc:  # noqa: BLE001
                return {
                    "work_state": f"sync_error:{type(exc).__name__}",
                    "tick": tick_result,
                }
        return {"work_state": "claimed", "tick": tick_result}

    def _advance_ready_work(self) -> list[dict[str, Any]]:
        """Advance durable scheduler items after claiming them, one job at a time."""

        failures: list[dict[str, Any]] = []
        mapping = self.state / "factory" / "work-jobs"
        if not mapping.is_dir():
            return failures
        try:
            items = work_scheduler.list_items(self.state)
            ready_ids = {
                str(entry.get("work_id") or "")
                for entry in work_scheduler.ready_items(self.state)
            }
        except Exception:  # noqa: BLE001
            return failures
        for item in items:
            work_id = str(item.get("work_id") or "")
            status = str(item.get("status") or "")
            if status == "claimed" and not self._is_our_owner(item.get("owner")):
                continue
            if status not in {"pending", "claimed"}:
                continue
            if status == "pending" and work_id not in ready_ids:
                continue
            if work_id in self._driven_this_pass:
                continue
            retry = self._retry_state(work_id)
            if retry.get("exhausted"):
                continue
            map_path = mapping / f"{work_id}.json"
            try:
                body = json.loads(map_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            job_id = str(body.get("job_id") or "")
            if not job_id:
                continue
            outcome = self._drive_work(work_id, job_id)
            if outcome.get("failure"):
                failures.append(outcome["failure"])
        return failures

    def _subscriber_binding(
        self, event: Mapping[str, Any], wake_record: Mapping[str, Any], fallback: str
    ) -> dict[str, Any] | None:
        """Load the configured subscriber record that owns this event."""

        identity = ""
        coordinator = _event_data(event).get("coordinator")
        if isinstance(coordinator, Mapping):
            identity = str(coordinator.get("identity") or "").strip()
        candidates = self.subscribers_for_pass()
        name = str(wake_record.get("subscriber") or "").strip()
        if not name:
            name = identity if identity in candidates else fallback
        name = _safe_session(name)
        try:
            path = bee_events._subscriber_path(self.state, name)
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unbound session still uses the explicit session
            return None
        if not isinstance(record, Mapping) or str(record.get("identity") or "") != name:
            return None
        return dict(record)

    def _wake_action_path(self, action_id: str) -> Path:
        return self.state / "factory" / WAKE_ACTION_DIR / f"{action_id}.json"

    def _read_wake_action(self, action_id: str) -> dict[str, Any]:
        return _read_json(self._wake_action_path(action_id)) or {}

    def _write_wake_action(self, action_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(body)
        record["schema"] = WAKE_ACTION_SCHEMA
        record["action_id"] = action_id
        _write_json(self._wake_action_path(action_id), record)
        return record

    def _wake_backoff_s(self, attempts: Any) -> float:
        """Bounded exponential delay after an actual gateway failure."""

        try:
            count = max(1, int(attempts or 1))
        except (TypeError, ValueError):
            count = 1
        delay = DEFAULT_WAKE_BACKOFF_S * (2 ** min(count - 1, 8))
        return min(MAX_WAKE_BACKOFF_S, delay)

    @staticmethod
    def _wake_failure_is_ambiguous(exc: BaseException) -> bool:
        """Whether a failed notification may already have reached the gateway.

        A missing endpoint/key or a routing refusal is known not to have been
        sent.  A timeout, transport error, non-2xx and any unknown transport
        exception is uncertain because the POST may have landed.
        """

        code = str(getattr(exc, "code", "") or "")
        if code in {
            "hermes_notification_unavailable",
            "hermes_notification_invalid",
            "hermes_notification_too_large",
            "hermes_session_mismatch",
        }:
            return False
        return True

    @staticmethod
    def _notify_accepts_runner() -> bool:
        """Keep the transport seam optional for a test double with a fixed signature."""

        try:
            parameters = inspect.signature(hermes_adapter.notify_coordinator).parameters
        except (TypeError, ValueError):
            return False
        return "runner" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    def _wake_runner(self, action_id: str) -> Any:
        """Carry the stable action identity to the gateway as an idempotency key."""

        def runner(url: str, *, headers: Mapping[str, str], body: bytes, timeout_s: float):
            carried = dict(headers)
            carried["Idempotency-Key"] = action_id
            carried["X-Bundlebees-Action-Id"] = action_id
            return hermes_adapter._post_wake(  # noqa: SLF001 - same package transport seam
                url, carried, body, timeout_s=timeout_s
            )

        return runner

    def _reconcile_wake_action(
        self, session: str, status: Mapping[str, Any], action_id: str
    ) -> dict[str, Any] | None:
        """Resolve a durable wake action left by a previous process.

        A recorded delivery is committed without a second POST.  A leftover
        ``dispatching`` action is ambiguous: it is marked uncertain and given a
        bounded delay instead of being retried blindly.  Action records keyed by
        the same stable id are reused for later bounded retries.
        """

        action = self._read_wake_action(action_id)
        if not action:
            return None
        state = str(action.get("status") or "")
        generation = int(action.get("generation") or status.get("generation") or 0)
        if state == "delivered" and generation == int(status.get("generation") or 0):
            committed = self.wake.commit_generation(session, generation, now=self.now)
            return {
                "session": session,
                "session_key": self.wake.key_for(session),
                "delivered": True,
                "reconciled": True,
                "recovered": bool(status.get("abandoned_turn")),
                "action_id": action_id,
                "action_state": "delivered",
                "event_id": action.get("event_id", ""),
                "turn": action.get("turn"),
                "turn_finished": bool(committed.get("committed")),
                "pending": bool(committed.get("pending")),
                "subscriber": action.get("subscriber_identity", ""),
            }
        if state not in {"dispatching", "uncertain"}:
            return None

        now_epoch = _clock(self.now)
        not_before = float(action.get("not_before_epoch") or 0.0)
        if state == "uncertain" and not_before > now_epoch:
            return {
                "session": session,
                "session_key": self.wake.key_for(session),
                "delivered": False,
                "reconciled": True,
                "uncertain": True,
                "action_id": action_id,
                "action_state": "uncertain",
                "turn": action.get("turn"),
                "turn_finished": False,
                "pending": True,
                "retry_after": _iso(not_before),
                "next_attempt_at": _iso(not_before),
                "code": "wake_outcome_uncertain",
                "fix": "Wait for the recorded reconciliation delay; do not re-POST the same wake blindly.",
            }
        if state == "dispatching":
            # The process died between persisting intent and recording the
            # result. Reconcile to uncertain and wait; never issue a fresh POST
            # just because the owner vanished.
            attempts = int(action.get("attempts") or 1)
            delay = self._wake_backoff_s(attempts)
            not_before = now_epoch + delay
            self._write_wake_action(
                action_id,
                dict(
                    action,
                    status="uncertain",
                    attempts=attempts,
                    first_uncertain_at=action.get("first_uncertain_at") or _iso(now_epoch),
                    not_before_epoch=not_before,
                    not_before=_iso(not_before),
                    detail="the process that dispatched this wake exited before recording its outcome",
                ),
            )
            active = status.get("active") if isinstance(status.get("active"), Mapping) else {}
            released = self.wake.release_turn(
                session,
                active.get("turn") if isinstance(active, Mapping) else None,
                now=self.now,
                backoff_s=delay,
                error="ambiguous wake outcome",
                code="wake_outcome_uncertain",
            )
            return {
                "session": session,
                "session_key": self.wake.key_for(session),
                "delivered": False,
                "reconciled": True,
                "uncertain": True,
                "recovered": bool(status.get("abandoned_turn")),
                "action_id": action_id,
                "action_state": "uncertain",
                "turn": action.get("turn"),
                "turn_finished": False,
                "pending": True,
                "retry_after": released.get("retry_after", ""),
                "next_attempt_at": released.get("retry_after", ""),
                "code": "wake_outcome_uncertain",
                "fix": "Reconcile the gateway journal before retrying; the wake is not re-POSTed blindly.",
            }
        return None

    def _deliver_wakes(self) -> list[dict[str, Any]]:
        """Deliver one coalesced wake per due session through the real adapter."""

        results: list[dict[str, Any]] = []
        sessions = self.wake.sessions() or self.subscribers_for_pass()
        fallback = (self.subscribers_for_pass() or [DEFAULT_SESSION])[0]
        for session in sessions:
            status = self.wake.status(session, now=self.now)
            if not status.get("due"):
                continue
            generation = int(status.get("generation") or 0)
            action_id = wake_action_id(session, generation, status.get("subscriber"))
            reconciled = self._reconcile_wake_action(session, status, action_id)
            if reconciled is not None:
                results.append(reconciled)
                continue
            prior = self._read_wake_action(action_id)
            prior_state = str(prior.get("status") or "")
            turn = self.wake.begin_turn(session, now=self.now)
            if not turn.get("started"):
                continue
            action_id = str(turn.get("active", {}).get("action_id") or action_id)
            event: Mapping[str, Any] = {}
            subscriber: Mapping[str, Any] | None = None
            try:
                event_path = (
                    self.state
                    / "factory"
                    / "wake-events"
                    / f"{self.wake.key_for(session)}.json"
                )
                event = json.loads(event_path.read_text(encoding="utf-8"))
                subscriber = self._subscriber_binding(event, status, fallback)
                attempts = int(prior.get("attempts") or 0) + 1
                self._write_wake_action(
                    action_id,
                    {
                        "session": session,
                        "session_key": self.wake.key_for(session),
                        "subscriber": _safe_session(status.get("subscriber")),
                        "subscriber_identity": (subscriber or {}).get("identity", ""),
                        "generation": int(turn.get("generation") or generation),
                        "turn": turn.get("turn"),
                        "pid": os.getpid(),
                        "status": "dispatching",
                        "attempts": attempts,
                        "previous_status": prior_state,
                        "first_uncertain_at": prior.get("first_uncertain_at", ""),
                        "not_before_epoch": 0,
                        "not_before": "",
                        "event_id": _event_id(event),
                        "updated_at": _iso(self.now),
                    },
                )
                notify_kwargs = {
                    "endpoint": os.environ.get("HERMES_API_URL") or None,
                    "api_key": os.environ.get("HERMES_API_KEY") or None,
                    "session": session,
                    "subscriber": subscriber,
                    "state_dir": self.state,
                    "timeout_s": float(os.environ.get("HERMES_WAKE_TIMEOUT_S") or 60.0),
                }
                if self._notify_accepts_runner():
                    notify_kwargs["runner"] = self._wake_runner(action_id)
                delivered = hermes_adapter.notify_coordinator(event, **notify_kwargs)
                self._write_wake_action(
                    action_id,
                    {
                        "session": session,
                        "session_key": self.wake.key_for(session),
                        "subscriber": _safe_session(status.get("subscriber")),
                        "subscriber_identity": (subscriber or {}).get("identity", ""),
                        "generation": int(turn.get("generation") or generation),
                        "turn": turn.get("turn"),
                        "pid": os.getpid(),
                        "status": "delivered",
                        "attempts": attempts,
                        "previous_status": prior_state,
                        "first_uncertain_at": prior.get("first_uncertain_at", ""),
                        "not_before_epoch": 0,
                        "not_before": "",
                        "event_id": delivered.get("event_id") or _event_id(event),
                        "result": {
                            "event_id": delivered.get("event_id"),
                            "status": delivered.get("status"),
                            "transport": delivered.get("transport"),
                        },
                        "updated_at": _iso(self.now),
                    },
                )
                finished = self.wake.finish_turn(
                    session, turn.get("turn"), now=self.now
                )
                results.append(
                    {
                        "session": session,
                        "session_key": self.wake.key_for(session),
                        "delivered": True,
                        "event_id": delivered.get("event_id"),
                        "action_id": action_id,
                        "action_state": "delivered",
                        "attempts": attempts,
                        "reconciled": bool(prior_state in {"failed", "uncertain", "dispatching"}),
                        "recovered": bool(turn.get("recovered")),
                        "turn": turn.get("turn"),
                        "turn_finished": bool(finished.get("finished")),
                        "pending": bool(finished.get("pending")),
                        "subscriber": (subscriber or {}).get("identity", ""),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - durable wake remains pending
                code = str(getattr(exc, "code", "") or "")
                ambiguous = self._wake_failure_is_ambiguous(exc)
                attempts = int(self._read_wake_action(action_id).get("attempts") or 1)
                delay = self._wake_backoff_s(attempts) if ambiguous else 0.0
                now_epoch = _clock(self.now)
                action_state = "uncertain" if ambiguous else "failed"
                self._write_wake_action(
                    action_id,
                    {
                        "session": session,
                        "session_key": self.wake.key_for(session),
                        "subscriber": _safe_session(status.get("subscriber")),
                        "subscriber_identity": (subscriber or {}).get("identity", ""),
                        "generation": int(turn.get("generation") or generation),
                        "turn": turn.get("turn"),
                        "pid": os.getpid(),
                        "status": action_state,
                        "attempts": attempts,
                        "previous_status": prior_state,
                        "first_uncertain_at": self._read_wake_action(action_id).get(
                            "first_uncertain_at", ""
                        )
                        or _iso(now_epoch),
                        "not_before_epoch": now_epoch + delay if delay > 0 else 0.0,
                        "not_before": _iso(now_epoch + delay) if delay > 0 else "",
                        "event_id": _event_id(event),
                        "error": type(exc).__name__,
                        "code": code,
                        "message": str(getattr(exc, "message", "") or str(exc))[:500],
                        "updated_at": _iso(self.now),
                    },
                )
                released = self.wake.release_turn(
                    session,
                    turn.get("turn"),
                    now=self.now,
                    backoff_s=delay,
                    error=str(getattr(exc, "message", "") or str(exc)),
                    code=code,
                )
                results.append(
                    {
                        "session": session,
                        "session_key": self.wake.key_for(session),
                        "delivered": False,
                        "turn": turn.get("turn"),
                        "turn_finished": False,
                        "action_id": action_id,
                        "action_state": action_state,
                        "attempts": attempts,
                        "ambiguous": ambiguous,
                        "reconciled": False,
                        "error": type(exc).__name__,
                        "code": code,
                        "message": str(getattr(exc, "message", "") or str(exc))[:500],
                        "fix": str(
                            getattr(exc, "fix", "")
                            or "Keep the wake pending and retry after the gateway recovers."
                        ),
                        "pending": True,
                        "retry_after": released.get("retry_after", ""),
                        "next_attempt_at": released.get("retry_after", ""),
                    }
                )
        return results

    def _advance_event_work(self, event_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drive authoritative job progress only for a newly accepted inbox ack."""

        failures: list[dict[str, Any]] = []
        for entry in event_results:
            if entry.get("state") != "acked":
                entry["work_state"] = "duplicate_skipped" if entry.get("duplicate") else "not_acked"
                continue
            job_id = str(entry.get("job_id") or "")
            work_id = str(entry.get("work_id") or "")
            if not job_id:
                entry["work_state"] = "no_job"
                continue
            outcome = self._drive_work(work_id, job_id)
            entry.update(outcome)
            if outcome.get("failure"):
                failures.append(outcome["failure"])
        return failures

    def _sweep_marker_path(self) -> Path:
        return self.state / "factory" / SWEEP_LAST_FILENAME

    def _sweep_result(self) -> dict[str, Any]:
        """Run the backstop reconciliation sweep, classify-only, at most once per interval."""

        views = self.sweep_views
        if views is None:
            return {"status": "not_configured"}
        clock = _clock(self.now)
        try:
            marker_path = self._sweep_marker_path()
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, OSError, ValueError):
                marker = None
            last = marker.get("at") if isinstance(marker, Mapping) else None
            # A future-dated marker (clock skew) is ignored rather than
            # suppressing the sweep until the clock catches up.
            if (
                isinstance(last, (int, float))
                and 0.0 <= clock - float(last) < self.sweep_interval_s
            ):
                remaining = self.sweep_interval_s - (clock - float(last))
                return {
                    "status": "skipped",
                    "reason": "interval",
                    "next_in_s": int(max(0.0, remaining)),
                }
            # Classify-only end to end: no processor, no sweep record, no
            # claim files, and no board-stock duty (that one can post comments).
            pr_view = views.get("open_prs")
            sweep = beat_reconciliation.sweep(
                self.state,
                launcher=views.get("launcher"),
                origin_branches=views.get("origin_branches"),
                open_prs=pr_view if pr_view is not None else {},
                processor=None,
                branch_of=views.get("branch_of"),
                now=clock,
                record=False,
                claim=False,
                landed=views.get("landed"),
            )
            if pr_view is None and sweep.get("status") != "error":
                sweep = _without_pr_view(sweep)
            _write_private_json(marker_path, {"at": clock})
            status = str(sweep.get("status") or "ok")
            return {
                "schema": beat.SCHEMA,
                "at": beat_reconciliation.stamp(clock),
                "mode": "classify_only",
                "status": "partial" if status == "error" else status,
                "duties": {beat.DUTY_SWEEP: sweep},
            }
        except Exception as exc:  # noqa: BLE001 - the sweep never breaks the pass
            return {
                "status": "error",
                "code": type(exc).__name__,
                "error": str(scrub(str(exc)))[:300],
            }

    def run_pass(self, *, advance_work: bool = True) -> dict[str, Any]:
        self._driven_this_pass = set()
        startup = self._ensure_startup()
        self._recover_work_claims()
        errors: list[dict[str, str]] = []

        queue_result: dict[str, Any]
        try:
            queue_result = queue_mod.run_once(
                self.state, self.queue, tick=self.tick, admit=not self.observation_only
            )
        except Exception as exc:  # noqa: BLE001 - landing and events still run
            queue_result = {"action": "error", "detail": f"{type(exc).__name__}: {exc}"}
            errors.append({"stage": "queue", "error": type(exc).__name__})

        landing_result: dict[str, Any] | None = None
        if self.landing_client is not None:
            try:
                landing_result = landing_bridge.ingest_remote(
                    self.state,
                    self.landing_client,
                    limit=self.landing_export_limit,
                    now=self.now,
                )
            except Exception as exc:  # noqa: BLE001
                landing_result = {"processed": 0, "duplicates": 0, "failed": 1, "pending": 0,
                                  "sources": [], "failures": [{"error": type(exc).__name__}]}
                errors.append({"stage": "landing", "error": type(exc).__name__})
        elif self.launcher_outbox is not None:
            try:
                landing_result = landing_bridge.consume_once(self.state, self.launcher_outbox, now=self.now)
            except Exception as exc:  # noqa: BLE001
                landing_result = {"processed": 0, "duplicates": 0, "failed": 1, "pending": 0,
                                  "sources": [], "failures": [{"error": type(exc).__name__}]}
                errors.append({"stage": "landing", "error": type(exc).__name__})

        event_results: list[dict[str, Any]] = []
        for subscriber in self.subscribers_for_pass():
            try:
                outcome = event_consumer.consume_once(
                    self.state,
                    subscriber,
                    self._event_handler(subscriber),
                    consumer="factory-driver",
                    now=self.now,
                )
            except Exception as exc:  # noqa: BLE001 - one subscriber must not stop the pass
                errors.append({"stage": "events", "subscriber": subscriber, "error": type(exc).__name__})
                continue
            if outcome is None:
                continue
            entry = {
                "subscriber": subscriber,
                "event_id": outcome.get("event_id"),
                "state": outcome.get("state"),
                "duplicate": bool(outcome.get("duplicate")),
                "attempts": outcome.get("attempts"),
                "inbox_ack": {
                    "state": outcome.get("state"),
                    "duplicate": bool(outcome.get("duplicate")),
                    "attempts": outcome.get("attempts"),
                    "error": outcome.get("error") or "",
                },
            }
            if outcome.get("error"):
                entry["error"] = outcome.get("error")
                entry["code"] = outcome.get("error_code") or outcome.get("error")
                entry["fix"] = outcome.get("fix") or "Inspect events/inbox and events/acks."
            result = outcome.get("result") if isinstance(outcome.get("result"), Mapping) else {}
            for key in (
                "work_id",
                "job_id",
                "attempt_id",
                "session",
                "session_key",
                "generation",
                "eligible",
                "created",
                "action_id",
                "action_state",
                "duplicate_action",
                "deferred",
                "turn_active",
            ):
                if key in result:
                    entry[key] = result[key]
            event_results.append(entry)

        failures: list[dict[str, Any]] = []
        if advance_work:
            failures.extend(self._advance_event_work(event_results))
            failures.extend(self._advance_ready_work())
        else:
            # Service mode dispatches through its own approval/cap gate. The
            # generic advance path must not claim anything behind that gate.
            for entry in event_results:
                entry.setdefault("work_state", "service_gated")
        wake_results = self._deliver_wakes()

        campaign_result: dict[str, Any] | None = None
        if self.campaign_id:
            try:
                campaign_result = campaign_mod.reconcile(
                    self.state, self.campaign_id, now=_iso(self.now)
                )
            except Exception as exc:  # noqa: BLE001 - a checkpoint must not stop the factory
                campaign_result = {
                    "campaign_id": self.campaign_id,
                    "phase": "blocked",
                    "error": getattr(exc, "code", type(exc).__name__),
                    "detail": str(exc),
                }
                errors.append({"stage": "campaign", "error": type(exc).__name__})

        try:
            ready = work_scheduler.ready_items(self.state)
        except Exception as exc:  # noqa: BLE001
            ready = []
            errors.append({"stage": "ready", "error": type(exc).__name__})

        event_activity = any(entry.get("state") in ("acked", "retryable", "failed") for entry in event_results)
        landing_activity = bool(landing_result and (landing_result.get("processed") or landing_result.get("failed")))
        queue_activity = queue_result.get("action") not in (None, "idle")
        idle = not queue_activity and not landing_activity and not event_activity and not ready and not failures

        wake_names = sorted(
            set(self.wake.sessions()) | set(self.subscribers_for_pass())
        )
        accepted_work = [
            entry.get("work_id")
            for entry in event_results
            if entry.get("work_state") == "done" and entry.get("work_id")
        ]

        result = {
            "action": queue_result.get("action"),
            "queue": queue_result,
            "landing": landing_result,
            "events": event_results,
            "accepted_work": accepted_work,
            "ready": [entry["work_id"] for entry in ready],
            "ready_items": ready,
            "wake": [self.wake.status(name) for name in wake_names],
            "wake_delivery": wake_results,
            "campaign": campaign_result,
            "failures": failures,
            "idle": idle,
            "observation_only": self.observation_only,
            "inference": 0,
            "model_calls": 0,
            "errors": errors,
        }
        if startup is not None:
            result["startup"] = startup
        result["sweep"] = self._sweep_result()
        return result

    def run_once(self) -> dict[str, Any]:
        return self.run_pass()


class FactoryService:
    """Event-driven service mode around one :class:`FactoryDriver`.

    One pass drains the completion consumer, runs the durable driver pass
    without its generic auto-dispatch, then asks the work scheduler for ready
    items and dispatches only the ones a readable pre-approval, the shared bee
    cap and the planning-slot limits allow.  Anything that needs judgment
    (missing/unreadable approval, scope, cap or limits) stops the dispatch and
    requests one coalesced Hermes wake through the existing gateway path.
    """

    def __init__(
        self,
        driver: FactoryDriver,
        *,
        subscribers: Sequence[str] | None = None,
        completion_subscribers: Sequence[str] | None = None,
        interval_s: float = DEFAULT_SERVICE_INTERVAL_S,
        poll_s: float = DEFAULT_SERVICE_POLL_S,
        watcher: Any = None,
        session: str | None = None,
        cap_reader: Any = None,
        limits_reader: Any = None,
        go_max_gate: Any = None,
    ) -> None:
        self.driver = driver
        # go-max (docs/contracts/go-max.md): inert unless the owner turned it on.
        self.go_max = go_max_gate
        self.subscribers = list(subscribers) if subscribers is not None else None
        self.completion_subscribers = (
            list(completion_subscribers)
            if completion_subscribers is not None
            else [event_consumer.DEFAULT_COMPLETION_SUBSCRIBER]
        )
        self.interval_s = max(0.01, float(interval_s))
        self.poll_s = max(0.01, float(poll_s))
        self.session = _raw_session(session) if session else ""
        self._cap_reader = cap_reader or read_shared_cap
        self._limits_reader = limits_reader or work_scheduler.planning_limits
        self.watcher = watcher or InboxEventWatcher(
            driver.state, interval_s=self.poll_s, subscribers=self.subscribers
        )
        self.passes = 0
        self.last_result: dict[str, Any] | None = None
        self._closed = False

    @property
    def state(self) -> Path:
        return self.driver.state

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.watcher, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "FactoryService":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # -- completion consumer -------------------------------------------------

    def drain_completions(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for subscriber in self.completion_subscribers:
            name = _safe_session(subscriber)
            for _ in range(1000):
                outcome = event_consumer.consume_completions(
                    self.state, subscriber=name, now=self.driver.now
                )
                if outcome is None or outcome.get("duplicate"):
                    break
                results.append(dict(outcome))
                if str(outcome.get("state") or "") != "acked":
                    break
        return results

    # -- pre-approval, cap and planning-slot gate ---------------------------

    @staticmethod
    def _job_id_mapping(state: Path, work_id: str) -> str:
        path = state / "factory" / "work-jobs" / f"{work_id}.json"
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FactoryError(
                "job_unreadable",
                f"the job mapping for {work_id!r} is unreadable: {exc}",
                "Restore factory/work-jobs before dispatching this item.",
            ) from exc
        job_id = str(body.get("job_id") or "") if isinstance(body, Mapping) else ""
        if not job_id:
            raise FactoryError(
                "job_unreadable",
                f"the job mapping for {work_id!r} names no job",
                "Restore factory/work-jobs before dispatching this item.",
            )
        return job_id

    def _evaluate_item(
        self,
        item: Mapping[str, Any],
        *,
        cap: int,
        limits: Mapping[str, int],
        items: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        work_id = str(item.get("work_id") or "")
        try:
            approval = read_dispatch_approval(self.state, work_id)
        except FactoryError as exc:
            return {"allowed": False, "reason": "approval_unreadable", "detail": exc.message}
        if approval is None:
            return {"allowed": False, "reason": "approval_absent", "detail": "no pre-approval record"}
        if approval.get("approved") is not True:
            return {"allowed": False, "reason": "not_preapproved", "detail": "approved is not true"}
        scope = approval.get("scope")
        if not isinstance(scope, list) or not scope:
            return {"allowed": False, "reason": "scope_unreadable", "detail": "scope is not a readable list"}
        if not all(isinstance(entry, str) and entry.strip() for entry in scope):
            return {"allowed": False, "reason": "scope_unreadable", "detail": "scope has a non-string entry"}
        lease_files = [str(path) for path in item.get("lease_files") or ()]
        if not _scope_allows(scope, lease_files):
            return {
                "allowed": False,
                "reason": "out_of_scope",
                "detail": f"{lease_files!r} is not covered by {scope!r}",
            }
        mapped_job = str(approval.get("job_id") or "")
        if mapped_job:
            try:
                expected = self._job_id_mapping(self.state, work_id)
            except FactoryError as exc:
                return {"allowed": False, "reason": "job_unreadable", "detail": exc.message}
            if expected != mapped_job:
                return {
                    "allowed": False,
                    "reason": "approval_unreadable",
                    "detail": f"approval names job {mapped_job!r}, mapping names {expected!r}",
                }
        active_total = sum(1 for entry in items if entry.get("status") == "claimed")
        if str(item.get("kind") or "") in work_scheduler.PLANNING_KINDS:
            active_planning = sum(
                1
                for entry in items
                if entry.get("status") == "claimed"
                and str(entry.get("kind") or "") in work_scheduler.PLANNING_KINDS
            )
            if active_planning >= int(limits["planning_max_active"]):
                return {
                    "allowed": False,
                    "reason": "planning_slots_exhausted",
                    "detail": f"{active_planning} planning item(s) already active",
                }
        if active_total >= cap:
            return {
                "allowed": False,
                "reason": "capacity_exhausted",
                "detail": f"{active_total} active item(s) already use the shared cap of {cap}",
            }
        return {"allowed": True, "reason": "allowed", "cap": cap, "limits": dict(limits)}

    # -- judgment wake -------------------------------------------------------

    def _wake_target(self) -> tuple[str, str]:
        session = self.session or (
            (self.subscribers or self.driver.subscribers_for_pass()) or [DEFAULT_SESSION]
        )[0]
        subscriber = (
            (self.subscribers or self.driver.subscribers_for_pass()) or [DEFAULT_SESSION]
        )[0]
        return _raw_session(session), _safe_session(subscriber)

    def request_judgment_wake(
        self,
        *,
        reason: str,
        item: Mapping[str, Any] | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        """Request one coalesced gateway wake for a dispatch that needs judgment."""

        session, subscriber = self._wake_target()
        work_id = str((item or {}).get("work_id") or "")
        job_id = str((item or {}).get("job_id") or "")
        seed = json.dumps(
            {"session": session, "reason": reason, "work_id": work_id, "detail": detail},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        event = {
            "specversion": "1.0",
            "id": "factory-judgment-" + hashlib.sha256(seed).hexdigest()[:24],
            "source": "bundlebees.factory-service",
            "type": "bundlebees.factory.judgment/1",
            "time": _iso(self.driver.now),
            "session": session,
            "data": {
                "coordinator": {"identity": subscriber, "session": session},
                "reason": str(reason),
                "work_id": work_id,
                "job_id": job_id,
                "detail": str(detail)[:500],
            },
        }
        wake_events = self.state / "factory" / "wake-events"
        wake_events.mkdir(parents=True, exist_ok=True)
        _write_json(wake_events / f"{self.driver.wake.key_for(session)}.json", event)
        wake = self.driver.wake.request_wake(
            session, now=self.driver.now, subscriber=subscriber
        )
        deliveries = self.driver._deliver_wakes()  # noqa: SLF001 - one durable wake path
        return {
            "requested": True,
            "reason": str(reason),
            "session": session,
            "session_key": self.driver.wake.key_for(session),
            "generation": int(wake.get("generation") or 0),
            "delivered": sum(1 for entry in deliveries if entry.get("delivered")),
            "deliveries": deliveries,
        }

    # -- dispatch ------------------------------------------------------------

    def dispatch_eligible(self) -> dict[str, Any]:
        dispatched: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        wake: dict[str, Any] | None = None
        gate = self.go_max if self.go_max is not None and self._go_max_enabled() else None
        try:
            if not work_scheduler.ready_items(self.state):
                out = {
                    "dispatched": 0,
                    "dispatched_items": dispatched,
                    "blocked": blocked,
                    "wake": None,
                    "errors": errors,
                    "cap": None,
                    "limits": {},
                }
                if gate is not None:
                    gate.begin_pass()
                    out["go_max"] = self._go_max_report(gate)
                return out
        except Exception as exc:  # noqa: BLE001 - a broken queue needs a human
            wake = self.request_judgment_wake(
                reason="scheduler_unreadable",
                detail=str(getattr(exc, "message", "") or exc),
            )
            blocked.append({"reason": "scheduler_unreadable", "detail": str(exc)})
            return {
                "dispatched": 0,
                "dispatched_items": dispatched,
                "blocked": blocked,
                "wake": wake,
                "errors": errors,
            }
        try:
            cap = int(self._cap_reader(self.state))
            if cap < 1:
                raise FactoryError("cap_unreadable", f"shared cap {cap} is below one")
        except Exception as exc:  # noqa: BLE001 - any unreadable cap fails closed
            wake = self.request_judgment_wake(
                reason="cap_unreadable",
                detail=str(getattr(exc, "message", "") or exc),
            )
            return {
                "dispatched": 0,
                "dispatched_items": dispatched,
                "blocked": [{"reason": "cap_unreadable", "detail": str(exc)}],
                "wake": wake,
                "errors": errors,
            }
        try:
            limits = self._limits_reader()
            for name in ("planning_max_active", "max_active", "reserved_for_landing"):
                int(limits[name])
        except Exception as exc:  # noqa: BLE001 - unreadable limits fail closed
            wake = self.request_judgment_wake(
                reason="limits_unreadable",
                detail=str(getattr(exc, "message", "") or exc),
            )
            return {
                "dispatched": 0,
                "dispatched_items": dispatched,
                "blocked": [{"reason": "limits_unreadable", "detail": str(exc)}],
                "wake": wake,
                "errors": errors,
            }

        go_max_held: set[str] = set()
        if gate is not None:
            admission = gate.begin_pass()
            if int(admission.get("free_slots") or 0) <= 0:
                # Fail closed without a judgment wake: the status names the reasons.
                blocked.append({"reason": "go_max_hold", "detail": ",".join(admission.get("reasons") or ())})
                return {
                    "dispatched": 0,
                    "dispatched_items": dispatched,
                    "blocked": blocked,
                    "wake": None,
                    "errors": errors,
                    "cap": cap,
                    "limits": dict(limits),
                    "go_max": self._go_max_report(gate),
                }

        # A generous bound: every successful claim removes one ready item, so
        # this only prevents a pathological loop, never caps normal work.
        for _ in range(1000):
            if len(dispatched) >= cap:
                blocked.append(
                    {"reason": "capacity_exhausted", "detail": f"shared cap of {cap} reached"}
                )
                break
            try:
                ready = work_scheduler.ready_items(self.state)
                items = work_scheduler.list_items(self.state)
            except Exception as exc:  # noqa: BLE001 - a broken queue needs a human
                wake = self.request_judgment_wake(
                    reason="scheduler_unreadable",
                    detail=str(getattr(exc, "message", "") or exc),
                )
                blocked.append({"reason": "scheduler_unreadable", "detail": str(exc)})
                break
            candidates = [
                item
                for item in ready
                if str(item.get("work_id") or "") not in self.driver._driven_this_pass
                and str(item.get("work_id") or "") not in go_max_held
            ]
            if not candidates:
                break
            item = candidates[0]
            work_id = str(item.get("work_id") or "")
            verdict = self._evaluate_item(item, cap=cap, limits=limits, items=items)
            if not verdict.get("allowed"):
                blocked.append({"work_id": work_id, **verdict})
                reason = str(verdict.get("reason") or "")
                if reason in {"capacity_exhausted", "planning_slots_exhausted"}:
                    break
                wake = self.request_judgment_wake(
                    reason=reason,
                    item=item,
                    detail=str(verdict.get("detail") or ""),
                )
                break
            try:
                job_id = self._job_id_mapping(self.state, work_id)
            except FactoryError as exc:
                blocked.append({"work_id": work_id, "reason": "job_unreadable", "detail": exc.message})
                wake = self.request_judgment_wake(
                    reason="job_unreadable", item=item, detail=exc.message
                )
                break
            go_verdict: Mapping[str, Any] | None = None
            if gate is not None:
                go_verdict = gate.check(item, job_id)
                if not go_verdict.get("allowed"):
                    reason = str(go_verdict.get("reason") or "held")
                    blocked.append({"work_id": work_id, "reason": f"go_max_{reason}"})
                    if reason == "no_free_slots":
                        break
                    go_max_held.add(work_id)
                    continue
            outcome = self.driver._drive_work(  # noqa: SLF001 - the driver owns the tick
                work_id, job_id, max_active=cap
            )
            if gate is not None and go_verdict is not None and str(outcome.get("work_state") or "") in {
                "claimed", "done", "retryable", "failed"
            }:
                gate.note(work_id, go_verdict)
            state_name = str(outcome.get("work_state") or "")
            failure = outcome.get("failure") if isinstance(outcome.get("failure"), Mapping) else {}
            if state_name == "skipped" and str(failure.get("code") or "") == "capacity_exhausted":
                blocked.append({"work_id": work_id, "reason": "capacity_exhausted"})
                break
            if state_name in {"claimed", "done"}:
                dispatched.append({"work_id": work_id, "job_id": job_id, "state": state_name})
                continue
            if state_name in {"retryable", "failed"}:
                dispatched.append(
                    {"work_id": work_id, "job_id": job_id, "state": state_name, "outcome": outcome}
                )
                wake = self.request_judgment_wake(
                    reason="dispatch_failed",
                    item=item,
                    detail=str(failure.get("message") or state_name),
                )
                break
            blocked.append(
                {
                    "work_id": work_id,
                    "reason": str(failure.get("code") or state_name or "not_dispatched"),
                    "detail": str(failure.get("message") or ""),
                }
            )
            break
        result = {
            "dispatched": len(dispatched),
            "dispatched_items": dispatched,
            "blocked": blocked,
            "wake": wake,
            "errors": errors,
            "cap": cap,
            "limits": dict(limits),
        }
        if gate is not None:
            result["go_max"] = self._go_max_report(gate)
        return result

    # -- go-max --------------------------------------------------------------

    def _go_max_enabled(self) -> bool:
        try:
            return bool(self.go_max.enabled())
        except Exception:  # noqa: BLE001 - an unreadable mode is off
            return False

    @staticmethod
    def _go_max_report(gate: Any) -> dict[str, Any]:
        record = gate.finish_pass()
        return {
            "admission": record.get("admission"),
            "lanes": record.get("lanes"),
            "empty_reasons": record.get("empty_reasons"),
            "summary": record.get("summary"),
        }

    # -- loop ----------------------------------------------------------------

    def stock_ready_column(self) -> dict[str, Any]:
        """Observe the ready column when the flag is on; never fail the pass.

        The observation can refuse (unreadable board, cap or scheduler); that
        refusal is reported for this pass and nothing is written.  The loop and
        the dispatch decision are unaffected.
        """

        try:
            record = board_stock.flag(self.state, now=self.driver.now)
        except Exception as exc:  # noqa: BLE001 - an observation never stops the factory
            return {
                "status": "error",
                "code": str(getattr(exc, "code", type(exc).__name__)),
                "error": str(getattr(exc, "message", "") or type(exc).__name__)[:300],
            }
        return {"status": "ok", **record}

    def run_pass(self) -> dict[str, Any]:
        self.passes += 1
        errors: list[dict[str, str]] = []
        try:
            completions = self.drain_completions()
        except Exception as exc:  # noqa: BLE001 - one completion store must not stop the loop
            completions = []
            errors.append({"stage": "completions", "error": type(exc).__name__})
        try:
            driven = self.driver.run_pass(advance_work=False)
        except Exception as exc:  # noqa: BLE001
            driven = {"events": [], "errors": [{"stage": "driver", "error": type(exc).__name__}]}
            errors.append({"stage": "driver", "error": type(exc).__name__})
        stock = None
        if self.go_max is not None and self._go_max_enabled():
            stock = self.go_max.stock_board()
        dispatch = self.dispatch_eligible()
        result = {
            "passes": self.passes,
            "completions": completions,
            "events": driven.get("events", []),
            "driver": driven,
            "dispatch": dispatch,
            "wake": dispatch.get("wake"),
            "errors": list(driven.get("errors") or []) + errors + list(dispatch.get("errors") or []),
        }
        # Flag-gated: with BUNDLEBEES_BOARD_STOCK unset the pass result is
        # byte-for-byte what it was before this duty existed.
        if board_stock.enabled():
            result["stock"] = self.stock_ready_column()
        if stock is not None:
            result["go_max_stock"] = stock
        self.last_result = result
        return result

    def wait_for_event(self, timeout: float | None = None) -> bool:
        return bool(self.watcher.wait(self.interval_s if timeout is None else timeout))

    def serve(
        self,
        *,
        max_passes: int | None = None,
        startup_pass: bool = False,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if startup_pass:
            self.run_pass()
        while max_passes is None or self.passes < max_passes:
            if stop_event is not None and stop_event.is_set():
                break
            self.wait_for_event(self.interval_s)
            if stop_event is not None and stop_event.is_set():
                break
            self.run_pass()
        return self.last_result or {}


def run_pass(
    state_dir: Any,
    queue: Any,
    *,
    tick: Any = None,
    launcher_outbox: Any = None,
    landing_client: Any = None,
    launcher_client: Any = None,
    landing_export_limit: int = landing_bridge.DEFAULT_EXPORT_LIMIT,
    subscribers: Sequence[str] | None = None,
    now: Any = None,
    startup: bool = True,
    max_work_attempts: int = MAX_WORK_ATTEMPTS,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    driver = FactoryDriver(
        state_dir,
        queue,
        tick=tick,
        launcher_outbox=launcher_outbox,
        landing_client=landing_client,
        launcher_client=launcher_client,
        landing_export_limit=landing_export_limit,
        subscribers=subscribers,
        now=now,
        startup=startup,
        max_work_attempts=max_work_attempts,
        campaign_id=campaign_id,
    )
    return driver.run_pass()


run_once = run_pass


def mechanical_tick(state_dir: Any) -> Any:
    """A no-inference tick that persists crash-adopted integration waves.

    The returned tick is marked ``observation_only``: it cannot dispatch, verify,
    integrate or adopt a worker result, so the driver must not let it claim a
    queue feature. A claim here would only mark a card running with nothing able
    to advance it.
    """

    state = _state_path(state_dir)

    def tick(job_id: str) -> dict[str, Any]:
        try:
            job = jobs.load_job(state, job_id, recover=False)
        except Exception as exc:  # noqa: BLE001
            return {"status": "blocked", "detail": f"job unreadable: {type(exc).__name__}"}
        jobs.recover_integration(state, job, persist=True)
        return {"status": "idle", "detail": "no worker configured; mechanical reconcile only"}

    tick.observation_only = True  # type: ignore[attr-defined]
    return tick


def make_job_tick(
    state_dir: Any,
    *,
    client: Any,
    repo: Any,
    gitleaks: str | None = None,
    suite_command: Sequence[str] | None = None,
    provisioner: Any = None,
    expect_host: str | None = None,
    runner: Any = None,
) -> Any:
    """Bind the real ``job_cli.tick`` signature to the driver's ``tick(job_id)``."""
    from pathlib import Path as _Path

    state = _Path(state_dir)
    repo_path = _Path(repo)
    suite = tuple(suite_command or (sys.executable, "-m", "pytest", "-q"))

    def tick(job_id: str) -> dict[str, Any]:
        return job_cli.tick(
            state,
            str(job_id),
            client=client,
            repo=repo_path,
            gitleaks=gitleaks,
            require_gitleaks=True,
            suite_command=suite,
            runner=runner,
            provisioner=provisioner,
            expect_host=expect_host,
        )

    return tick


def load_tick(spec: str | None) -> Any:
    if not spec:
        return None
    module_name, _, attribute = str(spec).partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attribute or "tick")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bundlebees-factory-driver",
        description="Run the durable bundlebees coordinator pass.",
    )
    parser.add_argument("--state", required=True, help="coordinator state directory")
    parser.add_argument("--queue", default=None, help="queue JSON file (default: <state>/queue.json)")
    parser.add_argument("--launcher-outbox", default=None, help="hyvv canonical landing outbox")
    parser.add_argument("--subscriber", action="append", dest="subscribers", default=[],
                        help="event subscriber to drain (repeatable)")
    parser.add_argument("--tick", default=None, help="mechanical worker tick as module:callable")
    parser.add_argument("--launcher-url", default=os.environ.get("BUNDLEBEES_LAUNCHER_URL", ""),
                        help="real launcher base URL (token from BUNDLEBEES_LAUNCHER_TOKEN)")
    parser.add_argument("--repo", default=os.environ.get("BUNDLEBEES_REPO", ""),
                        help="local repository for the real job tick (env: BUNDLEBEES_REPO)")
    parser.add_argument("--gitleaks", default=os.environ.get("BUNDLEBEES_GITLEAKS") or os.environ.get("GITLEAKS", ""),
                        help="gitleaks binary; required for a real tick (env: BUNDLEBEES_GITLEAKS)")
    parser.add_argument("--require-dispatch-config", action="store_true",
                        help="refuse to start unless launcher URL, repo and gitleaks are all configured")
    parser.add_argument("--suite-command", nargs=argparse.REMAINDER, default=None,
                        help="integration suite command, last option (default: python -m pytest -q)")
    parser.add_argument("--provisioning", action="store_true",
                        help="bind the configured hyvv provisioning client")
    parser.add_argument("--provisioning-host", default=None,
                        help="expected provisioning host policy label")
    parser.add_argument("--campaign-id", default=None,
                        help="durable campaign checkpoint to reconcile on every pass")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one pass and exit")
    mode.add_argument("--interval", nargs="?", const=1.0, type=float, default=None,
                      metavar="SECONDS", help="poll forever at this interval (default: 1.0)")
    mode.add_argument("--service", action="store_true",
                      help="run the event-driven service loop (inotify inbox wake)")
    parser.add_argument("--service-interval", type=float, default=DEFAULT_SERVICE_INTERVAL_S,
                        metavar="SECONDS",
                        help="maximum wait before an idle service pass (default: 30)")
    parser.add_argument("--sweep", action="store_true",
                        help="run the classify-only reconciliation sweep on every pass (rate-limited)")
    parser.add_argument("--sweep-interval-s", type=float, default=DEFAULT_SWEEP_INTERVAL_S,
                        metavar="SECONDS",
                        help="minimum seconds between reconciliation sweeps (default: 1800)")
    parser.add_argument("--max-passes", type=int, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state = Path(args.state)
    queue_path = Path(args.queue) if args.queue else state / "queue.json"
    tick = load_tick(args.tick)
    launcher_url = str(args.launcher_url or "").strip()
    repo_arg = str(args.repo or "").strip()
    if args.require_dispatch_config and tick is None:
        # Fail closed instead of silently degrading to the observation tick,
        # which looks healthy while it can never claim, dispatch or verify.
        missing = [
            name
            for name, value in (
                ("BUNDLEBEES_LAUNCHER_URL", launcher_url),
                ("BUNDLEBEES_REPO", repo_arg),
                ("BUNDLEBEES_GITLEAKS", str(args.gitleaks or "").strip()),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "bundlebees factory-driver: refusing to start: --require-dispatch-config is set "
                f"but {', '.join(missing)} is unset; set it in the service EnvironmentFile"
            )
    if tick is None:
        # Production selection is explicit. A partial launcher/worktree
        # configuration used to fall through to the mechanical tick, which then
        # claimed queue cards it could never dispatch or verify.
        if repo_arg and not launcher_url:
            raise SystemExit(
                "--repo requires --launcher-url or BUNDLEBEES_LAUNCHER_URL: without a launcher "
                "the driver would only run a mechanical tick and could not dispatch or verify"
            )
    client = None
    if launcher_url:
        from .client import LauncherClient

        token = os.environ.get("BUNDLEBEES_LAUNCHER_TOKEN", "")
        if not token:
            raise SystemExit("BUNDLEBEES_LAUNCHER_TOKEN is required for --launcher-url")
        client = LauncherClient(launcher_url, token=token)
    if tick is None and client is not None and repo_arg:
        from .execution import SandboxRunner, scanner
        from .settings import settings_from_hermes_config

        config = settings_from_hermes_config()
        scanner_path = scanner(config, args.gitleaks)
        if not scanner_path:
            raise SystemExit("--gitleaks or GITLEAKS is required for a real tick")
        provisioner = None
        if args.provisioning:
            from . import provisioning as provisioning_mod

            provisioner = provisioning_mod.from_client(
                {"provisioning": {"enabled": True, "host": args.provisioning_host or None}},
                client,
            )
        tick = make_job_tick(
            state,
            client=client,
            repo=Path(repo_arg),
            gitleaks=scanner_path,
            suite_command=args.suite_command,
            runner=SandboxRunner(config),
            provisioner=provisioner,
        )
    if tick is None:
        tick = mechanical_tick(state)
        print(
            "bundlebees factory-driver: no launcher URL configured; running the mechanical "
            "observation tick, which cannot claim, dispatch, verify or integrate",
            file=sys.stderr,
        )
    subscribers = args.subscribers or None
    driver_options: dict[str, Any] = {
        "tick": tick,
        # Production lands from the authenticated export. The worker-local
        # outbox directory is a test/legacy transport, never the live driver's.
        "launcher_outbox": args.launcher_outbox if client is None else None,
        "subscribers": subscribers,
    }
    if client is not None:
        driver_options["landing_client"] = client
    if args.sweep and client is not None:
        if not repo_arg:
            raise SystemExit("--sweep requires --repo or BUNDLEBEES_REPO for the origin view")
        driver_options["sweep_views"] = sweep_views_for(client, Path(repo_arg))
        driver_options["sweep_interval_s"] = float(args.sweep_interval_s)
    driver = FactoryDriver(
        state,
        queue_mod.JsonQueue(queue_path),
        campaign_id=args.campaign_id,
        **driver_options,
    )

    if args.once:
        result = driver.run_pass()
        print(json.dumps(result, sort_keys=True, default=str))
        return 0

    if args.service:
        gate = None
        if client is not None and hasattr(client, "host_capacity"):
            from . import go_max as go_max_mod

            gate = go_max_mod.FactoryGate(
                state,
                capacity_reader=client.host_capacity,
                allowed_reader=lambda: (client.health() or {}).get("models") or [],
            )
        service = FactoryService(
            driver,
            subscribers=subscribers,
            interval_s=max(0.01, float(args.service_interval)),
            go_max_gate=gate,
        )
        try:
            result = service.serve(max_passes=args.max_passes)
            print(json.dumps(result, sort_keys=True, default=str))
        except KeyboardInterrupt:
            return 130
        finally:
            service.close()
        return 0

    interval = max(0.0, float(args.interval if args.interval is not None else 1.0))
    passes = 0
    try:
        while True:
            result = driver.run_pass()
            passes += 1
            print(json.dumps(result, sort_keys=True, default=str), flush=True)
            if args.max_passes is not None and passes >= args.max_passes:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
