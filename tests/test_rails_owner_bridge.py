"""An independently enrolled signer and genuine sealed coordinator authorization."""
import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from bundlebees_plugin import job, rails_owner_bridge as bridge
from rizzstack_rails import RailsLedger
from rizzstack_rails.feedback import history


class OwnerBridge(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.ws = str(uuid4()); self.state = self.root/'state'; self.state.mkdir()
        with patch('sys.stdin.isatty', return_value=True):
            job.record_authorization(self.state, job_id='job1', by='alice', statement='Run the fixture only.',
                repo='public/example', base_rev='a'*40, scope=['tests/*'], bees=1, wall_clock_s=60)
        self.path = self.root/'rails.sqlite'
        self.db = RailsLedger(self.path); self.addCleanup(self.db.close)
        self.target = self.db.append('tool.finish',source={'adapter':'launcher-log','instance':'worker-1','record_id':'ev1','revision':'v1'},
            scope={'job_id':'job1','attempt_id':'att1'},lane={'lane_id':'att1'},dedupe_key='tool-1',authority='worker_report',
            payload={'call_id':'c1','tool_name':'shell','command_family':'git','outcome':'ok','summary':'ok','evidence_ref':'ref1'})['event_id']
        self.key=self.root/'signer';subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(self.key)],check=True)
        pub=self.key.with_suffix('.pub').read_text().split()[:2]
        self.signers=self.root/'allowed_signers';self.signers.write_text(f'{self.ws}:alice namespaces="rizzstack-rails-feedback-v1" '+ ' '.join(pub)+'\n');self.signers.chmod(0o600)
        self.config=self.root/'profile';self.config.write_text(json.dumps(dict(workspace_id=self.ws,owner='alice',ledger_uuid=self.db.ledger_uuid,
            ledger=str(self.path),state_dir=str(self.state),signers=str(self.signers))));self.config.chmod(0o600)
        self.draft=dict(workspace_id=self.ws,target_event_id=self.target,rating=1,category='quality',note='',idempotency_key=str(uuid4()))

    def sign(self,review):
        return subprocess.run(['ssh-keygen','-Y','sign','-n',bridge.NAMESPACE,'-f',str(self.key)],input=review.encode(),
                              stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=True).stdout.decode()

    def test_discarded_receipt_replay_is_one_durable_record(self):
        prepared=bridge.handle(self.config,dict(action='preview',draft=self.draft));text=prepared['review'];sig=self.sign(text)
        submit=dict(action='confirm',review=text,signature=sig)
        with self.assertRaises(bridge.Refusal): bridge.handle(self.config,dict(submit,review=text.replace('"rating":1','"rating":0')))
        # Lost response after commit. A fresh invocation receives the original identity.
        one=bridge.handle(self.config,submit);two=bridge.handle(self.config,submit)
        self.assertEqual(one['event_id'],two['event_id']);self.assertTrue(two['duplicated']);self.assertEqual(len(history(self.db,self.target)),1)
        self.signers.unlink()
        with self.assertRaises(bridge.Refusal): bridge.handle(self.config,submit)

    def test_wrong_workspace_signer_coordinator_key_and_unavailable_retry(self):
        def preview(d):return bridge.handle(self.config,dict(action='preview',draft=d))
        with self.assertRaises(bridge.Refusal):preview(dict(self.draft,workspace_id=str(uuid4())))
        review=preview(self.draft)['review'];sig=self.sign(review)
        self.signers.write_text(self.signers.read_text().replace(':alice',':bob'))
        with self.assertRaises(bridge.Refusal):bridge.handle(self.config,dict(action='confirm',review=review,signature=sig))
        with self.assertRaisesRegex(bridge.Refusal,'capability_unavailable'):bridge.handle(self.config,dict(action='retry'))
        self.signers.write_text(self.signers.read_text().replace(':bob',':alice'))
        (self.state/'lease.key').write_bytes(secrets.token_bytes(32))
        with self.assertRaises(bridge.Refusal):bridge.handle(self.config,dict(action='confirm',review=review,signature=sig))
        self.assertFalse(history(self.db,self.target))
