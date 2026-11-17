"""Read-only adapter over the ``events/outbox`` landing events (Rails R06).

The bundlebees outbox stores canonical ``rizzstack.bee.landed/1`` CloudEvents
under ``events/outbox/<event_id>.json``. This module never writes to that
directory. It maps each landing to one ``rizzstack.rails/1`` ``attempt.state``
rail event and emits it to a caller-supplied sink (an in-memory event list by
default) or to a ``rizzstack_rails`` ledger.

The adapter enforces the three source invariants:

* replaying a landing dedupes by event id and digest, so the same landing can
  never produce two rail events;
* a different digest under an already-seen event id is a contradiction and
  fails closed with :class:`OutboxDigestConflict` before anything is emitted;
* once an attempt has landed in a terminal state, any later landing for that
  attempt is refused with :class:`TerminalStateRefused` instead of being
  silently ignored.

The rail envelope body matches the R02 ``/1`` envelope exactly: schema, kind,
source, scope, lane, occurred_at, duration_ms, timing_quality, authority,
payload and redaction_policy. Server-assigned keys (event_id, seq,
recorded_at, prev_hash, hash) are left to the ledger. Digests are computed at
runtime from canonical JSON bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "SCHEMA",
    "LANDING_SCHEMA",
    "KIND",
    "OUTBOX_DIR",
    "TERMINAL_STATES",
    "OutboxSourceError",
    "OutboxSourceInvalid",
    "OutboxDigestConflict",
    "TerminalStateRefused",
    "LandingRecord",
    "landing_digest",
    "build_rail_event",
    "RailsOutboxSource",
    "OutboxSource",
    "replay_outbox",
]

SCHEMA = "rizzstack.rails/1"
LANDING_SCHEMA = "rizzstack.bee.landed/1"
KIND = "attempt.state"
OUTBOX_DIR = "outbox"
ADAPTER = "bundlebees.events.outbox"
DEDUPE_PREFIX = "outbox:"
MAX_EVENT_BYTES = 256 * 1024

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})

_EVENT_ID_RE = re.compile(r"^urn:rizzstack:bee-landed:[0-9a-f]{32}$")
_SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class OutboxSourceError(Exception):
    """A landing could not be replayed as a rail event."""

    code = "outbox_source"

    def __init__(self, message: str, code: str | None = None) -> None:
        self.message = str(message)
        if code is not None:
            self.code = str(code)
        super().__init__(self.code + ": " + self.message)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class OutboxSourceInvalid(OutboxSourceError):
    code = "source_invalid"


class OutboxDigestConflict(OutboxSourceError):
    """The same landing event id was seen with different canonical bytes."""

    code = "digest_conflict"

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__("event id %r was replayed with a contradictory digest" % (event_id,))


class TerminalStateRefused(OutboxSourceError):
    """A landing arrived after its attempt already reached a terminal state."""

    code = "terminal_state"

    def __init__(self, attempt_id: str, terminal_state: str, event_id: str) -> None:
        self.attempt_id = attempt_id
        self.terminal_state = terminal_state
        self.event_id = event_id
        super().__init__(
            "attempt %r is already terminal (%s); landing %r is refused"
            % (attempt_id, terminal_state, event_id)
        )


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise OutboxSourceInvalid("landing event is not canonical JSON") from exc


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutboxSourceInvalid("landing event is not UTF-8") from exc
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise OutboxSourceInvalid("landing event is not JSON") from exc
    if not isinstance(value, Mapping):
        raise OutboxSourceInvalid("landing event must be a JSON object")
    return dict(value)


def _safe_event_id(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not _EVENT_ID_RE.match(text):
        raise OutboxSourceInvalid("landing event id is not a canonical bee landing id")
    return text


def landing_digest(event: Any) -> str:
    """Return the runtime SHA-256 of a landing event's canonical bytes."""

    return hashlib.sha256(_canonical(_mapping(event))).hexdigest()


def _read_regular(path: Path) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OutboxSourceInvalid("cannot read outbox file %s" % (path.name,)) from exc
    try:
        info = os.fstat(fd)
        if (info.st_mode & 0o170000) != 0o100000:
            raise OutboxSourceInvalid("outbox entry %s is not a regular file" % (path.name,))
        chunks: list[bytes] = []
        remaining = MAX_EVENT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_EVENT_BYTES:
            raise OutboxSourceInvalid("outbox entry %s exceeds the size limit" % (path.name,))
        return raw
    finally:
        os.close(fd)


def _stamp(value: Any) -> tuple[str | None, float]:
    if value is None:
        return None, 0.0
    if not isinstance(value, str) or not value.strip():
        raise OutboxSourceInvalid("landing event time must be an ISO timestamp")
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutboxSourceInvalid("landing event time is not an ISO timestamp") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z", moment.timestamp()


@dataclass(frozen=True)
class LandingRecord:
    """One validated, immutable landing read from the outbox."""

    path: Path
    event_id: str
    digest: str
    attempt_id: str
    state: str
    occurred_at: str | None
    sort_key: float
    event: dict[str, Any]


def _landing_record(path: Path, event: Any) -> LandingRecord:
    value = _mapping(event)
    event_id = _safe_event_id(value.get("id"))
    if path.stem != event_id:
        raise OutboxSourceInvalid("outbox file name does not match landing event id")
    data = value.get("data")
    if not isinstance(data, Mapping):
        raise OutboxSourceInvalid("landing event data must be an object")
    attempt_id = data.get("attempt_id")
    state = data.get("state")
    if not isinstance(attempt_id, str) or not _SAFE_TEXT_RE.match(attempt_id):
        raise OutboxSourceInvalid("landing event attempt_id is not a safe identifier")
    if not isinstance(state, str) or not _SAFE_TEXT_RE.match(state):
        raise OutboxSourceInvalid("landing event state is not a safe identifier")
    occurred_at, sort_key = _stamp(value.get("time"))
    return LandingRecord(
        path=path,
        event_id=event_id,
        digest=landing_digest(value),
        attempt_id=attempt_id,
        state=state,
        occurred_at=occurred_at,
        sort_key=sort_key,
        event=value,
    )


def build_rail_event(event: Any, digest: str | None = None) -> dict[str, Any]:
    """Map one landing event to the R02 ``attempt.state`` rail body."""

    value = _mapping(event)
    data = value.get("data")
    if not isinstance(data, Mapping):
        raise OutboxSourceInvalid("landing event data must be an object")
    event_id = _safe_event_id(value.get("id"))
    attempt_id = data.get("attempt_id")
    state = data.get("state")
    if not isinstance(attempt_id, str) or not _SAFE_TEXT_RE.match(attempt_id):
        raise OutboxSourceInvalid("landing event attempt_id is not a safe identifier")
    if not isinstance(state, str) or not _SAFE_TEXT_RE.match(state):
        raise OutboxSourceInvalid("landing event state is not a safe identifier")
    if digest is None:
        digest = landing_digest(value)

    occurred_at, _ = _stamp(value.get("time"))
    coordinator = data.get("coordinator")
    instance = "bundlebees-outbox"
    if isinstance(coordinator, Mapping):
        identity = coordinator.get("identity")
        if isinstance(identity, str) and identity.strip():
            instance = identity.strip()
    source_rev = data.get("source_rev")
    revision = source_rev if isinstance(source_rev, str) and source_rev else digest

    scope: dict[str, Any] = {"attempt_id": attempt_id}
    for key in ("job_id", "task_id"):
        item = data.get(key)
        if isinstance(item, str) and item:
            scope[key] = item

    from_state = data.get("from_state")
    if not isinstance(from_state, str) or not from_state:
        from_state = "unknown"

    return {
        "schema": SCHEMA,
        "kind": KIND,
        "source": {
            "adapter": ADAPTER,
            "instance": instance,
            "record_id": event_id,
            "revision": revision,
        },
        "scope": scope,
        "lane": {"lane_id": None, "parent_lane_id": None, "parent_event_id": None},
        "occurred_at": occurred_at,
        "duration_ms": None,
        "timing_quality": "reported" if occurred_at else "unknown",
        "authority": "worker_report",
        "payload": {
            "operation_id": event_id,
            "fencing_epoch": 0,
            "code": state,
            "evidence_ref": digest,
            "from_state": from_state,
            "to_state": state,
        },
        "redaction_policy": {"mode": "structured_safe", "path_detail": "omitted"},
    }


class RailsOutboxSource:
    """Read-only replay of ``events/outbox`` into rail events.

    ``ledger`` may be a ``rizzstack_rails.RailsLedger`` (or any compatible
    object exposing ``append`` and ``query``). When supplied, dedupe and
    terminal state survive new adapter instances; otherwise they are kept in
    memory for the lifetime of this instance.
    """

    def __init__(self, state_dir: Any, *, ledger: Any = None, emit: Callable | None = None):
        if ledger is not None and emit is not None:
            raise OutboxSourceInvalid("pass either a ledger or an emit sink, not both")
        self.state_dir = Path(state_dir)
        self.ledger = ledger
        self._emit = emit
        self._rail_events: list[dict[str, Any]] = []
        self._receipts: list[dict[str, Any]] = []
        self._seen: dict[str, str] = {}
        self._terminal: dict[str, str] = {}
        self._ledger_terminals: dict[str, str | None] = {}

    @property
    def rail_events(self) -> list[dict[str, Any]]:
        return list(self._rail_events)

    @property
    def receipts(self) -> list[dict[str, Any]]:
        return list(self._receipts)

    def _outbox(self) -> Path:
        return self.state_dir / "events" / OUTBOX_DIR

    def scan(self) -> list[LandingRecord]:
        """Read and validate the outbox without changing it or emitting."""

        directory = self._outbox()
        if not directory.is_dir():
            return []
        records: list[LandingRecord] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.name.startswith(".") or path.suffix != ".json":
                continue
            raw = _read_regular(path)
            if raw is None:
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise OutboxSourceInvalid("outbox entry %s is not UTF-8 JSON" % (path.name,)) from exc
            records.append(_landing_record(path, value))
        records.sort(key=lambda record: (record.sort_key, record.event_id))
        return records

    def _ledger_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if self.ledger is None or not hasattr(self.ledger, "query"):
            return []
        try:
            return list(self.ledger.query(sql, params))
        except Exception:
            return []

    def _ledger_digest(self, event_id: str) -> str | None:
        rows = self._ledger_rows(
            "SELECT envelope_json FROM event_rows"
            " WHERE json_extract(envelope_json, '$.kind') = 'attempt.state'"
            " AND json_extract(envelope_json, '$.source.record_id') = ?"
            " ORDER BY seq DESC LIMIT 1",
            (event_id,),
        )
        if not rows:
            return None
        try:
            envelope = json.loads(rows[0]["envelope_json"])
            evidence = envelope["payload"]["evidence_ref"]
        except (KeyError, TypeError, ValueError):
            return None
        if isinstance(evidence, list):
            evidence = evidence[0] if evidence else None
        return evidence if isinstance(evidence, str) else None

    def _ledger_terminal(self, attempt_id: str) -> str | None:
        if attempt_id in self._ledger_terminals:
            return self._ledger_terminals[attempt_id]
        rows = self._ledger_rows(
            "SELECT envelope_json FROM event_rows"
            " WHERE json_extract(envelope_json, '$.kind') = 'attempt.state'"
            " AND json_extract(envelope_json, '$.scope.attempt_id') = ?"
            " ORDER BY seq DESC",
            (attempt_id,),
        )
        terminal = None
        for row in rows:
            try:
                payload = json.loads(row["envelope_json"])["payload"]
            except (KeyError, TypeError, ValueError):
                continue
            if payload.get("to_state") in TERMINAL_STATES:
                terminal = payload["to_state"]
                break
        self._ledger_terminals[attempt_id] = terminal
        return terminal

    def _append(self, record: LandingRecord, rail: dict[str, Any]) -> dict[str, Any]:
        if self.ledger is None:
            if self._emit is not None:
                self._emit(rail)
                return {"event_id": None, "duplicated": False}
            return {"event_id": None, "duplicated": False}
        try:
            return self.ledger.append(
                kind=rail["kind"],
                source=rail["source"],
                scope=rail["scope"],
                payload=rail["payload"],
                authority=rail["authority"],
                dedupe_key=DEDUPE_PREFIX + record.event_id,
                lane=rail["lane"],
                occurred_at=rail["occurred_at"],
                duration_ms=rail["duration_ms"],
                timing_quality=rail["timing_quality"],
                redaction_policy=rail["redaction_policy"],
            )
        except Exception as exc:
            if getattr(exc, "code", None) == "idempotency_conflict":
                raise OutboxDigestConflict(record.event_id) from exc
            raise

    def replay(self) -> dict[str, Any]:
        """Replay the whole outbox, dedupe and refuse terminal regressions."""

        records = self.scan()
        seen = dict(self._seen)
        terminal = dict(self._terminal)
        plan: list[LandingRecord] = []
        duplicates = 0

        for record in records:
            known = seen.get(record.event_id)
            if known is not None:
                if known != record.digest:
                    raise OutboxDigestConflict(record.event_id)
                duplicates += 1
                continue

            prior = self._ledger_digest(record.event_id)
            if prior is not None:
                if prior != record.digest:
                    raise OutboxDigestConflict(record.event_id)
                seen[record.event_id] = record.digest
                duplicates += 1
                continue

            prior_terminal = terminal.get(record.attempt_id) or self._ledger_terminal(
                record.attempt_id
            )
            if prior_terminal is not None:
                raise TerminalStateRefused(
                    record.attempt_id, prior_terminal, record.event_id
                )

            plan.append(record)
            seen[record.event_id] = record.digest
            if record.state in TERMINAL_STATES:
                terminal[record.attempt_id] = record.state

        emitted: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        for record in plan:
            rail = build_rail_event(record.event, record.digest)
            receipt = self._append(record, rail)
            if receipt.get("duplicated"):
                duplicates += 1
                continue
            emitted.append(rail)
            receipts.append(receipt)

        self._seen.update({record.event_id: record.digest for record in plan})
        self._terminal.update(
            {record.attempt_id: record.state for record in plan if record.state in TERMINAL_STATES}
        )
        self._ledger_terminals.clear()
        self._rail_events.extend(emitted)
        self._receipts.extend(receipts)
        return {
            "emitted": len(emitted),
            "duplicates": duplicates,
            "rail_events": list(emitted),
            "receipts": list(receipts),
        }

    def replay_one(self, event_id: str) -> dict[str, Any]:
        """Replay one named landing from the outbox."""

        for record in self.scan():
            if record.event_id != event_id:
                continue
            known = self._seen.get(event_id)
            if known is not None:
                if known != record.digest:
                    raise OutboxDigestConflict(event_id)
                return {"emitted": 0, "duplicates": 1, "rail_events": []}
            prior = self._ledger_digest(event_id)
            if prior is not None:
                if prior != record.digest:
                    raise OutboxDigestConflict(event_id)
                return {"emitted": 0, "duplicates": 1, "rail_events": []}
            prior_terminal = self._terminal.get(record.attempt_id) or self._ledger_terminal(
                record.attempt_id
            )
            if prior_terminal is not None:
                raise TerminalStateRefused(record.attempt_id, prior_terminal, event_id)
            rail = build_rail_event(record.event, record.digest)
            receipt = self._append(record, rail)
            self._seen[event_id] = record.digest
            if record.state in TERMINAL_STATES:
                self._terminal[record.attempt_id] = record.state
            self._ledger_terminals.clear()
            duplicated = bool(receipt.get("duplicated"))
            if not duplicated:
                self._rail_events.append(rail)
            self._receipts.append(receipt)
            return {
                "emitted": 0 if duplicated else 1,
                "duplicates": 1 if duplicated else 0,
                "rail_events": [] if duplicated else [rail],
                "receipts": [receipt],
            }
        raise OutboxSourceInvalid("the outbox has no landing event %r" % (event_id,))


OutboxSource = RailsOutboxSource


def replay_outbox(state_dir: Any, **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper: replay one outbox and return the result."""

    return RailsOutboxSource(state_dir, **kwargs).replay()
