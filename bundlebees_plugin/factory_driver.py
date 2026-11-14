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
    "deterministic_handler",
    "load_tick",
    "main",
    "run_once",
    "run_pass",
    "startup_reconcile",
    "work_id_for_event",
]

WAKE_SCHEMA = "bundlebees.factory-wake/1"
WAKE_DIR = "wakes"
DEFAULT_SESSION = "coordinator"
DEFAULT_WAKE_TTL_S = 900.0
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


def _safe_session(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        text = DEFAULT_SESSION
    if len(text) > 160:
        text = text[:160]
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if all(ch in allowed for ch in text) and text not in {".", ".."}:
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

    ``request_wake`` increments the generation even while a turn is running.
    ``finish_turn`` advances the cursor only to the generation the turn started
    from, so later arrivals remain pending for exactly one next turn.
    """

    def __init__(self, state_dir: Any, *, ttl_s: float = DEFAULT_WAKE_TTL_S) -> None:
        self.root = _factory_dir(state_dir) / WAKE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_s = float(ttl_s)

    def _path(self, session: Any) -> Path:
        return self.root / f"{_safe_session(session)}.json"

    def _lock_path(self, session: Any) -> Path:
        return self.root / f"{_safe_session(session)}.lock"

    def _load(self, session: str) -> dict[str, Any]:
        record = _read_json(self._path(session)) or {}
        return {
            "schema": WAKE_SCHEMA,
            "session": session,
            "generation": int(record.get("generation") or 0),
            "cursor": int(record.get("cursor") or 0),
            "active": record.get("active") if isinstance(record.get("active"), Mapping) else None,
            "coalesced": int(record.get("coalesced") or 0),
            "updated_at": str(record.get("updated_at") or ""),
        }

    def _save(self, session: str, record: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(record)
        body["schema"] = WAKE_SCHEMA
        body["session"] = session
        body["pending"] = int(body.get("generation") or 0) > int(body.get("cursor") or 0)
        body["active_turn"] = bool(body.get("active"))
        _write_json(self._path(session), body)
        return dict(body)

    def _expired(self, active: Mapping[str, Any], clock: float) -> bool:
        if not _pid_alive(active.get("pid")):
            return True
        started = active.get("started_epoch")
        if not isinstance(started, (int, float)):
            return True
        return clock - float(started) >= self.ttl_s

    def request_wake(self, session: Any, *, now: Any = None) -> dict[str, Any]:
        name = _safe_session(session)
        stamp = _iso(now)
        clock = _clock(now)
        with _locked(self._lock_path(name)):
            record = self._load(name)
            record["generation"] = int(record["generation"]) + 1
            record["coalesced"] = int(record["coalesced"]) + 1
            record["updated_at"] = stamp
            record["last_wake_epoch"] = clock
            return self._save(name, record)

    def begin_turn(self, session: Any, *, now: Any = None) -> dict[str, Any]:
        name = _safe_session(session)
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
        name = _safe_session(session)
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
            }

    def status(self, session: Any) -> dict[str, Any]:
        name = _safe_session(session)
        record = self._load(name)
        record["pending"] = int(record["generation"]) > int(record["cursor"])
        record["active_turn"] = bool(record.get("active"))
        record["due"] = bool(record["pending"] and not record["active_turn"])
        return record

    def due(self, session: Any) -> bool:
        return bool(self.status(session)["due"])

    def sessions(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.json") if path.is_file())


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
    for key in ("session", "session_id", "sessionId"):
        value = event.get(key)
        if value not in (None, ""):
            return _safe_session(value)
    coordinator = _event_data(event).get("coordinator")
    if isinstance(coordinator, Mapping):
        for key in ("session", "session_id"):
            value = coordinator.get(key)
            if value not in (None, ""):
                return _safe_session(value)
    return _safe_session(default)


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


def deterministic_handler(
    state_dir: Any,
    event: Mapping[str, Any],
    *,
    now: Any = None,
) -> dict[str, Any]:
    """Record the verification work an event makes eligible. No model call."""

    state = _state_path(state_dir)
    work_id = work_id_for_event(event)
    lease_files = _event_lease_files(event)
    title_attempt = str(_event_data(event).get("attempt_id") or _event_id(event) or work_id)
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
    ready = [entry["work_id"] for entry in work_scheduler.ready_items(state)]
    return {
        "accepted": True,
        "event_id": _event_id(event),
        "work_id": work_id,
        "created": item.get("status") == "pending",
        "eligible": ready,
    }


def record_eligible_work(state_dir: Any, event: Mapping[str, Any], *, now: Any = None) -> dict[str, Any]:
    return deterministic_handler(state_dir, event, now=now)


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
    ) -> None:
        self.state = _state_path(state_dir)
        self.state.mkdir(parents=True, exist_ok=True)
        self.queue = queue if hasattr(queue, "list_items") else queue_mod.JsonQueue(Path(queue))
        self.tick = tick if tick is not None else mechanical_tick(self.state)
        self.launcher_outbox = Path(launcher_outbox) if launcher_outbox is not None else None
        self.subscribers = list(subscribers) if subscribers is not None else None
        self.now = now
        self.wake = WakeCoalescer(self.state, ttl_s=wake_ttl_s)
        self._startup_done = not startup
        self.startup_report: dict[str, Any] | None = None

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
        return self.startup_report

    def _event_handler(self, subscriber: str) -> Any:
        def handler(event: Mapping[str, Any]) -> dict[str, Any]:
            session = _session_of(event, subscriber)
            self.wake.request_wake(session, now=self.now)
            turn = self.wake.begin_turn(session, now=self.now)
            if not turn.get("started"):
                raise FactoryError(
                    "turn_active",
                    f"session {session} already has an active turn",
                    "Leave the event in the inbox; the next pass handles it.",
                )
            result: dict[str, Any] | None = None
            try:
                result = deterministic_handler(self.state, event, now=self.now)
                result["session"] = session
                result["generation"] = int(turn.get("generation") or 0)
                return result
            finally:
                finished = self.wake.finish_turn(session, turn.get("turn"), now=self.now)
                if result is not None and finished.get("pending"):
                    result_pending = self.wake.status(session)
                    result_pending.pop("session", None)
                    result["wake"] = result_pending
        return handler

    def run_pass(self) -> dict[str, Any]:
        startup = self._ensure_startup()
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
            }
            result = outcome.get("result") if isinstance(outcome.get("result"), Mapping) else {}
            for key in ("work_id", "session", "generation", "eligible"):
                if key in result:
                    entry[key] = result[key]
            event_results.append(entry)

        try:
            ready = work_scheduler.ready_items(self.state)
        except Exception as exc:  # noqa: BLE001
            ready = []
            errors.append({"stage": "ready", "error": type(exc).__name__})

        event_activity = any(entry.get("state") in ("acked", "retryable", "failed") for entry in event_results)
        landing_activity = bool(landing_result and (landing_result.get("processed") or landing_result.get("failed")))
        queue_activity = queue_result.get("action") not in (None, "idle")
        idle = not queue_activity and not landing_activity and not event_activity and not ready

        result = {
            "action": queue_result.get("action"),
            "queue": queue_result,
            "landing": landing_result,
            "events": event_results,
            "ready": [entry["work_id"] for entry in ready],
            "ready_items": ready,
            "wake": [self.wake.status(name) for name in self.subscribers_for_pass()],
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
) -> dict[str, Any]:
    driver = FactoryDriver(
        state_dir,
        queue,
        tick=tick,
        launcher_outbox=launcher_outbox,
        subscribers=subscribers,
        now=now,
        startup=startup,
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
    tick = load_tick(args.tick) or mechanical_tick(state)
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
