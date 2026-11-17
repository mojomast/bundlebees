"""Local recovery handoff: real queue, real sealed job and create-only decision."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bundlebees_plugin import coordinator_actions as queue, job, recovery, rails_actions
from bundlebees_plugin.rails_queue_source import QueueSource
from tests.test_recovery_conformance import build, new_state
from contrib.rizzstack_rails.ledger import RailsLedger


class Actions(unittest.TestCase):
    def setUp(self):
        protocol = Path(os.environ.get('WORKFLOW_SRC') or Path(__file__).resolve().parents[2] / 'workflow')
        if not (protocol / 'tools/recovery.py').is_file():
            self.skipTest('requires a local WORKFLOW_SRC protocol checkout')
        env = patch.dict(os.environ, WORKFLOW_SRC=str(protocol))
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch('sys.stdin.isatty', return_value=True):
            self.state, _ = new_state(Path(tmp.name))
        self.req = build(self.state, evidence='worker implementation failed', code='tests_failed')
        self.db = RailsLedger(self.state / 'rails.sqlite')
        self.addCleanup(self.db.close)
        self.target = self.db.append('attempt.state',
            dedupe_key='target', source=dict(adapter='bundlebees', instance='coordinator', record_id='r1', revision='1'),
            authority='coordinator_record', scope={'attempt_id':'att_0', 'job_id':'job_a'},
            payload=dict(from_state='unknown', to_state='rejected', operation_id='obs_1',
                         fencing_epoch=0, code='observed', evidence_ref='ref_1'))['event_id']
        self.authorized = True
        self.service = rails_actions.RetryHandoff(self.state, self.db, authorize=lambda target, req, op:
            self.authorized and (target, req, op) == (self.target,self.req['request_id'],'retry'))

    def preview(self):
        return self.service.preview(self.target, self.req['request_id'])

    def submit(self):
        preview = self.preview()
        return self.service.submit(self.target, self.req['request_id'], preview['revision'])['queue_action_id']

    def test_replay_after_enqueue_before_audit(self):
        p = self.preview()
        self.assertFalse(queue.actions_dir(self.state).exists())
        first = self.service.submit(self.target, self.req['request_id'], p['revision'])
        second = self.service.submit(self.target, self.req['request_id'], p['revision'])
        self.assertEqual(first, second)
        self.assertEqual(len(queue.list_actions(self.state)), 1)
        source = QueueSource(self.state)
        facts = source.read_batch(None, 10, 100000)['records']
        self.assertEqual([r['kind'] for r in facts], ['intervention.requested'])
        self.assertEqual(facts, QueueSource(self.state).read_batch(None, 10, 100000)['records'])
        self.assertNotIn('fence_seq', str(facts))
        self.assertIsNone(recovery.read_decision(self.state, self.req))

    def test_import_publishes_request_and_then_decision_once(self):
        from rizzstack_rails.importer import Importer
        aid = self.submit()
        def publish():
            imp = Importer(self.db, authorities={'coordinator_record','imported_observation'})
            # Reopen the source and let the importer resolve the committed cursor.
            return imp.import_once(QueueSource(self.state))['imported']
        self.assertEqual(publish(), 1)
        self.service.commit(aid, self.service.claim(aid))
        self.assertEqual(publish(), 1)
        self.assertEqual(publish(), 0)
        self.assertEqual([r['kind'] for r in self.db.query(
            "select kind from event_rows where kind like 'intervention.%' order by seq")],
            ['intervention.requested','intervention.result'])

    def test_stale_fence_and_uncertain_operation(self):
        action = self.submit()
        fence = self.service.claim(action, now=1)
        replacement = queue.adopt(self.state, action, 'reconciler', now=5000)
        with self.assertRaisesRegex(rails_actions.Refused, 'stale_fence'):
            self.service.commit(action, fence)
        with self.assertRaisesRegex(rails_actions.Refused, 'reconcile_required'):
            self.service.reconcile(action, replacement['fence_seq'])
        self.assertIsNone(recovery.read_decision(self.state, self.req))

    def test_authority_checked_at_every_boundary(self):
        act = self.submit()
        self.authorized = False
        with self.assertRaisesRegex(rails_actions.Refused, 'forbidden'):
            self.service.claim(act)
        self.authorized = True
        fence = self.service.claim(act)
        job.authorization_path(self.state, 'job_a').unlink()
        with self.assertRaises(rails_actions.Refused):
            self.service.commit(act, fence)
        self.assertIsNone(recovery.read_decision(self.state, self.req))

    def test_crash_after_decision_reconciles_receipt_without_reissue(self):
        act = self.submit()
        fence = self.service.claim(act)
        with patch.object(queue, 'complete', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.service.commit(act, fence)
        decision = recovery.read_decision(self.state, self.req)
        self.assertEqual(decision['decision'], 'retry')
        with patch.object(recovery, 'record_decision', side_effect=AssertionError('must not replay')):
            self.service.reconcile(act, fence)
        records = QueueSource(self.state).read_batch(None, 20, 100000)['records']
        self.assertEqual([r['kind'] for r in records], ['intervention.requested','intervention.result'])
        self.assertEqual(records[-1]['payload']['decision'], 'approved')
        self.assertEqual(job.load_job(self.state,'job_a',recover=False)['tasks'][0]['state'], 'rejected')

    def test_changed_preview_and_unsupported_rewind_fail_closed(self):
        with self.assertRaisesRegex(rails_actions.Refused, 'stale_preview'):
            self.service.submit(self.target, self.req['request_id'], '0'*64)
        with self.assertRaisesRegex(rails_actions.Refused, 'capability_unavailable'):
            self.service.preview(self.target, self.req['request_id'], operation='rewind_retry')
        self.assertEqual(queue.list_actions(self.state), [])
