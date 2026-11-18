import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'contrib'))
from bundlebees_plugin.rails_hermes_source import HermesSource
from rizzstack_rails import RailsLedger
from rizzstack_rails.importer import Importer
from rizzstack_rails.source import SourceError


class HermesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'fixture ?#.db'
        with closing(sqlite3.connect(self.path)) as cx:
            cx.executescript('''
                CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT,
                    started_at REAL, ended_at REAL, title TEXT);
                CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT,
                    timestamp REAL, display_kind TEXT, active INTEGER DEFAULT 1);
                INSERT INTO sessions VALUES ('p','cli',NULL,100,NULL,'private title'),
                    ('k','subagent','p',101,NULL,NULL), ('r','subagent','k',102,110,NULL),
                    ('not-allowed','subagent','p',101,NULL,'private title');
                INSERT INTO messages VALUES (1,'p','user','private prompt',NULL,NULL,NULL,101,NULL,1),
                    (2,'r','assistant','private reasoning',NULL,NULL,NULL,102,NULL,1),
                    (3,'r','tool','private output',NULL,'opaque-a','terminal',104,NULL,1),
                    (4,'not-allowed','tool','private result',NULL,'opaque-b','read_file',105,NULL,1);
            ''')
            cx.execute('UPDATE messages SET tool_calls=? WHERE id=2', (json.dumps([
                {'id':'opaque-a','function':{'name':'terminal','arguments':'{"command":"pytest private_argument"}'}},
                {'id':'opaque-b','function':{'name':'private-tool','arguments':'private args'}}]),))
            cx.commit()
        self.source = HermesSource(self.path, {'p':'root', 'k':'child', 'r':'reviewer'})
        self.ledger = RailsLedger(self.path.with_name('rails.sqlite'))
        self.addCleanup(self.ledger.close)

    def walk(self, limit=1):
        imp = Importer(self.ledger)
        for _ in range(100):
            if not imp.import_once(self.source, limit=limit)['has_more']:
                return
        self.fail('pagination failed to terminate')

    def events(self):
        return [json.loads(r['envelope_json']) for r in self.ledger.query('select envelope_json from event_rows order by seq')]

    def test_private_readonly_paging_pairing_and_topology(self):
        before = self.path.read_bytes()
        page = self.source.read_batch(None, 1, 4096)
        self.assertEqual(page, self.source.read_batch(None, 1, 4096))
        self.walk()
        self.assertEqual(before, self.path.read_bytes())
        events = self.events()
        self.assertNotIn('not-allowed', str(events)); self.assertNotIn('private', str(events))
        opens = {e['scope']['session_id']: e for e in events if e['kind']=='lane.open'}
        self.assertEqual(opens['reviewer']['parent_lane_id'], opens['child']['lane_id'])
        self.assertEqual(opens['child']['parent_lane_id'], opens['root']['lane_id'])
        starts = [e for e in events if e['kind']=='tool.start']
        ends = [e for e in events if e['kind']=='tool.finish']
        self.assertEqual(len(starts), 2); self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['payload']['call_id'], starts[0]['payload']['call_id'])
        self.assertEqual(starts[0]['payload']['command_family'], 'pytest')
        self.assertEqual(ends[0]['payload']['outcome'], 'unknown')
        self.assertEqual(ends[0]['duration_ms'], 2000)
        self.assertEqual(self.source.health()['state'], 'live')

    def test_rewind_and_restore_use_real_immutable_event_ids(self):
        self.walk(500)
        before = self.events()
        old = {e['event_id'] for e in before if e['kind'] in ('tool.start','tool.finish')}
        self.walk(500)
        self.assertEqual(before, self.events())
        with closing(sqlite3.connect(self.path)) as cx:
            cx.execute('UPDATE messages SET active=0 WHERE id=2'); cx.commit()
        self.walk(500)
        changed = self.events()
        ghosts = [e for e in changed if e['kind']=='history.superseded']
        self.assertTrue(old.issubset({x for g in ghosts for x in g['payload']['old_event_ids']}))
        ids = {e['event_id'] for e in changed}
        self.assertTrue(all(g['payload']['replacement_event_id'] in ids for g in ghosts))
        with closing(sqlite3.connect(self.path)) as cx:
            cx.execute('UPDATE messages SET active=1 WHERE id=2'); cx.commit()
        self.walk(500)
        self.assertTrue(any(e['kind']=='tool.start' and e['event_id'] not in old for e in self.events()))
        self.assertTrue(self.ledger.verify_chain()['ok'])

    def test_schema_variants_and_missing_db(self):
        with closing(sqlite3.connect(self.path)) as cx:
            cx.execute('ALTER TABLE messages DROP COLUMN active')
        self.walk(500)
        with closing(sqlite3.connect(self.path)) as cx:
            cx.execute('ALTER TABLE messages RENAME COLUMN role TO other')
        with self.assertRaises(SourceError) as ex:
            self.source.read_batch(None, 10, 8192)
        self.assertEqual(ex.exception.code, 'unsupported_source_version')
        self.assertEqual(self.source.health()['error_code'], 'unsupported_source_version')
        missing = self.path.with_name('absent.db')
        with self.assertRaises(SourceError):
            HermesSource(missing, ['root']).read_batch(None, 1, 2048)
        self.assertFalse(missing.exists())
        with self.assertRaises(SourceError):
            HermesSource(self.path, [])

    def test_pristine_public_reference(self):
        root = ROOT / 'contrib/vibeline'
        manifest = json.loads((root / 'PROVENANCE.json').read_text())
        self.assertEqual(manifest['pin'], 'd5d0f425a64906ae1a50c1222314a5420da404d5')
        self.assertEqual(set(manifest['sha256']), {'LICENSE','paths.py','sessions.py','swarm.py','shell.py'})
        for name, digest in manifest['sha256'].items():
            self.assertEqual(hashlib.sha256((root / name).read_bytes()).hexdigest(), digest)

    def test_live_wal_and_byte_cap(self):
        # Only this synthetic writer creates the journals. The read must see
        # the committed WAL change (immutable=1 or copying the DB would not).
        with closing(sqlite3.connect(self.path)) as writer:
            self.assertEqual(writer.execute('pragma journal_mode=wal').fetchone()[0], 'wal')
            writer.execute('UPDATE sessions SET ended_at=120 WHERE id=?', ('k',)); writer.commit()
            files = [self.path, Path(str(self.path) + '-wal')]
            before = [f.read_bytes() for f in files]
            batch = self.source.read_batch(None, 500, 4000)
            self.assertLessEqual(len(json.dumps(batch, sort_keys=True, separators=(',', ':')).encode()), 4000)
            self.walk(500)
            self.assertEqual(before, [f.read_bytes() for f in files])
            self.assertTrue(any(e['kind']=='lane.close' and e['scope']['session_id']=='child' for e in self.events()))

    def test_reused_call_id_is_never_paired_twice(self):
        with closing(sqlite3.connect(self.path)) as cx:
            cx.execute('UPDATE messages SET id=5 WHERE id=3')
            cx.execute('INSERT INTO messages SELECT 3,session_id,role,content,tool_calls,tool_call_id,'
                       'tool_name,timestamp,display_kind,active FROM messages WHERE id=2')
            cx.commit()
        self.walk(500)
        self.assertFalse(any(e['kind']=='tool.finish' for e in self.events()))
        self.assertTrue(any(e['kind']=='source.error' and e['payload']['code']=='ambiguous_tool_result'
                            for e in self.events()))
