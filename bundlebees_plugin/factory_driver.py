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
import hashlib
import importlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

try:  # POSIX advisory locks; Linux is the target platform
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock has no claim protocol
    fcntl = None  # type: ignore[assignment]

from . import bee_events
from . import job_cli
from . import hermes_adapter
from . import event_consumer
from . import job as jobs
from . import landing_bridge
from . import leases
from . import queue as queue_mod
from . import work_scheduler

__all__ = [
    "FactoryDriver",
    "FactoryError",
    "WakeCoalescer",
    "action_id_for_event",
    "deterministic_handler",
    "load_tick",
    "main",
    "run_once",
    "run_pass",
    "startup_reconcile",
    "work_id_for_event",
]

WAKE_SCHEMA = "bundlebees.factory-wake/1"
ACTION_SCHEMA = "bundlebees.factory-action/1"
WAKE_DIR = "wakes"
ACTION_DIR = "actions"
WORK_RETRY_DIR = "work-retries"
JOB_LOCK_DIR = "job-locks"
DEFAULT_SESSION = "coordinator"
DEFAULT_WAKE_TTL_S = 900.0
MAX_WORK_ATTEMPTS = 2
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


def _pid_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
            return True
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
            if int(record["generation"]) <= int(record["cursor"]):
                return {
                    "started": False,
                    "reason": "no_wake",
                    "session": name,
                    "generation": int(record["generation"]),
                    "cursor": int(record["cursor"]),
                }
            turn = {
                "turn": "turn-" + uuid.uuid4().hex,
                "generation": int(record["generation"]),
                "pid": os.getpid(),
                "started_at": stamp,
                "started_epoch": clock,
            }
            record["active"] = turn
            record["updated_at"] = stamp
            self._save(name, record)
            return {
                "started": True,
                "session": name,
                "generation": int(record["generation"]),
                "cursor": int(record["cursor"]),
                "turn": turn["turn"],
                "active": turn,
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

    def release_turn(self, session: Any, turn: Any) -> dict[str, Any]:
        """Clear an active turn without advancing the cursor (delivery failed)."""
        name = _raw_session(session)
        with _locked(self._lock_path(name)):
            record = self._load(name)
            active = record.get("active") if isinstance(record.get("active"), Mapping) else None
            if active is not None and str(active.get("turn")) == str(turn):
                record["active"] = None
                self._save(name, record)
            record["pending"] = int(record["generation"]) > int(record["cursor"])
            return record

    def status(self, session: Any) -> dict[str, Any]:
        name = _raw_session(session)
        record = self._load(name)
        record["pending"] = int(record["generation"]) > int(record["cursor"])
        record["active_turn"] = bool(record.get("active"))
        record["due"] = bool(record["pending"] and not record["active_turn"])
        return record

    def due(self, session: Any) -> bool:
        return bool(self.status(session)["due"])

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
    tick: Any = None,
    launcher_outbox: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Repair crash residue through the durable modules, before the first pass."""

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
            if jobs.recover_integration(state, job):
                jobs.save_job(state, job)
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

    if abandoned:
        try:
            report["repair"] = queue_mod.run_once(state, queue, tick=tick or mechanical_tick(state))
        except Exception as exc:  # noqa: BLE001 - the regular pass still runs
            report["errors"].append({"repair": type(exc).__name__})

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


class FactoryDriver:
    """One coordinator loop over durable queue, landing and scheduler state."""

    def __init__(
        self,
        state_dir: Any,
        queue: Any,
        *,
        tick: Any = None,
        launcher_outbox: Any = None,
        subscribers: Sequence[str] | None = None,
        now: Any = None,
        wake_ttl_s: float = DEFAULT_WAKE_TTL_S,
        startup: bool = True,
        max_work_attempts: int = MAX_WORK_ATTEMPTS,
    ) -> None:
        self.state = _state_path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.queue = queue if hasattr(queue, "list_items") else queue_mod.JsonQueue(Path(queue))
        self._tick = tick if tick is not None else mechanical_tick(self.state)
        self.tick = self._serialized_tick(self._tick)
        self.launcher_outbox = Path(launcher_outbox) if launcher_outbox is not None else None
        self.subscribers = list(subscribers) if subscribers is not None else None
        self.now = now
        self.wake = WakeCoalescer(self.state, ttl_s=wake_ttl_s)
        self.max_work_attempts = max(1, int(max_work_attempts))
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
            tick=self.tick,
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

    def _drive_work(self, work_id: str, job_id: str) -> dict[str, Any]:
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
            work_scheduler.claim(self.state, work_id, owner, str(self.state), now=self.now)
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
            job = jobs.load_job(self.state, job_id, recover=False)
            tasks = job.get("tasks") or []
            done = bool(tasks) and all(str(task.get("state")) == "accepted" for task in tasks)
        except Exception:  # noqa: BLE001
            done = False
        completed = done or str(tick_result.get("action") or "") in {
            "ready_for_review",
            "accepted",
        }
        if completed:
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

    def _deliver_wakes(self) -> list[dict[str, Any]]:
        """Deliver one coalesced wake per due session through the real adapter."""

        results: list[dict[str, Any]] = []
        sessions = self.wake.sessions() or self.subscribers_for_pass()
        fallback = (self.subscribers_for_pass() or [DEFAULT_SESSION])[0]
        for session in sessions:
            status = self.wake.status(session)
            if not status.get("due"):
                continue
            turn = self.wake.begin_turn(session, now=self.now)
            if not turn.get("started"):
                continue
            try:
                event_path = (
                    self.state
                    / "factory"
                    / "wake-events"
                    / f"{self.wake.key_for(session)}.json"
                )
                event = json.loads(event_path.read_text(encoding="utf-8"))
                subscriber = self._subscriber_binding(event, status, fallback)
                delivered = hermes_adapter.notify_coordinator(
                    event,
                    endpoint=os.environ.get("HERMES_API_URL") or None,
                    api_key=os.environ.get("HERMES_API_KEY") or None,
                    session=session,
                    subscriber=subscriber,
                    state_dir=self.state,
                    timeout_s=float(os.environ.get("HERMES_WAKE_TIMEOUT_S") or 60.0),
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
                        "turn": turn.get("turn"),
                        "turn_finished": bool(finished.get("finished")),
                        "pending": bool(finished.get("pending")),
                        "subscriber": (subscriber or {}).get("identity", ""),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - durable wake remains pending
                self.wake.release_turn(session, turn.get("turn"))
                results.append(
                    {
                        "session": session,
                        "session_key": self.wake.key_for(session),
                        "delivered": False,
                        "turn": turn.get("turn"),
                        "turn_finished": False,
                        "error": type(exc).__name__,
                        "code": str(getattr(exc, "code", "") or ""),
                        "message": str(getattr(exc, "message", "") or str(exc))[:500],
                        "fix": str(
                            getattr(exc, "fix", "")
                            or "Keep the wake pending and retry after the gateway recovers."
                        ),
                        "pending": True,
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

    def run_pass(self) -> dict[str, Any]:
        self._driven_this_pass = set()
        startup = self._ensure_startup()
        self._recover_work_claims()
        errors: list[dict[str, str]] = []

        queue_result: dict[str, Any]
        try:
            queue_result = queue_mod.run_once(self.state, self.queue, tick=self.tick)
        except Exception as exc:  # noqa: BLE001 - landing and events still run
            queue_result = {"action": "error", "detail": f"{type(exc).__name__}: {exc}"}
            errors.append({"stage": "queue", "error": type(exc).__name__})

        landing_result: dict[str, Any] | None = None
        if self.launcher_outbox is not None:
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
        failures.extend(self._advance_event_work(event_results))
        failures.extend(self._advance_ready_work())
        wake_results = self._deliver_wakes()

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
            "failures": failures,
            "idle": idle,
            "inference": 0,
            "model_calls": 0,
            "errors": errors,
        }
        if startup is not None:
            result["startup"] = startup
        return result

    def run_once(self) -> dict[str, Any]:
        return self.run_pass()


def run_pass(
    state_dir: Any,
    queue: Any,
    *,
    tick: Any = None,
    launcher_outbox: Any = None,
    subscribers: Sequence[str] | None = None,
    now: Any = None,
    startup: bool = True,
    max_work_attempts: int = MAX_WORK_ATTEMPTS,
) -> dict[str, Any]:
    driver = FactoryDriver(
        state_dir,
        queue,
        tick=tick,
        launcher_outbox=launcher_outbox,
        subscribers=subscribers,
        now=now,
        startup=startup,
        max_work_attempts=max_work_attempts,
    )
    return driver.run_pass()


run_once = run_pass


def mechanical_tick(state_dir: Any) -> Any:
    """A no-inference tick that persists crash-adopted integration waves."""

    state = _state_path(state_dir)

    def tick(job_id: str) -> dict[str, Any]:
        try:
            job = jobs.load_job(state, job_id, recover=False)
        except Exception as exc:  # noqa: BLE001
            return {"status": "blocked", "detail": f"job unreadable: {type(exc).__name__}"}
        if jobs.recover_integration(state, job):
            jobs.save_job(state, job)
        return {"status": "idle", "detail": "no worker configured; mechanical reconcile only"}

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
) -> Any:
    """Bind the real ``job_cli.tick`` signature to the driver's ``tick(job_id)``."""
    from pathlib import Path as _Path

    state = _Path(state_dir)
    repo_path = _Path(repo)
    suite = tuple(suite_command or ("python", "-m", "pytest", "-q"))

    def tick(job_id: str) -> dict[str, Any]:
        return job_cli.tick(
            state,
            str(job_id),
            client=client,
            repo=repo_path,
            gitleaks=gitleaks,
            require_gitleaks=gitleaks is not None,
            suite_command=suite,
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
    parser.add_argument("--repo", default=None, help="local repository for the real job tick")
    parser.add_argument("--gitleaks", default=os.environ.get("GITLEAKS", ""),
                        help="gitleaks binary; required for a real tick")
    parser.add_argument("--suite-command", nargs=argparse.REMAINDER, default=None,
                        help="integration suite command, last option (default: python -m pytest -q)")
    parser.add_argument("--provisioning", action="store_true",
                        help="bind the configured hyvv provisioning client")
    parser.add_argument("--provisioning-host", default=None,
                        help="expected provisioning host policy label")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one pass and exit")
    mode.add_argument("--interval", nargs="?", const=1.0, type=float, default=None,
                      metavar="SECONDS", help="poll forever at this interval (default: 1.0)")
    parser.add_argument("--max-passes", type=int, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state = Path(args.state)
    queue_path = Path(args.queue) if args.queue else state / "queue.json"
    tick = load_tick(args.tick)
    if tick is None and args.launcher_url and args.repo:
        from .client import LauncherClient

        token = os.environ.get("BUNDLEBEES_LAUNCHER_TOKEN", "")
        if not token:
            raise SystemExit("BUNDLEBEES_LAUNCHER_TOKEN is required for --launcher-url")
        if not args.gitleaks:
            raise SystemExit("--gitleaks or GITLEAKS is required for a real tick")
        client = LauncherClient(args.launcher_url, token=token)
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
            repo=Path(args.repo),
            gitleaks=args.gitleaks,
            suite_command=args.suite_command,
            provisioner=provisioner,
        )
    if tick is None:
        tick = mechanical_tick(state)
    subscribers = args.subscribers or None
    driver = FactoryDriver(
        state,
        queue_mod.JsonQueue(queue_path),
        tick=tick,
        launcher_outbox=args.launcher_outbox,
        subscribers=subscribers,
    )

    if args.once:
        result = driver.run_pass()
        print(json.dumps(result, sort_keys=True, default=str))
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
