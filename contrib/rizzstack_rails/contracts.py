"""Frozen contract layer for the rizzstack rails ledger (schema rizzstack.rails/1).

Canonicalization, hash formulas, envelope value rules, the closed kind list
for /1, trust roster and per-kind payload rules originate here. The legacy
vibeline hash formula is pinned exactly as upstream (default separators,
default ensure_ascii, default=str, keys in LEGACY_CHAIN_KEYS order via
sort_keys).
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone

SCHEMA = "rizzstack.rails/1"

GENESIS = "genesis"

INT_LIMIT = 2 ** 53 - 1

KINDS = frozenset(
    {
        "lane.open",
        "lane.spawn",
        "lane.close",
        "tool.start",
        "tool.finish",
        "decision.recorded",
        "attempt.state",
        "job.state",
        "queue.transition",
        "lease.recorded",
        "verification.result",
        "review.result",
        "integration.result",
        "merge.result",
        "acceptance.result",
        "usage.observed",
        "feedback.recorded",
        "intervention.requested",
        "intervention.result",
        "history.superseded",
        "source.gap",
        "source.checkpoint",
        "source.error",
    }
)

AUTHORITIES = (
    "worker_report",
    "coordinator_record",
    "verifier_record",
    "reviewer_record",
    "owner_record",
    "imported_observation",
)

TIMING_QUALITY = ("monotonic_measured", "reported", "unknown")

OUTCOMES = ("pending", "ok", "failed", "cancelled", "unknown")

VERDICTS = ("PASS", "FAIL", "NOT_RUN", "BLOCKED", "UNVERIFIED", "EXPECTED_RED")

PHASES = ("red", "green", "refactor", "integration")

USAGE_BASES = ("delta", "cumulative", "final")

FEEDBACK_RATINGS = (-1, 0, 1, None)

FEEDBACK_CATEGORIES = ("quality", "intervention", "both")

FEEDBACK_NOTE_MAX = 2000

INTERVENTION_OPERATIONS = ("retry", "rewind_retry")

INTERVENTION_DECISIONS = ("pending", "approved", "declined", "blocked", "completed", "failed")

REDACTION_MODES = ("structured_safe",)

SOURCE_FIELDS = ("adapter", "instance", "record_id", "revision")

SCOPE_FIELDS = (
    "repo_slug",
    "campaign_id",
    "job_id",
    "task_id",
    "attempt_id",
    "session_id",
    "action_id",
    "contract_sha256",
    "code_rev",
)

GROUPING_IDS = (
    "repo_slug",
    "campaign_id",
    "job_id",
    "task_id",
    "attempt_id",
    "session_id",
    "action_id",
)

LANE_FIELDS = ("lane_id", "parent_lane_id", "parent_event_id")

RAW_FIELDS = (
    "raw_prompt",
    "raw_response",
    "raw_tool_output",
    "reasoning_transcript",
    "transcript_blob",
    "tool_argv",
)

REDACTION_FIELDS = ("mode", "path_detail", "notes")

LEGACY_CHAIN_KEYS = (
    "ts",
    "task_id",
    "actor",
    "parent",
    "kind",
    "ac_id",
    "result",
    "why",
    "tokens_in",
    "tokens_out",
    "sha",
    "prev_hash",
)

RAILS_HASH_PREFIX = b"rizzstack.rails/1\n"

SERVER_ASSIGNED_KEYS = ("event_id", "seq", "recorded_at", "prev_hash", "hash")

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

EVENT_ID_RE = re.compile(r"^.+:\d+$")


class RailsRefusal(Exception):
    status_code = 400

    def __init__(self, message, code="refused"):
        super().__init__(message)
        self.code = code


class CanonicalBytesRefusal(RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code="canonical_bytes")


class PayloadRefusal(RailsRefusal):
    def __init__(self, message, code="payload"):
        super().__init__(message, code=code)


def validate_canonical_value(value, where="value"):
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise CanonicalBytesRefusal("lone surrogate string at " + where)
        return
    if isinstance(value, int):
        if not (-INT_LIMIT <= value <= INT_LIMIT):
            raise CanonicalBytesRefusal("integer outside 2^53-1 at " + where)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_canonical_value(item, where + "." + str(index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalBytesRefusal("non-string object key at " + where)
            validate_canonical_value(item, where + "." + key)
        return
    raise CanonicalBytesRefusal("unsupported type %r at %s" % (type(value).__name__, where))


def canonicalize(obj):
    validate_canonical_value(obj)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _pairs_without_duplicates(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise CanonicalBytesRefusal("duplicate JSON key %r" % (key,))
        seen.add(key)
    return dict(pairs)


def canonicalize_text(text):
    try:
        parsed = json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except CanonicalBytesRefusal:
        raise
    except ValueError as exc:
        raise CanonicalBytesRefusal("unparsable JSON: %s" % (exc,))
    return canonicalize(parsed)


def validate_prev_hash(prev_hash):
    if prev_hash == GENESIS:
        return
    if not isinstance(prev_hash, str) or not HEX64_RE.match(prev_hash):
        raise RailsRefusal("prev_hash must be 'genesis' or 64 lowercase hex", code="hash")


def rails_hash(prev_hash, envelope):
    if not isinstance(envelope, dict):
        raise CanonicalBytesRefusal("rails hash input must be an object")
    if "hash" in envelope:
        raise RailsRefusal("envelope passed to rails_hash must not already carry a hash", code="hash")
    validate_prev_hash(prev_hash)
    blob = canonicalize(envelope)
    return hashlib.sha256(RAILS_HASH_PREFIX + prev_hash.encode("ascii") + b"\n" + blob).hexdigest()


def legacy_hash(prev_hash, row):
    blob = json.dumps(
        {key: row.get(key) for key in LEGACY_CHAIN_KEYS}, sort_keys=True, default=str
    ).encode()
    return hashlib.sha256(prev_hash.encode() + blob).hexdigest()


def semantic_digest(envelope):
    trimmed = {key: value for key, value in envelope.items() if key not in SERVER_ASSIGNED_KEYS}
    return hashlib.sha256(canonicalize(trimmed)).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def monotonic_span(start_monotonic):
    return int(round((time.monotonic() - start_monotonic) * 1000)), "monotonic_measured"


def _require_nonempty_str(payload, key, kind):
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PayloadRefusal("%s requires nonempty string %r" % (kind, key))
    return value


def _require_str_or_none(payload, key, kind):
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise PayloadRefusal("%s field %r must be a string or null" % (kind, key))
    return value


def _require_evidence_ref(payload, kind):
    value = payload.get("evidence_ref")
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) and item for item in value):
            raise PayloadRefusal("%s evidence_ref list must be nonempty strings" % kind)
        return
    _require_nonempty_str(payload, "evidence_ref", kind)


def _check_tool(payload, kind):
    if kind == "tool.finish" and payload.get("outcome") == "pending":
        raise PayloadRefusal("tool.finish outcome must not be pending")
    for key in ("call_id", "tool_name", "command_family", "summary"):
        _require_nonempty_str(payload, key, kind)
    if payload.get("outcome") not in OUTCOMES:
        raise PayloadRefusal("%s outcome must be one of %s" % (kind, ", ".join(OUTCOMES)))
    _require_evidence_ref(payload, kind)


def _check_verification(payload, kind):
    for key in ("ac_id", "run_id"):
        _require_nonempty_str(payload, key, kind)
    digest = payload.get("contract_sha256")
    if not isinstance(digest, str) or not HEX64_RE.match(digest):
        raise PayloadRefusal("%s contract_sha256 must be 64 lowercase hex" % kind)
    _require_nonempty_str(payload, "code_rev", kind)
    if payload.get("phase") not in PHASES:
        raise PayloadRefusal("%s phase must be one of %s" % (kind, ", ".join(PHASES)))
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise PayloadRefusal("%s verdict must be one of %s" % (kind, ", ".join(VERDICTS)))
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list) or not all(isinstance(item, str) and item for item in refs):
        raise PayloadRefusal("%s evidence_refs must be a list of strings" % kind)
    if verdict == "PASS" and not refs:
        raise PayloadRefusal("%s PASS requires nonempty evidence_refs" % kind)
    if verdict != "PASS":
        _require_nonempty_str(payload, "reason", kind)
    if verdict == "EXPECTED_RED" and not payload.get("frozen_test_intent"):
        raise PayloadRefusal("%s EXPECTED_RED requires frozen test intent" % kind)


def _check_review(payload, kind):
    for key in ("review_id", "reviewer", "author", "reviewed_head", "verdict", "basis"):
        _require_nonempty_str(payload, key, kind)
    if not isinstance(payload.get("independent"), bool):
        raise PayloadRefusal("%s independent must be a boolean" % kind)


def _check_state(payload, kind, require_states=True):
    _require_nonempty_str(payload, "operation_id", kind)
    epoch = payload.get("fencing_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        raise PayloadRefusal("%s fencing_epoch must be a nonnegative integer" % kind)
    _require_nonempty_str(payload, "code", kind)
    _require_evidence_ref(payload, kind)
    if require_states:
        _require_nonempty_str(payload, "from_state", kind)
        _require_nonempty_str(payload, "to_state", kind)


def _check_usage(payload, kind):
    _require_nonempty_str(payload, "usage_record_id", kind)
    _require_nonempty_str(payload, "metric_scope", kind)
    _require_nonempty_str(payload, "provider", kind)
    _require_nonempty_str(payload, "model", kind)
    if payload.get("basis") not in USAGE_BASES:
        raise PayloadRefusal("%s basis must be one of %s" % (kind, ", ".join(USAGE_BASES)))
    for key in ("tokens_in", "tokens_out", "tokens_thinking"):
        value = payload.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise PayloadRefusal("%s %s must be an integer or null" % (kind, key))


def _check_feedback(payload, kind):
    _require_nonempty_str(payload, "target_event_id", kind)
    rating = payload.get("rating", None)
    if rating is not None:
        if isinstance(rating, bool) or not isinstance(rating, int) or rating not in (-1, 0, 1):
            raise PayloadRefusal("%s rating must be -1, 0, 1 or null" % kind)
    note = payload.get("note")
    if note is not None:
        if not isinstance(note, str):
            raise PayloadRefusal("%s note must be a string" % kind)
        if len(note) > FEEDBACK_NOTE_MAX:
            raise PayloadRefusal("%s note must be at most %d characters" % (kind, FEEDBACK_NOTE_MAX))
    if payload.get("category") not in FEEDBACK_CATEGORIES:
        raise PayloadRefusal("%s category must be one of %s" % (kind, ", ".join(FEEDBACK_CATEGORIES)))
    _require_str_or_none(payload, "supersedes_event_id", kind)


def _check_intervention(payload, kind):
    for key in ("request_id", "target_event_id", "reason", "authority_ref"):
        _require_nonempty_str(payload, key, kind)
    if payload.get("operation") not in INTERVENTION_OPERATIONS:
        raise PayloadRefusal("%s operation must be one of %s" % (kind, ", ".join(INTERVENTION_OPERATIONS)))
    if payload.get("decision") not in INTERVENTION_DECISIONS:
        raise PayloadRefusal("%s decision must be one of %s" % (kind, ", ".join(INTERVENTION_DECISIONS)))
    _require_str_or_none(payload, "queue_action_id", kind)


def _check_history(payload, kind):
    old_ids = payload.get("old_event_ids")
    if not isinstance(old_ids, list) or not old_ids or not all(
        isinstance(item, str) and item for item in old_ids
    ):
        raise PayloadRefusal("%s old_event_ids must be a nonempty list of strings" % kind)
    for key in ("replacement_lane_id", "replacement_event_id", "operation_ref"):
        _require_nonempty_str(payload, key, kind)


def _check_lane(payload, kind):
    _require_nonempty_str(payload, "lane_id", kind)
    if kind == "lane.spawn":
        _require_nonempty_str(payload, "parent_lane_id", kind)
        _require_nonempty_str(payload, "parent_event_id", kind)


def _check_source(payload, kind):
    span = payload.get("range")
    if (
        not isinstance(span, (list, tuple))
        or len(span) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in span)
    ):
        raise PayloadRefusal("%s range must be a two-integer list" % kind)
    cursor = payload.get("cursor")
    if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int)):
        raise PayloadRefusal("%s cursor must be an integer or null" % kind)
    _require_nonempty_str(payload, "code", kind)
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise PayloadRefusal("%s count must be a nonnegative integer" % kind)
    _require_nonempty_str(payload, "digest", kind)


def _check_decision(payload, kind):
    _require_nonempty_str(payload, "decision", kind)


_KIND_RULES = {
    "tool.start": lambda payload, kind: _check_tool(payload, kind),
    "tool.finish": lambda payload, kind: _check_tool(payload, kind),
    "verification.result": _check_verification,
    "acceptance.result": _check_verification,
    "review.result": _check_review,
    "queue.transition": lambda payload, kind: _check_state(payload, kind, require_states=True),
    "job.state": lambda payload, kind: _check_state(payload, kind, require_states=True),
    "attempt.state": lambda payload, kind: _check_state(payload, kind, require_states=True),
    "lease.recorded": lambda payload, kind: _check_state(payload, kind, require_states=False),
    "usage.observed": _check_usage,
    "feedback.recorded": _check_feedback,
    "intervention.requested": _check_intervention,
    "intervention.result": _check_intervention,
    "history.superseded": _check_history,
    "lane.open": _check_lane,
    "lane.spawn": _check_lane,
    "lane.close": _check_lane,
    "source.gap": _check_source,
    "source.checkpoint": _check_source,
    "source.error": _check_source,
    "decision.recorded": _check_decision,
    "integration.result": lambda payload, kind: _require_nonempty_str(payload, "result", kind),
    "merge.result": lambda payload, kind: _require_nonempty_str(payload, "result", kind),
}


def _scan_raw_fields(value, where):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key in RAW_FIELDS:
                raise PayloadRefusal("raw ingestion field %r is refused at %s" % (key, where))
            _scan_raw_fields(item, where + "." + str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_raw_fields(item, where + "." + str(index))


def validate_event(
    kind,
    source,
    scope,
    payload,
    authority,
    lane,
    occurred_at,
    duration_ms,
    timing_quality,
    redaction_policy,
):
    if kind not in KINDS:
        raise PayloadRefusal("unknown kind %r; /1 kinds are closed" % (kind,), code="kind")
    if authority not in AUTHORITIES:
        raise PayloadRefusal("authority %r is not on the trust roster" % (authority,), code="authority")
    if not isinstance(source, dict) or set(source) - set(SOURCE_FIELDS) or set(source) != set(SOURCE_FIELDS):
        raise PayloadRefusal("source must carry exactly %s" % (", ".join(SOURCE_FIELDS)), code="source")
    for key in SOURCE_FIELDS:
        value = source.get(key)
        if not isinstance(value, str) or not value:
            raise PayloadRefusal("source.%s must be a nonempty string" % key, code="source")
    if not isinstance(scope, dict) or set(scope) - set(SCOPE_FIELDS):
        raise PayloadRefusal("scope keys must be a subset of %s" % (", ".join(SCOPE_FIELDS)), code="scope")
    for key in scope:
        value = scope[key]
        if value is not None and not isinstance(value, str):
            raise PayloadRefusal("scope.%s must be a string or null" % key, code="scope")
    if not any(scope.get(key) for key in GROUPING_IDS):
        raise PayloadRefusal("scope needs at least one grouping id", code="scope")
    if lane is None:
        lane = {}
    if not isinstance(lane, dict) or set(lane) - set(LANE_FIELDS):
        raise PayloadRefusal("lane keys must be a subset of %s" % (", ".join(LANE_FIELDS)), code="lane")
    for key in lane:
        value = lane[key]
        if value is not None and not isinstance(value, str):
            raise PayloadRefusal("lane.%s must be a string or null" % key, code="lane")
    if occurred_at is not None:
        if not isinstance(occurred_at, str) or not STAMP_RE.match(occurred_at):
            raise PayloadRefusal("occurred_at must be null or YYYY-MM-DDTHH:MM:SS.ffffffZ", code="timestamp")
    if duration_ms is not None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise PayloadRefusal("duration_ms must be a nonnegative integer or null", code="duration")
    if timing_quality not in TIMING_QUALITY:
        raise PayloadRefusal("timing_quality must be one of %s" % ", ".join(TIMING_QUALITY), code="timing")
    if redaction_policy is None:
        redaction_policy = {}
    if not isinstance(redaction_policy, dict) or set(redaction_policy) - set(REDACTION_FIELDS):
        raise PayloadRefusal(
            "redaction_policy keys must be a subset of %s" % ", ".join(REDACTION_FIELDS), code="redaction"
        )
    if "mode" in redaction_policy and redaction_policy["mode"] not in REDACTION_MODES:
        raise PayloadRefusal("redaction_policy mode must be one of %s" % ", ".join(REDACTION_MODES), code="redaction")
    if not isinstance(payload, dict):
        raise PayloadRefusal("payload must be an object", code="payload")
    _scan_raw_fields(payload, "payload")
    rule = _KIND_RULES.get(kind)
    if rule is not None:
        rule(payload, kind)
