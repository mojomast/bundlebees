"""Read-only Rails projection of an explicitly enrolled coordinator state tree.

The trusted input for commit-local and wave records is the coordinator's own
directory, *not* a filename in a JSON snapshot. TDD records have a separate
seal. As in tdd.review_identity(), that seal authenticates a coordinator's
recording of a review, not an owner's identity or independent execution.

Directory snapshots have no native sequence. Each checkpoint binds the source
instance, position and digest of the observed prefix. New suffixes can be read
after restart; a changed/missing prefix requires explicit reconciliation.
Only the importer persists a checkpoint. Reads have hard scan and output caps.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping

from . import leases, revision, tdd

try:
    from rizzstack_rails import contracts, privacy
except ImportError:
    import contrib.rizzstack_rails.contracts as contracts
    import contrib.rizzstack_rails.privacy as privacy


ADAPTER_ID = 'bundlebees'
SNAPSHOT_VERSION = 'bundlebees-source/1'
MAX_RECORD = 64 * 1024
MAX_BATCH = 1024 * 1024
# A snapshot directory has no trusted index. Bound rescans rather than claim
# a partial directory listing is its head (including directories of non-JSON).
MAX_ENTRIES = 8192
MAX_SCAN_BYTES = 16 * MAX_BATCH
MAX_DRAFTS = 8192
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}')
_REV = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})')
_AC = re.compile(r'AC-\d{3,12}', re.ASCII)


class SourceError(contracts.RailsRefusal):
    def __init__(self, code, error=None, fix=''):
        self.code, self.error, self.fix = code, error or code, fix
        super().__init__(self.error, code=code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'),
                      allow_nan=False).encode('ascii')


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _alias(label, value):
    return label + ':' + _hash([label, value])[:32]


def _instance(path):
    return _alias('bb', os.path.abspath(path))


def _id(value):
    if isinstance(value, str) and _TOKEN.fullmatch(value) and privacy.scrub_text(value) == value:
        return value
    return None


def _hex(value, pattern=_REV):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _pairs(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate field')
    return result


def _parse(raw):
    def invalid(_):
        raise ValueError('nonfinite number')
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('expected object')
    _canonical(value)  # Reject overflowed JSON floats before provenance checks.
    return value


@contextmanager
def _directory(name, parent=None, *, optional=False):
    """Pin the directory for enumeration and all child opens (POSIX coordinator)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=parent[0] if parent else None)
    except FileNotFoundError:
        if optional:
            yield None
            return
        raise SourceError('source_unavailable') from None
    except OSError:
        raise SourceError('source_unsafe') from None
    try:
        info = os.fstat(fd)
        enclosed = bool(parent and parent[1])
        _private_writer(info, enclosed)
        yield fd, enclosed or (info.st_mode & 0o011 == 0)
    finally:
        os.close(fd)


def _private_writer(info, enclosed):
    # Coordinator private roots commonly contain default-umask descendants.
    # Without an owner-only traversal boundary, reject shared writable paths.
    if info.st_uid != os.geteuid() or (not enclosed and info.st_mode & 0o022):
        raise SourceError('source_unsafe')


class _Scan:
    def __init__(self):
        self.entries = 0
        self.remaining = MAX_SCAN_BYTES

    def names(self, directory):
        if directory is not None:
            with os.scandir(directory[0]) as children:
                for child in children:
                    self.entries += 1
                    if self.entries > MAX_ENTRIES:
                        raise SourceError('scan_limit')
                    yield child.name

    def read(self, directory, name, max_bytes=MAX_RECORD):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory[0])
            with os.fdopen(fd, 'rb') as file:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise SourceError('source_unsafe')
                _private_writer(info, directory[1])
                if info.st_size > max_bytes or info.st_size > self.remaining:
                    raise SourceError('scan_limit')
                raw = file.read(min(self.remaining, max_bytes) + 1)
                after = os.fstat(fd)
                if (len(raw) != info.st_size or info.st_mtime_ns != after.st_mtime_ns or
                        info.st_ctime_ns != after.st_ctime_ns):
                    raise SourceError('source_changed')
                self.remaining -= len(raw)
                return raw, info.st_mtime_ns
        except OSError:
            raise SourceError('source_unavailable') from None


def _disk_entries(root):
    budget = _Scan()

    def files(fd, category, contract=None):
        for name in budget.names(fd):
            if name.startswith('.') or not name.endswith('.json'):
                continue
            raw, order = budget.read(fd, name)
            try:
                value = _parse(raw)
            except (ValueError, RecursionError):
                value = None
            path = '/'.join(x for x in (category, contract, name) if x)
            yield (order, path, category, value, hashlib.sha256(raw).hexdigest())

    entries = []
    with _directory(root) as state:
        for category in ('verify-results', 'evidence', 'waves', 'worker-claims'):
            with _directory(category, state, optional=True) as directory:
                if category == 'evidence':
                    for contract in budget.names(directory):
                        if contract.startswith('.'):
                            continue
                        with _directory(contract, directory) as fd:
                            entries.extend(files(fd, category, contract))
                else:
                    entries.extend(files(directory, category))
    return sorted(entries, key=lambda e: e[:2])


def _stamp(text):
    if isinstance(text, str):
        if text.endswith('+00:00'):
            text = text[:-6] + 'Z'
        if len(text) == 20 and text.endswith('Z'):
            text = text[:-1] + '.000000Z'
        if contracts.STAMP_RE.fullmatch(text):
            return text
    return None


def _signed(record, key):
    if isinstance(key, str):
        key = key.encode()
    if not isinstance(key, bytes) or not key or not tdd.verify_signature(record, key):
        return False
    try:
        tdd._validate_record(record)
        tdd._require_review_note(record)
    except tdd.TddError:
        return False
    return True


def _map(entry, instance, *, disk, key):
    _, path, category, record, raw_digest = entry
    rid = _alias('record', path)
    r = record or {}
    attempt = _id(r.get('attempt_id'))
    contract = _id(r.get('contract_id'))
    wave = _id(r.get('wave_id'))
    digest = _hex(r.get('contract_sha256'), contracts.HEX64_RE)
    rev = _hex(r.get({'verify-results': 'sha', 'waves': 'head_sha'}.get(category, 'revision')))
    scope = {field: value for field, value in (
        ('attempt_id', attempt), ('task_id', contract or wave),
        ('code_rev', rev), ('contract_sha256', digest)) if value is not None}
    if not any(scope.get(f) for f in contracts.GROUPING_IDS):
        scope['session_id'] = instance
    at = _stamp(r.get('at'))

    def draft(kind, payload, authority, part='record'):
        src = dict(adapter=ADAPTER_ID, instance=instance, record_id=rid, revision=raw_digest)
        lane = dict(lane_id=attempt or contract or wave or instance,
                    parent_lane_id=None, parent_event_id=None)
        timing = 'reported' if at else 'unknown'
        redact = dict(mode='structured_safe', path_detail='omitted')
        contracts.validate_event(kind, src, scope, payload, authority, lane, at, None, timing, redact)
        return dict(kind=kind, payload=payload, authority=authority, source=src,
                    scope=scope, occurred_at=at, duration_ms=None, timing_quality=timing,
                    lane=lane, redaction_policy=redact, dedupe_key=_alias('bb', [instance, rid, part]))

    def error(code):
        return [draft('source.error', dict(range=[0, 0], cursor=None, code=code,
                                           count=1, digest=raw_digest), 'imported_observation')]

    if record is None:
        return error('invalid_record')
    if not rev:
        return error('missing_revision')
    worker = (r.get('authority') == 'worker_report' or r.get('claim') is True or
              r.get('provenance') in ('worker', 'worker_report') or
              r.get('trusted') is False or r.get('unsigned') is True or
              r.get('verified') is False or r.get('signed') is False or
              r.get('claimed') is True or r.get('worker_pass') is True)
    producer = r.get('produced_by')
    role = producer.get('role') if isinstance(producer, dict) else None
    worker = worker or role == 'worker'

    if category == 'waves':
        if not disk or worker or path != f'waves/{wave}.json' or not wave:
            return error('untrusted_wave')
        accepted = r.get('accepted')
        if type(accepted) is not bool:
            return error('missing_wave_decision')
        if accepted and not (revision.suite_passed(r.get('suite')) and
                             r.get('problems') == {} and type(r.get('regressions')) is int and
                             r['regressions'] == 0 and isinstance(r.get('attempts'), list) and
                             r['attempts'] and all(_id(a) for a in r['attempts'])):
            return error('insufficient_wave_evidence')
        return [draft('merge.result', dict(result='merged' if accepted else 'rejected',
                                           wave_id=wave, head_sha=rev, evidence_refs=[rid]),
                      'coordinator_record')]

    # Only locally verified signatures are evidence. Never consume a caller's
    # signature_valid, claimed role, or the identity_verified flag as authority.
    sealed = category == 'evidence' and not worker and _signed(r, key)
    if disk and sealed:
        phase = r.get('phase')
        if not contract or not re.fullmatch(r'evidence/' + re.escape(contract) +
                                            r'/\d{4,}-' + re.escape(phase) + r'\.json', path):
            return error('misbound_evidence')
    if r.get('phase') == 'review':
        recorded = sealed and role in ('reviewer', 'human')
        outcome = r.get('classification')
        verdict = ({'passed': 'PASS', 'failed': 'FAIL'}.get(outcome, 'UNVERIFIED')
                   if recorded else 'UNVERIFIED')
        return [draft('review.result', dict(review_id=rid, reviewer=_alias('label', producer['name'])
                                            if recorded else 'unknown', author='unknown',
                                            reviewed_head=rev, independent=False, verdict=verdict,
                                            basis='coordinator_recorded' if recorded else 'unverified'),
                      'coordinator_record' if recorded else 'worker_report')]

    # verify_local._record has no contract binding. R02 requires one even for
    # non-PASS verification.result: expose a diagnostic rather than invent it.
    if not digest:
        return error('missing_contract_sha256')
    phase = r.get('phase', 'green')
    if phase not in contracts.PHASES:
        return error('unsupported_phase')
    committed = (disk and category == 'verify-results' and attempt and
                 path == f'verify-results/{attempt}.json' and not worker and
                 r.get('mode') == 'commit-local' and role is None)
    authority, verdict = 'worker_report', 'UNVERIFIED'
    intent = None
    if committed:
        authority = 'coordinator_record'
        if r.get('ok') is False:
            verdict = 'FAIL'
        # verify_workers._evidence_contract_problems: a resumed receipt or a
        # bare ok flag is not successful RED, regression and scanner evidence.
        elif (r.get('ok') is True and revision.suite_passed(r.get('suite')) and
              r['suite'].get('timed_out') is False and phase == 'green' and
              r.get('resumed') is False and r.get('branch') == 'bb-verify/' + attempt and
              _hex(r.get('base_rev')) and isinstance(r.get('red'), dict) and
              type(r['red'].get('exit_code')) is int and r['red']['exit_code'] != 0 and
              r['red'].get('timed_out') is False and revision.suite_passed(r.get('gitleaks'))):
            verdict = 'PASS'
    elif sealed and role == 'verifier':
        authority = 'verifier_record'
        result = r['result']
        classified = r['classification']
        ran = result['tests'] > result['skipped'] and not result['timed_out']
        cases = result.get('cases', [])
        if (classified == 'passed' and ran and phase != 'red' and r['command'] and
                not any(c['outcome'] in ('error', 'failed') for c in cases) and
                result['exit_code'] == 0 and result['errors'] == result['failures'] == 0):
            verdict = 'PASS'
        elif (classified == 'behavioral_failure' and ran and phase == 'red' and
              type(result['exit_code']) is int and result['exit_code'] != 0 and
              result['failures'] > 0 and r['test_fingerprints'] and r['test_first'] is True):
            verdict = 'EXPECTED_RED'
            intent = _alias('intent', r['test_fingerprints'])
        elif classified == 'not_run':
            verdict = 'NOT_RUN'
        elif classified != 'passed':
            verdict = 'FAIL'

    criteria = r.get('criteria', {})
    if (not isinstance(criteria, dict) or len(criteria) > 100 or
            any(not isinstance(a, str) or not _AC.fullmatch(a) for a in criteria)):
        return error('invalid_criteria')
    run = attempt or rid
    rows = []
    ac_id = r.get('ac_id')
    if not criteria and (not isinstance(ac_id, str) or not _AC.fullmatch(ac_id)):
        return error('missing_criterion')
    for ac, status in sorted(criteria.items()) or [(ac_id, None)]:
        effective = verdict
        if effective == 'PASS' and (criteria or not committed):
            effective = ('PASS' if status == 'passed' else 'FAIL' if status == 'failed'
                         else 'NOT_RUN' if status == 'not_run' else 'UNVERIFIED')
        p = dict(ac_id=ac, run_id=run, contract_sha256=digest, code_rev=rev, phase=phase,
                 verdict=effective, evidence_refs=[rid],
                 reason='' if effective == 'PASS' else 'No passing evidence for this criterion.')
        if intent:
            p['frozen_test_intent'] = intent
        rows.append(draft('verification.result', p, authority, ac))
    return rows


def _entries(snapshot):
    if isinstance(snapshot, str):
        snapshot = _parse(snapshot)
    if isinstance(snapshot, (list, tuple)):
        snapshot = {'records': snapshot}
    if not isinstance(snapshot, Mapping):
        raise SourceError('snapshot_invalid')
    if 'records' not in snapshot:
        raise SourceError('snapshot_invalid')
    records = snapshot['records']
    if not isinstance(records, (list, tuple)) or len(records) > MAX_ENTRIES:
        raise SourceError('snapshot_invalid')
    entries = []
    size = 0
    for item in records:
        if isinstance(item, str):
            item = _parse(item)
        if not isinstance(item, Mapping):
            raise SourceError('snapshot_invalid')
        r = item.get('record', item)
        if not isinstance(r, Mapping):
            raise SourceError('snapshot_invalid')
        body = _canonical(r)
        size += len(body)
        if size > MAX_SCAN_BYTES or len(body) > MAX_RECORD:
            raise SourceError('scan_limit')
        kind = item.get('type')
        cat = ('worker-claims' if kind == 'claim' else 'waves' if kind == 'merge' or 'wave_id' in r
               else 'evidence' if 'phase' in r else 'verify-results')
        digest = hashlib.sha256(body).hexdigest()
        rid = item.get('record_id', digest)
        if not isinstance(rid, str) or len(rid) > 1024:
            raise SourceError('snapshot_invalid')
        entries.append((0, rid, cat, r, digest))
    return sorted(entries, key=lambda e: e[:2])


def _drafts(entries, instance, *, disk, key):
    result = []
    seen = {}
    for entry in entries:
        for row in _map(entry, instance, disk=disk, key=key):
            candidate = _hash(row)
            prior = seen.get(row['dedupe_key'])
            if prior is not None:
                if prior != candidate:
                    raise SourceError('digest_conflict')
                continue
            seen[row['dedupe_key']] = candidate
            result.append(row)
            if len(result) > MAX_DRAFTS:
                raise SourceError('scan_limit')
    return result


def events_from_snapshot(snapshot, *, key=None):
    """Project caller-supplied JSON; it carries no coordinator filesystem provenance."""
    return _drafts(_entries(snapshot), 'snapshot', disk=False, key=key)


def events_from_records(records, *, key=None):
    return events_from_snapshot({'records': list(records)}, key=key)


def collect_events(state_dir, *, key=None):
    return _drafts(_disk_entries(state_dir), _instance(state_dir), disk=True, key=key)


def collect_snapshot(state_dir, *, key=None):
    """Export a de-identified snapshot, without transporting local authority."""
    return _public_snapshot(_disk_entries(state_dir))


def _public_snapshot(records):
    kinds = {'waves': 'merge', 'verify-results': 'verify', 'worker-claims': 'claim'}
    entries = []
    for _, rid, cat, record, _ in records:
        r = record or {}
        public = {}
        for field in ('attempt_id', 'contract_id', 'wave_id'):
            if value := _id(r.get(field)):
                public[field] = value
        for field in ('sha', 'revision', 'head_sha', 'contract_sha256'):
            if value := _hex(r.get(field)):
                public[field] = value
        phase = r.get('phase')
        if phase in (*contracts.PHASES, 'review'):
            public['phase'] = phase
        if ac := r.get('ac_id'):
            if isinstance(ac, str) and _AC.fullmatch(ac):
                public['ac_id'] = ac
        if isinstance(r.get('criteria'), dict):
            public['criteria'] = {ac: 'review_required' for ac in r['criteria'] if _AC.fullmatch(ac)}
        entries.append(dict(type=kinds.get(cat, 'review' if phase == 'review' else 'verify'),
                            record_id=_alias('record', rid), record=public))
    return dict(snapshot_version=SNAPSHOT_VERSION, records=entries)


snapshot = collect_snapshot
events = collect_events


def append_events(ledger, drafts):
    return [ledger.append(**row) for row in drafts]


class _PagedSource:
    """Common, bounded snapshot pager. The caller owns the durable cursor."""
    _last_success = None
    _read_error = None

    def describe(self):
        return dict(schema='rizzstack.rails-describe/1', adapter_id=self.adapter_id,
                    version='1', source_instance=self.instance,
                    capabilities=['read_batch', 'snapshot'], supported_formats=[])

    def health(self):
        code = self._read_error
        try:
            if not code:
                self._rows()
        except (SourceError, OSError, ValueError) as exc:
            code = getattr(exc, 'code', 'source_unavailable')
        state = ('stale' if self._last_success else 'unavailable') if code else 'live'
        return dict(schema='rizzstack.rails-source-health/1', state=state,
                    last_success=self._last_success, error_code=code)

    def read_batch(self, cursor=None, limit=100, byte_limit=MAX_BATCH):
        if (type(limit) is not int or not 1 <= limit <= 500 or
                type(byte_limit) is not int or not 1 <= byte_limit <= MAX_BATCH):
            raise SourceError('invalid_limit')
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
            raise SourceError('invalid_cursor')
        try:
            result = self._page(cursor, limit, byte_limit)
        except SourceError as exc:
            if exc.code not in ('byte_limit', 'invalid_cursor'):
                self._read_error = exc.code
            raise
        except OSError:
            self._read_error = 'source_unavailable'
            raise SourceError(self._read_error) from None
        self._read_error = None
        self._last_success = contracts.utc_now()
        return result

    def _page(self, cursor, limit, byte_limit):
        # Use the shared frozen wire builder. Legacy convenience APIs return
        # append kwargs; the source protocol returns only closed /1 candidates.
        rows = []
        for row in self._rows():
            body = contracts.event_body(row['kind'], row['source'], row['scope'], row['payload'],
                                        row['authority'], row.get('lane'), row.get('occurred_at'),
                                        row.get('duration_ms'), row.get('timing_quality', 'unknown'),
                                        row.get('redaction_policy'))
            rows.append(dict(body, schema='rizzstack.rails-candidate/1', dedupe_key=row['dedupe_key']))
        # Pin the observed head while paging, including not-yet-read records.
        # The chain seed binds cursors to an adapter and its configured source.
        hashes = [_hash([self.adapter_id, self.instance])]
        for row in rows:
            hashes.append(_hash([hashes[-1], row]))
        stop = len(rows)
        start = 0
        if cursor is not None:
            parts = cursor.split(':')
            if len(parts) != 4 or not all(re.fullmatch(r'[0-9]{1,8}', p) for p in parts[:2]):
                raise SourceError('invalid_cursor')
            start, end = map(int, parts[:2])
            if (not 0 <= start <= end <= stop or hashes[start] != parts[2] or hashes[end] != parts[3]):
                raise SourceError('source_gap', 'snapshot no longer contains the checkpointed prefix')
            if start < end:
                stop = end

        def position(n):
            return f'{n}:{stop}:{hashes[n]}:{hashes[stop]}'

        def page(n):
            return dict(schema='rizzstack.rails-read-batch-result/1', records=rows[start:n],
                        next_cursor=position(n) if n else cursor, has_more=n < stop,
                        source_head=position(stop) if stop else None, diagnostics=[])

        end = start
        # Measure the full serialized page (including cursor metadata) in O(n).
        overhead = len(_canonical(page(start)))
        content = 0
        while end < min(stop, start + limit):
            size = len(_canonical(rows[end]))
            if size > MAX_RECORD:
                raise SourceError('record_limit')
            if content + size + overhead > byte_limit and end > start:
                break
            content += size + (end != start)
            end += 1
        result = page(end)
        while end > start and len(_canonical(result)) > byte_limit:
            end -= 1
            result = page(end)
        if (end == start and start < stop) or len(_canonical(result)) > byte_limit:
            raise SourceError('byte_limit')
        return result


class BundlebeesSource(_PagedSource):
    adapter_id = ADAPTER_ID

    @classmethod
    def from_existing_state(cls, state_dir):
        """Use the producer's derived TDD key, without creating a lease key."""
        # leases.evidence_key() uses _ensure_key(), which writes on first use.
        try:
            root_key = leases._load_key(Path(state_dir))
        except leases.LeaseError:
            key = None
        else:
            key = hmac.new(root_key, leases._EVIDENCE_KEY_LABEL, hashlib.sha256).digest()
        return cls(state_dir, key=key)

    def __init__(self, state_dir=None, *, snapshot=None, key=None):
        if (state_dir is None) == (snapshot is None):
            raise SourceError('source_invalid', 'specify one source')
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self._snapshot = snapshot
        self.key = key
        self.instance = _instance(state_dir) if state_dir is not None else 'snapshot'

    def snapshot(self):
        if self.state_dir is not None:
            return collect_snapshot(self.state_dir)
        return _public_snapshot(_entries(self._snapshot))

    def events(self):
        if self.state_dir is not None:
            return collect_events(self.state_dir, key=self.key)
        return events_from_snapshot(self._snapshot, key=self.key)

    _rows = events
    emit = events

    def __iter__(self):
        return iter(self.events())


BundlebeesSourceAdapter = BundlebeesSource
