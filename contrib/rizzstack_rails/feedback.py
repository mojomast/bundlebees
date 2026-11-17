"""Feedback overlay keyed by rail event (Rails R11).

Feedback rows are rail events of kind ``feedback.recorded``. A row names the
rail event it comments on (``target_event_id``), a stable ``feedback_id`` and
a monotonic ``revision``. Clearing appends a marker revision; it never deletes
or rewrites history.

Idempotency: every write carries an ``idempotency_key`` translated to a ledger
dedupe key. A retry returns the existing row with ``duplicated=True``; the same
key with different semantic bytes is refused with
:class:`FeedbackConflictRefusal`. Editing or clearing from an outdated
``base_revision`` is refused with :class:`StaleEditRefusal`, and an unknown
rail event or feedback id is refused with :class:`TargetMissingRefusal`.
"""

import hashlib
import json

from rizzstack_rails import contracts

FEEDBACK_KIND = "feedback.recorded"
INTERVENTION_KINDS = ("intervention.requested", "intervention.result")

_RECORD = "record"
_EDIT = "edit"
_CLEAR = "clear"

_DEFAULT_CATEGORY = "quality"


class FeedbackRefusal(contracts.RailsRefusal):
    def __init__(self, message, code="feedback"):
        super().__init__(message, code=code)


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


def _require_key(idempotency_key):
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise FeedbackRefusal("feedback requires a nonempty idempotency_key")
    return "feedback:" + idempotency_key


def _feedback_id_for(target_event_id, idempotency_key):
    digest = hashlib.sha256(
        (target_event_id + "\n" + idempotency_key).encode("utf-8")
    ).hexdigest()[:20]
    return "fb:" + digest


def _source_for(feedback_id, revision, source):
    if source is not None:
        return source
    return {
        "adapter": "rizzstack_rails.feedback",
        "instance": "local",
        "record_id": feedback_id,
        "revision": "r" + str(revision),
    }


def _target_exists(ledger, target_event_id):
    if not isinstance(target_event_id, str) or not target_event_id:
        return False
    rows = ledger.query(
        "SELECT 1 FROM event_rows WHERE event_id = ?", (target_event_id,)
    )
    if rows:
        return True
    rows = ledger.query(
        "SELECT 1 FROM imported_origin_rows WHERE event_id = ?", (target_event_id,)
    )
    return bool(rows)


def _row_from_envelope(envelope):
    payload = envelope.get("payload") or {}
    revision = payload.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int):
        revision = 1
    return {
        "event_id": envelope.get("event_id"),
        "seq": envelope.get("seq"),
        "hash": envelope.get("hash"),
        "recorded_at": envelope.get("recorded_at"),
        "chain_id": envelope.get("chain_id"),
        "feedback_id": payload.get("feedback_id"),
        "target_event_id": payload.get("target_event_id"),
        "revision": revision,
        "operation": payload.get("operation") or _RECORD,
        "rating": payload.get("rating"),
        "category": payload.get("category"),
        "note": payload.get("note"),
        "cleared": bool(payload.get("cleared", False)),
        "supersedes_event_id": payload.get("supersedes_event_id"),
        "scope": envelope.get("scope") or {},
        "source": envelope.get("source") or {},
    }


def _feedback_events(ledger):
    rows = ledger.query(
        "SELECT envelope_json FROM event_rows WHERE kind = ? ORDER BY seq",
        (FEEDBACK_KIND,),
    )
    events = []
    for row in rows:
        envelope = json.loads(row["envelope_json"])
        event = _row_from_envelope(envelope)
        if event["feedback_id"]:
            events.append(event)
    return events


def _latest(ledger, feedback_id):
    matches = [row for row in _feedback_events(ledger) if row["feedback_id"] == feedback_id]
    if not matches:
        return None
    return max(matches, key=lambda row: (row["revision"], row["seq"]))


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


def _result(row, duplicated):
    return {
        "event_id": row["event_id"],
        "seq": row["seq"],
        "hash": row["hash"],
        "recorded_at": row["recorded_at"],
        "chain_id": row["chain_id"],
        "feedback_id": row["feedback_id"],
        "revision": row["revision"],
        "operation": row["operation"],
        "cleared": row["cleared"],
        "duplicated": duplicated,
    }


def _append(ledger, payload, scope, source, authority, dedupe_key, lane):
    try:
        result = ledger.append(
            kind=FEEDBACK_KIND,
            source=_source_for(payload["feedback_id"], payload["revision"], source),
            scope=scope,
            payload=payload,
            authority=authority,
            dedupe_key=dedupe_key,
            lane=lane,
        )
    except contracts.RailsRefusal as exc:
        code = getattr(exc, "code", None)
        if code == "target":
            raise TargetMissingRefusal(str(exc)) from exc
        if code == "idempotency_conflict":
            raise FeedbackConflictRefusal(str(exc)) from exc
        raise
    row = _find_event(ledger, result["event_id"])
    if row is None:
        row = {
            "event_id": result["event_id"],
            "seq": result["seq"],
            "hash": result["hash"],
            "recorded_at": result["recorded_at"],
            "chain_id": result["chain_id"],
            "feedback_id": payload["feedback_id"],
            "target_event_id": payload["target_event_id"],
            "revision": payload["revision"],
            "operation": payload["operation"],
            "cleared": bool(payload.get("cleared")),
        }
    return _result(row, result["duplicated"])


def record(ledger, target_event_id, *, idempotency_key, scope, source=None,
           authority="owner_record", rating=None, category=_DEFAULT_CATEGORY,
           note=None, feedback_id=None, lane=None):
    """Record feedback against one existing rail event.

    The returned row is keyed by ``feedback_id``. Repeating the call with the
    same ``idempotency_key`` returns the original event id with
    ``duplicated=True``; the ledger holds exactly one row.
    """
    dedupe_key = _require_key(idempotency_key)
    if not _target_exists(ledger, target_event_id):
        raise TargetMissingRefusal("unknown target event %r" % (target_event_id,))
    if feedback_id is None:
        feedback_id = _feedback_id_for(target_event_id, idempotency_key)
    if not isinstance(feedback_id, str) or not feedback_id:
        raise FeedbackRefusal("feedback_id must be a nonempty string")
    if _existing_for_key(ledger, dedupe_key) is None and _latest(ledger, feedback_id) is not None:
        raise FeedbackConflictRefusal(
            "feedback %r already exists; use edit for a new revision" % (feedback_id,)
        )
    payload = {
        "target_event_id": target_event_id,
        "feedback_id": feedback_id,
        "revision": 1,
        "operation": _RECORD,
        "rating": rating,
        "category": category,
        "note": note,
        "cleared": False,
        "supersedes_event_id": None,
    }
    return _append(ledger, payload, scope, source, authority, dedupe_key, lane)


def edit(ledger, feedback_id, *, base_revision, idempotency_key, scope,
         source=None, authority="owner_record", rating=None,
         category=None, note=None, lane=None):
    """Append a new revision of ``feedback_id`` on top of ``base_revision``."""
    dedupe_key = _require_key(idempotency_key)
    existing = _existing_for_key(ledger, dedupe_key)
    if existing is not None:
        if existing["feedback_id"] != feedback_id:
            raise FeedbackConflictRefusal(
                "idempotency key already names feedback %r" % (existing["feedback_id"],)
            )
        return _result(existing, True)
    latest = _latest(ledger, feedback_id)
    if latest is None:
        raise TargetMissingRefusal("unknown feedback %r" % (feedback_id,))
    if base_revision != latest["revision"]:
        raise StaleEditRefusal(
            "feedback %r is at revision %s, not %s"
            % (feedback_id, latest["revision"], base_revision)
        )
    payload = {
        "target_event_id": latest["target_event_id"],
        "feedback_id": feedback_id,
        "revision": latest["revision"] + 1,
        "operation": _EDIT,
        "rating": latest["rating"] if rating is None else rating,
        "category": latest["category"] if category is None else category,
        "note": latest["note"] if note is None else note,
        "cleared": False,
        "supersedes_event_id": latest["event_id"],
    }
    return _append(ledger, payload, scope, source, authority, dedupe_key, lane)


def clear(ledger, feedback_id, *, base_revision, idempotency_key, scope,
          source=None, authority="owner_record", note=None, lane=None):
    """Append a clear marker; prior revisions stay in history untouched."""
    dedupe_key = _require_key(idempotency_key)
    existing = _existing_for_key(ledger, dedupe_key)
    if existing is not None:
        if existing["feedback_id"] != feedback_id:
            raise FeedbackConflictRefusal(
                "idempotency key already names feedback %r" % (existing["feedback_id"],)
            )
        return _result(existing, True)
    latest = _latest(ledger, feedback_id)
    if latest is None:
        raise TargetMissingRefusal("unknown feedback %r" % (feedback_id,))
    if base_revision != latest["revision"]:
        raise StaleEditRefusal(
            "feedback %r is at revision %s, not %s"
            % (feedback_id, latest["revision"], base_revision)
        )
    payload = {
        "target_event_id": latest["target_event_id"],
        "feedback_id": feedback_id,
        "revision": latest["revision"] + 1,
        "operation": _CLEAR,
        "rating": None,
        "category": latest["category"] or _DEFAULT_CATEGORY,
        "note": "cleared" if note is None else note,
        "cleared": True,
        "supersedes_event_id": latest["event_id"],
    }
    return _append(ledger, payload, scope, source, authority, dedupe_key, lane)


def current(ledger, feedback_id):
    """Return the latest revision for ``feedback_id``, or ``None``."""
    return _latest(ledger, feedback_id)


def history(ledger, target_event_id=None, feedback_id=None):
    """Return every feedback revision in append order (never filtered out)."""
    rows = _feedback_events(ledger)
    if target_event_id is not None:
        rows = [row for row in rows if row["target_event_id"] == target_event_id]
    if feedback_id is not None:
        rows = [row for row in rows if row["feedback_id"] == feedback_id]
    return rows


def intervention_events(ledger):
    """Return requested/result intervention events in append order."""
    rows = ledger.query(
        "SELECT envelope_json FROM event_rows WHERE kind IN (?, ?) ORDER BY seq",
        INTERVENTION_KINDS,
    )
    events = []
    for row in rows:
        envelope = json.loads(row["envelope_json"])
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
