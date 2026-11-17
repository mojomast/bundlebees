"""Exercise the actual Bundlebees registration and profile-bound Rails entrypoints."""
import argparse
import contextlib
import io
import json
import secrets
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from bundlebees_plugin import cli, tools
from contrib.rizzstack_rails.ledger import RailsLedger


class Context:
    def __init__(self, root):
        self.bundlebees_state_dir = root
        self.tools = {}
        self.settings = {'rails_import_sources':'bundlebees,queue,outbox', 'rails_db':str(root / 'rails.sqlite')}

    def get_config(self, name, default=None):
        return self.settings.get(name, default)

    def register_tool(self, **entry):
        self.tools[entry['name']] = entry

    def register_cli_command(self, **entry):
        self.command = entry


class Registered(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = Context(self.root)
        tools.register_tools(self.ctx)
        cli.register_cli(self.ctx)

    def invoke(self, *args):
        parser = argparse.ArgumentParser()
        self.ctx.command['setup_fn'](parser)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.ctx.command['handler_fn'](parser.parse_args(args))
        return code, json.loads(output.getvalue())

    def test_real_tool_and_cli_read_same_id(self):
        db = RailsLedger(self.root/'rails.sqlite')
        event = db.append('decision.recorded', source=dict(adapter='bundlebees',instance='coordinator',record_id='r1',revision='1'),
                          dedupe_key='d', authority='coordinator_record', scope={'attempt_id':'att_1'},payload={'decision':'recorded'})
        db.close()
        handler = self.ctx.tools['bundlebees_rails']['handler']
        from rizzstack_rails.query import make_request
        query = make_request('attempt', 'att_1', format='json')
        read = json.loads(handler(query))
        code, other = self.invoke('rails','query','--kind','attempt','--id','att_1','--format','json')
        self.assertEqual(code,0)
        self.assertIn(event['event_id'], json.dumps(read['items']))
        self.assertEqual(read['items'],other['items'])
        self.assertFalse(json.loads(handler(dict(query, path=str(self.root))))['ok'])
        from bundlebees_plugin import doctor
        with patch.object(doctor,'collect',return_value=dict(schema=doctor.REPORT_SCHEMA,generated_at='test',
                 parity_ok=True,repos=[],checks=[],remediation=[])):
            code, report = self.invoke('doctor','--json')
        self.assertEqual((code,report['checks'][-1]['state']),(0,'healthy'))

    def test_no_read_or_doctor_initialization(self):
        from rizzstack_rails.query import make_request
        handler = self.ctx.tools['bundlebees_rails']['handler']
        self.assertFalse(json.loads(handler(make_request('attempt','att_1')))['ok'])
        from bundlebees_plugin import doctor
        with patch.object(doctor, 'collect', return_value=dict(schema=doctor.REPORT_SCHEMA, generated_at='test',
                 parity_ok=True, repos=[], checks=[], remediation=[])):
            _, report = self.invoke('doctor','--json')
        self.assertIn('rails',[r['check'] for r in report['checks']])
        self.assertFalse((self.root/'rails.sqlite').exists())

    def test_real_imports_resume_at_committed_page(self):
        from tests.test_rails_outbox_source import landing, record
        record(self.root, landing('attempt_one'))
        record(self.root, landing('attempt_two'))
        for expected in (1,1,0):
            self.ctx = Context(self.root)  # new CLI, new source, new SQL connection
            cli.register_cli(self.ctx)
            status, out = self.invoke('rails-import','--source','outbox','--limit','1')
            self.assertEqual((status,out.get('imported')), (0,expected),out)
        db = RailsLedger(self.root/'rails.sqlite',read_only=True)
        self.addCleanup(db.close)
        self.assertEqual(len(db.query("select 1 from event_rows where kind='attempt.state'")),2)
        self.assertEqual(len(db.query("select 1 from source_pages")),2)
        status, _ = self.invoke('rails-import','--source','launcher')
        self.assertEqual(status,3)

    def test_launcher_import_resumes_and_preserves_real_origin(self):
        origin = RailsLedger(self.root / 'origin.sqlite')
        self.addCleanup(origin.close)
        for n in (1,2):
            origin.append('decision.recorded',dedupe_key=str(n),authority='worker_report',scope={'attempt_id':'worker_a'},
                          payload={'decision':'observed'},source=dict(adapter='launcher',instance='worker',record_id=str(n),revision='1'))
        rows = [r['envelope_json'] for r in origin.query('select envelope_json from event_rows order by seq')]
        head = origin.head()
        credential = secrets.token_urlsafe(20)
        offsets = []

        class Peer(BaseHTTPRequestHandler):
            def do_GET(peer):
                u = urlsplit(peer.path)
                if peer.headers.get('Authorization') != 'Bearer ' + credential or u.path != '/v1/rails/events':
                    peer.send_error(403)
                    return
                after = int(parse_qs(u.query)['after'][0])
                offsets.append(after)
                end = min(after+1,len(rows))
                body = dict(schema='rizzstack.rails-export/1',chain_id=head['chain_id'],after=after,head=head,
                            next_cursor=end,has_more=end<len(rows),events=[json.loads(s) for s in rows[after:end]])
                peer.send_response(200)
                peer.end_headers()
                peer.wfile.write(json.dumps(body).encode())
            def log_message(self,*_):
                pass

        with HTTPServer(('127.0.0.1',0), Peer) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.ctx.settings.update(rails_import_sources='launcher',rails_launcher_chain=head['chain_id'],adapter='launcher',
                                         launcher_url=f'http://127.0.0.1:{server.server_port}')
                with patch('bundlebees_plugin.client.get_launcher_token',
                           side_effect=lambda name: credential if name=='BUNDLEBEES_RAILS_READ_TOKEN' else None):
                    for expected in (1,1,0):
                        rc,result = self.invoke('rails-import','--source','launcher','--limit','1')
                        self.assertEqual((rc,result.get('imported')),(0,expected),result)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(offsets,[0,1,2])
        replica = RailsLedger(self.ctx.settings['rails_db'],read_only=True)
        self.addCleanup(replica.close)
        self.assertEqual([r['origin_bytes'] for r in replica.query('select origin_bytes from imported_origin_rows order by seq')],rows)

    def test_registered_hermes_enrollment_reads_only_named_session(self):
        source = self.root/'hermes.sqlite'
        with contextlib.closing(sqlite3.connect(source)) as cx:
            cx.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,parent_session_id TEXT,started_at REAL,ended_at REAL);
              CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,tool_calls TEXT,tool_call_id TEXT,timestamp REAL,display_kind TEXT);
              INSERT INTO sessions VALUES ('native-one','cli',NULL,1,NULL),('native-private','cli',NULL,2,NULL);''')
        self.ctx.settings.update(rails_hermes_db=str(source), rails_import_sources='hermes',rails_session_aliases='{"native-one":"public-one"}')
        code,out=self.invoke('rails-import','--source','hermes')
        self.assertEqual(code,0,out)
        _,result=self.invoke('rails','query','--kind','session','--id','public-one','--format','json')
        self.assertIn('lane.open',json.dumps(result['items']))
        self.assertNotIn('native-',json.dumps(result))
