"""Coordinator-side landing event consumer.

Bees and adapters *land* events; the coordinator consumes them out of band, not
inside the mechanical queue tick.  This module owns only the durable inbox and
its acknowledgements::

    events/inbox/<subscriber>/<event_id>.json
    events/acks/<subscriber>/<event_id>.json

A claim is a durable read-modify-write under ``flock``.  The handler is invoked
after the claim is released, so a slow or blocked handler can never hold the
queue lock and the mechanical tick never waits for it.  The acknowledgement is
written only after the handler returns a mapping.  A handler failure releases
the event for one bounded retry; when the attempt budget is exhausted the event
is marked failed and is never delivered again without an explicit replay.

Deduplication is by event id: once an acknowledgement exists for
``(subscriber, event_id)`` a duplicate delivery returns ``duplicate: True`` and
the handler is not called.  The crash window is deliberate: if the process dies
after the handler ran but before the acknowledgement was durable, the claim is
adopted on the next pass and the handler may run again.  This is at-least-once
handling with one accepted decision, never an exactly-once promise that cannot
be kept without a distributed transaction.

Only ``events/inbox`` and ``events/acks`` are ever written.  No approval, grant,
job or recovery record is created here.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock cannot prove one claim
    fcntl = None  # type: ignore[assignment]

from . import coordinator_actions

SCHEMA = "rizzstack.landing-event/1"
DELIVERY_SCHEMA = "rizzstack.event-delivery/1"
ACK_SCHEMA = "rizzstack.event-ack/1"
SUMMARY_SCHEMA = "rizzstack.event-inbox-summary/1"

EVENTS_DIR = "events"
INBOX_DIR = "inbox"
ACKS_DIR = "acks"

#: One event may be handed to a handler at most this many claims.  A claim is
#: counted before the handler runs, so a crash after the handler still spends
#: one attempt and the next claim can be refused instead of retrying forever.
MAX_ATTEMPTS = 2

DEFAULT_CLAIM_TTL_S = 900

STATES: tuple[str, ...] = ("pending", "claimed", "retryable", "acked", "failed")

_ALLOWED_ID = frozenset(
    ":abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)

_FIX = (
    "Inspect events/inbox and events/acks; an inbox record is never overwritten "
    "to hide a second handling decision."
)


class EventConsumerError(Exception):
    """An inbox event cannot be claimed or acknowledged."""

    def __init__(self, code: str, message: str, fix: str = _FIX) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Identifiers, paths and time
# ---------------------------------------------------------------------------


def _safe_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 160
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise EventConsumerError(
            "identifier_invalid",
            f"{what} {text!r} is not a safe path component",
            "Use letters, digits, '-', '_', ':' and '.' only, at most 160 characters.",
        )
    return text


def _events_root(state_dir: Path) -> Path:
    return Path(state_dir) / EVENTS_DIR


def inbox_dir(state_dir: Path, subscriber: str) -> Path:
    return _events_root(Path(state_dir)) / INBOX_DIR / _safe_id(subscriber, "subscriber")


def acks_dir(state_dir: Path, subscriber: str) -> Path:
    return _events_root(Path(state_dir)) / ACKS_DIR / _safe_id(subscriber, "subscriber")


def inbox_path(state_dir: Path, subscriber: str, event_id: str) -> Path:
    return inbox_dir(state_dir, subscriber) / f"{_safe_id(event_id, 'event_id')}.json"


def ack_path(state_dir: Path, subscriber: str, event_id: str) -> Path:
    return acks_dir(state_dir, subscriber) / f"{_safe_id(event_id, 'event_id')}.json"


def _lock_path(state_dir: Path, subscriber: str, event_id: str) -> Path:
    safe_subscriber = _safe_id(subscriber, "subscriber")
    safe_event = _safe_id(event_id, "event_id")
    return (
        _events_root(Path(state_dir))
        / INBOX_DIR
        / ".locks"
        / f"{safe_subscriber}.{safe_event}.lock"
    )


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _clock(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    epoch = _epoch(value)
    if epoch is None:
        raise EventConsumerError(
            "clock_invalid",
            "now must be epoch seconds, an ISO timestamp or a callable",
            "Pass a clock the consumer can read.",
        )
    return epoch


# ---------------------------------------------------------------------------
# Durable JSON
# ---------------------------------------------------------------------------


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EventConsumerError(
            "event_unreadable", f"{Path(path).name} cannot be read: {exc}"
        ) from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise EventConsumerError(
            "event_corrupt",
            f"{Path(path).name} is not valid JSON: {exc}",
            "Inspect it by hand; a corrupt inbox event is not permission to handle it.",
        ) from exc
    if not isinstance(value, dict):
        raise EventConsumerError(
            "event_corrupt", f"{Path(path).name} is not a JSON object"
        )
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace one inbox record; the caller holds its lock."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = _payload(value)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
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


def _create_only_json(path: Path, value: Mapping[str, Any]) -> bool:
    """Publish an acknowledgement exactly once, atomically, mode 0600."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = _payload(value)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
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
        _fsync_dir(path.parent)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    if fcntl is None:  # pragma: no cover - the claim protocol needs POSIX flock
        raise EventConsumerError(
            "locking_unavailable",
            "this platform has no flock, so a single claim cannot be proven",
            "Run the coordinator on POSIX (Linux/macOS); an unlocked claim is not a claim.",
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
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


def _holder_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _claim_expired(claim: Mapping[str, Any], clock: float) -> bool:
    expires = _epoch(claim.get("expires_at"))
    if expires is not None and clock >= expires:
        return True
    return not _holder_alive(claim.get("pid"))


# ---------------------------------------------------------------------------
# Event records
# ---------------------------------------------------------------------------


def _event_id(event: Mapping[str, Any], path: Path) -> str:
    raw = event.get("event_id")
    if raw in (None, ""):
        raw = event.get("id")
    if raw in (None, ""):
        raw = Path(path).stem
    return _safe_id(raw, "event_id")


def _declared_targets(event: Mapping[str, Any]) -> list[str]:
    for key in ("subscriber", "subscribers", "recipient", "audience"):
        if key not in event:
            continue
        value = event.get(key)
        if isinstance(value, (list, tuple, set)):
            found = [str(item).strip() for item in value if str(item).strip()]
        elif value is not None:
            found = [str(value).strip()] if str(value).strip() else []
        else:
            found = []
        if found:
            return found
    return []


def _is_for(event: Mapping[str, Any], subscriber: str) -> bool:
    targets = _declared_targets(event)
    return not targets or subscriber in targets or "*" in targets


def _event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """The event as the producer wrote it, without consumer bookkeeping."""

    wrapped = event.get("event")
    if isinstance(wrapped, Mapping):
        return dict(wrapped)
    return {key: value for key, value in event.items() if key != "_delivery"}


def _delivery_of(event: Mapping[str, Any]) -> dict[str, Any]:
    value = event.get("_delivery")
    return dict(value) if isinstance(value, Mapping) else {}


def _event_fingerprint(event: Mapping[str, Any]) -> str:
    payload = _payload(_event_payload(event))
    return hashlib.sha256(payload).hexdigest()


def _iter_events(state_dir: Path, subscriber: str | None) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every inbox record, optionally restricted to one subscriber.

    The per-subscriber directory is the canonical layout.  A flat
    ``events/inbox/<event_id>.json`` record is also read when it names the
    subscriber, which keeps older landing producers working without inventing
    a second acknowledgement namespace.
    """

    root = _events_root(Path(state_dir)) / INBOX_DIR
    paths: dict[Path, None] = {}
    if subscriber is not None:
        per = root / _safe_id(subscriber, "subscriber")
        if per.is_dir():
            for path in sorted(per.glob("*.json")):
                paths[path] = None
    else:
        if root.is_dir():
            for per in sorted(root.iterdir()):
                if not per.is_dir() or per.name.startswith("."):
                    continue
                for path in sorted(per.glob("*.json")):
                    paths[path] = None
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            paths[path] = None

    for path in sorted(paths, key=str):
        event = _read_json(path)
        if event is None:
            continue
        if subscriber is not None and not _is_for(event, subscriber):
            continue
        yield path, event


def _ack_destination(state_dir: Path, subscriber: str, event_id: str, event_path: Path) -> Path:
    """The ack lives beside the inbox layout the event actually used."""

    # A flat legacy inbox gets a flat legacy ack, so one event still produces
    # exactly one acknowledgement file instead of two parallel names.
    if Path(event_path).parent == _events_root(Path(state_dir)) / INBOX_DIR:
        return _events_root(Path(state_dir)) / ACKS_DIR / f"{_safe_id(event_id, 'event_id')}.json"
    return ack_path(state_dir, subscriber, event_id)


def _find_ack(
    state_dir: Path, subscriber: str, event_id: str
) -> tuple[dict[str, Any], Path] | tuple[None, None]:
    """The acknowledgement for exactly one (subscriber, event id), if any."""

    candidates = [ack_path(state_dir, subscriber, event_id)]
    # A flat legacy ack is accepted only when it names this subscriber.
    legacy = _events_root(Path(state_dir)) / ACKS_DIR / f"{_safe_id(event_id, 'event_id')}.json"
    if legacy not in candidates:
        candidates.append(legacy)
    for candidate in candidates:
        record = _read_json(candidate)
        if record is None:
            continue
        named = record.get("subscriber")
        if candidate == legacy and named not in (None, "", subscriber):
            continue
        return record, candidate
    return None, None


def _find_event_path(state_dir: Path, subscriber: str, event_id: str) -> Path | None:
    for path, event in _iter_events(Path(state_dir), subscriber):
        if _event_id(event, path) == event_id:
            return path
    return None


# ---------------------------------------------------------------------------
# Publish (small convenience for producers and tests)
# ---------------------------------------------------------------------------


def publish_event(
    state_dir: Path,
    event: Mapping[str, Any],
    *,
    subscriber: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Land one event in a subscriber's inbox, create-only.

    The canonical API is the inbox directory itself; this helper simply writes
    the record with the same validation the consumer uses.  Replaying the same
    event id returns the stored record instead of overwriting a handling
    decision.
    """

    if not isinstance(event, Mapping):
        raise EventConsumerError("event_invalid", "an event must be a mapping")
    body = dict(event)
    targets = _declared_targets(body)
    chosen = str(subscriber or (targets[0] if targets else "")).strip()
    if not chosen:
        raise EventConsumerError(
            "subscriber_missing",
            "the event names no subscriber",
            "Pass subscriber=... or include a subscriber field.",
        )
    chosen = _safe_id(chosen, "subscriber")
    if body.get("event_id") in (None, "") and body.get("id") in (None, ""):
        body["event_id"] = "evt_" + uuid.uuid4().hex
    event_id = _event_id(body, Path(str(body.get("event_id") or body.get("id"))))
    if body.get("event_id") in (None, ""):
        body["event_id"] = event_id
    body.setdefault("schema", SCHEMA)
    body.setdefault("created_at", _iso(_clock(now)))
    path = inbox_path(state_dir, chosen, event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _create_only_json(path, body):
        return dict(body)
    existing = _read_json(path) or {}
    return dict(existing)


enqueue_event = publish_event
enqueue = publish_event


# ---------------------------------------------------------------------------
# Claim, acknowledge, fail
# ---------------------------------------------------------------------------


def _claim(
    state_dir: Path,
    subscriber: str,
    path: Path,
    event: Mapping[str, Any],
    *,
    consumer: str,
    clock: float,
    ttl_s: float,
    max_attempts: int,
) -> dict[str, Any]:
    """Claim one event atomically; the caller runs the handler outside the lock."""

    event_id = _event_id(event, path)
    lock = _lock_path(state_dir, subscriber, event_id)
    with _locked(lock):
        fresh = _read_json(path)
        if fresh is None:
            return {"status": "vanished", "event_id": event_id}
        ack, _ack_location = _find_ack(state_dir, subscriber, event_id)
        delivery = _delivery_of(fresh)
        state_name = str(delivery.get("state") or "")
        if ack is not None or state_name == "acked":
            return {
                "status": "duplicate",
                "event_id": event_id,
                "event": fresh,
                "delivery": delivery,
                "ack": ack,
            }
        if state_name == "failed":
            return {
                "status": "failed_terminal",
                "event_id": event_id,
                "event": fresh,
                "delivery": delivery,
            }
        attempts = int(delivery.get("attempts") or 0)
        if state_name == "claimed":
            claim = delivery.get("claim") if isinstance(delivery.get("claim"), Mapping) else {}
            if not _claim_expired(claim, clock):
                return {"status": "busy", "event_id": event_id}
        not_before = _epoch(delivery.get("next_attempt_at"))
        if (
            state_name in ("pending", "retryable")
            and not_before is not None
            and clock < not_before
        ):
            return {
                "status": "backoff",
                "event_id": event_id,
                "event": fresh,
                "delivery": delivery,
            }
        if attempts >= max_attempts:
            stamp = _iso(clock)
            delivery.update(
                schema=DELIVERY_SCHEMA,
                subscriber=subscriber,
                event_id=event_id,
                state="failed",
                claim=None,
                updated_at=stamp,
                failed_at=stamp,
                last_error=str(delivery.get("last_error") or "attempts_exhausted"),
            )
            fresh["_delivery"] = delivery
            _write_json(path, fresh)
            return {
                "status": "exhausted",
                "event_id": event_id,
                "event": fresh,
                "delivery": delivery,
            }

        attempts += 1
        stamp = _iso(clock)
        delivery = {
            **delivery,
            "schema": DELIVERY_SCHEMA,
            "subscriber": subscriber,
            "event_id": event_id,
            "state": "claimed",
            "attempts": attempts,
            "claim": {
                "consumer": consumer,
                "pid": os.getpid(),
                "claimed_at": stamp,
                "expires_at": _iso(clock + float(ttl_s)),
            },
            "last_error": "",
            "updated_at": stamp,
        }
        fresh["_delivery"] = delivery
        _write_json(path, fresh)
        return {
            "status": "claimed",
            "event_id": event_id,
            "event": fresh,
            "delivery": delivery,
            "path": path,
        }


def _ack(
    state_dir: Path,
    subscriber: str,
    path: Path,
    event: Mapping[str, Any],
    delivery: Mapping[str, Any],
    *,
    consumer: str,
    clock: float,
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Record the one accepted decision, after the handler succeeded."""

    event_id = _event_id(event, path)
    lock = _lock_path(state_dir, subscriber, event_id)
    stamp = _iso(clock)
    record = {
        "schema": ACK_SCHEMA,
        "event_id": event_id,
        "subscriber": subscriber,
        "consumer": consumer,
        "state": "acked",
        "attempts": int(delivery.get("attempts") or 0),
        "acked_at": stamp,
        "handled_at": stamp,
        "event_sha256": _event_fingerprint(event),
        "result": dict(result),
        "decision": dict(result),
        "duplicate_replays": int(delivery.get("duplicate_replays") or 0),
    }
    destination = _ack_destination(state_dir, subscriber, event_id, path)
    with _locked(lock):
        existing, _location = _find_ack(state_dir, subscriber, event_id)
        if existing is not None:
            return existing, False
        created = _create_only_json(destination, record)
        fresh = _read_json(path)
        if fresh is not None:
            current = _delivery_of(fresh)
            current.update(
                schema=DELIVERY_SCHEMA,
                subscriber=subscriber,
                event_id=event_id,
                state="acked",
                claim=None,
                attempts=int(delivery.get("attempts") or current.get("attempts") or 0),
                updated_at=stamp,
                acked_at=stamp,
                last_error="",
            )
            fresh["_delivery"] = current
            _write_json(path, fresh)
        if not created:
            existing, _location = _find_ack(state_dir, subscriber, event_id)
            return (existing or record), False
    return record, True


def _fail(
    state_dir: Path,
    subscriber: str,
    path: Path,
    event: Mapping[str, Any],
    delivery: Mapping[str, Any],
    *,
    consumer: str,
    clock: float,
    error: str,
    ttl_s: float,
    max_attempts: int,
    error_code: str = "",
    error_fix: str = "",
    backoff_s: float = 0.0,
) -> dict[str, Any]:
    """Release for a bounded retry, or mark failed when the budget is spent.

    A backoff is durable (``next_attempt_at``) and bounded by the same attempt
    budget as the retry itself.  The default is zero so a caller opts in.
    """

    del consumer, ttl_s  # a release is not a claim; it never extends a lease
    event_id = _event_id(event, path)
    lock = _lock_path(state_dir, subscriber, event_id)
    stamp = _iso(clock)
    with _locked(lock):
        fresh = _read_json(path)
        current = _delivery_of(fresh if fresh is not None else event)
        attempts = int(current.get("attempts") or delivery.get("attempts") or 0)
        exhausted = attempts >= max_attempts
        delay = max(0.0, float(backoff_s))
        next_attempt = "" if exhausted or delay <= 0 else _iso(clock + delay)
        current.update(
            schema=DELIVERY_SCHEMA,
            subscriber=subscriber,
            event_id=event_id,
            state="failed" if exhausted else "retryable",
            claim=None,
            attempts=attempts,
            updated_at=stamp,
            last_error=str(error)[:500],
            last_error_code=str(error_code or error)[:120],
            last_error_fix=str(error_fix)[:500],
            next_attempt_at=next_attempt,
        )
        if exhausted:
            current["failed_at"] = stamp
        if fresh is not None:
            fresh["_delivery"] = current
            _write_json(path, fresh)
    return {
        "event_id": event_id,
        "state": "failed" if exhausted else "retryable",
        "attempts": attempts,
        "exhausted": exhausted,
        "error": str(error),
        "error_code": str(error_code or error),
        "fix": str(error_fix),
        "retry_after": next_attempt,
    }


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    return type(exc).__name__


def _error_fix(exc: BaseException) -> str:
    fix = getattr(exc, "fix", None)
    if isinstance(fix, str) and fix.strip():
        return fix.strip()
    return "Inspect the handler and the event; a failed event is retried only up to its budget."


def _summary_result(
    *,
    consumer: str,
    subscriber: str,
    event_id: str,
    status: str,
    duplicate: bool,
    attempts: int,
    result: Mapping[str, Any] | None = None,
    ack: Mapping[str, Any] | None = None,
    error: str = "",
    error_code: str = "",
    fix: str = "",
    retry_after: str = "",
) -> dict[str, Any]:
    action = {
        "acked": "acked",
        "duplicate": "duplicate",
        "retryable": "released",
        "failed": "refused",
    }.get(status, status)
    return {
        "consumer": consumer,
        "subscriber": subscriber,
        "event_id": event_id,
        "state": status,
        "status": status,
        "action": action,
        "duplicate": bool(duplicate),
        "attempts": int(attempts),
        "result": dict(result) if isinstance(result, Mapping) else None,
        "ack": dict(ack) if isinstance(ack, Mapping) else None,
        "error": str(error),
        "error_code": str(error_code or error),
        "fix": str(fix),
        "retry_after": str(retry_after),
    }


# ---------------------------------------------------------------------------
# Public consumer
# ---------------------------------------------------------------------------


def consume_once(
    state_dir: Path,
    subscriber: str,
    handler: Any,
    *,
    consumer: str,
    now: Any = None,
    ttl_s: float = DEFAULT_CLAIM_TTL_S,
    max_attempts: int = MAX_ATTEMPTS,
    retry_backoff_s: float = 0.0,
) -> dict[str, Any] | None:
    """Claim and handle at most one event; ``None`` means nothing was claimable.

    The claim is durable before the handler is called.  A successful handler
    result (a mapping) is acknowledged exactly once.  A duplicate delivery of an
    acknowledged event returns ``duplicate: True`` without calling the handler.
    A failure releases the event for one bounded retry; exhausted events are
    marked failed and skipped by later passes.
    """

    state = Path(state_dir)
    name = _safe_id(subscriber, "subscriber")
    consumer_name = str(consumer or "").strip()
    if not consumer_name:
        raise EventConsumerError("consumer_invalid", "the consumer must have a name")
    if not callable(handler):
        raise EventConsumerError("handler_invalid", "handler must be callable")
    clock = _clock(now)
    ttl = float(ttl_s)
    if ttl <= 0:
        raise EventConsumerError("ttl_invalid", "the claim TTL must be positive")
    budget = int(max_attempts)
    if budget <= 0:
        raise EventConsumerError("attempts_invalid", "max_attempts must be positive")
    try:
        backoff = max(0.0, float(retry_backoff_s))
    except (TypeError, ValueError) as exc:
        raise EventConsumerError(
            "backoff_invalid", "retry_backoff_s must be a non-negative number"
        ) from exc

    duplicates: list[dict[str, Any]] = []
    for path, event in _iter_events(state, name):
        outcome = _claim(
            state,
            name,
            path,
            event,
            consumer=consumer_name,
            clock=clock,
            ttl_s=ttl,
            max_attempts=budget,
        )
        status = str(outcome.get("status") or "")
        if status in ("busy", "vanished", "backoff"):
            continue
        if status == "failed_terminal":
            # Terminal failures are not claimable again; like a completed work
            # item they make this pass skip to the next candidate.
            continue
        if status == "duplicate":
            duplicates.append(outcome)
            continue
        if status == "exhausted":
            delivery = outcome.get("delivery") or {}
            return _summary_result(
                consumer=consumer_name,
                subscriber=name,
                event_id=str(outcome.get("event_id") or ""),
                status="failed",
                duplicate=False,
                attempts=int(delivery.get("attempts") or 0),
                error=str(delivery.get("last_error") or "attempts_exhausted"),
                error_code=str(delivery.get("last_error_code") or "attempts_exhausted"),
                fix=str(delivery.get("last_error_fix") or ""),
            )

        if status != "claimed":
            continue
        fresh = outcome.get("event") or {}
        delivery = outcome.get("delivery") or {}
        event_id = str(outcome.get("event_id") or "")
        try:
            result = handler(_event_payload(fresh))
            if not isinstance(result, Mapping):
                raise EventConsumerError(
                    "handler_result_invalid",
                    "the handler must return a mapping on success",
                    "Return a JSON-compatible dict; anything else is not an acknowledgement.",
                )
            ack, created = _ack(
                state,
                name,
                path,
                fresh,
                delivery,
                consumer=consumer_name,
                clock=clock,
                result=result,
            )
        except BaseException as exc:  # noqa: BLE001 - every handler failure is bounded
            failure = _fail(
                state,
                name,
                path,
                fresh,
                delivery,
                consumer=consumer_name,
                clock=clock,
                error=_error_code(exc),
                ttl_s=ttl,
                max_attempts=budget,
                error_code=_error_code(exc),
                error_fix=_error_fix(exc),
                backoff_s=backoff,
            )
            if not isinstance(exc, Exception):
                # A process-interrupting exception still leaves a retryable
                # record, but it must keep propagating to its caller.
                raise
            return _summary_result(
                consumer=consumer_name,
                subscriber=name,
                event_id=event_id,
                status=str(failure["state"]),
                duplicate=False,
                attempts=int(failure["attempts"]),
                error=str(failure["error"]),
                error_code=str(failure.get("error_code") or ""),
                fix=str(failure.get("fix") or ""),
                retry_after=str(failure.get("retry_after") or ""),
            )

        if not created:
            return _summary_result(
                consumer=consumer_name,
                subscriber=name,
                event_id=event_id,
                status="duplicate",
                duplicate=True,
                attempts=int(delivery.get("attempts") or 0),
                result=ack.get("result") if isinstance(ack, Mapping) else None,
                ack=ack,
            )
        return _summary_result(
            consumer=consumer_name,
            subscriber=name,
            event_id=event_id,
            status="acked",
            duplicate=False,
            attempts=int(delivery.get("attempts") or 0),
            result=result,
            ack=ack,
        )

    if duplicates:
        outcome = duplicates[0]
        ack = outcome.get("ack") if isinstance(outcome.get("ack"), Mapping) else {}
        delivery = outcome.get("delivery") or {}
        return _summary_result(
            consumer=consumer_name,
            subscriber=name,
            event_id=str(outcome.get("event_id") or ""),
            status="duplicate",
            duplicate=True,
            attempts=int(delivery.get("attempts") or 0),
            result=ack.get("result") if isinstance(ack, Mapping) else None,
            ack=ack,
        )
    return None


def replay_acked(state_dir: Path, subscriber: str, event_id: str) -> dict[str, Any]:
    """Allow one acknowledged event to be handled again.

    The acknowledgement is archived under ``events/acks/<subscriber>/.replays``
    and the inbox record is reset to a retryable state.  Replay is explicit: it
    is the only operation that can make a second accepted decision for an event.
    """

    state = Path(state_dir)
    name = _safe_id(subscriber, "subscriber")
    identifier = _safe_id(event_id, "event_id")
    ack, location = _find_ack(state, name, identifier)
    if ack is None or location is None:
        return {
            "replayed": False,
            "event_id": identifier,
            "subscriber": name,
            "reason": "not_acked",
        }
    path = _find_event_path(state, name, identifier)
    if path is None:
        return {
            "replayed": False,
            "event_id": identifier,
            "subscriber": name,
            "reason": "event_missing",
        }

    stamp = _iso(time.time())
    lock = _lock_path(state, name, identifier)
    with _locked(lock):
        fresh = _read_json(path)
        if fresh is None:
            return {
                "replayed": False,
                "event_id": identifier,
                "subscriber": name,
                "reason": "event_missing",
            }
        archive_dir = acks_dir(state, name) / ".replays"
        archive = archive_dir / f"{identifier}.{uuid.uuid4().hex}.json"
        _create_only_json(
            archive,
            {
                **dict(ack),
                "replayed_at": stamp,
                "previous_ack": dict(ack),
            },
        )
        try:
            Path(location).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return {
                "replayed": False,
                "event_id": identifier,
                "subscriber": name,
                "reason": f"ack_unlink_failed: {exc}",
            }
        delivery = _delivery_of(fresh)
        delivery.update(
            schema=DELIVERY_SCHEMA,
            subscriber=name,
            event_id=identifier,
            state="retryable",
            claim=None,
            attempts=0,
            last_error="",
            updated_at=stamp,
            duplicate_replays=int(delivery.get("duplicate_replays") or 0) + 1,
        )
        fresh["_delivery"] = delivery
        _write_json(path, fresh)
    return {
        "replayed": True,
        "event_id": identifier,
        "subscriber": name,
        "ack": dict(ack),
        "archive": str(archive),
    }


def _state_of(event: Mapping[str, Any], ack: Mapping[str, Any] | None) -> str:
    if ack is not None:
        return "acked"
    delivery = _delivery_of(event)
    state_name = str(delivery.get("state") or "")
    if state_name in STATES:
        return state_name
    return "pending"


def inbox_summary(state_dir: Path, subscriber: str | None = None) -> dict[str, Any]:
    """Count inbox events by delivery state, optionally for one subscriber."""

    state = Path(state_dir)
    name = _safe_id(subscriber, "subscriber") if subscriber is not None else None
    counts = {
        "total": 0,
        "pending": 0,
        "claimed": 0,
        "retryable": 0,
        "undelivered": 0,
        "acked": 0,
        "failed": 0,
        "refused": 0,
        "duplicates": 0,
    }
    per_subscriber: dict[str, dict[str, int]] = {}
    seen: set[tuple[Path, str]] = set()
    for path, event in _iter_events(state, name):
        identifier = _event_id(event, path)
        owner = str(
            (_delivery_of(event).get("subscriber"))
            or (_declared_targets(event)[0] if _declared_targets(event) else "")
            or (name or path.parent.name)
        )
        key = (path, identifier)
        if key in seen:
            continue
        seen.add(key)
        ack, _location = _find_ack(state, owner, identifier)
        current = _state_of(event, ack)
        counts["total"] += 1
        if current in ("pending", "claimed", "retryable"):
            counts["undelivered"] += 1
        if current == "failed":
            counts["failed"] += 1
            counts["refused"] += 1
        else:
            counts[current] = counts.get(current, 0) + 1
        bucket = per_subscriber.setdefault(
            owner,
            {"total": 0, "pending": 0, "claimed": 0, "retryable": 0,
             "undelivered": 0, "acked": 0, "failed": 0, "refused": 0,
             "duplicates": 0},
        )
        bucket["total"] += 1
        if current in ("pending", "claimed", "retryable"):
            bucket["undelivered"] += 1
        if current == "failed":
            bucket["failed"] += 1
            bucket["refused"] += 1
        else:
            bucket[current] = bucket.get(current, 0) + 1
    return {
        "schema": SUMMARY_SCHEMA,
        "subscriber": name,
        "counts": counts,
        **counts,
        "subscribers": per_subscriber,
    }


# ---------------------------------------------------------------------------
# Coordinator action queue, composed with terminal completion events
# ---------------------------------------------------------------------------
#
# W6 landed the durable completion signal: a bee's terminal landing event is
# recorded once in the outbox and delivered to every subscriber. F6 (the
# coordinator action queue, PR #54, :mod:`bundlebees_plugin.coordinator_actions`)
# names *what* the coordinator should do next as durable items with idempotent
# keys. This section is the composition: a consumer that subscribes to the
# landing events and translates each terminal completion into two action-queue
# items, so worker completions advance the pipeline without polling and without
# Hermes being awake.
#
# The composition is additive. The mechanical tick, the Hermes wake path and
# the 10-minute heartbeat remain exactly what they were; a completion that the
# consumer has not seen yet is still picked up by the heartbeat as before.
#
# Two stores, never one shared directory. The merged F6 queue owns
# ``coordinator/actions`` exclusively — its ``bundlebees.coordinator-actions/1``
# records, its durable ``fence.seq`` counter and its ``actions.lock``. The
# composition's bundled item store keeps its own records, locks and skip
# counter in ``coordinator/completion-actions/``: a different record shape
# (``rizzstack.coordinator-action/1``, the idempotent key ``<kind>:<attempt>``
# as the file name), a per-item fence, and an attempts budget with backoff.
# The merged queue's reader refuses any record it did not write
# (``state_corrupt``), so a composition item inside its directory would break
# it; the separate directories keep both stores readable in one state
# directory. To publish the same logical actions into the merged queue instead,
# pass :func:`coordinator_actions_enqueue` as the handler's ``enqueue`` seam —
# the explicit adapter that writes conformant records through
# ``coordinator_actions.enqueue`` (colon-free queue id, the idempotent key as
# ``action_key``, the completion facts in ``payload``, ``fence_seq`` fencing,
# ``status: "queued"``).
#
# Exactly-once is layered, not promised by one mechanism:
#
# * event level: ``consume_once`` acknowledges each (subscriber, event_id)
#   exactly once, so duplicate delivery never reaches the handler again;
# * effect level: the completion ledger records one accepted decision per
#   ``(attempt_id, state)`` and action items are published create-only under
#   their idempotent key (``verify:<attempt>`` / ``process-completion:<attempt>``),
#   so a crash after the effect replays into the same items, never new ones;
# * claim level: action items are claimed under ``flock`` with a fencing
#   token that increments on every claim; a stale consumer's token is refused.
#
# Failure modes: a queue that cannot be written or read raises
# ``ActionQueueError`` (a store ``OSError`` is wrapped as ``queue_unavailable``,
# so an outage surfaces as the queue's typed error, never a bare exception
# class name), which releases the event for a bounded backoff inside its
# still-durable inbox record (the outbox event is never deleted); a malformed
# or non-terminal event is skipped with a counted reason and never enqueued; a
# completion that was already processed is a no-op.

ACTION_SCHEMA = "rizzstack.coordinator-action/1"
ACTION_SUMMARY_SCHEMA = "rizzstack.coordinator-action-summary/1"
COMPLETION_SCHEMA = "rizzstack.completion-ledger/1"
COMPLETION_EVENT_SCHEMA = "rizzstack.bee-completion/1"
COMPLETION_EVENT_TYPE = "rizzstack.bee.completed.v1"
COMPLETION_EVENT_ID_PREFIX = "urn:rizzstack:bee-completion:"
SKIPS_SCHEMA = "rizzstack.completion-skips/1"

#: The directory of the composition's bundled item store, under the
#: coordinator state directory. The merged coordinator action queue
#: (:mod:`bundlebees_plugin.coordinator_actions`) owns ``coordinator/actions``;
#: this store must never share that directory with it.
COMPLETION_ACTIONS_DIR = "completion-actions"

#: The kinds of coordinator action this composition enqueues, and the
#: idempotent-key prefix each one uses.
ACTION_KINDS: tuple[str, ...] = ("verify", "process-completion")

ACTION_STATUSES: tuple[str, ...] = ("pending", "claimed", "done", "failed")

#: Landing states that mean the bee's work is finished. ``landed`` and
#: ``succeeded`` carry a result the coordinator should verify; every terminal
#: state is processed as a completion.
TERMINAL_COMPLETION_STATES = frozenset({"landed", "succeeded", "failed", "cancelled"})
VERIFY_ELIGIBLE_STATES = frozenset({"landed", "succeeded"})

#: The inbox subscriber the composed consumer reads by default, the default
#: consumer name on action claims, and the backoff between queue retries.
DEFAULT_COMPLETION_SUBSCRIBER = "coordinator-actions"
DEFAULT_ACTION_CONSUMER = "coordinator-actions"
DEFAULT_RETRY_BACKOFF_S = 30.0

#: Action claims are bounded like event claims. A claim counts before the
#: owner acts, so a crash after the claim still spends one attempt.
MAX_ACTION_ATTEMPTS = 2

#: Completion deliveries get a deeper budget than the inbox default so a
#: transient action-queue outage is bridged by the bounded backoff instead of
#: stranding events in ``failed``. An exhausted event is still recoverable
#: through :func:`requeue_failed`.
COMPLETION_MAX_ATTEMPTS = 4

ACTION_FIX = (
    "Inspect coordinator/completion-actions and coordinator/completions; an "
    "action item or a completion decision is never overwritten to hide a "
    "second decision."
)


class ActionQueueError(Exception):
    """An action-queue item cannot be enqueued, claimed or completed."""

    def __init__(self, code: str, message: str, fix: str = ACTION_FIX) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


@contextmanager
def _queue_store(operation: str) -> Iterator[None]:
    """Surface store I/O failures as the queue's typed error.

    Every queue read-modify-write runs inside this guard: an ``OSError`` from
    the store (and the ``event_unreadable`` wrapper the shared reader raises
    for one) becomes ``ActionQueueError("queue_unavailable", ...)``, so an
    outage reaches the caller as the queue's error code instead of a bare
    exception class name. Queue refusals and corrupt-record reports pass
    through untouched — they are decisions, not outages.
    """

    try:
        yield
    except ActionQueueError:
        raise
    except EventConsumerError as exc:
        if str(exc.code) != "event_unreadable":
            raise
        raise ActionQueueError(
            "queue_unavailable",
            f"the action-queue store could not be {operation}: {exc.message}",
            "Retry when the coordinator state directory is readable again.",
        ) from exc
    except OSError as exc:
        raise ActionQueueError(
            "queue_unavailable",
            f"the action-queue store could not be {operation}: {exc}",
            "Retry when the coordinator state directory is writable again.",
        ) from exc


def completion_actions_dir(state_dir: Path) -> Path:
    """The composition's bundled item store, beside — never inside — the
    merged coordinator action queue's ``coordinator/actions`` directory."""

    return Path(state_dir) / "coordinator" / COMPLETION_ACTIONS_DIR


def completions_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "coordinator" / "completions"


def action_path(state_dir: Path, action_id: str) -> Path:
    return completion_actions_dir(state_dir) / f"{_safe_id(action_id, 'action_id')}.json"


def _action_lock_path(state_dir: Path, action_id: str) -> Path:
    return (
        completion_actions_dir(state_dir)
        / ".locks"
        / f"{_safe_id(action_id, 'action_id')}.lock"
    )


def completion_path(state_dir: Path, attempt_id: str, state: str) -> Path:
    key = _safe_id(f"{_safe_id(attempt_id, 'attempt_id')}:{_safe_id(state, 'state')}", "completion key")
    return completions_dir(state_dir) / f"{key}.json"


def _completion_lock_path(state_dir: Path, attempt_id: str, state: str) -> Path:
    path = completion_path(state_dir, attempt_id, state)
    return path.parent / ".locks" / f"{path.stem}.lock"


def _skips_path(state_dir: Path) -> Path:
    return completion_actions_dir(state_dir) / ".skipped.json"


def _safe_attempt(value: Any) -> str | None:
    """A validated attempt/job/task identifier, or ``None`` when unusable."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if (
        not text
        or len(text) > 160
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        return None
    return text


def _advisory_text(value: Any, limit: int = 200) -> str:
    """An advisory, non-load-bearing copy: safe strings pass, the rest drop."""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or any(ch not in _ALLOWED_ID for ch in text):
        return ""
    return text


def _advisory_path(value: Any, limit: int = 200) -> str:
    """An advisory path-like copy (lease files, refs): safe strings pass."""

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit or "\x00" in text:
        return ""
    return text


def _advisory_files(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [
        _advisory_path(item)
        for item in value
        if isinstance(item, str) and _advisory_path(item)
    ]


def action_id_for(kind: str, attempt_id: str) -> str:
    """The F6 idempotent key for one action: ``<kind>:<attempt_id>``."""

    if kind not in ACTION_KINDS:
        raise ActionQueueError(
            "action_kind_invalid",
            f"kind {kind!r} is not one of {', '.join(ACTION_KINDS)}",
        )
    attempt = _safe_attempt(attempt_id)
    if attempt is None:
        raise ActionQueueError(
            "action_attempt_invalid",
            f"attempt id {attempt_id!r} is not a safe identifier",
        )
    return f"{kind}:{attempt}"


def classify_completion(event: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one delivered event as a terminal completion or a skip.

    Both shapes are accepted: the canonical ``rizzstack.bee.landed/1``
    CloudEvents envelope (facts under ``data``) and a flat event mapping that
    carries the fields at the top level. The result is ``{"completion": False,
    "reason": ...}`` for anything that must not enqueue — malformed facts or a
    non-terminal state — and the completion facts otherwise. Only load-bearing
    identifiers are validated strictly; advisory copies (``source_rev``,
    ``result_reference``, ``lease_files``) drop unsafe values instead of
    refusing the event.
    """

    if not isinstance(event, Mapping):
        return {"completion": False, "reason": "malformed_event"}
    data = event.get("data")
    if not isinstance(data, Mapping):
        data = event
    attempt = _safe_attempt(data.get("attempt_id") or event.get("attempt_id"))
    if attempt is None:
        missing = data.get("attempt_id") or event.get("attempt_id")
        return {
            "completion": False,
            "reason": "malformed_attempt_missing" if missing in (None, "") else "malformed_attempt_id",
        }
    state = data.get("state") or event.get("state")
    if not isinstance(state, str) or not state.strip():
        return {"completion": False, "reason": "malformed_state_missing"}
    state = state.strip()
    if state not in TERMINAL_COMPLETION_STATES:
        return {
            "completion": False,
            "reason": "not_terminal",
            "state": state,
        }
    for name in ("job_id", "task_id"):
        value = data.get(name) or event.get(name)
        if value in (None, ""):
            continue
        if _safe_attempt(value) is None:
            return {"completion": False, "reason": f"malformed_{name}"}
    result = data.get("result") if isinstance(data.get("result"), Mapping) else {}
    event_id = ""
    for key in ("event_id", "id"):
        raw = event.get(key) or data.get(key)
        if isinstance(raw, str) and raw.strip():
            event_id = raw.strip()
            break
    return {
        "completion": True,
        "attempt_id": attempt,
        "state": state,
        "event_id": _advisory_text(event_id, 160),
        "job_id": _safe_attempt(data.get("job_id") or event.get("job_id")) or "",
        "task_id": _safe_attempt(data.get("task_id") or event.get("task_id")) or "",
        "source_rev": _advisory_text(data.get("source_rev") or event.get("source_rev")),
        "result_available": (
            bool(result.get("available")) if "available" in result else None
        ),
        "result_reference": _advisory_path(result.get("reference")),
        "lease_files": _advisory_files(
            (data.get("lease") or {}).get("assigned_files")
            if isinstance(data.get("lease"), Mapping)
            else event.get("assigned_files")
        ),
    }


def completion_event_id(attempt_id: str, state: str) -> str:
    """The deterministic inbox id for one ``(attempt_id, terminal state)``.

    The landing's own CloudEvents id also covers the transition, so the same
    attempt and state can arrive with different canonical ids. A completion is
    keyed only by the pair the ledger uses::

        urn:rizzstack:bee-completion:<first 32 hex of sha256(attempt\\0state)>

    ``publish_event`` is create-only, so this id turns a replay of the same
    terminal fact into a no-op instead of a second inbox event.
    """

    attempt = _safe_attempt(attempt_id)
    if attempt is None:
        raise ActionQueueError(
            "action_attempt_invalid",
            f"attempt id {attempt_id!r} is not a safe identifier",
        )
    state_name = _safe_attempt(state)
    if state_name is None:
        raise ActionQueueError("state_invalid", f"state {state!r} is not a safe identifier")
    if state_name not in TERMINAL_COMPLETION_STATES:
        raise ActionQueueError(
            "state_not_terminal",
            f"state {state_name!r} is not a terminal completion state",
        )
    seed = f"{attempt}\x00{state_name}".encode("utf-8")
    return COMPLETION_EVENT_ID_PREFIX + hashlib.sha256(seed).hexdigest()[:32]


def completion_event(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one accepted landing into a durable completion event.

    Returns ``None`` for a non-terminal or malformed landing. The projection
    carries only the facts the completion consumer already copies — never the
    whole landing — so coordinator/backend metadata and any free-form worker
    text stay out of the inbox record. The event is addressed to
    :data:`DEFAULT_COMPLETION_SUBSCRIBER` and its id depends only on the
    ``(attempt_id, state)`` pair.
    """

    classified = classify_completion(source)
    if not classified.get("completion"):
        return None
    attempt = str(classified["attempt_id"])
    state_name = str(classified["state"])
    event: dict[str, Any] = {
        "schema": COMPLETION_EVENT_SCHEMA,
        "event_id": completion_event_id(attempt, state_name),
        "type": COMPLETION_EVENT_TYPE,
        "attempt_id": attempt,
        "state": state_name,
        "subscriber": DEFAULT_COMPLETION_SUBSCRIBER,
    }
    for name in ("job_id", "task_id", "source_rev"):
        value = str(classified.get(name) or "")
        if value:
            event[name] = value
    landing_event_id = str(classified.get("event_id") or "")
    if landing_event_id:
        event["landing_event_id"] = landing_event_id
    result: dict[str, Any] = {}
    available = classified.get("result_available")
    if available is not None:
        result["available"] = bool(available)
    reference = str(classified.get("result_reference") or "")
    if reference:
        result["reference"] = reference
    if result:
        event["result"] = result
    lease_files = list(classified.get("lease_files") or ())
    if lease_files:
        event["lease"] = {"assigned_files": lease_files}
    return event


def publish_completion_event(
    state_dir: Path,
    source: Mapping[str, Any],
    *,
    subscriber: str = DEFAULT_COMPLETION_SUBSCRIBER,
    now: Any = None,
) -> dict[str, Any] | None:
    """Publish one landing's completion event, or ``None`` when not terminal.

    Create-only through :func:`publish_event`: the deterministic event id makes
    a replay return the stored record without overwriting it. A write failure
    raises (:class:`EventConsumerError` or ``OSError``) so the caller keeps the
    landing unacknowledged and retries.
    """

    event = completion_event(source)
    if event is None:
        return None
    return publish_event(state_dir, event, subscriber=subscriber, now=now)


def enqueue_action(
    state_dir: Path,
    action_id: str,
    *,
    kind: str,
    attempt_id: str,
    event_id: str = "",
    event_state: str = "",
    job_id: str = "",
    task_id: str = "",
    source_rev: str = "",
    lease_files: Any = (),
    result_available: bool | None = None,
    result_reference: str = "",
    dedupe_key: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """Publish one action item under its idempotent key, create-only.

    Replaying the same key returns the stored item with ``created: False`` and
    never overwrites it; a different ``kind``/``attempt_id`` under a live key
    is refused with ``action_conflict``. A ``done`` item stays ``done``: a
    later event for the same attempt is a no-op, never a resurrection.
    """

    if kind not in ACTION_KINDS:
        raise ActionQueueError(
            "action_kind_invalid",
            f"kind {kind!r} is not one of {', '.join(ACTION_KINDS)}",
        )
    attempt = _safe_attempt(attempt_id)
    if attempt is None:
        raise ActionQueueError(
            "action_attempt_invalid",
            f"attempt id {attempt_id!r} is not a safe identifier",
        )
    expected = f"{kind}:{attempt}"
    path = action_path(state_dir, action_id)
    stamp = _iso(_clock(now))
    with _queue_store("written"), _locked(_action_lock_path(state_dir, action_id)):
        stored = _read_json(path)
        if stored is not None:
            if (
                str(stored.get("kind") or "") != kind
                or str(stored.get("attempt_id") or "") != attempt
            ):
                raise ActionQueueError(
                    "action_conflict",
                    f"{action_id} already exists for a different attempt or kind",
                    "Keep one durable item per idempotent key; do not rewrite it.",
                )
            return {
                "action_id": action_id,
                "created": False,
                "duplicate": True,
                "status": str(stored.get("status") or ""),
                "item": stored,
            }
        if str(action_id or "").strip() != expected:
            raise ActionQueueError(
                "action_id_mismatch",
                f"action_id {action_id!r} must be the idempotent key {expected!r}",
            )
        item = {
            "schema": ACTION_SCHEMA,
            "action_id": action_id,
            "kind": kind,
            "attempt_id": attempt,
            "job_id": _safe_attempt(job_id) or "",
            "task_id": _safe_attempt(task_id) or "",
            "event_id": _advisory_text(event_id, 160),
            "event_state": _safe_attempt(event_state) or "",
            "dedupe_key": _safe_attempt(dedupe_key) or "",
            "source_rev": _advisory_text(source_rev),
            "lease_files": _advisory_files(lease_files),
            "result_available": result_available,
            "result_reference": _advisory_path(result_reference),
            "status": "pending",
            "attempts": 0,
            "fence": 0,
            "claimed_by": "",
            "claimed_at": "",
            "claim_pid": 0,
            "claim_expires_at": "",
            "next_attempt_at": "",
            "result": None,
            "error": "",
            "created_at": stamp,
            "updated_at": stamp,
        }
        if not _create_only_json(path, item):
            stored = _read_json(path)
            return {
                "action_id": action_id,
                "created": False,
                "duplicate": True,
                "status": str((stored or {}).get("status") or ""),
                "item": stored or item,
            }
        return {
            "action_id": action_id,
            "created": True,
            "duplicate": False,
            "status": "pending",
            "item": item,
        }


def coordinator_actions_enqueue(
    state_dir: Path,
    action_id: str,
    *,
    kind: str,
    attempt_id: str,
    event_id: str = "",
    event_state: str = "",
    job_id: str = "",
    task_id: str = "",
    source_rev: str = "",
    lease_files: Any = (),
    result_available: bool | None = None,
    result_reference: str = "",
    dedupe_key: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """The explicit adapter at the ``enqueue`` seam: publish one composition
    action through the merged coordinator action queue.

    This function has exactly the ``enqueue`` signature the composed handler
    calls, so it can be passed as ``enqueue=`` to :func:`action_queue_handler`
    or :func:`consume_completions` when the merged F6 queue
    (:mod:`bundlebees_plugin.coordinator_actions`) is the system of record.
    The translation it performs — and the reason a bare
    ``coordinator_actions.enqueue`` cannot sit at the seam — is:

    * the idempotent key ``<kind>:<attempt_id>`` (colons are part of the
      composition's key grammar) becomes the merged queue's colon-free
      action id, ``<kind>_<attempt_id>`` — the key itself is stored as the
      record's ``action_key``;
    * the completion facts travel in ``payload`` instead of top-level
      record fields;
    * the item is enqueued with the merged queue's own fencing (``fence_seq``
      minted at claim) and ``status: "queued"``; the composition declares no
      workflow protocol commit, so the record carries the legacy empty
      ``workflow_ref`` rather than an invented pin.

    A merged-queue refusal is re-raised as ``ActionQueueError`` with the
    queue's own code (``invalid_action_id``, ``action_id_conflict``, ...), so
    an unusable id or a collided key surfaces precisely and the event is
    buffered, never silently dropped. Two different keys that collapse onto
    the same colon-free id are refused as ``action_id_conflict`` — a precise
    failure, never a silent merge of two logical actions. The ``created``
    flag is derived from the record's existence just before the create-only
    enqueue; under a race both callers may report ``created`` for one new
    record — the completion ledger, not this flag, is what deduplicates the
    effect.
    """

    if kind not in ACTION_KINDS:
        raise ActionQueueError(
            "action_kind_invalid",
            f"kind {kind!r} is not one of {', '.join(ACTION_KINDS)}",
        )
    attempt = _safe_attempt(attempt_id)
    if attempt is None:
        raise ActionQueueError(
            "action_attempt_invalid",
            f"attempt id {attempt_id!r} is not a safe identifier",
        )
    key = action_id_for(kind, attempt)
    if str(action_id or "").strip() != key:
        raise ActionQueueError(
            "action_id_mismatch",
            f"action_id {action_id!r} must be the idempotent key {key!r}",
        )
    queue_id = key.replace(":", "_")
    payload = {
        "attempt_id": attempt,
        "event_id": _advisory_text(event_id, 160),
        "event_state": _safe_attempt(event_state) or "",
        "job_id": _safe_attempt(job_id) or "",
        "task_id": _safe_attempt(task_id) or "",
        "dedupe_key": _safe_attempt(dedupe_key) or "",
        "source_rev": _advisory_text(source_rev),
        "lease_files": _advisory_files(lease_files),
        "result_available": result_available,
        "result_reference": _advisory_path(result_reference),
    }
    with _queue_store("written"):
        try:
            before = coordinator_actions.load(state_dir, queue_id)
            record = coordinator_actions.enqueue(
                state_dir,
                queue_id,
                action_key=key,
                kind=kind,
                payload=payload,
                now=now,
            )
        except coordinator_actions.ActionError as exc:
            raise ActionQueueError(exc.code, exc.message, exc.fix) from exc
    return {
        "action_id": queue_id,
        "action_key": key,
        "created": before is None,
        "duplicate": before is not None,
        "status": str(record.get("status") or ""),
        "item": record,
    }


def _clear_claim(item: dict[str, Any]) -> None:
    item["claimed_by"] = ""
    item["claimed_at"] = ""
    item["claim_pid"] = 0
    item["claim_expires_at"] = ""


def claim_action(
    state_dir: Path,
    action_id: str,
    *,
    consumer: str,
    now: Any = None,
    ttl_s: float = DEFAULT_CLAIM_TTL_S,
) -> dict[str, Any]:
    """Claim one action item atomically and return its fencing token.

    The fence increments on every claim, so a token issued to a consumer whose
    claim was later adopted (its TTL expired or its pid died) is stale. The
    claim is durable before the caller acts. ``done`` and ``failed`` items are
    terminal and are never claimed again.
    """

    name = str(consumer or "").strip()
    if not name:
        raise ActionQueueError("consumer_invalid", "the consumer must have a name")
    clock = _clock(now)
    ttl = float(ttl_s)
    if ttl <= 0:
        raise ActionQueueError("ttl_invalid", "the claim TTL must be positive")
    path = action_path(state_dir, action_id)
    stamp = _iso(clock)
    with _queue_store("written"), _locked(_action_lock_path(state_dir, action_id)):
        item = _read_json(path)
        if item is None:
            raise ActionQueueError(
                "action_missing", f"{action_id} does not exist in the queue"
            )
        status = str(item.get("status") or "")
        if status in ("done", "failed"):
            return {"status": status, "claimed": False, "fence": int(item.get("fence") or 0), "item": item}
        if status == "claimed":
            claim = {
                "pid": item.get("claim_pid"),
                "expires_at": item.get("claim_expires_at"),
            }
            if not _claim_expired(claim, clock):
                return {"status": "busy", "claimed": False, "fence": int(item.get("fence") or 0), "item": item}
        if status == "pending":
            not_before = _epoch(item.get("next_attempt_at"))
            if not_before is not None and clock < not_before:
                # A released item waits out its bounded backoff before the
                # next claim; a resumed or restart adopter sees the same rule.
                return {"status": "backoff", "claimed": False, "fence": int(item.get("fence") or 0), "item": item}
        attempts = int(item.get("attempts") or 0)
        if attempts >= MAX_ACTION_ATTEMPTS:
            item.update(
                status="failed",
                failed_at=stamp,
                error=str(item.get("error") or "attempts_exhausted"),
                updated_at=stamp,
            )
            _clear_claim(item)
            _write_json(path, item)
            return {"status": "exhausted", "claimed": False, "fence": int(item.get("fence") or 0), "item": item}
        attempts += 1
        fence = int(item.get("fence") or 0) + 1
        item.update(
            status="claimed",
            attempts=attempts,
            fence=fence,
            claimed_by=name,
            claimed_at=stamp,
            claim_pid=os.getpid(),
            claim_expires_at=_iso(clock + ttl),
            next_attempt_at="",
            updated_at=stamp,
        )
        _write_json(path, item)
        return {"status": "claimed", "claimed": True, "fence": fence, "item": item}


def _fenced_item(state_dir: Path, action_id: str, fence: Any) -> dict[str, Any]:
    path = action_path(state_dir, action_id)
    item = _read_json(path)
    if item is None:
        raise ActionQueueError(
            "action_missing", f"{action_id} does not exist in the queue"
        )
    try:
        presented = int(fence)
    except (TypeError, ValueError) as exc:
        raise ActionQueueError("fence_invalid", "the fencing token must be an integer") from exc
    if int(item.get("fence") or 0) != presented:
        raise ActionQueueError(
            "stale_fence",
            f"fencing token {presented} is stale; the item is at fence "
            f"{int(item.get('fence') or 0)}",
            "Re-read the item and claim it again with the current fence.",
        )
    return item


def complete_action(
    state_dir: Path,
    action_id: str,
    *,
    fence: Any,
    result: Mapping[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Complete a claimed action item; the current fence proves ownership.

    Only an item that is *currently claimed* can be completed: a pending
    (never claimed or released), failed or otherwise unclaimed item is
    refused with ``not_claimed``, so a stale owner can never mark work done
    that no live claim owns. Re-completing an already ``done`` item is the
    idempotent duplicate decision and changes nothing.
    """

    stamp = _iso(_clock(now))
    with _queue_store("written"), _locked(_action_lock_path(state_dir, action_id)):
        item = _fenced_item(state_dir, action_id, fence)
        status = str(item.get("status") or "")
        if status == "done":
            return {"status": "done", "duplicate": True, "item": item}
        if status != "claimed":
            raise ActionQueueError(
                "not_claimed",
                f"action {action_id} is {status}; completing requires the current claim",
                "Claim the action first; an unclaimed or failed item cannot be completed.",
            )
        item.update(
            status="done",
            result=dict(result) if isinstance(result, Mapping) else {},
            completed_at=stamp,
            error="",
            updated_at=stamp,
        )
        _clear_claim(item)
        _write_json(action_path(state_dir, action_id), item)
        return {"status": "done", "duplicate": False, "item": item}


def fail_action(
    state_dir: Path,
    action_id: str,
    *,
    fence: Any,
    error: str,
    now: Any = None,
    retry_backoff_s: float = 0.0,
) -> dict[str, Any]:
    """Release a claimed item for one bounded retry, or mark it failed.

    The fence must be current: a stale consumer cannot release an item a
    newer claim owns. Only an item that is *currently claimed* can be
    released — a pending (never claimed or already released) or failed item
    is refused with ``not_claimed``, so a stale owner can never fail or
    resurrect work no live claim owns. ``attempts`` is already counted by the
    claim, so the second failure of one item is terminal.
    """

    clock = _clock(now)
    stamp = _iso(clock)
    with _queue_store("written"), _locked(_action_lock_path(state_dir, action_id)):
        item = _fenced_item(state_dir, action_id, fence)
        status = str(item.get("status") or "")
        if status == "done":
            return {"status": "done", "duplicate": True, "item": item}
        if status != "claimed":
            raise ActionQueueError(
                "not_claimed",
                f"action {action_id} is {status}; failing requires the current claim",
                "Claim the action first; an unclaimed or failed item cannot be released.",
            )
        attempts = int(item.get("attempts") or 0)
        exhausted = attempts >= MAX_ACTION_ATTEMPTS
        try:
            delay = max(0.0, float(retry_backoff_s))
        except (TypeError, ValueError) as exc:
            raise ActionQueueError(
                "backoff_invalid", "retry_backoff_s must be a non-negative number"
            ) from exc
        next_attempt = "" if exhausted or delay <= 0 else _iso(clock + delay)
        item.update(
            status="failed" if exhausted else "pending",
            error=str(error)[:500],
            next_attempt_at=next_attempt,
            updated_at=stamp,
        )
        if exhausted:
            item["failed_at"] = stamp
        _clear_claim(item)
        _write_json(action_path(state_dir, action_id), item)
        return {
            "status": str(item.get("status")),
            "attempts": attempts,
            "exhausted": exhausted,
            "retry_after": next_attempt,
            "item": item,
        }


def actions_summary(state_dir: Path) -> dict[str, Any]:
    """Count the composition's own action items by status.

    This summarises the bundled store in ``coordinator/completion-actions``
    only; the merged coordinator action queue keeps its own records, leases
    and counters in ``coordinator/actions`` and is never read here.
    """

    root = completion_actions_dir(state_dir)
    counts = {"total": 0, "pending": 0, "claimed": 0, "done": 0, "failed": 0}
    if root.is_dir():
        for path in sorted(root.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") or path.suffix != ".json" or not path.is_file():
                continue
            item = _read_json(path)
            status = str(item.get("status") or "pending")
            counts["total"] += 1
            counts[status] = counts.get(status, 0) + 1
    return {"schema": ACTION_SUMMARY_SCHEMA, "counts": counts}


def _bump_skip(state_dir: Path, reason: str, *, now: Any = None) -> dict[str, Any]:
    """Count one skipped delivery durably, by reason."""

    path = _skips_path(state_dir)
    stamp = _iso(_clock(now))
    with _queue_store("written"), _locked(
        completion_actions_dir(state_dir) / ".locks" / "skips.lock"
    ):
        record = _read_json(path) or {"schema": SKIPS_SCHEMA, "counts": {}}
        counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
        counts[str(reason)] = int(counts.get(str(reason)) or 0) + 1
        record.update(counts=counts, updated_at=stamp)
        _write_json(path, record)
        return dict(record)


def skipped_events(state_dir: Path) -> dict[str, Any]:
    """The durable counter of skipped completion deliveries, by reason."""

    record = _read_json(_skips_path(state_dir))
    if record is None:
        return {"schema": SKIPS_SCHEMA, "counts": {}}
    counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
    return {"schema": SKIPS_SCHEMA, "counts": dict(counts)}


def record_completion(
    state_dir: Path,
    *,
    attempt_id: str,
    state: str,
    event_id: str = "",
    actions: Any = (),
    job_id: str = "",
    task_id: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """Record the one accepted decision for ``(attempt_id, state)``.

    Create-only: replaying the same pair returns the stored record with
    ``created: False``. This ledger is the dedupe key the consumer enforces,
    so a second event for an already-processed attempt is a no-op.
    """

    attempt = _safe_attempt(attempt_id)
    if attempt is None:
        raise ActionQueueError(
            "action_attempt_invalid",
            f"attempt id {attempt_id!r} is not a safe identifier",
        )
    state_name = _safe_attempt(state)
    if state_name is None:
        raise ActionQueueError("state_invalid", f"state {state!r} is not a safe identifier")
    path = completion_path(state_dir, attempt, state_name)
    stamp = _iso(_clock(now))
    with _queue_store("written"), _locked(_completion_lock_path(state_dir, attempt, state_name)):
        stored = _read_json(path)
        if stored is not None:
            return {"created": False, "duplicate": True, "record": stored}
        record = {
            "schema": COMPLETION_SCHEMA,
            "attempt_id": attempt,
            "state": state_name,
            "event_id": _advisory_text(event_id, 160),
            "job_id": _safe_attempt(job_id) or "",
            "task_id": _safe_attempt(task_id) or "",
            "actions": [_advisory_text(action, 160) for action in actions if isinstance(action, str)],
            "processed_at": stamp,
        }
        if not _create_only_json(path, record):
            stored = _read_json(path) or record
            return {"created": False, "duplicate": True, "record": stored}
        return {"created": True, "duplicate": False, "record": record}


def completion_record(
    state_dir: Path, attempt_id: str, state: str
) -> dict[str, Any] | None:
    """The stored completion decision for ``(attempt_id, state)``, if any."""

    attempt = _safe_attempt(attempt_id)
    state_name = _safe_attempt(state)
    if attempt is None or state_name is None:
        return None
    with _queue_store("read"):
        return _read_json(completion_path(state_dir, attempt, state_name))


def action_queue_handler(
    state_dir: Path,
    *,
    now: Any = None,
    enqueue: Any = None,
) -> Any:
    """Build the ``consume_once`` handler that translates completions.

    The handler classifies the event, skips malformed or non-terminal
    deliveries with a counted reason, honours the completion ledger, enqueues
    the F6 action items under their idempotent keys and records the decision.
    A queue write failure raises ``ActionQueueError``, which releases the
    event for its bounded backoff — the buffering failure mode.
    """

    state = Path(state_dir)
    enqueue_fn = enqueue if enqueue is not None else enqueue_action

    def handler(event: Mapping[str, Any]) -> dict[str, Any]:
        classified = classify_completion(event)
        if not classified.get("completion"):
            reason = str(classified.get("reason") or "malformed_event")
            skips = _bump_skip(state, reason, now=now)
            total = sum(int(value) for value in skips.get("counts", {}).values())
            return {
                "accepted": True,
                "skipped": True,
                "reason": reason,
                "actions": [],
                "skipped_total": total,
            }
        attempt = str(classified["attempt_id"])
        event_state = str(classified["state"])
        existing = completion_record(state, attempt, event_state)
        if existing is not None:
            return {
                "accepted": True,
                "skipped": False,
                "duplicate": True,
                "reason": "completion_already_processed",
                "actions": [str(action) for action in existing.get("actions") or []],
            }
        dedupe_key = f"{attempt}:{event_state}"
        wanted: list[tuple[str, str]] = []
        if event_state in VERIFY_ELIGIBLE_STATES:
            wanted.append(("verify", action_id_for("verify", attempt)))
        wanted.append(("process-completion", action_id_for("process-completion", attempt)))
        outcomes: list[dict[str, Any]] = []
        for kind, action_id in wanted:
            outcome = enqueue_fn(
                state,
                action_id,
                kind=kind,
                attempt_id=attempt,
                event_id=str(classified.get("event_id") or ""),
                event_state=event_state,
                job_id=str(classified.get("job_id") or ""),
                task_id=str(classified.get("task_id") or ""),
                source_rev=str(classified.get("source_rev") or ""),
                lease_files=classified.get("lease_files") or (),
                result_available=classified.get("result_available"),
                result_reference=str(classified.get("result_reference") or ""),
                dedupe_key=dedupe_key,
                now=now,
            )
            # The backend names the id it actually stored: the bundled store
            # keeps the idempotent key, the merged-queue adapter returns its
            # colon-free queue id. The ledger records that id.
            outcomes.append(
                {
                    "action_id": str(outcome.get("action_id") or action_id),
                    "kind": kind,
                    "created": bool(outcome.get("created")),
                    "duplicate": bool(outcome.get("duplicate")),
                    "status": str(outcome.get("status") or ""),
                }
            )
        record = record_completion(
            state,
            attempt_id=attempt,
            state=event_state,
            event_id=str(classified.get("event_id") or ""),
            actions=[outcome["action_id"] for outcome in outcomes],
            job_id=str(classified.get("job_id") or ""),
            task_id=str(classified.get("task_id") or ""),
            now=now,
        )
        return {
            "accepted": True,
            "skipped": False,
            "duplicate": False,
            "actions": [outcome["action_id"] for outcome in outcomes],
            "created": [outcome["action_id"] for outcome in outcomes if outcome["created"]],
            "completion_created": bool(record.get("created")),
        }

    return handler


def consume_completions(
    state_dir: Path,
    *,
    subscriber: str = DEFAULT_COMPLETION_SUBSCRIBER,
    consumer: str = DEFAULT_ACTION_CONSUMER,
    now: Any = None,
    ttl_s: float = DEFAULT_CLAIM_TTL_S,
    max_attempts: int = COMPLETION_MAX_ATTEMPTS,
    retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
    handler: Any = None,
    enqueue: Any = None,
) -> dict[str, Any] | None:
    """Consume at most one terminal completion into action-queue items.

    This is the whole composition in one call: the durable inbox claim from
    :func:`consume_once` plus :func:`action_queue_handler`. ``None`` means
    nothing was claimable. A queue outage releases the event with
    ``retry_backoff_s`` between attempts, ``max_attempts`` claims in total;
    beyond the budget the event is marked failed and stays recoverable through
    :func:`requeue_failed`.
    """

    state = Path(state_dir)
    handle = handler if handler is not None else action_queue_handler(state, now=now, enqueue=enqueue)
    return consume_once(
        state,
        subscriber,
        handle,
        consumer=consumer,
        now=now,
        ttl_s=ttl_s,
        max_attempts=max_attempts,
        retry_backoff_s=retry_backoff_s,
    )


def requeue_failed(
    state_dir: Path, subscriber: str, event_id: str
) -> dict[str, Any]:
    """Return one failed delivery to the retryable state, explicitly.

    This is the operator's recovery path for an event whose retries were
    exhausted while the action queue was down. It never touches an
    acknowledged event (that is :func:`replay_acked`) and never resets a
    claim; it only clears the terminal failure so the next pass may claim it.
    """

    state = Path(state_dir)
    name = _safe_id(subscriber, "subscriber")
    identifier = _safe_id(event_id, "event_id")
    path = _find_event_path(state, name, identifier)
    if path is None:
        return {
            "requeued": False,
            "event_id": identifier,
            "subscriber": name,
            "reason": "event_missing",
        }
    ack, _location = _find_ack(state, name, identifier)
    if ack is not None:
        return {
            "requeued": False,
            "event_id": identifier,
            "subscriber": name,
            "reason": "acked",
        }
    lock = _lock_path(state, name, identifier)
    with _locked(lock):
        fresh = _read_json(path)
        if fresh is None:
            return {
                "requeued": False,
                "event_id": identifier,
                "subscriber": name,
                "reason": "event_missing",
            }
        delivery = _delivery_of(fresh)
        if str(delivery.get("state") or "") != "failed":
            return {
                "requeued": False,
                "event_id": identifier,
                "subscriber": name,
                "reason": f"state_{delivery.get('state') or 'pending'}",
            }
        delivery.update(
            state="retryable",
            claim=None,
            attempts=0,
            last_error="",
            last_error_code="",
            last_error_fix="",
            next_attempt_at="",
            failed_at="",
            updated_at=_iso(time.time()),
        )
        fresh["_delivery"] = delivery
        _write_json(path, fresh)
    return {
        "requeued": True,
        "event_id": identifier,
        "subscriber": name,
    }


__all__ = [
    "ACK_SCHEMA",
    "ACTION_FIX",
    "ACTION_KINDS",
    "ACTION_SCHEMA",
    "ACTION_STATUSES",
    "ACTION_SUMMARY_SCHEMA",
    "COMPLETION_ACTIONS_DIR",
    "COMPLETION_EVENT_ID_PREFIX",
    "COMPLETION_EVENT_SCHEMA",
    "COMPLETION_EVENT_TYPE",
    "COMPLETION_MAX_ATTEMPTS",
    "COMPLETION_SCHEMA",
    "DEFAULT_ACTION_CONSUMER",
    "DEFAULT_CLAIM_TTL_S",
    "DEFAULT_COMPLETION_SUBSCRIBER",
    "DEFAULT_RETRY_BACKOFF_S",
    "DELIVERY_SCHEMA",
    "EventConsumerError",
    "MAX_ACTION_ATTEMPTS",
    "MAX_ATTEMPTS",
    "SCHEMA",
    "SKIPS_SCHEMA",
    "STATES",
    "SUMMARY_SCHEMA",
    "TERMINAL_COMPLETION_STATES",
    "VERIFY_ELIGIBLE_STATES",
    "ack_path",
    "acks_dir",
    "action_id_for",
    "action_path",
    "action_queue_handler",
    "actions_summary",
    "claim_action",
    "classify_completion",
    "coordinator_actions_enqueue",
    "complete_action",
    "completion_actions_dir",
    "completion_event",
    "completion_event_id",
    "completion_path",
    "completion_record",
    "completions_dir",
    "consume_completions",
    "consume_once",
    "enqueue",
    "enqueue_action",
    "enqueue_event",
    "fail_action",
    "inbox_dir",
    "inbox_path",
    "inbox_summary",
    "publish_completion_event",
    "publish_event",
    "record_completion",
    "replay_acked",
    "requeue_failed",
    "skipped_events",
]
