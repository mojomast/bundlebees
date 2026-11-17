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
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import bee_events
from .rails_bundlebees_source import (
    MAX_RECORD, SourceError, _ERROR_REVISION, _PagedSource, _Scan, _alias, _directory, _hex, _id,
    _instance, _parse,
)

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
LANDING_SCHEMA = bee_events.SCHEMA
KIND = "attempt.state"
OUTBOX_DIR = "outbox"
ADAPTER = "bundlebees.events.outbox"
DEDUPE_PREFIX = "outbox:"
MAX_EVENT_BYTES = 256 * 1024

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})

_EVENT_ID_RE = re.compile(r"^urn:rizzstack:bee-landed:[0-9a-f]{32}$")
_STATES = TERMINAL_STATES | {'queued', 'starting', 'running', 'backoff', 'cancel_requested'}


class OutboxSourceError(SourceError):
    """A landing could not be replayed as a rail event."""

    code = "outbox_source"

    def __init__(self, message: str, code: str | None = None) -> None:
        self.message = str(message)
        if code is not None:
            self.code = str(code)
        super().__init__(self.code, self.message)

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
    if not _EVENT_ID_RE.fullmatch(text):
        raise OutboxSourceInvalid("landing event id is not a canonical bee landing id")
    return text


def landing_digest(event: Any) -> str:
    """Return the runtime SHA-256 of a landing event's canonical bytes."""

    return hashlib.sha256(_canonical(_mapping(event))).hexdigest()


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
    """Only the public projection survives validation, even for scan callers."""

    event_id: str
    digest: str
    attempt_id: str
    state: str
    occurred_at: str | None
    sort_key: tuple
    job_id: str | None
    task_id: str | None
    code_rev: str | None


def _landing_record(path: Path, event: Any, order: int = 0) -> LandingRecord:
    try:
        # Use the same version and local structural checks as record_landing;
        # schema validation must not fetch schemas or follow arbitrary paths.
        value = bee_events._validate_event_local(_mapping(event))
    except (bee_events.EventError, ValueError, TypeError, RecursionError):
        raise OutboxSourceInvalid('invalid landing record') from None
    event_id = _safe_event_id(value.get("id"))
    if path.stem != event_id:
        raise OutboxSourceInvalid("outbox file name does not match landing event id")
    data = value.get("data")
    if not isinstance(data, Mapping):
        raise OutboxSourceInvalid("landing event data must be an object")
    attempt_id = data.get("attempt_id")
    state = data.get("state")
    if not _id(attempt_id):
        raise OutboxSourceInvalid("landing event attempt_id is not a safe identifier")
    if not isinstance(state, str) or state not in _STATES:
        raise OutboxSourceInvalid('unsupported landing state')
    occurred_at, sort_key = _stamp(value.get("time"))
    return LandingRecord(
        event_id=event_id,
        digest=landing_digest(value),
        attempt_id=attempt_id,
        state=state,
        occurred_at=occurred_at,
        sort_key=(order, sort_key, event_id),
        job_id=_id(data.get('job_id')),
        task_id=_id(data.get('task_id')),
        code_rev=_hex(data.get('source_rev')),
    )


def build_rail_event(event: Any, digest: str | None = None, *, instance='bundlebees-outbox') -> dict[str, Any]:
    """Map one landing event to the R02 ``attempt.state`` rail body."""

    value = _mapping(event)
    event_id = _safe_event_id(value.get("id"))
    record = _landing_record(Path(event_id + '.json'), value)
    if digest is not None and digest != record.digest:
        raise OutboxDigestConflict(event_id)
    return _body(record, instance)


def _body(record, instance):
    event_id, digest = record.event_id, record.digest
    occurred_at, attempt_id, state = record.occurred_at, record.attempt_id, record.state

    scope: dict[str, Any] = {"attempt_id": attempt_id}
    for key in ("job_id", "task_id", 'code_rev'):
        item = getattr(record, key)
        if item:
            scope[key] = item

    return {
        "schema": SCHEMA,
        "kind": KIND,
        "source": {
            "adapter": ADAPTER,
            "instance": instance,
            "record_id": event_id,
            "revision": digest,
        },
        "scope": scope,
        "lane": {"lane_id": attempt_id, "parent_lane_id": None, "parent_event_id": None},
        "occurred_at": occurred_at,
        "duration_ms": None,
        "timing_quality": "reported" if occurred_at else "unknown",
        "authority": "worker_report",
        "payload": {
            "operation_id": event_id,
            "fencing_epoch": 0,
            "code": state,
            "evidence_ref": digest,
            "from_state": "unknown",
            "to_state": state,
        },
        "redaction_policy": {"mode": "structured_safe", "path_detail": "omitted"},
    }


class RailsOutboxSource(_PagedSource):
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
        self.adapter_id = ADAPTER
        self.instance = _instance(self.state_dir)
        self.ledger = ledger
        self._emit = emit
        self._rail_events: list[dict[str, Any]] = []
        self._receipts: list[dict[str, Any]] = []
        self._seen: dict[str, str] = {}
        self._terminal: dict[str, str] = {}

    @property
    def rail_events(self) -> list[dict[str, Any]]:
        return list(self._rail_events)

    @property
    def receipts(self) -> list[dict[str, Any]]:
        return list(self._receipts)

    def _scan(self, diagnostics=None) -> list[LandingRecord]:
        budget = _Scan()
        records: list[LandingRecord] = []
        with _directory(self.state_dir) as root, _directory('events', root, optional=True) as events:
            if events is None:
                return []
            with _directory(OUTBOX_DIR, events, optional=True) as directory:
                for name in budget.names(directory):
                    if name.startswith('.') or not name.endswith('.json'):
                        continue
                    # Only the coordinator's durable file order locates the
                    # high-water; a worker clock may be far behind it. On a
                    # coarse filesystem, ties use source time and then ID; a
                    # newly interposed record forces a gap, never a silent skip.
                    raw, order = budget.read(directory, name, MAX_RECORD)
                    try:
                        records.append(_landing_record(Path(name), _parse(raw), order))
                    except (ValueError, OutboxSourceInvalid, RecursionError, OverflowError):
                        if diagnostics is None:
                            raise OutboxSourceInvalid('invalid landing record') from None
                        rid = _alias('record', name)
                        diagnostics.append(((order, 0.0, rid), dict(kind='source.error',
                            source=dict(adapter=ADAPTER, instance=self.instance, record_id=rid, revision=_ERROR_REVISION),
                            payload=dict(code='malformed_record', range_start=None, range_end=None,
                                         source_id=ADAPTER, cursor=None, count=1, digest=None),
                            scope=dict(session_id=self.instance), authority='imported_observation',
                            dedupe_key=_alias('outbox', [self.instance, rid]))))
        records.sort(key=lambda record: record.sort_key)
        return records

    def scan(self):
        return self._scan()

    def _key(self, rid):
        # The create-only CloudEvent ID is the fact ID. Use the same key as
        # Importer._prepare for an existing R06 landing body on this chain.
        return DEDUPE_PREFIX + rid

    def _rows(self):
        # Validate the entire bounded snapshot before returning even its first
        # page. A cursor may bisect an attempt history or come from a restart.
        errors = []
        rows = []
        terminals = {}
        for record in self._scan(errors):
            if record.attempt_id in terminals:
                raise TerminalStateRefused(record.attempt_id, terminals[record.attempt_id], record.event_id)
            if record.state in TERMINAL_STATES:
                terminals[record.attempt_id] = record.state
            row = _body(record, self.instance)
            del row['schema']
            row['dedupe_key'] = self._key(record.event_id)
            rows.append((record.sort_key, row))
        return [row for _, row in sorted(rows + errors, key=lambda pair: pair[0])]

    def _ledger_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if self.ledger is None:
            return []
        try:
            return list(self.ledger.query(sql, params))
        except Exception:
            raise OutboxSourceError('ledger history is unavailable', code='ledger_unavailable') from None

    def _ledger_digest(self, event_id: str) -> str | None:
        rows = self._ledger_rows(
            "SELECT envelope_json FROM event_rows"
            " WHERE json_extract(envelope_json, '$.kind') = 'attempt.state'"
            " AND json_extract(envelope_json, '$.source.adapter') = ?"
            " AND json_extract(envelope_json, '$.source.instance') = ?"
            " AND json_extract(envelope_json, '$.source.record_id') = ?"
            " ORDER BY seq DESC LIMIT 1",
            (ADAPTER, self.instance, event_id),
        )
        if not rows:
            return None
        try:
            envelope = json.loads(rows[0]["envelope_json"])
            evidence = envelope["payload"]["evidence_ref"]
        except (KeyError, TypeError, ValueError):
            raise OutboxSourceInvalid('unreadable ledger receipt') from None
        if not isinstance(evidence, str):
            raise OutboxSourceInvalid('unreadable ledger digest')
        return evidence

    def _ledger_terminal(self, attempt_id: str) -> str | None:
        rows = self._ledger_rows(
            "SELECT envelope_json FROM event_rows"
            " WHERE json_extract(envelope_json, '$.kind') = 'attempt.state'"
            " AND json_extract(envelope_json, '$.source.adapter') = ?"
            " AND json_extract(envelope_json, '$.source.instance') = ?"
             " AND json_extract(envelope_json, '$.scope.attempt_id') = ?"
            " AND json_extract(envelope_json, '$.payload.to_state') IN (?, ?, ?, ?)"
            " ORDER BY seq DESC LIMIT 1",
            (ADAPTER, self.instance, attempt_id, *sorted(TERMINAL_STATES)),
        )
        terminal = None
        for row in rows:
            try:
                payload = json.loads(row["envelope_json"])["payload"]
            except (KeyError, TypeError, ValueError):
                raise OutboxSourceInvalid('unreadable ledger terminal') from None
            if payload.get("to_state") in TERMINAL_STATES:
                terminal = payload["to_state"]
                break
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
                dedupe_key=self._key(record.event_id),
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
            rail = _body(record, self.instance)
            receipt = self._append(record, rail)
            # Record each completed delivery immediately. A later sink error
            # must not discard the successful prefix on an in-process retry.
            self._seen[record.event_id] = record.digest
            if record.state in TERMINAL_STATES:
                self._terminal[record.attempt_id] = record.state
            if receipt.get("duplicated"):
                duplicates += 1
                continue
            emitted.append(rail)
            receipts.append(receipt)
            self._rail_events.append(rail)
            self._receipts.append(receipt)

        return {
            "emitted": len(emitted),
            "duplicates": duplicates,
            "rail_events": list(emitted),
            "receipts": list(receipts),
        }

    def replay_one(self, event_id: str) -> dict[str, Any]:
        """Replay one named landing from the outbox."""

        records = self.scan()
        # Selecting an ID must not bypass a terminal already in this outbox.
        terminals = {}
        for record in records:
            if record.attempt_id in terminals:
                raise TerminalStateRefused(record.attempt_id, terminals[record.attempt_id], record.event_id)
            if record.state in TERMINAL_STATES:
                terminals[record.attempt_id] = record.state
        for record in records:
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
            rail = _body(record, self.instance)
            receipt = self._append(record, rail)
            self._seen[event_id] = record.digest
            if record.state in TERMINAL_STATES:
                self._terminal[record.attempt_id] = record.state
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
        raise OutboxSourceInvalid('the landing record is missing')


OutboxSource = RailsOutboxSource


def replay_outbox(state_dir: Any, **kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper: replay one outbox and return the result."""

    return RailsOutboxSource(state_dir, **kwargs).replay()
