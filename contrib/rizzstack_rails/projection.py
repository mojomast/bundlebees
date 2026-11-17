"""Project R02 stored rows and frozen R01 origin envelopes without rendering prose.

The closed view/1 contains only lanes and marks. ``upstream_groups`` exposes
parents/subs/events separately, so neither extra wire keys nor synthetic events
are needed. No global state, writes, lookups by path, or cross-clock ordering.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from . import contracts, privacy
from .shell_labels import command_family, tool_label

ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,191}', re.ASCII)
_ADDRESS = re.compile(r'(?:\d{1,3}\.){3}\d{1,3}|::|(?:\.local|\.internal|\.lan|\.com|\.org|\.net)(?:[:.]|$)', re.I)
_REV = re.compile(r'(?:[a-f0-9]{40}|[a-f0-9]{64})')
_DIGEST = re.compile(r'[a-f0-9]{64}')
MARK = ('event_id', 'kind', 'occurred_at', 'duration_ms', 'timing_quality', 'outcome',
        'evidence_refs', 'ghost')
_STATES = frozenset('open closed pending queued active running started stopping cancelled failed '
                    'passed succeeded accepted approved rejected verified integrated merged held '
                    'declined blocked completed unknown not_run changes_requested approve '
                    'approve_with_followups'.split())


def public_id(value):
    if (isinstance(value, str) and ID.fullmatch(value) and not _ADDRESS.search(value)
            and privacy.scrub_text(value) == value):
        return value
    return None


def _member(value, allowed, fallback=None):
    return value if isinstance(value, str) and value in allowed else fallback


def _counter(value):
    return value if type(value) is int and 0 <= value <= contracts.INT_LIMIT else None


def _time(value):
    if isinstance(value, str) and contracts.STAMP_RE.fullmatch(value):
        datetime.fromisoformat(value)  # refuse impossible dates
        return value
    if value is not None:
        raise ValueError('invalid timestamp')
    return None


def _refs(p):
    refs = p.get('evidence_refs', p.get('evidence_ref'))
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if public_id(ref)][:20]


def _verification(e, item):
    p, scope = e['payload'], e['scope']
    digest, rev = public_id(p.get('contract_sha256')), public_id(p.get('code_rev'))
    ac, run = public_id(p.get('ac_id')), public_id(p.get('run_id'))
    verdict = _member(p.get('verdict'), contracts.VERDICTS)
    authority = 'verifier_record' if e['kind'] == 'verification.result' else 'owner_record'
    item.update(ac_id=ac, run_id=run, contract_sha256=digest, code_rev=rev,
                phase=_member(p.get('phase'), contracts.PHASES), outcome='UNVERIFIED', current=False)
    if (item['authority'] == authority and ac and run and verdict and item['phase']
            and _DIGEST.fullmatch(digest or '') and _REV.fullmatch(rev or '')
            and scope.get('contract_sha256') == digest and scope.get('code_rev') == rev
            and (item['evidence_refs'] if verdict == 'PASS' else p.get('reason'))
            and (verdict != 'EXPECTED_RED' or public_id(p.get('expected_intent_ref')))):
        item['outcome'] = verdict
        # AC identity is not a run, attempt, or page. Compare like task/contract/head.
        return (e['kind'], scope.get('job_id'), scope.get('task_id'), ac, digest, rev)
    return None


def _usage(e):
    p = e['payload']
    usage = {'counted': False, 'usage_record_id': public_id(p.get('usage_record_id')),
             'metric_scope': public_id(p.get('metric_scope')),
             'basis': _member(p.get('usage_basis', p.get('basis')), contracts.USAGE_BASES)}
    for name, local in [('input_tokens','tokens_in'), ('output_tokens','tokens_out'),
                        ('thinking_tokens','tokens_thinking')]:
        usage[name] = _counter(p.get(name, p.get(local)))
    return usage


def project_events(events, *, local_chain, show_tokens=False, trusted_origins=None,
                   comparison_context=None, full_context=True):
    """Return one safe item per input, in order. Unknown comparability stays unknown.

    trusted_origins maps imported chain IDs to authenticated roles. A hash alone
    confers none. comparison_context optionally maps (origin, lane) to the trusted
    (provider, model, environment fingerprint); tool payload /1 omits those keys.
    """
    items, ghosts, starts = [], set(), {}
    previous_finish = {}
    runs, usages, samples = defaultdict(list), defaultdict(list), defaultdict(list)
    comparable = {}
    for e in events:
        kind, p, chain = e['kind'], e['payload'], e['chain_id']
        if e['schema'] != contracts.SCHEMA or kind not in contracts.KINDS or not public_id(e['event_id']):
            raise ValueError('invalid event')
        if not isinstance(p,dict) or not isinstance(e['scope'],dict) or not isinstance(e['source'],dict):
            raise ValueError('invalid structures')
        role = _member(e.get('authority'),contracts.AUTHORITIES,'imported_observation')
        if chain != local_chain and role not in (trusted_origins or {}).get(chain, ()):
            role = 'imported_observation'
        lane = e.get('lane', e)  # stored R02 nesting / original R01 flat fields
        lane_id = public_id(lane.get('lane_id'))
        raw_parent = lane.get('parent_lane_id')
        parent = public_id(raw_parent)
        if (raw_parent is not None and not parent) or (lane.get('lane_id') is not None and not lane_id):
            raise ValueError('unsafe topology')
        duration = _counter(e.get('duration_ms'))
        quality = {'measured':'measured','estimated':'estimated', 'monotonic_measured':'measured',
                   'reported':'estimated'}.get(e.get('timing_quality'), 'unknown')
        if quality == 'unknown':
            duration = None
        item = dict(event_id=e['event_id'], kind=kind, lane_id=lane_id, parent_lane_id=parent,
                    parent_event_id=public_id(lane.get('parent_event_id')),
                    occurred_at=_time(e.get('occurred_at')), duration_ms=duration,
                    timing_quality=quality if duration is not None else 'unknown',
                    authority=role, outcome=None, evidence_refs=_refs(p), ghost=False)
        item['origin_hash'] = e['hash']
        item['source'] = {k: public_id(e['source'].get(k)) for k in contracts.SOURCE_FIELDS}
        item['scope'] = {k: v for k, v in e['scope'].items() if k in contracts.SCOPE_FIELDS
                         and k not in ('repo','repo_slug') and public_id(v)}
        predecessor = previous_finish.pop((chain, lane_id), None)
        if kind.startswith('tool.'):
            family = command_family(p.get('command_family'))
            call = public_id(p.get('call_id'))
            item.update(tool_name=tool_label(p.get('tool_name')), command_family=family, call_id=call,
                        outcome=_member(p.get('outcome'), contracts.OUTCOMES,'unknown'))
            key = (chain,lane_id,call)
            if kind == 'tool.start':
                item.update(duration_ms=None,timing_quality='unknown',outcome='pending')
                if full_context and predecessor and item['occurred_at']:
                    gap = datetime.fromisoformat(item['occurred_at']) - datetime.fromisoformat(predecessor)
                    item['observed_gap_ms'] = _counter(gap // timedelta(milliseconds=1))
                if lane_id and call:
                    # Duplicate starts make timestamp pairing ambiguous.
                    starts[key] = None if key in starts else item['occurred_at']
            else:
                if lane_id and item['occurred_at']:
                    previous_finish[chain,lane_id] = item['occurred_at']
                start = starts.pop(key,None)
                if start and item['occurred_at'] and duration is None:
                    delta = datetime.fromisoformat(item['occurred_at']) - datetime.fromisoformat(start)
                    ms = delta // timedelta(milliseconds=1)
                    if _counter(ms) is not None:
                        item.update(duration_ms=ms,timing_quality='estimated')
                ctx = (comparison_context or {}).get((chain,lane_id))
                item['latency'] = 'unknown' if item['duration_ms'] is None else 'insufficient_baseline'
                if (family != 'unknown' and ctx and len(ctx) == 3 and all(public_id(x) for x in ctx)
                        and item['duration_ms'] is not None and item['outcome'] in ('ok','failed')):
                    comparable[item['event_id']] = (family, *ctx)
        elif kind in ('verification.result','acceptance.result'):
            key = _verification(e,item)
            if key:
                stream = (chain, e['source']['adapter'], e['source']['instance'])
                runs[key].append((stream, e['seq'], item))
                if not full_context:
                    item['current'] = None
        elif kind == 'review.result':
            independent = (role == 'reviewer_record' and p.get('independent') is True and bool(p.get('basis'))
                           and bool(public_id(p.get('review_id'))) and bool(_REV.fullmatch(p.get('reviewed_head') or ''))
                           and p['reviewed_head'] == e['scope'].get('code_rev'))
            item.update(independent=independent, reviewed_head=public_id(p.get('reviewed_head')),
                        outcome=_member(p.get('review_verdict',p.get('verdict')), _STATES,'unknown')
                        if independent else 'UNVERIFIED')
        elif kind == 'usage.observed' and show_tokens:
            u = item['usage'] = _usage(e)
            if u['metric_scope'] and u['usage_record_id'] and u['basis']:
                usages[u['metric_scope']].append((chain,e['seq'],u,item))
        elif kind == 'history.superseded' and role in ('coordinator_record','owner_record'):
            if public_id(p.get('operation_id', p.get('operation_ref'))) and (public_id(p.get('replacement_event_id')) or public_id(p.get('replacement_lane_id'))):
                ghosts.update(x for x in p.get('old_event_ids',[]) if public_id(x))
        elif kind.startswith('source.'):
            item['source_id'] = (public_id(p.get('source_id')) or public_id(e['source'].get('adapter')) or 'unknown')[:80]
        elif kind == 'feedback.recorded':
            rating = p.get('rating')
            item.update(rating=rating if type(rating) is int and rating in (-1,0,1) else None,
                        target_event_id=public_id(p.get('target_event_id')),
                        category=_member(p.get('category'), contracts.FEEDBACK_CATEGORIES))
        elif kind.startswith('intervention.'):
            item.update(request_id=public_id(p.get('request_id')), target_event_id=public_id(p.get('target_event_id')),
                        queue_action_id=public_id(p.get('queue_action_id')),
                        operation=_member(p.get('operation'),contracts.INTERVENTION_OPERATIONS),
                        outcome=_member(p.get('decision'),contracts.INTERVENTION_DECISIONS))
        else:
            item['outcome'] = _member(p.get('to_state',p.get('status',p.get('decision',p.get('result')))), _STATES)
        items.append(item)

    for item in items:
        item['ghost'] = item['event_id'] in ghosts
        key = comparable.get(item['event_id'])
        if full_context and key and not item['ghost']:
            samples[key].append(item['duration_ms'])
    if full_context:
        for group in runs.values():
            live = [entry for entry in group if not entry[2]['ghost']]
            if live and len({stream for stream, _, _ in live}) == 1:
                # A backfilled older source run can arrive late. Compare observed
                # times only on one producer clock; otherwise retain origin order.
                timed = all(row['occurred_at'] for _, _, row in live)
                def run_order(entry):
                    return (entry[2]['occurred_at'] if timed else '', entry[1])
                max(live, key=run_order)[2]['current'] = True
            elif live:
                for _, _, row in live:
                    row['current'] = None
        for group in usages.values():
            live = [entry for entry in group if not entry[3]['ghost']]
            if not live:
                continue
            # Final > cumulative > delta. Different origins are not ordered;
            # copies of the same receipt can still be recognized by identity.
            basis = next(b for b in ('final','cumulative','delta') if any(e[2]['basis']==b for e in live))
            by_id = {}
            conflict = False
            for chain, seq, u, _ in live:
                if u['basis'] != basis:
                    continue
                previous = by_id.setdefault(u['usage_record_id'],(chain,seq,u))
                if previous[2] != u:
                    conflict = True
            entries = list(by_id.values())
            if conflict or (basis != 'delta' and len({e[0] for e in entries}) != 1):
                continue
            for _, _, u in (entries if basis == 'delta' else [max(entries,key=lambda e:e[1])]):
                u['counted'] = True
        baselines = {key:median(durations) for key,durations in samples.items() if len(durations)>=5}
        for item in items:
            base = baselines.get(comparable.get(item['event_id']))
            if base is not None and not item['ghost']:
                ms = item['duration_ms']
                item['latency'] = 'slow' if ms >= 2000 and ms > 3*base else 'normal'
    return items


def validate_topology(items):
    """Detect contradictions across the entire available context, not just one page."""
    parents = {}
    by_id = {item['event_id']:item['lane_id'] for item in items}
    for item in items:
        lane = item['lane_id']
        parent = item['parent_lane_id']
        parent_event = item['parent_event_id']
        if parent_event and parent_event not in by_id:
            item['parent_unresolved'] = True
        if parent and lane:
            if parents.setdefault(lane,parent) != parent:
                raise ValueError('ambiguous topology')
    visited = set()
    for lane in parents:
        path = set()
        while lane in parents and lane not in visited:
            if lane in path:
                raise ValueError('cyclic topology')
            path.add(lane)
            lane = parents[lane]
        visited.update(path)


def _lanes(items):
    lanes = {}
    for item in items:
        # R02 permits a null lane; group unattached marks by origin without
        # inventing a parent, causal link, or event. Pairing never uses this fallback.
        lane_id = item['lane_id'] or item['event_id'].rsplit(':',1)[0]
        lane = lanes.setdefault(lane_id, dict(lane_id=lane_id,parent_lane_id=None,status='unknown',
                                              source_status='unknown',marks=[]))
        parent = item['parent_lane_id']
        if parent:
            if lane['parent_lane_id'] not in (None,parent):
                raise ValueError('ambiguous topology')
            lane['parent_lane_id'] = parent
        if item['kind'] == 'lane.open': lane['status'] = 'open'
        if item['kind'] == 'lane.close': lane['status'] = 'closed'
        if item['kind'] == 'source.gap': lane['source_status'] = 'stale'
        if item['kind'] == 'source.error': lane['source_status'] = 'unavailable'
        if item.get('parent_unresolved'): lane['status'] = 'orphan'
        lane['marks'].append({k:item[k] for k in MARK})
    for lane in list(lanes.values()):
        if all(m['ghost'] for m in lane['marks']): lane['status'] = 'ghost'
        ident = lane['parent_lane_id']
        if ident and ident not in lanes:
            lanes[ident] = dict(lane_id=ident,parent_lane_id=None,status='orphan',source_status='unknown',marks=[])
    return list(lanes.values())


def build_view(items, *, completeness, missing_sources, integrity):
    lanes = _lanes(items)
    if len(lanes)>500 or sum(len(l['marks']) for l in lanes)>2000 or any(len(l['marks'])>500 for l in lanes):
        raise OverflowError('view too large')
    return dict(schema='rizzstack.rails-view/1',generated_at=contracts.utc_now(),lanes=lanes,
                completeness=completeness,missing_sources=missing_sources,integrity=integrity)


def upstream_groups(view):
    nodes = {l['lane_id']:dict(id=l['lane_id'],events=l['marks'],subs=[]) for l in view['lanes']}
    parents = []
    for lane in view['lanes']:
        parent = nodes.get(lane['parent_lane_id'])
        (parent['subs'] if parent else parents).append(nodes[lane['lane_id']])
    return {'parents':parents}
