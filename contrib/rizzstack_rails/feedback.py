"""Feedback overlay keyed by rail event (Rails R11).

Feedback rows are rail events of kind ``feedback.recorded``. A row names the
rail event it comments on (``payload.target_event_id``). The canonical /1
payload has no feedback_id, revision or operation fields. Store feedback_id
as ``source.record_id`` and ``r11:<operation>:<revision>:<provenance hash>``
as ``source.revision``. The hash binds the caller's sanitized source identity
for idempotency. Clearing appends a marker revision; it never deletes history.

Idempotency: every write carries an ``idempotency_key`` translated to a ledger
dedupe key. A retry returns the existing row with ``duplicated=True``; the same
key with different semantic bytes is refused with
:class:`FeedbackConflictRefusal`. Editing or clearing from an outdated
``base_revision`` is refused with :class:`StaleEditRefusal`, and an unknown
rail event or feedback id is refused with :class:`TargetMissingRefusal`.
"""

import hashlib
import json
import re
import sqlite3

from rizzstack_rails import contracts, privacy
from rizzstack_rails.ledger import _scan_raw_fields

FEEDBACK_KIND = "feedback.recorded"
INTERVENTION_KINDS = ("intervention.requested", "intervention.result")

_RECORD = "record"
_EDIT = "edit"
_CLEAR = "clear"

_DEFAULT_CATEGORY = "quality"
_R11_REVISION = re.compile(r"r11:(record|edit|clear):([1-9][0-9]{0,15}):[0-9a-f]{64}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}")
_ADDRESS = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|::|\.(?:local|internal|lan|com|org|net)\b", re.I)
# Human notes are short, single-line plain text, not a transport for URLs,
# paths, bindings, transcripts or terminal escapes. Fail closed on rich text.
_PLAIN_NOTE = re.compile(r"[\w .,?!'()+@=:\-]*")
_PRIVATE_DETAIL = re.compile(r"\w[.=@]\w|::|\b\w+\s*[:=]\s*\S")


class FeedbackRefusal(contracts.RailsRefusal):
    def __init__(self, message, code="feedback"):
        super().__init__(message, code=code)


class FeedbackSizeRefusal(FeedbackRefusal):
    status_code = 413

    def __init__(self, message):
        super().__init__(message, code="too_large")


class FeedbackBusyRefusal(FeedbackRefusal):
    status_code = 503

    def __init__(self, message):
        super().__init__(message, code="feedback_busy")


class FeedbackConflictRefusal(FeedbackRefusal):
    status_code = 409

    def __init__(self, message):
        super().__init__(message, code="feedback_conflict")


class StaleEditRefusal(FeedbackRefusal):
    status_code = 409

    def __init__(self, message):
        super().__init__(message, code="stale_edit")


class TargetMissingRefusal(FeedbackRefusal):
    status_code = 404

    def __init__(self, message):
        super().__init__(message, code="target_missing")


def _require_id(value, field):
    # Refuse unsafe keys rather than redacting two identities to the same one.
    if (not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value)
            or _ADDRESS.search(value) or privacy.scrub_text(value) != value):
        raise FeedbackRefusal(field + " must be a public opaque identifier (1-192 characters)")
    return value


def preview_note(note):
    """Preview sanitized text. None means unspecified (inherit on edit)."""
    if note is None:
        return None
    if not isinstance(note, str) or len(note) > contracts.FEEDBACK_NOTE_MAX:
        raise FeedbackRefusal("note must be a string of at most 2000 characters or null")
    if (privacy.scrub_text(note) != note or not _PLAIN_NOTE.fullmatch(note)
            or _PRIVATE_DETAIL.search(note)):
        return privacy.REDACTED
    return note


def _feedback_id_for(target_event_id, idempotency_key):
    digest = hashlib.sha256(
        (target_event_id + "\n" + idempotency_key).encode("utf-8")
    ).hexdigest()[:20]
    return "fb:" + digest


def _source_for(feedback_id, revision, operation, source):
    if source is None:
        source = {"adapter": "rizzstack_rails.feedback", "instance": "local",
                  "record_id": feedback_id, "revision": "r" + str(revision)}
    if not isinstance(source, dict) or set(source) != set(contracts.SOURCE_FIELDS):
        raise FeedbackRefusal("source must carry the four public source fields", code="source")
    if not all(isinstance(v, str) and 1 <= len(v) <= 2000 for v in source.values()):
        raise FeedbackRefusal("invalid source values", code="source")
    contracts.validate_canonical_value(source)
    # Never derive a public source fingerprint from a credential-bearing path.
    if privacy.scrub_text(source["record_id"]) != source["record_id"]:
        raise FeedbackRefusal("source record_id must be public", code="source")
    source = privacy.scrub_source(source)
    for value in source.values():
        _require_id(value, "source value")
    digest = hashlib.sha256(contracts.canonicalize(source)).hexdigest()
    return dict(source, record_id=feedback_id, revision=f"r11:{operation}:{revision}:{digest}")


def _row_from_envelope(envelope):
    payload = envelope.get("payload") or {}
    revision = payload.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int):
        revision = 1
    operation = payload.get("operation") or _RECORD
    feedback_id = payload.get("feedback_id")
    source = envelope.get("source") or {}
    tag = _R11_REVISION.fullmatch(source.get("revision", ""))
    if tag:
        operation, revision = tag[1], int(tag[2])
        feedback_id = source["record_id"]
    return {
        "event_id": envelope.get("event_id"),
        "seq": envelope.get("seq"),
        "hash": envelope.get("hash"),
        "recorded_at": envelope.get("recorded_at"),
        "chain_id": envelope.get("chain_id"),
        "feedback_id": feedback_id,
        "target_event_id": payload.get("target_event_id"),
        "revision": revision,
        "operation": operation,
        "rating": payload.get("rating"),
        "category": payload.get("category"),
        "note": payload.get("note"),
        "cleared": operation == _CLEAR,
        "supersedes_event_id": payload.get("supersedes_event_id"),
        "scope": envelope.get("scope") or {},
        "source": envelope.get("source") or {},
    }


# Also reduce original, pre-freeze R11 overlay rows if present. Never treat a
# producer's arbitrary source.record_id on a generic feedback event as R11.
_OVERLAY_ID_SQL = ("CASE WHEN json_extract(envelope_json, '$.source.revision') GLOB 'r11:*' "
                   "THEN json_extract(envelope_json, '$.source.record_id') "
                   "ELSE json_extract(envelope_json, '$.payload.feedback_id') END")


def _latest(ledger, feedback_id):
    rows = ledger.query(
        "SELECT envelope_json FROM event_rows WHERE kind = ?"
        " AND " + _OVERLAY_ID_SQL + " = ?"
        " ORDER BY seq DESC LIMIT 1", (FEEDBACK_KIND, feedback_id),
    )
    return _row_from_envelope(json.loads(rows[0]["envelope_json"])) if rows else None


def _find_event(ledger, event_id):
    rows = ledger.query(
        "SELECT envelope_json FROM event_rows WHERE event_id = ?", (event_id,)
    )
    if not rows:
        return None
    return _row_from_envelope(json.loads(rows[0]["envelope_json"]))


def _existing_for_key(ledger, dedupe_key):
    rows = ledger.query(
        "SELECT event_id FROM dedupe_index WHERE chain_id = ? AND dedupe_key = ?",
        (ledger.chain_id, dedupe_key),
    )
    if not rows:
        return None
    return _find_event(ledger, rows[0]["event_id"])


def _mutate(ledger, operation, feedback_id, target_event_id, base_revision, idempotency_key,
            scope, source, authority, rating, category, note, lane, authorize_new_record=None):
    """CAS and dedupe run under the same BEGIN IMMEDIATE as the event append."""
    ledger._ensure_writable()
    ledger._probe_capacity()
    if not isinstance(authority, str) or authority not in contracts.AUTHORITIES:
        raise FeedbackRefusal("invalid feedback authority", code="authority")
    ledger._check_authority(authority)
    dedupe_key = "feedback:" + _require_id(idempotency_key, "idempotency_key")
    if len(dedupe_key) > 192:
        raise FeedbackRefusal("idempotency_key exceeds 183 characters")
    if operation == _RECORD:
        _require_id(target_event_id, "target_event_id")
        if feedback_id is None:
            feedback_id = _feedback_id_for(target_event_id, idempotency_key)
    elif type(base_revision) is not int or not 1 <= base_revision < contracts.INT_LIMIT:
        raise FeedbackRefusal("base_revision must be a positive integer")
    _require_id(feedback_id, "feedback_id")
    revision = base_revision + 1
    # Keep the R11-specific provenance, public-ID and note rules ahead of CAS.
    source = _source_for(feedback_id, revision, operation, source)
    _scan_raw_fields([scope, lane, rating, category, note])
    for meta in (scope, lane):
        if isinstance(meta, dict):
            for field, value in meta.items():
                if value is not None:
                    if meta is scope and field in ("repo", "repo_slug") and isinstance(value, str):
                        for part in value.split("/"):
                            _require_id(part, "repo segment")
                        continue
                    _require_id(value, "metadata value")
    note = preview_note(note)

    def work(conn):
        # ledger.query reads this same connection and therefore the locked state.
        previous = _existing_for_key(ledger, dedupe_key)
        if previous is not None:
            if (previous["feedback_id"] != feedback_id or previous["operation"] != operation
                    or previous["revision"] != revision or (operation == _RECORD
                    and previous["target_event_id"] != target_event_id)):
                raise FeedbackConflictRefusal("idempotency key names a different mutation")
            # A retry may arrive many revisions later. Rebuild its semantic
            # bytes from the immutable *original* parent, never today's head.
            base = (_find_event(ledger, previous["supersedes_event_id"])
                    if operation != _RECORD else None)
            if operation != _RECORD and base is None:
                raise FeedbackConflictRefusal("feedback revision has no recorded parent")
        else:
            base = _latest(ledger, feedback_id)
            if operation == _RECORD and base is not None:
                raise FeedbackConflictRefusal("feedback already exists; use edit")
            if operation != _RECORD:
                if base is None:
                    raise TargetMissingRefusal("unknown feedback")
                if base["revision"] != base_revision:
                    raise StaleEditRefusal("feedback is at revision %d" % base["revision"])
        payload = {"target_event_id": base["target_event_id"] if base else target_event_id,
                   "supersedes_event_id": base["event_id"] if base else None,
                   "actor": authority, "rating": rating, "category": category, "note": note}
        if operation == _EDIT:
            for key in ("rating", "category", "note"):
                if payload[key] is None:
                    payload[key] = base[key]
        elif operation == _CLEAR:
            payload.update(category=base["category"] or _DEFAULT_CATEGORY,
                           note="cleared" if note is None else note)
        payload["note"] = preview_note(payload["note"]) or ""
        # The ledger's locked append enforces the canonical schema, recursive
        # scrubbing, target existence, semantic dedupe, and both chain hashes.
        receipt = ledger._append_locked(conn, FEEDBACK_KIND, source, scope, payload, authority,
                                        dedupe_key, lane, None, None, "unknown", None)
        # Only an exact semantic replay can return duplicated=True. Anything
        # newly staged (including its dedupe entry) rolls back on refusal.
        if authorize_new_record is not None and not receipt['duplicated']:
            authorize_new_record()
        return dict(receipt, feedback_id=feedback_id, revision=revision,
                    operation=operation, cleared=operation == _CLEAR)

    try:
        return ledger._write_tx(work)
    except contracts.RailsRefusal as exc:
        if exc.code == "target":
            raise TargetMissingRefusal("unknown target event") from None
        if exc.code == "idempotency_conflict":
            raise FeedbackConflictRefusal("idempotency key names a different mutation") from None
        raise
    except sqlite3.OperationalError as exc:
        if (getattr(exc, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise FeedbackBusyRefusal("feedback writer is busy; retry with the same key") from None
        raise


def record(ledger, target_event_id, *, idempotency_key, scope, source=None,
           authority="owner_record", rating=None, category=_DEFAULT_CATEGORY,
           note=None, feedback_id=None, lane=None, authorize_new_record=None):
    """Record feedback against one existing rail event.

    The returned row is keyed by ``feedback_id``. Repeating the call with the
    same ``idempotency_key`` returns the original event id with
    ``duplicated=True``; the ledger holds exactly one row. If provided,
    ``authorize_new_record()`` runs inside the writer transaction just before
    a first record is committed. It must raise if authority is no longer valid.
    """
    return _mutate(ledger, _RECORD, feedback_id, target_event_id, 0, idempotency_key,
                   scope, source, authority, rating, category, note, lane, authorize_new_record)


def edit(ledger, feedback_id, *, base_revision, idempotency_key, scope,
         source=None, authority="owner_record", rating=None,
         category=None, note=None, lane=None):
    """Append a new revision of ``feedback_id`` on top of ``base_revision``."""
    return _mutate(ledger, _EDIT, feedback_id, None, base_revision, idempotency_key,
                   scope, source, authority, rating, category, note, lane)


def clear(ledger, feedback_id, *, base_revision, idempotency_key, scope,
          source=None, authority="owner_record", note=None, lane=None):
    """Append a clear marker; prior revisions stay in history untouched."""
    return _mutate(ledger, _CLEAR, feedback_id, None, base_revision, idempotency_key,
                   scope, source, authority, None, None, note, lane)


def current(ledger, feedback_id):
    """Return the latest revision for ``feedback_id``, or ``None``."""
    return _latest(ledger, _require_id(feedback_id, "feedback_id"))


def _envelopes(ledger, kinds, target_event_id, feedback_id, limit):
    # A bounded read must not materialize all history, or an arbitrarily large
    # legacy envelope. 32 KiB fits an R11 row even with escaped Unicode notes.
    column = "envelope_json"
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= 501:
            raise FeedbackRefusal("history limit must be between 1 and 501")
        column = "CASE WHEN length(envelope_json) <= 32768 THEN envelope_json END"
    sql = "SELECT %s AS body FROM event_rows WHERE kind IN (%s)" % (
        column, ",".join("?" for _ in kinds))
    params = list(kinds)
    if kinds == (FEEDBACK_KIND,):
        sql += " AND (" + _OVERLAY_ID_SQL + ") IS NOT NULL"
    if target_event_id is not None:
        sql += " AND json_extract(envelope_json, '$.payload.target_event_id') = ?"
        params.append(_require_id(target_event_id, "target_event_id"))
    if feedback_id is not None:
        sql += " AND " + _OVERLAY_ID_SQL + " = ?"
        params.append(_require_id(feedback_id, "feedback_id"))
    sql += " ORDER BY seq"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    for row in ledger.query(sql, params):
        if row["body"] is None:
            raise FeedbackSizeRefusal("record exceeds bounded history size")
        yield json.loads(row["body"])


def history(ledger, target_event_id=None, feedback_id=None, *, limit=None):
    """Return local overlay revisions, including clear markers, in append order."""
    return [_row_from_envelope(e) for e in
            _envelopes(ledger, (FEEDBACK_KIND,), target_event_id, feedback_id, limit)]


def intervention_events(ledger, *, target_event_id=None, limit=None):
    """Return requested/result intervention events in append order."""
    events = []
    for envelope in _envelopes(ledger, INTERVENTION_KINDS, target_event_id, None, limit):
        payload = envelope.get("payload") or {}
        events.append(
            {
                "event_id": envelope.get("event_id"),
                "seq": envelope.get("seq"),
                "recorded_at": envelope.get("recorded_at"),
                "kind": envelope.get("kind"),
                "request_id": payload.get("request_id"),
                "target_event_id": payload.get("target_event_id"),
                "operation": payload.get("operation"),
                "decision": payload.get("decision"),
                "authority_ref": payload.get("authority_ref"),
                "queue_action_id": payload.get("queue_action_id"),
                "reason": payload.get("reason"),
            }
        )
    return events


def interventions_by_id(ledger):
    """Aggregate intervention events by request id; each id appears once."""
    counted = {}
    for event in intervention_events(ledger):
        request_id = event["request_id"]
        if not request_id:
            continue
        entry = counted.get(request_id)
        if entry is None:
            entry = {
                "request_id": request_id,
                "event_count": 0,
                "event_ids": [],
                "kinds": [],
                "decisions": [],
            }
            counted[request_id] = entry
        entry["event_count"] += 1
        entry["event_ids"].append(event["event_id"])
        entry["kinds"].append(event["kind"])
        entry["decisions"].append(event["decision"])
    return counted


def distinct_interventions(ledger):
    """Count interventions once per request id, regardless of event volume."""
    return len(interventions_by_id(ledger))
