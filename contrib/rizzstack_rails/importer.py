"""Explicit one-page ingestion; never called from a read handler.

R05 supplies R02 append drafts, R06 supplies a keyed landing body, and R09
passes original canonical strings to import_origin_page(). Cursor CAS and the
append-only receipts are in the same Rails database. Adapter identities and
authority permissions are trusted process configuration, never HTTP inputs.
"""
import re

from . import contracts, privacy
from .source import MAX_BYTES, MAX_RECORD, MAX_RECORDS, RESULT, SourceError, digest, limits, token

_FIELDS = {'kind', 'source', 'scope', 'payload', 'authority', 'dedupe_key', 'lane',
           'occurred_at', 'duration_ms', 'timing_quality', 'redaction_policy'}
_REQUIRED = {'kind', 'source', 'scope', 'payload', 'authority', 'dedupe_key'}
_WIRE_KIND = 'rizzstack.rails-candidate/1'


def _wire_args(candidate):
    """A wire body must round-trip *without* any legacy defaults/repairs."""
    fields = (_FIELDS - {'lane'}) | set(contracts.LANE_FIELDS)
    if candidate.keys() != fields:
        raise SourceError('invalid_candidate')
    row = candidate.copy()
    row['lane'] = {k: row.pop(k) for k in contracts.LANE_FIELDS}
    normalized = contracts.event_body(**{k:v for k,v in row.items() if k != 'dedupe_key'})
    if dict(normalized, dedupe_key=row['dedupe_key']) != dict(candidate, schema=contracts.SCHEMA):
        raise SourceError('invalid_candidate')
    return row


def _cursor(value):
    # Opaque transport token, not a path, note, URL or nested JSON document.
    if value is not None and (not isinstance(value, str) or not re.fullmatch(
            r'[A-Za-z0-9._:=+~-]{1,1024}', value) or privacy.scrub_text(value) != value):
        raise SourceError('invalid_cursor')
    return value


def _bounded(records, limit, byte_limit, *, origin=False):
    limits(limit, byte_limit)
    if not isinstance(records, (list, tuple)) or len(records) > limit:
        raise SourceError('invalid_batch')
    used = 0
    for row in records:
        if origin:
            if not isinstance(row, str) or not row.isascii():
                raise SourceError('origin_bytes')
            size = len(row)
        else:
            size = len(contracts.canonicalize(row))
        used += size
        if size > MAX_RECORD or used > byte_limit:
            raise SourceError('too_large')


def _prepare(record, authorities):
    if not isinstance(record, dict):
        raise SourceError('invalid_candidate')
    row = record.copy()
    schema = row.pop('schema', None)
    if schema == _WIRE_KIND:
        row = _wire_args(row)
    elif schema not in (None, contracts.SCHEMA):
        raise SourceError('unsupported_source_version')
    # R06's pager supplies exactly its sink key. Never invent a dedupe key
    # for an unkeyed builder result (it may refer to a different instance).
    source = row.get('source')
    if not isinstance(source, dict) or set(source) != set(contracts.SOURCE_FIELDS):
        raise SourceError('invalid_candidate')
    if not _REQUIRED <= row.keys() <= _FIELDS:
        raise SourceError('invalid_candidate')
    if not isinstance(row['authority'], str) or row['authority'] not in authorities:
        raise SourceError('authority')
    # Path-based draft IDs from older adapters must not escape into dedupe_index.
    key = row['dedupe_key']
    if not isinstance(key, str) or not key or len(key) > 1024:
        raise SourceError('invalid_identifier')
    if privacy.looks_like_path(key):
        row['dedupe_key'] = privacy.scrub_record_id(key)
    token(row['dedupe_key'])
    if privacy.scrub_text(row['dedupe_key']) != row['dedupe_key']:
        raise SourceError('invalid_identifier')
    row['source'] = privacy.scrub_source(source)
    for value in row['source'].values():
        token(value)
    for k in ('scope', 'payload', 'lane'):
        row[k] = privacy.scrub_structure(row.get(k))
    row.setdefault('occurred_at', None)
    row.setdefault('duration_ms', None)
    row.setdefault('timing_quality', 'unknown')
    row.setdefault('redaction_policy', None)
    contracts.validate_event(**{k: v for k, v in row.items() if k != 'dedupe_key'})
    return row


class Importer:
    """Use a separate, least-privilege authority mapping per configured source.

    import_once() fences the read with both cursor and durable page ordinal
    (including ABA). commit_batch() accepts an already normalized bounded page;
    direct callers with cycling cursors must pass the ordinal from position().
    """
    def __init__(self, ledger, *, authorities=('imported_observation', 'worker_report')):
        self.ledger = ledger
        self.authorities = frozenset(authorities)

    def position(self, adapter_id, source_instance):
        key = digest([token(adapter_id), token(source_instance)])
        rows = self.ledger.query("select 1 from sqlite_master where type='table' and name='source_pages'")
        if rows:
            rows = self.ledger.query('select page,cursor from source_pages where source_key=? '
                                     'order by page desc limit 1', (key,))
        return rows[0] if rows else {'page': 0, 'cursor': None}

    def cursor(self, adapter_id, source_instance):
        return self.position(adapter_id, source_instance)['cursor']

    @property
    def chain_id(self):
        return self.ledger.chain_id

    def import_cursor(self, chain_id):
        """R09 restarts from this ledger's committed origin head."""
        return self.ledger.import_cursor(token(chain_id))

    def halt_import_origin(self, chain_id):
        return self.ledger.halt_import_origin(token(chain_id))

    def import_once(self, source, limit=100, byte_limit=MAX_BYTES):
        self.ledger._ensure_writable()
        limits(limit, byte_limit)
        desc = source.describe()
        adapter, instance = desc['adapter_id'], desc['source_instance']
        pos = self.position(adapter, instance)
        batch = source.read_batch(pos['cursor'], limit, byte_limit)
        if (not isinstance(batch, dict) or batch.get('schema', RESULT) != RESULT or
                batch.keys() - {'schema'} != {'records', 'next_cursor', 'has_more', 'source_head', 'diagnostics'} or
                type(batch['has_more']) is not bool or not isinstance(batch['diagnostics'], list) or
                len(batch['diagnostics']) > 100 or any(not isinstance(d, str) or not re.fullmatch(
                    r'[a-z0-9_:-]{1,80}', d) for d in batch['diagnostics'])):
            raise SourceError('invalid_batch')
        if len(contracts.canonicalize(batch)) > byte_limit:
            raise SourceError('too_large')
        if batch['has_more'] and batch['next_cursor'] == pos['cursor']:
            raise SourceError('cursor_stalled')
        receipt = self.commit_batch(adapter, instance, batch['records'], expected_cursor=pos['cursor'],
                                    expected_page=pos['page'], next_cursor=batch['next_cursor'], limit=limit,
                                    byte_limit=byte_limit, revision_groups='revision-groups' in desc['capabilities'])
        return dict(receipt, has_more=batch['has_more'], diagnostics=batch['diagnostics'])

    def commit_batch(self, adapter_id, source_instance, records, *, expected_cursor, next_cursor,
                     expected_page=None, limit=MAX_RECORDS, byte_limit=MAX_BYTES, revision_groups=False):
        db = self.ledger
        db._ensure_writable()
        db._probe_capacity()
        ident = digest([token(adapter_id), token(source_instance)])
        _cursor(expected_cursor)
        _cursor(next_cursor)
        _bounded(records, limit, byte_limit)
        drafts = [_prepare(r, self.authorities) for r in records]
        for row in drafts:
            db._check_authority(row['authority'])
            if revision_groups and (row['source']['adapter'], row['source']['instance']) != (adapter_id, source_instance):
                raise SourceError('source_identity')

        def work(conn):
            conn.execute('CREATE TABLE IF NOT EXISTS source_pages(source_key TEXT NOT NULL,page INTEGER NOT NULL,'
                         ' cursor TEXT, checkpoint TEXT NOT NULL, PRIMARY KEY(source_key,page))')
            for op in ('UPDATE', 'DELETE'):
                conn.execute(f'CREATE TRIGGER IF NOT EXISTS source_pages_no_{op} BEFORE {op} ON source_pages '
                             "BEGIN SELECT RAISE(ABORT, 'source_pages is append-only'); END")
            pos = self.position(adapter_id, source_instance)
            if pos['cursor'] != expected_cursor or (expected_page is not None and pos['page'] != expected_page):
                raise SourceError('stale_cursor')
            if revision_groups:
                conn.execute("CREATE INDEX IF NOT EXISTS source_revisions ON event_rows("
                             "json_extract(envelope_json,'$.source.adapter'),json_extract(envelope_json,'$.source.instance'),"
                             "json_extract(envelope_json,'$.source.record_id'),seq DESC)")
            receipts = [self._append_revision(conn, row) if revision_groups else db._append_locked(conn, **row)
                        for row in drafts]
            count = sum(not r['duplicated'] for r in receipts)
            checkpoint = None
            if count or next_cursor != pos['cursor']:
                db._check_authority('imported_observation')
                page = pos['page'] + 1
                mark = digest([ident, page, next_cursor, [r['event_id'] for r in receipts]])
                checkpoint = db._append_locked(conn, 'source.checkpoint',
                    {'adapter':'rails-import', 'instance':ident, 'record_id':f'page:{page}', 'revision':mark},
                    {'session_id':'rails-import'}, {'range':[page,page], 'cursor':page, 'code':'imported',
                    'count':count, 'digest':mark}, 'imported_observation', 'page:'+mark, None, None, None, 'unknown', None)
                conn.execute('INSERT INTO source_pages VALUES (?,?,?,?)',
                             (ident, page, next_cursor, checkpoint['event_id']))
            return dict(imported=count, receipts=receipts, next_cursor=next_cursor,
                        checkpoint_event_id=checkpoint['event_id'] if checkpoint else None)
        return db._write_tx(work)

    def _append_revision(self, conn, row):
        # One source record can project several (bounded) marks, possibly across
        # pages. Its revision covers the complete normalized group. Derive each
        # occurrence from the preceding revision's last real event ID, so an
        # observed active->inactive->active transition is not a duplicate.
        s = row['source']
        history = conn.execute("SELECT event_id,json_extract(envelope_json,'$.source.revision') AS rev FROM event_rows "
            "WHERE json_extract(envelope_json,'$.source.adapter')=? AND json_extract(envelope_json,'$.source.instance')=? "
            "AND json_extract(envelope_json,'$.source.record_id')=? ORDER BY seq DESC LIMIT ?",
            (s['adapter'], s['instance'], s['record_id'], MAX_RECORDS + 1))
        old, previous, newest = [], None, None
        for eid, rev in history:
            if newest is None:
                newest = rev
            if newest == s['revision']:
                if rev != newest:
                    previous = eid
                    break
                old.append(eid)
            elif rev == newest:
                old.append(eid)
            else:
                break
        if len(old) > MAX_RECORDS:
            raise SourceError('revision_group_too_large')
        changed = newest is not None and newest != s['revision']
        occurrence = old[0] if changed else previous
        key = 'revision:' + digest([s, row['dedupe_key'], occurrence])
        receipt = self.ledger._append_locked(conn, **dict(row, dedupe_key=key))
        if changed:
            self.ledger._check_authority('imported_observation')
            eid = receipt['event_id']
            link = digest([old, eid])
            self.ledger._append_locked(conn, 'history.superseded',
                dict(adapter='rails-import', instance=s['instance'], record_id='revision:'+link, revision=link),
                row['scope'], dict(old_event_ids=old, replacement_lane_id=row['lane']['lane_id'],
                replacement_event_id=eid, operation_ref='observation:'+link), 'imported_observation',
                'supersession:'+link, row['lane'], None, None, 'unknown', None)
        return receipt

    def import_origin_page(self, chain_id, pages):
        """R09 ledger sink: reject unsafe bytes, never rewrite or locally re-sequence."""
        self.ledger._ensure_writable()
        token(chain_id)
        if chain_id == self.ledger.chain_id:
            raise SourceError('source_identity')
        _bounded(pages, MAX_RECORDS, MAX_BYTES, origin=True)
        for text in pages:
            env = contracts.parse_text(text)
            if not isinstance(env, dict) or not isinstance(env.get('authority'), str) or env['authority'] not in self.authorities:
                raise SourceError('authority')
            self.ledger._check_authority(env['authority'])
        # R02 validates original bytes (including hash on replay), admissible
        # envelopes and chain continuity inside its own import transaction.
        return self.ledger.import_origin_page(chain_id, pages)
