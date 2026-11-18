"""Read-only Rails query API: query_events(request, reader) -> rails-result/1.

Pass an R02 RailsLedger opened read_only=True. Optional allowed_scope is a
trusted AND-filter on the actual envelope (also for event-ID lookup). Never
take it, a DB handle/path, comparison metadata or role grants from a query.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
import uuid

from . import contracts
from .markdown import render_markdown
from .projection import build_view, project_events, public_id, validate_topology

QUERY_SCHEMA = 'rizzstack.rails-query/1'
RESULT_SCHEMA = 'rizzstack.rails-result/1'
KINDS = ('attempt','job','campaign','session','event')
GROUPS = {'tools':('tool.start','tool.finish'),
          'verification':('verification.result','acceptance.result'),
          'review':('review.result',),
          'feedback':('feedback.recorded','intervention.requested','intervention.result')}
MAX_BYTES = 32768
MAX_CONTEXT_ROWS = 2000
MAX_CONTEXT_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 65536


class QueryError(Exception):
    """Stable boundary error; never attach exception prose, input or DB paths."""
    def __init__(self, code, exit_code=2):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code

    def as_dict(self):
        return {'ok':False, 'error':{'code':self.code, 'retryable':self.exit_code == 3,
                                     'request_id':uuid.uuid4().hex}}


def wire_json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',',':'))


def make_request(kind, id, *, format='markdown', after=None, limit=100, max_bytes=MAX_BYTES,
                 include=None, show_tokens=False):
    return dict(schema=QUERY_SCHEMA, selector={'kind':kind,'id':id},format=format,after=after,
                limit=limit,max_bytes=max_bytes,include=list(GROUPS) if include is None else include,
                show_tokens=show_tokens)


def validate_request(req):
    if not isinstance(req,dict) or 'selector' not in req:
        raise QueryError('not_found')
    if set(req) != {'schema','selector','format','after','limit','max_bytes','include','show_tokens'}:
        raise QueryError('invalid_request')
    s = req['selector']
    if (req['schema'] != QUERY_SCHEMA or not isinstance(s,dict) or set(s) != {'kind','id'}
            or s['kind'] not in KINDS or not public_id(s['id']) or req['format'] not in ('json','markdown')
            or type(req['limit']) is not int or not 1 <= req['limit'] <= 500
            or type(req['max_bytes']) is not int or not 1 <= req['max_bytes'] <= MAX_BYTES
            or type(req['show_tokens']) is not bool or not isinstance(req['include'],list)
            or len(req['include']) > 4 or any(not isinstance(g,str) or g not in GROUPS for g in req['include'])
            or req['after'] is not None and (not isinstance(req['after'],str) or len(req['after'])>1024)):
        raise QueryError('invalid_request')


def _filter_digest(req, allowed_scope, surface):
    filters = [surface,req['selector'],sorted(set(req['include'])),req['show_tokens'],req['format'],allowed_scope]
    return hashlib.sha256(contracts.canonicalize(filters)).hexdigest()


def _cursor(ledger, high, key, digest):
    return base64.urlsafe_b64encode(wire_json([ledger,high,*key,digest]).encode()).decode().rstrip('=')


def _resume(token, reader, current_head, digest):
    if token is None:
        return current_head, (0,0)
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]+',token): raise ValueError
        data = json.loads(base64.b64decode(token + '='*(-len(token)%4),altchars=b'-_',validate=True))
        if not isinstance(data,list) or len(data)!=5: raise ValueError
        uid, high, last, sub, given = data
        if (uid != reader.ledger_uuid or given != digest or any(type(n) is not int for n in (high,last,sub))
                or not 1 <= last <= high <= current_head or not 0 <= sub <= contracts.INT_LIMIT
                or _cursor(uid,high,(last,sub),given) != token):
            raise ValueError
    except (ValueError, TypeError, RecursionError, binascii.Error):
        raise QueryError('reset_required') from None
    return high, (last,sub)


# An imported original has no global ingest_seq column in R02. Its transaction
# DOES have a committed local checkpoint. Use its ingest_seq + origin seq as a
# composite position, so all originals have a stable arrival cursor even if
# another page/chain is imported between calls. Never re-author original IDs.
_ARRIVALS = '''WITH arrivals AS (
  SELECT ingest_seq AS arrival, 0 AS sub, event_id AS eid, ingest_seq AS pk,
         envelope_json AS raw, rails_hash AS stored FROM event_rows WHERE ingest_seq <= ?
  UNION ALL
  SELECT ck.ingest_seq AS arrival, o.seq AS sub, o.event_id AS eid, o.rowid AS pk,
         o.origin_bytes AS raw, o.origin_hash AS stored FROM import_receipts AS r
  JOIN dedupe_index AS d ON d.chain_id=? AND d.dedupe_key=('import:'||r.chain_id||':'||r.through_seq)
  JOIN event_rows AS ck ON ck.event_id=d.event_id AND ck.ingest_seq <= ?
  JOIN imported_origin_rows AS o ON o.chain_id=r.chain_id
       AND o.seq BETWEEN r.through_seq-r.origin_count+1 AND r.through_seq
) '''


def _scan(reader, high, selector, allowed_scope, *, after=(0,0), exclude=(), limit):
    # On a local-only ledger the UNION forces SQLite to sort the *entire*
    # selector before LIMIT. Seek the ingest_seq PK and stop at LIMIT instead.
    # An import committed after this probe has checkpoint seq > high; imported
    # arrivals at or below this snapshot cannot appear after the probe.
    if not reader.query('SELECT 1 FROM import_receipts LIMIT 1'):
        arrivals = ('WITH arrivals AS (SELECT ingest_seq AS arrival, 0 AS sub, event_id AS eid,'
                    ' ingest_seq AS pk, envelope_json AS raw, rails_hash AS stored FROM event_rows) ')
        params = [high, after[0]]
        clauses = ['arrival <= ?', 'arrival > ?']
        order = 'arrival'
    else:
        arrivals = _ARRIVALS
        params = [high, reader.chain_id, high, *after]
        clauses = ['(arrival,sub) > (?,?)']
        order = 'arrival,sub'
    if selector['kind'] == 'event':
        clauses.append('eid=?')
    else:
        clauses.append("json_extract(raw, '$.scope.%s_id')=?" % selector['kind'])
    params.append(selector['id'])
    for k,v in allowed_scope.items():  # validated column path, always bind the identifier value
        clauses.append("json_extract(raw, '$.scope.%s')=?" % k)
        params.append(v)
    if exclude:
        clauses.append("json_extract(raw,'$.kind') NOT IN (%s)" % ','.join('?' for _ in exclude))
        params.extend(exclude)
    params.append(limit)
    return reader.query(arrivals + 'SELECT arrival,sub,eid,pk,stored,length(CAST(raw AS BLOB)) AS size '
                        'FROM arrivals WHERE '+ ' AND '.join(clauses)+' ORDER BY '+ order +' LIMIT ?',params)


def _envelopes(reader, rows):
    blobs = {}
    for imported,table,field in [(False,'event_rows','envelope_json'),(True,'imported_origin_rows','origin_bytes')]:
        pks = [r['pk'] for r in rows if bool(r['sub']) == imported]
        for i in range(0,len(pks),250):
            batch = pks[i:i+250]
            for v in reader.query('SELECT rowid AS pk,%s AS raw FROM %s WHERE rowid IN (%s)' %
                                  (field,table,','.join('?' for _ in batch)),batch):
                blobs[imported,v['pk']] = v['raw']
    out = []
    for row in rows:
        raw = blobs[bool(row['sub']),row['pk']]
        e = json.loads(raw)
        digest = e.pop('hash')
        if (e['event_id'] != row['eid'] or e['event_id'] != f"{e['chain_id']}:{e['seq']}"
                or e['schema'] != contracts.SCHEMA or digest != row['stored']
                or contracts.rails_hash(e['prev_hash'],e) != digest):
            raise QueryError('source_fork',4)
        e['hash'] = digest
        if contracts.canonicalize(e).decode('ascii') != raw:
            raise QueryError('source_fork',4)
        out.append(e)
    return out


def _result(req, candidates, more, next_token, completeness, missing, warnings, surface):
    detail = candidates
    markdown = ''
    integrity = {'status':'unchecked','checked_through':None}
    if surface == 'view':
        view = build_view(candidates,completeness=completeness,missing_sources=missing,integrity=integrity)
        detail = [view]
    elif req['format']=='markdown':
        markdown = render_markdown(candidates)
        detail = [{k:r[k] for k in ('event_id','kind','evidence_refs')} for r in candidates]
    return dict(schema=RESULT_SCHEMA,ok=True,items=detail,markdown=markdown,next_cursor=next_token,
                has_more=more,truncated=more,completeness=completeness,missing_sources=missing,
                integrity=integrity,warnings=warnings)


def _context(reader, high, last, digest, selector, allowed_scope, excluded, limit):
    # A byte-limited continuation often needs a suffix of the *same* verified
    # lookahead. Keep at most one bounded window on this read-only connection.
    # data_version (not just MAX(seq)) changes even for a repair/tampering
    # commit with no new events. Never use it across connections.
    version = reader.query('PRAGMA data_version')[0]['data_version']
    key = (version, high, digest)
    saved = getattr(reader, '_rails_bounded_window', None)
    reuse = saved is not None and saved[0] == key
    if reuse:
        _, start, rows, envelopes = saved
        bounded = populated = True
        if start <= last:
            i = next((i for i,r in enumerate(rows) if (r['arrival'],r['sub']) > last), len(rows))
            end = i + limit + 1
            if end <= len(rows):  # includes the original scan's lookahead row
                return rows[i:end], envelopes[i:end], populated, bounded, False
    else:
        reader._rails_bounded_window = None
        rows = _scan(reader,high,selector,allowed_scope,limit=MAX_CONTEXT_ROWS+1)
        populated = bool(rows)
        if not populated and selector['kind']=='event':
            raise QueryError('not_found')
        bounded = len(rows) > MAX_CONTEXT_ROWS or sum(r['size'] for r in rows)>MAX_CONTEXT_BYTES
    overflow = False
    if bounded:
        if not reuse and last == (0,0) and not excluded and len(rows) >= limit+1:
            # The context probe already read this exact unfiltered prefix.
            rows = rows[:limit+1]
        else:
            rows = _scan(reader,high,selector,allowed_scope,after=last,exclude=excluded,limit=limit+1)
        used = 0
        for i,r in enumerate(rows):
            used += r['size']
            if used > MAX_CONTEXT_BYTES:
                rows = rows[:i]; overflow = True; break
    if any(r['size'] > MAX_RECORD_BYTES for r in rows) or overflow and not rows:
        raise QueryError('too_large')
    envelopes = _envelopes(reader,rows)
    if bounded:
        reader._rails_bounded_window = (key, last, rows, envelopes)
    return rows, envelopes, populated, bounded, overflow


def _read(req, reader, allowed_scope, trusted_origins, comparison_context, surface):
    if not getattr(reader,'read_only',False):
        raise QueryError('forbidden')
    if allowed_scope is None: allowed_scope = {}
    if not isinstance(allowed_scope,dict) or any(k not in ('attempt_id','campaign_id','session_id','job_id','task_id')
                                                or not public_id(v) for k,v in allowed_scope.items()):
        raise QueryError('forbidden')
    digest = _filter_digest(req,allowed_scope,surface)
    now = reader.query('SELECT COALESCE(MAX(ingest_seq),0) AS n FROM event_rows')[0]['n']
    high, last = _resume(req['after'],reader,now,digest)
    selector = req['selector']
    excluded = tuple(k for group,kinds in GROUPS.items() if group not in req['include'] for k in kinds)
    rows, envelopes, populated, bounded, overflow = _context(
        reader, high, last, digest, selector, allowed_scope, excluded, req['limit'])
    # Repeat the projection, topology and coverage over the exact window of
    # THIS request; none of those reductions are safely reusable by suffix.
    items = project_events(envelopes,local_chain=reader.chain_id,show_tokens=req['show_tokens'],
                           trusted_origins=trusted_origins, comparison_context=comparison_context,
                           full_context=not bounded and selector['kind'] != 'event')
    validate_topology(items)
    missing = sorted({r['source_id'] for r in items if r['kind'] in ('source.gap','source.error')})[:50]
    warnings = ['coverage_unconfirmed'] if populated else []
    if bounded: warnings.append('bounded_context_no_global_reductions')
    if selector['kind'] == 'event': warnings.append('event_only_no_global_reductions')
    completeness = 'partial' if populated else 'empty'  # R02 has no authoritative coverage roster
    selected = [((r['arrival'],r['sub']),item) for r,item in zip(rows,items) if item['kind'] not in excluded
                and (r['arrival'],r['sub']) > last]

    def page(n):
        more = n < len(selected) or overflow
        token = _cursor(reader.ledger_uuid,high,selected[n-1][0],digest) if more and n else None
        return _result(req,[item for _,item in selected[:n]],more,token,completeness,missing,warnings,surface)

    # The terminal page can cost LESS than its prefix (no cursor). Try it
    # first, then search prefixes. Never return a zero-progress cursor or slice JSON.
    maximum = min(req['limit'],len(selected))
    def fits(n):
        try:
            out = page(n)
            return out if len(wire_json(out)) <= req['max_bytes'] else None
        except OverflowError:
            return None
    out = fits(maximum)
    if out is not None: return out
    low, top = 1, maximum-1
    best = None
    while low <= top:
        n = (low+top)//2
        out = fits(n)
        if out is None: top = n-1
        else: best,low = out,n+1
    if best is None: raise QueryError('too_large')
    return best


def _run(request,reader,allowed_scope,trusted_origins,comparison_context,surface):
    validate_request(request)
    try:
        return _read(request,reader,allowed_scope,trusted_origins,comparison_context,surface)
    except sqlite3.DatabaseError as exc:
        # SQLite reports malformed envelope JSON as an operational error.
        if 'malformed' in str(exc).lower() or 'not a database' in str(exc).lower():
            raise QueryError('source_fork',4) from None
        raise QueryError('source_unavailable',3) from None
    except (KeyError,ValueError,TypeError,AttributeError,RecursionError,contracts.RailsRefusal):
        raise QueryError('source_fork',4) from None


def query_events(request, reader, *, allowed_scope=None, trusted_origins=None, comparison_context=None):
    return _run(request,reader,allowed_scope,trusted_origins,comparison_context,'events')


def query_view_page(request, reader, *, allowed_scope=None, trusted_origins=None, comparison_context=None):
    """Result/1 with items=[view/1], including the continuation and complete wire cap."""
    return _run(request,reader,allowed_scope,trusted_origins,comparison_context,'view')


def query_view(request, reader, **options):
    """Bare view/1 for a single page. Servers should return query_view_page."""
    return query_view_page(request,reader,**options)['items'][0]


query = query_events
