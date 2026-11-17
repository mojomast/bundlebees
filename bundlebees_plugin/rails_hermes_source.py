"""Strict read-only adapter for the pinned Hermes session shape (Rails R07).

`allowed_sessions` maps native IDs to operator-approved public aliases. A list
of already-public IDs is shorthand. No default database, wildcard, recursive
authorization, content export or swarm discovery. Each poll scans a bounded
page of the authorized sessions. On finishing a sweep, the next poll revisits
existing IDs: MAX(id) alone cannot detect an active flag or a tool edit.
"""
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

try:
    from rizzstack_rails import contracts, source
except ModuleNotFoundError as exc:
    if exc.name != 'rizzstack_rails':
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'contrib'))
    from rizzstack_rails import contracts, source

ADAPTER = 'hermes-session'
MAX_CALLS = 32
_TABLES = {'sessions': {'id','source','parent_session_id','started_at','ended_at'},
           'messages': {'id','session_id','role','tool_calls','tool_call_id','timestamp','display_kind'}}
_TOOLS = {'terminal','bash','shell','read_file','write_file','patch','search_files','delegate_task',
          'execute_code','web_search','web_extract','clarify','todo','browser_navigate'}
_FAMILIES = {'pytest','git','python','python3','node','npm','ls','cat','rg','grep','make','cargo','go'}
_SCOPE = dict.fromkeys(('repo','campaign_id','job_id','task_id','attempt_id','session_id','action_id',
                        'contract_sha256','code_rev'))


def _time(value):
    if type(value) not in (float, int):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    except (ValueError, OSError, OverflowError):
        return None


def _family(name, arguments):
    if name not in {'terminal','bash','shell'} or not isinstance(arguments, (dict, str)):
        return 'unknown'
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, RecursionError):
        return 'unknown'
    cmd = args.get('command') if isinstance(args, dict) else None
    words = cmd.lstrip().split(None, 1) if isinstance(cmd, str) else []
    return words[0] if words and words[0] in _FAMILIES else 'unknown'


def _observation(kind, payload, stamp=None, duration=None):
    return dict(kind=kind, payload=payload, occurred_at=_time(stamp), duration_ms=duration,
                timing_quality='estimated' if duration is not None else 'unknown')


def _notice(kind, alias, mid, code):
    return _observation(kind, {'source_id': alias, 'cursor': str(mid), 'range_start': mid,
                        'range_end': mid, 'code': code, 'count': 1,
                        'digest': source.digest([alias, mid, code])})


def _pairs(items):
    obj = dict(items)
    if len(obj) != len(items):
        raise ValueError()
    return obj


class HermesSource:
    def __init__(self, db_path, allowed_sessions, *, source_instance='hermes-local'):
        if isinstance(allowed_sessions, (list, tuple, set, frozenset)):
            allowed_sessions = {x: x for x in allowed_sessions}
        if not isinstance(allowed_sessions, dict) or not 1 <= len(allowed_sessions) <= 100:
            raise source.SourceError('allowlist_required')
        aliases = {}
        for native, alias in allowed_sessions.items():
            if (not isinstance(native, str) or not 1 <= len(native) <= 256 or '\x00' in native or
                    alias in aliases.values() or not isinstance(alias, str) or len(alias) > 96):
                raise source.SourceError('invalid_allowlist')
            aliases[native] = source.token(alias)
        self._aliases = aliases
        self._ids = sorted(aliases, key=aliases.get)
        self.path = Path(db_path).absolute()
        self.instance = source.token(source_instance)
        self._binding = source.digest([self.instance, sorted(aliases.values())])[:20]
        self._last = None
        self._error = 'never_read'

    def describe(self):
        return dict(schema='rizzstack.rails-describe/1', adapter_id=ADAPTER, version='1',
                    source_instance=self.instance, capabilities=['revision-groups','allowlist','tool-pairs'],
                    supported_formats=['sqlite-ro'])

    def health(self):
        return dict(schema='rizzstack.rails-source-health/1', last_success=self._last, error_code=self._error,
                    state=('stale' if self._last else 'unavailable') if self._error else 'live')

    def _schema(self, cx):
        active = False
        for table, required in _TABLES.items():
            # Refuse views/virtual tables in place of the two native tables.
            row = cx.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()
            if not row or not re.match(r'CREATE\s+TABLE\s', row[0] or '', re.I):
                raise source.SourceError('unsupported_source_version')
            cols = {col[1] for col in cx.execute(f'PRAGMA table_info({table})')}
            if not required.issubset(cols):
                raise source.SourceError('unsupported_source_version')
            if table == 'messages':
                active = 'active' in cols
        return active

    def _state(self, cursor):
        prefix = 'h1:' + self._binding + ':'
        if cursor is None or cursor == prefix+'idle':
            return None
        if not isinstance(cursor, str) or not cursor.startswith(prefix):
            raise source.SourceError('invalid_cursor')
        m = re.fullmatch(r'(\d{1,16}):(\d{1,3}):(\d{1,16}):(\d{1,3}):(-|[0-9a-f]{64})', cursor[len(prefix):])
        if not m:
            raise source.SourceError('invalid_cursor')
        high, index, mid, slot = map(int, m.groups()[:4])
        if index >= len(self._ids) or high > contracts.INT_LIMIT or mid > high+1 or slot > MAX_CALLS*2:
            raise source.SourceError('invalid_cursor')
        return high, index, mid, slot, m[5]

    def read_batch(self, cursor=None, limit=100, byte_limit=source.MAX_BYTES):
        source.limits(limit, byte_limit)
        state = self._state(cursor)
        try:
            # A read-only SQLite client may attempt to create absent WAL sidecars.
            # Refuse that configuration rather than using immutable=1 on a live DB.
            with self.path.open('rb') as f:
                header = f.read(20)
            if not header.startswith(b'SQLite format 3\0'):
                raise source.SourceError('unsupported_source_version')
            if header[18:20] == b'\x02\x02' and not all(Path(str(self.path)+s).is_file() for s in ('-wal','-shm')):
                raise source.SourceError('wal_unavailable')
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=1)) as cx:
                cx.row_factory = sqlite3.Row
                cx.execute('PRAGMA query_only=ON')
                cx.execute('PRAGMA trusted_schema=OFF')
                # Also cap SQLite VM work when an installation lacks useful indexes.
                steps = iter(range(5000))
                cx.set_progress_handler(lambda: next(steps, None) is None, 1000)
                cx.execute('BEGIN')
                active = self._schema(cx)
                page = source.fit_page(lambda n: self._read(cx, active, state, n, byte_limit), limit, byte_limit)
        except source.SourceError as exc:
            self._error = exc.code
            raise
        except (OSError, sqlite3.Error):
            self._error = 'source_unavailable'
            raise source.SourceError(self._error) from None
        self._last, self._error = contracts.utc_now(), None
        return page

    def _read(self, cx, active, state, limit, cap):
        if state is None:
            marks = ','.join('?' for _ in self._ids)
            high = cx.execute(f'SELECT coalesce(max(id),0) FROM messages WHERE session_id COLLATE BINARY IN ({marks})',
                              self._ids).fetchone()[0]
            if type(high) is not int or not 0 <= high <= contracts.INT_LIMIT:
                raise source.SourceError('unsupported_source_version')
            state = high, 0, 0, 0, '-'
        high, index, mid, slot, seen = state
        prefix = 'h1:'+self._binding+':'
        rows, used = [], 0
        flag = 'active' if active else '1'
        while index < len(self._ids):
            native = self._ids[index]
            alias = self._aliases[native]
            lane = 'hermes:'+source.digest([self.instance, alias])[:32]
            sess = cx.execute('SELECT parent_session_id,started_at,ended_at FROM sessions WHERE id COLLATE BINARY=?',
                              (native,)).fetchone()
            parent = self._aliases.get(sess[0]) if sess else None
            parent_lane = 'hermes:'+source.digest([self.instance,parent])[:32] if parent else None
            if mid == 0:
                group = [_observation('lane.open', {'status':'observed'}, sess[1])] if sess else [
                    _notice('source.gap', alias, 0, 'session_missing')]
                if sess and sess[2] is not None:
                    group.append(_observation('lane.close', {'status':'closed'}, sess[2]))
            else:
                msg = cx.execute(f'''SELECT id,role,timestamp,{flag} AS active,
                    display_kind='hidden' AS hidden,
                    length(CAST(tool_calls AS BLOB)) AS size,
                    CASE WHEN length(CAST(tool_calls AS BLOB))<=65536 THEN tool_calls END AS calls
                    FROM messages WHERE session_id COLLATE BINARY=? AND id BETWEEN ? AND ? ORDER BY id LIMIT 1''',
                                 (native, mid, high)).fetchone() if sess else None
                if not msg:
                    index, mid, slot, seen = index+1, 0, 0, '-'
                    continue
                if mid != msg['id']:
                    mid, slot, seen = msg['id'], 0, '-'
                group = self._message(cx, native, alias, msg, flag, high)
            rid = 'fact:'+source.digest([self.instance,alias,mid])
            revision = source.digest([lane,parent_lane,group])
            if seen != revision:
                slot = 0  # changed between pages: revisit the whole safe revision
            while slot < len(group):
                item = dict(group[slot], schema='rizzstack.rails-candidate/1',
                    dedupe_key='hermes:'+source.digest([rid,revision,slot]),
                    source=dict(adapter=ADAPTER, instance=self.instance, record_id=rid, revision=revision),
                    scope=dict(_SCOPE, session_id=alias), lane_id=lane, parent_lane_id=parent_lane,
                    parent_event_id=None, authority='imported_observation', redaction_policy='rails-safe/1')
                size = len(contracts.canonicalize(item))
                if len(rows) == limit or used+size > cap:
                    if not rows:
                        raise source.SourceError('byte_limit')
                    cur = f'{prefix}{high}:{index}:{mid}:{slot}:{revision}'
                    return source.result(rows, cur, True, str(high))
                rows.append(item)
                used += size
                slot += 1
            mid, slot, seen = mid+1, 0, '-'
        return source.result(rows, prefix+'idle', False, str(high))

    def _message(self, cx, native, alias, msg, flag, high):
        mid, ts = msg['id'], msg['timestamp']
        if msg['active'] not in (0,1):
            return [_notice('source.error', alias, mid, 'invalid_active_flag')]
        if msg['hidden'] or not msg['active']:
            return [_notice('source.checkpoint', alias, mid, 'message_inactive' if not msg['active'] else 'message_hidden')]
        if msg['role'] != 'assistant' or msg['size'] in (None,0):
            return [_notice('source.checkpoint', alias, mid, 'message_observed')]
        if msg['size'] > source.MAX_RECORD:
            return [_notice('source.error', alias, mid, 'record_too_large')]
        try:
            calls = json.loads(msg['calls'], object_pairs_hook=_pairs)
            if not isinstance(calls, list) or len(calls) > MAX_CALLS:
                raise ValueError()
            normalized = []
            for c in calls:
                if not isinstance(c, dict) or not isinstance(c.get('function'), dict):
                    raise ValueError()
                cid = c.get('id') or c.get('call_id')
                if not isinstance(cid, str) or not 1 <= len(cid) <= 256 or '\x00' in cid:
                    raise ValueError()
                cid.encode('utf-8')
                name = c['function'].get('name')
                name = name if isinstance(name,str) and name in _TOOLS else 'unknown'
                normalized.append((cid, name, _family(name,c['function'].get('arguments'))))
            if len({c[0] for c in normalized}) != len(normalized):
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            return [_notice('source.error', alias, mid, 'malformed_tool_calls')]
        rows = []
        for slot, (cid, name, family) in enumerate(normalized):
            public = 'call:'+source.digest([self.instance,alias,mid,slot])[:32]
            base = dict(call_id=public, tool_name=name, command_family=family, evidence_ref=public)
            rows.append(_observation('tool.start', dict(base, outcome='pending', summary='Tool call observed'), ts))
            results = cx.execute(f'''SELECT id,timestamp,{flag} FROM messages WHERE session_id COLLATE BINARY=?
                AND role='tool' AND tool_call_id COLLATE BINARY=? AND id<=? AND display_kind IS NOT 'hidden'
                ORDER BY id LIMIT 2''', (native, cid, high)).fetchall()
            if len(results) > 1 or (results and results[0][0] <= mid):
                rows.append(_notice('source.error', alias, mid, 'ambiguous_tool_result'))
            elif results and results[0][2] == 1:
                # A reused call ID on two visible assistant messages is not
                # evidence for *either* finish. Inspect only call IDs in this
                # allowed session, under the same snapshot and VM-work cap.
                matches = cx.execute(f'''SELECT m.id FROM messages m, json_each(CASE
                    WHEN length(CAST(m.tool_calls AS BLOB))<=65536 AND json_valid(m.tool_calls)
                    THEN m.tool_calls ELSE '[]' END) c
                    WHERE m.session_id COLLATE BINARY=? AND m.role='assistant'
                    AND m.id<? AND m.display_kind IS NOT 'hidden' AND {('m.active=1' if flag=='active' else '1')}
                    AND CASE WHEN c.type='object' THEN coalesce(nullif(json_extract(c.value,'$.id'),''),
                    json_extract(c.value,'$.call_id')) END COLLATE BINARY=? LIMIT 2''',
                                     (native, results[0][0], cid)).fetchall()
                if len(matches) != 1 or matches[0][0] != mid:
                    rows.append(_notice('source.error', alias, mid, 'ambiguous_tool_result'))
                    continue
                end = results[0][1]
                duration = round((end-ts)*1000) if _time(end) and _time(ts) and end >= ts else None
                rows.append(_observation('tool.finish', dict(base, outcome='unknown', summary='Tool result observed'),
                                         end, duration))
        return rows or [_notice('source.checkpoint', alias, mid, 'message_observed')]
