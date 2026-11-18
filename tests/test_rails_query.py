"""Exercise the shared reader on real R02 databases and original R01 bytes."""
import json
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'contrib'))
from rizzstack_rails import RailsLedger, contracts
from rizzstack_rails.query import QueryError, make_request, query_events, query_view, query_view_page, wire_json


class LedgerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'rails.sqlite'
        self.writer = RailsLedger(self.path)
        self.addCleanup(self.writer.close)
        self.reader = RailsLedger(self.path, read_only=True)
        self.addCleanup(self.reader.close)
        self.n = 0

    def append(self, kind='decision.recorded', payload=None, scope=None, lane='bee',
               authority='worker_report', parent=None, **kw):
        self.n += 1
        return self.writer.append(kind, payload=payload or {'decision': 'observed'},
            authority=authority, scope={'attempt_id': 'A', **(scope or {})},
            lane={'lane_id': lane, 'parent_lane_id': parent}, dedupe_key=str(self.n),
            source={'adapter': 'bundlebees', 'record_id': str(self.n),
                    'instance': 'machine-1', 'revision': 'r1'}, **kw)['event_id']

    def request(self, **opts):
        return make_request('attempt', 'A', **{'format': 'json', **opts})

    def items(self, **opts):
        return query_events(self.request(**opts), self.reader)['items']

    def tool(self, kind, call, family='pytest', outcome='ok', **kw):
        return self.append(kind, dict(call_id=call, tool_name='terminal', command_family=family,
            outcome=outcome, summary='IGNORE <script> `run it`\n\x1b[31m', evidence_ref='ev:1'), **kw)


class QueryTests(LedgerCase):
    def test_frozen_origin_vectors_and_wire_schemas(self):
        # BundleBees can be tested without its sibling checkout; run this
        # cross-repo contract check whenever the reviewed artifacts are present.
        schemas = ROOT.parent / 'workflow/schemas/rails'
        if not schemas.is_dir(): self.skipTest('requires workflow frozen contract artifacts')
        vectors = json.loads((schemas/'examples.json').read_text())
        worker = vectors[0]
        # The worker-export import boundary allows observations only. A local
        # verifier is exercised separately in the authority/precedence test.
        self.writer.import_origin_page(worker['chain_id'],[contracts.canonicalize(worker).decode()])
        req = make_request('attempt', worker['scope']['attempt_id'], format='json')
        result = query_events(req,self.reader)
        items = {i['event_id']:i for i in result['items']}
        self.assertEqual(items[worker['event_id']]['lane_id'], worker['lane_id'])
        self.assertEqual(items[worker['event_id']]['origin_hash'],worker['hash'])
        view = query_view_page(req,self.reader)['items'][0]
        try:
            from jsonschema import Draft202012Validator as Validator
        except ImportError:
            self.skipTest('requires development jsonschema for frozen wire schema cross-check')
        request_schema,result_schema = json.loads((schemas/'query.schema.json').read_text())['anyOf']
        for schema,value in [(request_schema,req),(result_schema,result),
                             (json.loads((schemas/'view.schema.json').read_text()),view)]:
            Validator(schema).validate(value)

    def test_visible_source_gap_and_access_applies_to_event_lookup(self):
        event = self.append(scope={'job_id':'other'})
        self.append('source.gap', dict(source_id='launcher-log',range=[10,20],
            cursor=None,count=11,digest='b'*64,code='gap'),scope={'job_id':'J'})
        result = query_events(self.request(),self.reader,allowed_scope={'job_id':'J'})
        self.assertEqual(result['completeness'],'partial')
        self.assertEqual(result['missing_sources'],['launcher-log'])
        self.assertNotIn(event,wire_json(result))
        with self.assertRaises(QueryError) as err:
            query_events(make_request('event',event),self.reader,allowed_scope={'job_id':'J'})
        self.assertEqual(err.exception.code,'not_found')

    def test_large_selector_continues_with_explicit_bounded_context(self):
        expected = [self.append() for _ in range(7)]
        with patch('rizzstack_rails.query.MAX_CONTEXT_ROWS',4):
            req = self.request(limit=3)
            seen = []
            while True:
                page = query_events(req,self.reader)
                self.assertIn('bounded_context_no_global_reductions',page['warnings'])
                seen.extend(i['event_id'] for i in page['items'])
                if not page['has_more']: break
                req['after'] = page['next_cursor']
        self.assertEqual(seen,expected)

    def test_bounded_wire_continuation_reduces_over_its_own_context(self):
        # The gap and start both disappear from the continuation's context.
        self.append('source.gap', dict(source_id='worker',range=[1,2],cursor=None,
                    count=2,digest='a'*64,code='gap'))
        self.tool('tool.start','call',outcome='pending',occurred_at='2026-01-01T00:00:00.000000Z')
        finish = self.tool('tool.finish','call',occurred_at='2026-01-01T00:00:01.000000Z')
        for _ in range(6): self.append()
        with patch('rizzstack_rails.query.MAX_CONTEXT_ROWS',4):
            cap = len(wire_json(query_events(self.request(limit=2),self.reader)))
            first = query_events(self.request(limit=6,max_bytes=cap),self.reader)
            self.assertEqual(len(first['items']),2)
            self.assertEqual(first['missing_sources'],['worker'])
            resume = self.request(limit=4,after=first['next_cursor'])
            with closing(RailsLedger(self.path,read_only=True)) as fresh:
                expected = query_events(resume,fresh)
            actual = query_events(resume,self.reader)
            self.assertEqual(wire_json(actual),wire_json(expected))
            self.assertEqual(actual['missing_sources'],[])
            self.assertEqual(actual['items'][0]['event_id'],finish)
            self.assertIsNone(actual['items'][0]['duration_ms'])

    def test_bounded_lookahead_damage_without_new_seq_fails_closed(self):
        events = [self.append() for _ in range(8)]
        with patch('rizzstack_rails.query.MAX_CONTEXT_ROWS',4):
            cap = len(wire_json(query_events(self.request(limit=1),self.reader)))
            first = query_events(self.request(limit=6,max_bytes=cap),self.reader)
            self.assertEqual([r['event_id'] for r in first['items']],events[:1])
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute('DROP TRIGGER event_rows_no_update')
                conn.execute("UPDATE event_rows SET envelope_json=replace(envelope_json,'observed','altered') WHERE event_id=?",
                             (events[4],))  # damage in the continuation's lookahead
            with self.assertRaises(QueryError) as caught:
                query_events(self.request(limit=3,after=first['next_cursor']),self.reader)
            self.assertEqual(caught.exception.code,'source_fork')

    def test_scoped_snapshot_continuation_across_real_import_and_append(self):
        first = self.append()
        self.append(scope={'attempt_id': 'B'})
        other = RailsLedger(self.path.parent / 'peer.sqlite')
        self.addCleanup(other.close)
        for n in range(2):
            other.append('decision.recorded', scope={'attempt_id': 'A'}, payload={'decision': 'ok'},
                source={'adapter': 'peer', 'instance': 'm', 'record_id': str(n), 'revision': '1'},
                dedupe_key=str(n), authority='worker_report')
        pages = [r['envelope_json'] for r in other.query('SELECT envelope_json FROM event_rows ORDER BY seq')]
        self.writer.import_origin_page(other.chain_id, pages)
        last = self.append()
        req = self.request(limit=1)
        result = query_events(req, self.reader)
        ids = [result['items'][0]['event_id']]
        self.append()
        while result['next_cursor']:
            token = result['next_cursor']
            with self.assertRaises(QueryError):
                query_events({**req, 'selector': {'kind': 'attempt','id': 'B'}, 'after': token}, self.reader)
            with self.assertRaises(QueryError):
                query_events({**req, 'after': token}, self.reader, allowed_scope={'job_id': 'J'})
            result = query_events({**req, 'after': token}, self.reader)
            ids.extend(r['event_id'] for r in result['items'])
        self.assertEqual(ids, [first, other.chain_id+':1', other.chain_id+':2', last])
        self.assertEqual(query_events(make_request('event', ids[1]), self.reader)['items'][0]['event_id'], ids[1])

    def test_first_import_between_pages_preserves_local_only_snapshot(self):
        first, second = self.append(), self.append()
        req = self.request(limit=1)
        page = query_events(req, self.reader)
        self.assertEqual([r['event_id'] for r in page['items']], [first])
        other = RailsLedger(self.path.parent / 'new-peer.sqlite')
        self.addCleanup(other.close)
        peer = other.append('decision.recorded', scope={'attempt_id': 'A'}, payload={'decision':'ok'},
            source={'adapter':'peer','instance':'m','record_id':'1','revision':'1'},
            dedupe_key='1', authority='worker_report')['event_id']
        self.writer.import_origin_page(other.chain_id, [other.query('SELECT envelope_json FROM event_rows')[0]['envelope_json']])
        self.append()
        final = query_events({**req, 'after':page['next_cursor']}, self.reader)
        self.assertEqual([r['event_id'] for r in final['items']], [second])
        self.assertFalse(final['has_more'])
        self.assertIn(peer, [r['event_id'] for r in query_events(self.request(limit=10),self.reader)['items']])

    def test_authority_contract_head_and_snapshot_latest_failure(self):
        scope = {'contract_sha256':'a'*64, 'code_rev':'b'*40}
        def verify(verdict, **opts):
            return self.append(opts.pop('kind','verification.result'),
                {**scope, 'ac_id':'AC-1','phase':'green','run_id':f'r{self.n}',
                 'verdict':verdict,'evidence_refs':['ref:1'],'reason':'tests', **opts.pop('extra',{})},
                scope=scope, authority=opts.pop('authority','verifier_record'), **opts)
        verify('PASS'); verify('FAIL'); verify('PASS', extra={'contract_sha256':'c'*64})
        verify('PASS', extra={'code_rev':'d'*40}); verify('PASS', authority='worker_report')
        verify('PASS', kind='acceptance.result')
        rows = self.items()
        self.assertEqual([r['outcome'] for r in rows], ['PASS','FAIL'] + ['UNVERIFIED'] * 4)
        self.assertEqual([r['current'] for r in rows], [False, True, False, False, False, False])
        self.assertFalse(self.items(limit=1)[0]['current'])

    def test_pair_by_lane_only_baseline_failure_and_injection(self):
        for i, ms in enumerate((100, 98, 101, 100, 99, 2500, 2400)):
            self.tool('tool.start', str(i), outcome='pending')
            self.tool('tool.finish', str(i), duration_ms=ms, timing_quality='monotonic_measured',
                      outcome='failed' if i == 6 else 'ok')
        self.tool('tool.finish', 'ls1', family='ls', duration_ms=9000, timing_quality='monotonic_measured')
        self.tool('tool.start', 'cross', occurred_at='2026-01-01T00:00:00.000000Z', outcome='pending')
        self.tool('tool.finish', 'cross', lane='another', occurred_at='2026-01-01T00:00:02.000000Z')
        self.tool('tool.finish', 'cross', occurred_at='2026-01-01T00:00:02.000000Z')
        rows = query_events(self.request(), self.reader,
            comparison_context={(self.writer.chain_id,'bee'): ('provider','model','env')})['items']
        self.assertEqual(rows[11]['latency'], 'slow')
        self.assertEqual(rows[13]['outcome'], 'failed')
        self.assertEqual(rows[14]['latency'], 'insufficient_baseline')
        self.assertIsNone(rows[-2]['duration_ms'])
        self.assertEqual((rows[-1]['duration_ms'], rows[-1]['timing_quality']), (2000,'estimated'))
        self.assertEqual(self.items()[11]['latency'], 'insufficient_baseline')
        encoded = wire_json(query_events(self.request(format='markdown'), self.reader))
        for forbidden in ('IGNORE','<script>', 'run it', '\\u001b'):
            self.assertNotIn(forbidden, encoded)

    def test_backfill_older_success_does_not_replace_newer_failure(self):
        scope = {'contract_sha256':'a'*64, 'code_rev':'b'*40}
        for verdict, stamp in [('FAIL','12:02'),('PASS','12:01')]:
            self.append('verification.result',{**scope,'ac_id':'AC1','phase':'green',
                        'run_id':verdict, 'verdict':verdict,'evidence_refs':['ref'],'reason':'tests'},
                        authority='verifier_record',scope=scope,
                        occurred_at='2026-02-20T'+stamp+':00.000000Z')
        self.assertEqual([(r['outcome'],r['current']) for r in self.items()], [('FAIL',True),('PASS',False)])

    def test_usage_final_once_and_null_unknown(self):
        for receipt, basis, count in [('a','delta',5),('b','cumulative',9),('c','final',11),('c','final',11)]:
            self.append('usage.observed',dict(usage_record_id=receipt, metric_scope='attempt:A',
                basis=basis, tokens_in=count,tokens_out=None,tokens_thinking=None, provider='p', model='m'))
        self.assertNotIn('usage', self.items()[0])
        rows = self.items(show_tokens=True)
        counted = [r['usage'] for r in rows if r['usage']['counted']]
        self.assertEqual([(u['input_tokens'],u['output_tokens']) for u in counted], [(11,None)])

    def test_view_orphan_ghost_and_cycle_rejection(self):
        old = self.append(parent='missing')
        self.append('history.superseded', {'old_event_ids':[old], 'replacement_lane_id':'b',
            'replacement_event_id':'new:1','operation_ref':'op:1'}, lane='b',authority='coordinator_record')
        view = query_view(self.request(), self.reader)
        self.assertEqual(view['schema'], 'rizzstack.rails-view/1')
        lanes = {v['lane_id']:v for v in view['lanes']}
        self.assertEqual(lanes['bee']['parent_lane_id'], 'missing')
        self.assertTrue(lanes['bee']['marks'][0]['ghost'])
        self.assertEqual(lanes['missing']['marks'], [])
        self.assertEqual(lanes['missing']['status'], 'orphan')
        self.append(parent='bee', lane='missing')
        with self.assertRaises(QueryError) as caught:
            query_view(self.request(), self.reader)
        self.assertEqual(caught.exception.exit_code,4)

    def test_invalid_read_and_malformed_inputs_fail_closed(self):
        req = self.request()
        for key,value in [('sql','select 1'),('limit',True),('limit',501),('max_bytes',32769),
                          ('format','view'),('after','!'),('show_tokens',1),('include',[{}]),
                          ('schema','rizzstack.rails-query/2'), ('max_bytes',1)]:
            with self.subTest(key=key,value=value), self.assertRaises(QueryError):
                query_events({**req,key:value}, self.reader)
        with self.assertRaises(QueryError): query_events(req,self.writer)
        with self.assertRaises(QueryError): query_events({**req,'selector':{'kind':'attempt','id':'/tmp/private'}}, self.reader)
        with self.assertRaises(QueryError): query_events(make_request('event','no:1'), self.reader)
        self.assertEqual(query_events(req,self.reader)['completeness'],'empty')

    def test_byte_cap_whole_envelope_progress_and_empty_include(self):
        ids = [self.tool('tool.finish', str(n)) for n in range(12)]
        self.assertEqual(self.items(include=[]), [])
        req = self.request(format='markdown', max_bytes=1200)
        seen = []
        while True:
            obj = query_events(req,self.reader)
            self.assertLessEqual(len(wire_json(obj).encode()), 1200)
            self.assertTrue(obj['items'])
            seen.extend(x['event_id'] for x in obj['items'])
            if not obj['has_more']: break
            self.assertTrue(obj['truncated'])
            req['after'] = obj['next_cursor']
        self.assertEqual(seen, ids)

    def test_damage_in_selected_row_is_integrity_error(self):
        self.append()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('DROP TRIGGER event_rows_no_update')
            conn.execute("UPDATE event_rows SET envelope_json=replace(envelope_json,'observed','altered')")
        with self.assertRaises(QueryError) as caught:
            self.items()
        self.assertEqual(caught.exception.exit_code,4)
