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


__all__ = [
    "ACK_SCHEMA",
    "DEFAULT_CLAIM_TTL_S",
    "DELIVERY_SCHEMA",
    "EventConsumerError",
    "MAX_ATTEMPTS",
    "SCHEMA",
    "STATES",
    "SUMMARY_SCHEMA",
    "ack_path",
    "acks_dir",
    "consume_once",
    "enqueue",
    "enqueue_event",
    "inbox_dir",
    "inbox_path",
    "inbox_summary",
    "publish_event",
    "replay_acked",
]
