import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bundlebees_plugin import rails_cli, rails_tool
from contrib.reference_coordinator.rails_query_example import query
from rizzstack_rails import RailsLedger
from rizzstack_rails.query import make_request

ROOT = Path(__file__).resolve().parents[1]


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / 'data.sqlite'
        ledger = RailsLedger(self.db)
        self.event = ledger.append('decision.recorded', authority='worker_report', dedupe_key='key',
            payload={'decision':'private words should not appear'},scope={'attempt_id':'A'},
            source={'adapter':'launcher-log','record_id':'r1','instance':'m','revision':'r1'})['event_id']
        ledger.close()
        self.env = {'RIZZSTACK_RAILS_DB':str(self.db)}

    def test_real_hermes_registration_cli_module_and_coordinator_agree(self):
        request = make_request('attempt','A',format='json')
        class Context:
            def register_tool(ctx, **spec): ctx.spec = spec
            def get_config(ctx, key, default): return {'rails_db':str(self.db)}.get(key,default)
        ctx = Context()
        rails_tool.register_tools(ctx)
        self.assertEqual(ctx.spec['name'],'bundlebees_rails')
        tool = json.loads(ctx.spec['handler'](request))
        reader = RailsLedger(self.db,read_only=True)
        self.addCleanup(reader.close)
        reference = query(request,reader)
        args = ['query','--kind','attempt','--id','A','--format','json']
        for cwd, module in [(ROOT/'contrib','rizzstack_rails'),(ROOT,'bundlebees_plugin.rails_cli')]:
            env = dict(os.environ, **self.env); env.pop('PYTHONPATH',None)
            proc = subprocess.run([sys.executable,'-m',module,*args],env=env,cwd=cwd,capture_output=True,text=True)
            self.assertEqual(proc.returncode,0,proc.stderr)
            self.assertEqual(json.loads(proc.stdout),reference)
        parser = argparse.ArgumentParser()
        rails_cli.add_parsers(parser.add_subparsers(dest='command'))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(rails_cli.cmd_rails(ctx,parser.parse_args(['rails',*args])),0)
        self.assertEqual(json.loads(stdout.getvalue()),tool)
        self.assertEqual(reference,tool)
        self.assertEqual([r['event_id'] for r in tool['items']],[self.event])
        self.assertNotIn('private words',json.dumps(tool))

    def test_no_raw_fields_no_connection_override_no_unbounded_results(self):
        with patch.dict(os.environ,self.env):
            request = make_request('attempt','A',format='json')
            for bad in [{'db': '/tmp/SECRET'}, {'raw_prompt':'SECRET'}, {'max_bytes': 65536},
                        {'after': 'SECRET'}, {'selector':{'kind':'attempt','id':'http://SECRET'}}]:
                response = rails_tool.handle({**request,**bad})
                self.assertFalse(json.loads(response)['ok'])
                self.assertNotIn('SECRET', response)
            duplicate = '{"selector":{},"selector":{"kind":"attempt","id":"A"}}'
            self.assertFalse(json.loads(rails_tool.handle(duplicate))['ok'])

    def test_missing_config_does_not_create_db_or_leak_path(self):
        missing = self.db.parent/'private-operator-path'
        env = dict(os.environ,RIZZSTACK_RAILS_DB=str(missing)); env.pop('PYTHONPATH',None)
        for extra,code in [([],3),(['--db','secret-path'],2)]:
            proc = subprocess.run([sys.executable,'-m','bundlebees_plugin.rails_cli','query',
                '--kind','attempt','--id','A',*extra], env=env,cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(proc.returncode,code)
            self.assertFalse(missing.exists())
            self.assertNotIn('private-operator-path',proc.stdout+proc.stderr)
            self.assertNotIn('secret-path',proc.stdout+proc.stderr)
