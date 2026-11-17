"""Stdlib wire contract for Workflow's frozen event.schema + per-kind payload.schema.

Only producer *inputs* get the R02 spelling conversions. New envelopes are
closed; origin bytes and both hash formulas are independent of normalization.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone

SCHEMA = 'rizzstack.rails/1'
GENESIS = 'genesis'
INT_LIMIT = 2**53 - 1
MAX_RECORD = 65536
MAX_PAGE = 1048576
MAX_RECORDS = 500
POLICY = 'rails-safe/1'
OLD_POLICY = {'mode': 'structured_safe', 'path_detail': 'omitted'}

AUTHORITIES = ('worker_report', 'coordinator_record', 'verifier_record', 'reviewer_record',
               'owner_record', 'imported_observation')
TIMING_QUALITY = ('measured', 'estimated', 'unknown')
OUTCOMES = ('pending', 'ok', 'failed', 'cancelled', 'unknown')
VERDICTS = ('PASS', 'FAIL', 'NOT_RUN', 'BLOCKED', 'UNVERIFIED', 'EXPECTED_RED')
PHASES = ('red', 'green', 'refactor', 'integration')
USAGE_BASES = ('delta', 'cumulative', 'final')
FEEDBACK_RATINGS = (-1, 0, 1, None)
FEEDBACK_CATEGORIES = ('quality', 'intervention', 'both')
FEEDBACK_NOTE_MAX = 2000
INTERVENTION_OPERATIONS = ('retry', 'rewind_retry')
INTERVENTION_DECISIONS = ('pending', 'approved', 'declined', 'blocked', 'completed', 'failed')
SOURCE_FIELDS = ('adapter', 'instance', 'record_id', 'revision')
GROUPING_IDS = ('campaign_id', 'job_id', 'task_id', 'attempt_id', 'session_id', 'action_id')
SCOPE_FIELDS = ('repo', *GROUPING_IDS, 'contract_sha256', 'code_rev')
LANE_FIELDS = ('lane_id', 'parent_lane_id', 'parent_event_id')
RAW_FIELDS = frozenset('raw_prompt raw_response raw_tool_output reasoning_transcript '
                       'transcript_blob tool_argv prompt response transcript stdout stderr '
                       'tool_output argv raw_command_line'.split())
LEGACY_CHAIN_KEYS = ('ts', 'task_id', 'actor', 'parent', 'kind', 'ac_id', 'result', 'why',
                     'tokens_in', 'tokens_out', 'sha', 'prev_hash')
RAILS_HASH_PREFIX = b'rizzstack.rails/1\n'
SERVER_ASSIGNED_KEYS = ('event_id', 'seq', 'recorded_at', 'prev_hash', 'hash')
STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$', re.ASCII)
HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
EVENT_ID_RE = re.compile(r'^.+:\d+$')
ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,191}')
REV_RE = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
REPO_RE = re.compile(r'[A-Za-z0-9._-]+/[A-Za-z0-9._-]+')


class RailsRefusal(Exception):
    status_code = 400

    def __init__(self, message, code='refused'):
        super().__init__(message)
        self.code = code


class CanonicalBytesRefusal(RailsRefusal):
    def __init__(self, message):
        super().__init__(message, code='canonical_bytes')


class PayloadRefusal(RailsRefusal):
    def __init__(self, message, code='payload'):
        super().__init__(message, code=code)


def validate_canonical_value(value, where='value'):
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            raise CanonicalBytesRefusal('lone surrogate string') from None
        return
    if isinstance(value, int) and -INT_LIMIT <= value <= INT_LIMIT:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_canonical_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalBytesRefusal('non-string object key')
            validate_canonical_value(key)
            validate_canonical_value(item)
        return
    raise CanonicalBytesRefusal('unsupported canonical value')


def canonicalize(obj):
    try:
        validate_canonical_value(obj)
        return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                          allow_nan=False).encode('ascii')
    except RecursionError:
        raise CanonicalBytesRefusal('excessive nesting') from None


def _pairs_without_duplicates(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise CanonicalBytesRefusal('duplicate JSON key')
    return obj


def parse_text(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs_without_duplicates)
    except (ValueError, RecursionError):
        raise CanonicalBytesRefusal('invalid JSON') from None


def canonicalize_text(text):
    return canonicalize(parse_text(text))


def validate_prev_hash(prev_hash):
    if prev_hash != GENESIS and not _matches(HEX64_RE, prev_hash):
        raise RailsRefusal("prev_hash must be 'genesis' or 64 lowercase hex", code='hash')


def rails_hash(prev_hash, envelope):
    if not isinstance(envelope, dict):
        raise CanonicalBytesRefusal('rails hash input must be an object')
    if 'hash' in envelope:
        raise RailsRefusal('hash present in its own preimage', code='hash')
    validate_prev_hash(prev_hash)
    return hashlib.sha256(RAILS_HASH_PREFIX + prev_hash.encode('ascii') + b'\n' +
                          canonicalize(envelope)).hexdigest()


def legacy_hash(prev_hash, row):
    blob = json.dumps({key: row.get(key) for key in LEGACY_CHAIN_KEYS},
                      sort_keys=True, default=str).encode()
    return hashlib.sha256(prev_hash.encode() + blob).hexdigest()


def semantic_digest(envelope):
    trimmed = {key: value for key, value in envelope.items() if key not in SERVER_ASSIGNED_KEYS}
    return hashlib.sha256(canonicalize(trimmed)).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + 'Z'


def monotonic_span(start_monotonic):
    return int(round((time.monotonic() - start_monotonic) * 1000)), 'monotonic_measured'


# One closed field set per kind, with shared value rules below. Optional
# properties aren't blanket extensions to every event with the same spelling.
_TOOL = 'call_id tool_name command_family outcome summary evidence_ref'
_STATE = 'from_state to_state operation_id fencing_epoch code evidence_ref'
_VERIFICATION = 'ac_id contract_sha256 code_rev run_id phase verdict evidence_refs reason'
_INTERVENTION = 'request_id target_event_id operation queue_action_id decision reason authority_ref'
_SOURCE = 'source_id cursor range_start range_end code count digest'
_REQUIRED = {
    'lane.open': 'status', 'lane.close': 'status', 'lane.spawn': 'child_lane_id',
    'decision.recorded': 'summary', 'tool.start': _TOOL, 'tool.finish': _TOOL,
    'attempt.state': _STATE, 'job.state': _STATE, 'queue.transition': _STATE,
    'lease.recorded': _STATE, 'verification.result': _VERIFICATION,
    'acceptance.result': _VERIFICATION,
    'review.result': 'review_id reviewer author reviewed_head review_verdict independent basis',
    'integration.result': 'status summary', 'merge.result': 'status summary',
    'usage.observed': 'usage_record_id metric_scope input_tokens output_tokens thinking_tokens '
                      'usage_basis provider model',
    'feedback.recorded': 'target_event_id rating note category actor supersedes_event_id',
    'intervention.requested': _INTERVENTION, 'intervention.result': _INTERVENTION,
    'history.superseded': 'old_event_ids replacement_event_id replacement_lane_id operation_id',
    'source.gap': _SOURCE, 'source.error': _SOURCE, 'source.checkpoint': _SOURCE,
}
KINDS = frozenset(_REQUIRED)
_OPTIONAL = {'verification.result': ('expected_intent_ref',), 'acceptance.result': ('expected_intent_ref',),
             'integration.result': ('reason', 'evidence_refs'), 'merge.result': ('reason', 'evidence_refs')}
_TEXT = {'command_family': 80, 'summary': 1000, 'reason': 1000, 'basis': 200,
         'note': FEEDBACK_NOTE_MAX, 'cursor': 1024}
_NULLABLE = frozenset('command_family evidence_ref from_state input_tokens output_tokens '
                      'thinking_tokens provider model supersedes_event_id queue_action_id authority_ref '
                      'replacement_event_id replacement_lane_id cursor range_start range_end digest'.split())
_COUNTERS = frozenset('range_start range_end count fencing_epoch input_tokens output_tokens thinking_tokens'.split())
_ENUMS = dict(outcome=OUTCOMES, verdict=VERDICTS, phase=PHASES, operation=INTERVENTION_OPERATIONS,
              decision=INTERVENTION_DECISIONS, category=FEEDBACK_CATEGORIES, usage_basis=USAGE_BASES,
              review_verdict=('approve', 'approve_with_followups', 'changes_requested', 'not_run'))


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def require_id(value, field):
    if not _matches(ID_RE, value):
        raise PayloadRefusal('expected bounded public ID: ' + field, code=field)


def _scan_raw_fields(value, where='value', depth=0):
    """Shared import/append guard; exceptions never contain source text."""
    if depth > 16:
        raise PayloadRefusal('record exceeds nesting limit', code='raw_ingestion')
    if isinstance(value, dict):
        if any(isinstance(k, str) and k.casefold() in RAW_FIELDS for k in value):
            raise PayloadRefusal('raw content field refused', code='raw_ingestion')
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        return
    for child in children:
        _scan_raw_fields(child, where, depth + 1)


def _field_ok(key, v):
    if v is None and key in _NULLABLE:
        return True
    if key in _TEXT:
        return isinstance(v, str) and len(v) <= _TEXT[key]
    if key in _ENUMS:
        return isinstance(v, str) and v in _ENUMS[key]
    if key in _COUNTERS:
        return type(v) is int and 0 <= v <= INT_LIMIT
    if key in ('contract_sha256', 'digest'):
        return _matches(HEX64_RE, v)
    if key in ('code_rev', 'reviewed_head'):
        return _matches(REV_RE, v)
    if key == 'independent':
        return isinstance(v, bool)
    if key == 'rating':
        return v is None or type(v) is int and v in (-1, 0, 1)
    if key in ('evidence_refs', 'old_event_ids'):
        lo, hi = (1, 500) if key == 'old_event_ids' else (0, 100)
        return isinstance(v, list) and lo <= len(v) <= hi and all(_matches(ID_RE, x) for x in v)
    return _matches(ID_RE, v)


def _validate_payload(kind, payload):
    required = set(_REQUIRED[kind].split())
    if not required <= payload.keys() <= required.union(_OPTIONAL.get(kind, ())):
        raise PayloadRefusal('missing or unexpected fields for ' + kind)
    for key, value in payload.items():
        if not _field_ok(key, value):
            raise PayloadRefusal('invalid ' + kind + ' field: ' + key)
    if kind.startswith('tool.') and (kind == 'tool.start') != (payload['outcome'] == 'pending'):
        raise PayloadRefusal('only tool.start has pending outcome')
    if kind in ('verification.result', 'acceptance.result'):
        verdict = payload['verdict']
        if verdict == 'PASS' and not payload['evidence_refs'] or verdict != 'PASS' and not payload['reason']:
            raise PayloadRefusal('verdict lacks required evidence or reason')
        if verdict == 'EXPECTED_RED' and not payload.get('expected_intent_ref'):
            raise PayloadRefusal('EXPECTED_RED requires frozen test intent reference')
    if kind == 'review.result' and payload['independent'] and not payload['basis']:
        raise PayloadRefusal('independent review requires a basis')


def _rename(obj, old, new):
    if old in obj:
        if new in obj and obj[old] != obj[new]:
            raise PayloadRefusal('conflicting legacy and canonical ' + new)
        obj[new] = obj.pop(old)


def _producer_payload(kind, p, source, scope, lane, authority):
    """Compatibility at append, never a repair of an origin's signed bytes."""
    if kind == 'decision.recorded' and 'decision' in p:
        summary = p.pop('decision')
        if not isinstance(summary, str):
            raise PayloadRefusal('invalid legacy decision')
        if 'summary' in p and p['summary'] != summary:
            if not isinstance(p['summary'], str):
                raise PayloadRefusal('invalid summary')
            summary += ': ' + p['summary']
        p['summary'] = summary
    elif kind.startswith('lane.'):
        for field in LANE_FIELDS:
            if field in p:
                if lane.get(field) is not None and lane[field] != p[field]:
                    raise PayloadRefusal('conflicting lane topology', code='lane')
                lane[field] = p.pop(field)
        if kind == 'lane.spawn' and lane.get('lane_id'):
            p.setdefault('child_lane_id', lane['lane_id'])
        elif kind != 'lane.spawn':
            p.setdefault('status', 'open' if kind == 'lane.open' else 'closed')
    elif kind == 'usage.observed':
        for old, new in (('basis', 'usage_basis'), ('tokens_in', 'input_tokens'),
                         ('tokens_out', 'output_tokens'), ('tokens_thinking', 'thinking_tokens')):
            _rename(p, old, new)
            if new != 'usage_basis':
                p.setdefault(new, None)
    elif kind == 'history.superseded':
        _rename(p, 'operation_ref', 'operation_id')
    elif kind in ('acceptance.result', 'verification.result'):
        _rename(p, 'frozen_test_intent', 'expected_intent_ref')
        if p.get('verdict') == 'PASS' and p.get('reason') is None:
            p['reason'] = ''
    elif kind == 'review.result' and 'verdict' in p:
        _rename(p, 'verdict', 'review_verdict')
        legacy = {'PASS': 'approve', 'FAIL': 'changes_requested', 'NOT_RUN': 'not_run', 'UNVERIFIED': 'not_run'}
        if isinstance(p['review_verdict'], str):
            p['review_verdict'] = legacy.get(p['review_verdict'], p['review_verdict'])
    elif kind in ('merge.result', 'integration.result'):
        _rename(p, 'result', 'status')
        p.setdefault('summary', p.get('status'))
        # Old wave drafts repeated these already-bound scope IDs in the payload.
        for key, bound in [('wave_id', 'task_id'), ('head_sha', 'code_rev')]:
            if key in p and p[key] == scope.get(bound):
                del p[key]
    elif kind.startswith('source.') and 'range' in p:
        span = p.pop('range')
        if not isinstance(span, (list, tuple)) or len(span) != 2 or 'range_start' in p or 'range_end' in p:
            raise PayloadRefusal('invalid legacy source range')
        p['range_start'], p['range_end'] = span
        if type(p.get('cursor')) is int:
            p['cursor'] = str(p['cursor'])
        p.setdefault('source_id', source['adapter'])
    elif kind == 'feedback.recorded':
        p.setdefault('actor', authority)
        p.setdefault('rating', None)
        p.setdefault('supersedes_event_id', None)
        if p.get('note') is None:
            p['note'] = ''
    if isinstance(p.get('evidence_ref'), list) and len(p['evidence_ref']) == 1:
        p['evidence_ref'] = p['evidence_ref'][0]


def event_body(kind, source, scope, payload, authority, lane, occurred_at, duration_ms,
               timing_quality, redaction_policy):
    if not isinstance(kind, str) or kind not in KINDS:
        raise PayloadRefusal('unknown /1 kind', code='kind')
    if authority not in AUTHORITIES:
        raise PayloadRefusal('unknown producer authority', code='authority')
    if not isinstance(source, dict) or set(source) != set(SOURCE_FIELDS):
        raise PayloadRefusal('invalid source fields', code='source')
    for field in SOURCE_FIELDS:
        require_id(source[field], 'source.' + field)
    if not isinstance(scope, dict):
        raise PayloadRefusal('invalid scope', code='scope')
    scope = dict(scope)
    _rename(scope, 'repo_slug', 'repo')
    if set(scope).difference(SCOPE_FIELDS) or not any(scope.get(k) for k in GROUPING_IDS):
        raise PayloadRefusal('invalid scope fields or no grouping ID', code='scope')
    scope = {k: scope.get(k) for k in SCOPE_FIELDS}
    for k, v in scope.items():
        if v is not None and not _matches({'repo': REPO_RE, 'code_rev': REV_RE,
                                          'contract_sha256': HEX64_RE}.get(k, ID_RE), v):
            raise PayloadRefusal('invalid scope.' + k, code='scope')
    if lane is not None and (not isinstance(lane, dict) or set(lane).difference(LANE_FIELDS)):
        raise PayloadRefusal('invalid lane fields', code='lane')
    lane = {k: (lane or {}).get(k) for k in LANE_FIELDS}
    if not isinstance(payload, dict):
        raise PayloadRefusal('payload must be an object')
    p = dict(payload)
    _producer_payload(kind, p, source, scope, lane, authority)
    _validate_payload(kind, p)
    if lane['lane_id'] is None:
        # No explicit topology: stable lane per producer and grouping. Hash
        # public IDs only; never infer a parent, or pair unrelated tool calls.
        identity = [source['adapter'], source['instance'], scope]
        lane['lane_id'] = 'lane:' + hashlib.sha256(canonicalize(identity)).hexdigest()
    for k, v in lane.items():
        if v is not None:
            require_id(v, k)
    if occurred_at is not None and not _matches(STAMP_RE, occurred_at):
        raise PayloadRefusal('invalid occurred_at', code='timestamp')
    if duration_ms is not None and (type(duration_ms) is not int or not 0 <= duration_ms <= INT_LIMIT):
        raise PayloadRefusal('invalid duration', code='duration')
    if timing_quality == 'monotonic_measured':
        timing_quality = 'measured'
    elif timing_quality == 'reported':
        timing_quality = 'estimated'
    if timing_quality not in TIMING_QUALITY:
        raise PayloadRefusal('invalid timing_quality', code='timing')
    if redaction_policy not in (None, POLICY, OLD_POLICY, {'mode': 'structured_safe'}, {}):
        raise PayloadRefusal('unsupported redaction_policy', code='redaction')
    return dict(schema=SCHEMA, kind=kind, source=source, scope=scope, authority=authority,
                payload=p, occurred_at=occurred_at, duration_ms=duration_ms,
                timing_quality=timing_quality, redaction_policy=POLICY, **lane)


def validate_event(kind, source, scope, payload, authority, lane, occurred_at,
                   duration_ms, timing_quality, redaction_policy):
    event_body(kind, source, scope, payload, authority, lane, occurred_at,
               duration_ms, timing_quality, redaction_policy)
