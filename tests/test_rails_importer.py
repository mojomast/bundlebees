import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'contrib'))
from rizzstack_rails import RailsLedger, contracts
from rizzstack_rails.importer import Importer
from rizzstack_rails.source import JsonlSource, SourceError


def draft(n):
    return dict(kind='decision.recorded', dedupe_key='observation:' + str(n),
                source=dict(adapter='fixture', instance='unit', record_id=str(n), revision='1'),
                scope={'job_id': 'job'}, authority='imported_observation',
                payload={'decision': 'observed'})


class ImportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'rails.db'
        self.lines = self.path.with_suffix('.jsonl')
        self.lines.write_bytes(b'{"n":1}\n{"n":2')
        self.source = JsonlSource(self.lines, adapter_id='fixture', source_instance='unit',
                                  generation='gen1', project=lambda row, pos: draft(row['n']))
        self.db = RailsLedger(self.path)
        self.addCleanup(lambda: self.db.close())
        self.imp = Importer(self.db)

    def test_read_partial_malformed_rotation_and_replay(self):
        source = self.source
        first = source.read_batch(None, 10, 4096)
        self.assertEqual(first, source.read_batch(None, 10, 4096))
        self.assertEqual(len(first['records']), 1)
        self.assertIn('partial_line', first['diagnostics'])
        self.assertEqual(self.db.head()['seq'], 0)
        self.imp.import_once(source)
        self.db.close()
        self.db = RailsLedger(self.path)
        self.imp = Importer(self.db)
        self.assertEqual(self.imp.import_once(source)['imported'], 0)
        with self.lines.open('ab') as f:
            f.write(b'}\nprivate invalid input\n{"n":3}\n')
        self.assertEqual(self.imp.import_once(source)['imported'], 3)
        rows = self.db.query('select envelope_json from event_rows')
        self.assertNotIn('private invalid', str(rows))
        self.assertEqual(sum(json.loads(r['envelope_json'])['kind'] == 'source.error' for r in rows), 1)
        self.lines.write_bytes(b'{"n":4}\n')
        self.assertEqual(self.imp.import_once(source)['imported'], 2)
        self.assertEqual(len(self.db.query("select 1 from event_rows where kind='source.gap'")), 1)
        self.assertTrue(self.db.verify_chain()['legacy_ok'])

    def test_rollback_and_loss_after_commit(self):
        def crash():
            raise RuntimeError('interrupted')
        self.db.hooks['before_commit'] = crash
        with self.assertRaises(RuntimeError):
            self.imp.import_once(self.source)
        self.assertEqual(self.db.head()['seq'], 0)
        self.assertIsNone(self.imp.cursor('fixture', 'unit'))
        self.db.hooks = {'after_commit': crash}
        with self.assertRaises(RuntimeError):
            self.imp.import_once(self.source)
        self.db.hooks = {}
        self.assertEqual(self.imp.import_once(self.source)['imported'], 0)
        self.assertEqual(self.db.head()['seq'], 2)

    def test_source_conflict_rollback_and_read_only(self):
        self.imp.import_once(self.source)
        cursor = self.imp.cursor('fixture', 'unit')
        records = [draft(2), dict(draft(1), payload={'decision': 'different'})]
        with self.assertRaises(contracts.RailsRefusal) as ex:
            self.imp.commit_batch('fixture', 'unit', records, expected_cursor=cursor, next_cursor='later')
        self.assertEqual(ex.exception.code, 'idempotency_conflict')
        self.assertEqual(self.db.head()['seq'], 2)
        self.assertEqual(self.imp.cursor('fixture', 'unit'), cursor)
        with self.assertRaises(contracts.RailsRefusal):
            self.imp.commit_batch('fixture', 'unit', [draft(3)], expected_cursor=None, next_cursor='rewind')
        reader = RailsLedger(self.path, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual(Importer(reader).cursor('fixture', 'unit'), cursor)
        with self.assertRaises(contracts.RailsRefusal):
            Importer(reader).import_once(self.source)

    def test_limits_and_authority(self):
        for count, cap in ((501, 1000), (True, 100), (1, 1048577), (0, 1)):
            with self.assertRaises(SourceError):
                self.source.read_batch(None, count, cap)
        for record in (dict(draft(1), authority='owner_record'), dict(draft(1), seq=3),
                       dict(draft(1), payload={'raw_prompt': 'no'}),
                       dict(draft(1), payload={'decision': 'x' * 65536})):
            with self.assertRaises(contracts.RailsRefusal):
                self.imp.commit_batch('fixture', 'unit', [record], expected_cursor=None, next_cursor='1')
        self.assertEqual(self.db.head()['seq'], 0)
        with self.assertRaises(SourceError):
            self.source.read_batch(None, 1, 4)

    def test_origin_gap_fork_and_receipt(self):
        origin = RailsLedger(self.path.with_suffix('.origin'))
        self.addCleanup(origin.close)
        for n in (1, 2, 3):
            origin.append(**draft(n))
        chain = origin.chain_id
        raw = [r['envelope_json'] for r in origin.query('select envelope_json from event_rows order by seq')]
        with self.assertRaises(contracts.RailsRefusal) as ex:
            self.imp.import_origin_page(chain, raw[1:])
        self.assertEqual(ex.exception.code, 'source_gap')
        receipt = self.imp.import_origin_page(chain, raw[:2])
        self.assertTrue(receipt['checkpoint_event_id'])
        self.assertTrue(self.imp.import_origin_page(chain, raw[:2])['duplicated'])
        self.assertEqual(self.db.query('select origin_bytes from imported_events where seq=2')[0]['origin_bytes'], raw[1])
        altered = json.loads(raw[1]); altered.pop('hash'); altered['payload'] = {'summary': 'changed'}
        altered['hash'] = contracts.rails_hash(altered['prev_hash'], altered)
        with self.assertRaises(contracts.RailsRefusal) as ex:
            self.imp.import_origin_page(chain, [contracts.canonicalize(altered).decode()])
        self.assertEqual(ex.exception.code, 'source_fork')
        self.assertEqual(self.db.import_cursor(chain)['state'], 'halted')

    def test_remote_source_uses_the_actual_worker_receipt_and_import_anchor(self):
        from bundlebees_plugin.rails_remote_source import RemoteRailSource
        origin = RailsLedger(self.path.with_suffix('.worker'))
        self.addCleanup(origin.close)
        receipt = origin.append(**dict(draft(1), authority='worker_report'))
        text = origin.query('select envelope_json from event_rows')[0]['envelope_json']
        chain = origin.chain_id

        def transport(url):
            replay = 'after=1&' in url
            page = dict(schema='rizzstack.rails-export/1', chain_id=chain,
                        after=int(replay), events=[] if replay else [json.loads(text)],
                        next_cursor=1, has_more=False, head={'seq':1, 'hash':receipt['hash']})
            return dict(status=200, headers={}, url=url, body=json.dumps(page).encode())

        def peer():
            return RemoteRailSource(base_url='https://peer.example', peer_id='worker-1', env={}, transport=transport)

        self.assertEqual(peer().import_into(self.imp, chain)['ledger']['imported'], 1)
        self.db.close()
        self.db = RailsLedger(self.path)
        self.assertEqual(peer().import_into(Importer(self.db), chain)['ledger']['imported'], 0)
        row = self.db.query('select origin_bytes,event_id from imported_events')[0]
        self.assertEqual((row['origin_bytes'], row['event_id']), (text, receipt['event_id']))
