import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'contrib'))
import rails_attempt_index as tool
from rizzstack_rails import RailsLedger
from rizzstack_rails.query import make_request, query_events, wire_json


class AttemptIndexTests(unittest.TestCase):
    def test_bounds_identity_lock_and_idempotent_read_write(self):
        with tempfile.TemporaryDirectory() as td, closing(RailsLedger(Path(td)/'db')) as writer:
            path = Path(td)/'db'
            def append(n):
                return writer.append('decision.recorded', payload={'decision':'observed'},
                    authority='worker_report', scope={'attempt_id':'A'}, dedupe_key=str(n),
                    source={'adapter':'bench','record_id':str(n),'instance':'m','revision':'r1'})['event_id']
            def read():
                with closing(RailsLedger(path, read_only=True)) as reader:
                    reader.query('PRAGMA trusted_schema=OFF')
                    return query_events(make_request('attempt','A',format='json'),reader)
            first = append(1)
            before = read()
            with self.assertRaises(ValueError): tool.install(path, str(uuid4()))
            with patch.object(tool,'MAX_ROWS',0), self.assertRaises(ValueError):
                tool.install(path,writer.ledger_uuid)
            writer._conn.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(sqlite3.OperationalError): tool.install(path,writer.ledger_uuid)
            finally:
                writer._conn.rollback()
            self.assertFalse(writer.query('SELECT 1 FROM sqlite_schema WHERE name=?',(tool.NAME,)))
            # Ignore host disk size in the small successful fixture only.
            with patch.object(tool.shutil,'disk_usage') as usage:
                usage.return_value.free = 3*tool.MAX_BYTES
                # Older SQLite builds can query JSON but forbid it in schema
                # expressions under trusted_schema=OFF: the optional tool must refuse.
                if not writer.query("SELECT 1 FROM pragma_function_list WHERE name='json_extract' AND flags & 0x200000"):
                    with self.assertRaises(sqlite3.DatabaseError): tool.install(path,writer.ledger_uuid)
                    self.assertFalse(writer.query('SELECT 1 FROM sqlite_schema WHERE name=?',(tool.NAME,)))
                    return
                self.assertEqual(tool.install(path,writer.ledger_uuid),'installed')
                self.assertEqual(tool.install(path,writer.ledger_uuid),'already installed')
            self.assertEqual(wire_json(before),wire_json(read()))
            writer._conn.execute('PRAGMA trusted_schema=OFF')
            second = append(2)
            self.assertEqual([r['event_id'] for r in read()['items']],[first,second])
