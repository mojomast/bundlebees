"""Queue intake, durable claim and one-pass consumption."""

from __future__ import annotations

import json
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from bundlebees_plugin import job_cli
from bundlebees_plugin import queue as Q


def card(feature_id, job_id="job_1", deps=(), status="queued", repo="mojomast/smoke-real"):
    return {"repo": repo, "job_id": job_id, "depends_on": list(deps), "status": status}

class QueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "queue.json"
        self.path.write_text(json.dumps({"f1": card("f1"), "f2": card("f2", deps=["f1"], status="queued")}))
        self.queue = Q.JsonQueue(self.path)

    def test_a_claim_is_exclusive(self):
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        with self.assertRaises(Q.QueueError) as caught:
            store.claim("f1", job_id="job_1")
        self.assertEqual(caught.exception.code, "already_claimed")

    def test_eligibility_respects_dependencies(self):
        items = self.queue.list_items()
        self.assertEqual(Q.eligible(items, done=set()).feature_id, "f1")
        self.assertIsNone(Q.eligible([i for i in items if i.feature_id == "f2"], done=set()))
        self.assertEqual(Q.eligible(items, done={"f1"}).feature_id, "f2")

    def test_run_once_is_idle_without_queued_work(self):
        for item in self.queue.list_items():
            self.queue.set_status(item.feature_id, "ready_for_review")
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(result["action"], "idle")

    def test_run_once_claims_and_calls_the_production_tick(self):
        called = []
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)

        def tick(job_id):
            called.append(job_id)
            return {"status": "ok", "transitions": ["T1->dispatched"]}

        result = Q.run_once(self.tmp, self.queue, tick=tick)

        self.assertEqual(result["feature"], "f1")
        self.assertEqual(called, ["job_1"])
        record = Q.QueueStore(self.tmp).read("f1")
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["job_id"], "job_1")
        self.assertEqual(self.queue.get("f1").status, "running")

    def test_a_claimed_card_whose_job_is_missing_blocks_instead_of_dispatching(self):
        Q.QueueStore(self.tmp).claim("f1", job_id="job_missing")
        called = []
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id))

        self.assertEqual(result["action"], "blocked")
        self.assertEqual(called, [])
        self.assertFalse(self.queue.get("f1").status == "ready_for_review")

    def test_a_second_pass_does_not_duplicate_the_tick_for_a_claimed_card(self):
        calls = []
        Q.QueueStore(self.tmp).write("f1", {"state": "running", "job_id": "job_1"})
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)

        def tick(job_id):
            calls.append(job_id)
            return {"status": "ok"}

        Q.run_once(self.tmp, self.queue, tick=tick)
        self.assertEqual(len(calls), 1, "a claimed card must not be claimed again")

    def test_a_queue_status_alone_is_not_completion_evidence(self):
        # The card says ready_for_review, but the job has no accepted evidence:
        # queue.run_once never treats that as completion for another item.
        self.queue.set_status("f1", "ready_for_review")
        self.path.write_text(json.dumps({"f2": card("f2", status="queued")}))
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(result["feature"], "f2")

    def test_the_kanban_adapter_parses_supported_json(self):
        class Out:
            returncode = 0
            stdout = json.dumps([{"id": "f9", "title": "Feature", "status": "queued",
                                  "metadata": {"repo": "mojomast/smoke-real", "job_id": "job_9"}}])
            stderr = ""

        adapter = Q.KanbanQueue(lambda args: Out())
        item = adapter.list_items()[0]
        self.assertEqual((item.feature_id, item.job_id), ("f9", "job_9"))

    def test_an_unparseable_kanban_response_is_an_error_not_an_empty_queue(self):
        class Out:
            returncode = 0
            stdout = "not json"
            stderr = ""

        with self.assertRaises(Q.QueueError) as caught:
            Q.KanbanQueue(lambda args: Out()).list_items()
        self.assertEqual(caught.exception.code, "kanban_invalid")

    def test_a_running_feature_continues_to_be_advanced(self):
        Q.QueueStore(self.tmp).write("f1", {"state": "running", "job_id": "job_1"})
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)
        accepted = mock.patch.object(Q, "_job_accepted", lambda state_dir, job_id: True)
        accepted.start()
        self.addCleanup(accepted.stop)
        called = []

        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, ["job_1"])
        self.assertEqual(result["action"], "ready_for_review")
        self.assertEqual(self.queue.get("f1").status, "ready_for_review")

    def test_dependencies_unlock_on_job_evidence_not_card_status(self):
        self.queue.set_status("f1", "ready_for_review")   # card says done
        accepted = mock.patch.object(Q, "_job_accepted", lambda state_dir, job_id: False)
        accepted.start()
        self.addCleanup(accepted.stop)
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)
        called = []

        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertNotIn("job_2", called, "f2 must not run while f1 has no accepted evidence")
        self.assertIn(result["action"], {"idle", "blocked"})

    def test_completion_requires_real_verification_and_integration_evidence(self):
        from bundlebees_plugin import verify_local

        record = {"attempt_id": "att_1", "ok": True, "sha": "a" * 40, "base_rev": "b" * 40,
                  "files": ["smoke/answer.py"], "tests": ["tests/test_smoke.py"],
                  "suite": {"exit_code": 0}, "gitleaks": {"exit_code": 0}, "mode": "commit-local"}
        path = verify_local.record_path(self.tmp / "state", "att_1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

        task = {"task_id": "T1", "state": "accepted", "attempts": ["att_1"],
                "contract_sha256": "c" * 64,
                "binding": {"source_rev": "b" * 40, "contract_sha256": "c" * 64}}
        job = {"tasks": [task], "integration": {"accepted_waves": [{"wave": 1, "commit": "d" * 40}],
                                                "last_commit": "d" * 40}}

        with mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id: job):
            self.assertTrue(Q._job_accepted(self.tmp / "state", "j"))
            for broken in (
                {**job, "integration": {"accepted_waves": []}},
                {**job, "tasks": [{**task, "state": "verified"}]},
                {**job, "tasks": [{**task, "binding": {"source_rev": "e" * 40, "contract_sha256": "c" * 64}}]},
            ):
                with mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id, b=broken: b):
                    self.assertFalse(Q._job_accepted(self.tmp / "state", "j"), broken)

        path.write_text(json.dumps({**record, "gitleaks": {"exit_code": 1}}))
        with mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id: job):
            self.assertFalse(Q._job_accepted(self.tmp / "state", "j"), "a failed secret scan cannot complete")

    def test_queue_run_refuses_to_start_without_a_scanner(self):
        with TemporaryDirectory() as tmp:
            args = type("A", (), {"command": "queue", "queue_handler": "run", "once": True, "repo": tmp,
                                  "state_dir": tmp, "backend": "json", "gitleaks": None})()
            ctx = type("C", (), {"config": {}})()
            with mock.patch("shutil.which", lambda name: None):
                code = job_cli.cmd_queue(ctx, args, state_dir=Path(tmp), client_factory=lambda: None)
        self.assertEqual(code, 2)

    # -- a tick's own status is not progress evidence ----------------------

    def _claimed(self, tick, *, task_state="dispatched"):
        """Claim f1, then run one pass with a job row in ``task_state``."""
        job = {"job_id": "job_1", "tasks": [{"task_id": "T1", "state": task_state}]}
        patcher = mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id: job)
        patcher.start()
        self.addCleanup(patcher.stop)
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        self.queue.set_status("f1", "claimed")
        return store, Q.run_once(self.tmp, self.queue, tick=tick)

    def test_an_escalated_tick_blocks_the_card_instead_of_marking_it_running(self):
        store, result = self._claimed(lambda job_id: {"status": "escalated", "action": "integrate",
                                                      "detail": "two integration attempts failed"})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")
        self.assertIn("integration attempts failed", store.read("f1")["evidence"]["detail"])

    def test_an_awaiting_decision_tick_blocks_the_card(self):
        store, result = self._claimed(lambda job_id: {"status": "awaiting_decision", "action": "recovery"})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_a_locked_tick_defers_without_rewriting_the_claim(self):
        store, result = self._claimed(lambda job_id: {"status": "locked", "detail": "another live tick"})
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(self.queue.get("f1").status, "claimed", "a pass that did not run must not re-status the card")
        self.assertEqual(store.read("f1")["state"], "claimed")

    def test_an_idle_tick_with_a_worker_in_flight_keeps_the_card_running(self):
        store, result = self._claimed(lambda job_id: {"status": "idle", "detail": "nothing to do"})
        self.assertEqual(result["action"], "running")
        self.assertEqual(self.queue.get("f1").status, "running")

    def test_an_idle_tick_with_no_worker_in_flight_blocks_the_card(self):
        # A verified row is integration work, not a worker; the tick said idle,
        # so integration declined to advance and nothing will move this job
        # without a decision.
        store, result = self._claimed(lambda job_id: {"status": "idle", "detail": "integration budget exhausted"},
                                      task_state="verified")
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")
        self.assertIn("integration budget exhausted", store.read("f1")["evidence"]["detail"])

    def test_an_idle_tick_on_a_rejected_job_blocks_the_card(self):
        store, result = self._claimed(lambda job_id: {"status": "idle"}, task_state="rejected")
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_a_requeued_card_names_its_existing_claim_instead_of_raising(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "blocked", "job_id": "job_1"})
        called = []
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id) or {"status": "ok"})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(called, [])
        self.assertIn("blocked", result["detail"])
        # The stale record is not deleted; the card is made visibly blocked so
        # it cannot be re-admitted on every pass and starve later work.
        self.assertEqual(self.queue.get("f1").status, "blocked", "a refused admission must move the card out of queued")

    def test_a_refused_card_does_not_starve_the_next_eligible_card(self):
        self.path.write_text(json.dumps({
            "f1": card("f1"),
            "f2": card("f2", job_id="job_2"),
        }))
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "blocked", "job_id": "job_1"})
        calls = []

        def tick(job_id):
            calls.append(job_id)
            return {"status": "ok"}

        with mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id}):
            first = Q.run_once(self.tmp, self.queue, tick=tick)
            second = Q.run_once(self.tmp, self.queue, tick=tick)

        self.assertEqual(first["action"], "blocked")
        self.assertEqual(first["feature"], "f1")
        self.assertEqual(second["feature"], "f2")
        self.assertEqual(calls, ["job_2"])

    def test_a_claim_held_for_another_job_is_refused(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "running", "job_id": "job_other"})
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(result["action"], "blocked")
        self.assertIn("job_other", result["detail"])
        self.assertIn("job_1", result["detail"])
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_a_failed_card_write_leaves_the_claim_active_for_a_retry(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "running", "job_id": "job_1"})
        self.queue.set_status("f1", "claimed")

        class BrokenQueue:
            def list_items(self):
                return self._queue.list_items()

            def set_status(self, *args, **kwargs):
                raise RuntimeError("card write failed")

        broken = BrokenQueue()
        broken._queue = self.queue

        with self.assertRaises(RuntimeError):
            Q.run_once(self.tmp, broken, tick=lambda job_id: {"status": "escalated", "detail": "terminal"})

        # The record must remain active when the card could not be acknowledged;
        # otherwise the next pass reports idle and the card stays claimed forever.
        self.assertEqual(store.read("f1")["state"], "running")
        self.assertEqual(self.queue.get("f1").status, "claimed")

    def test_a_pass_without_a_dispatch_capable_tick_claims_nothing(self):
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "idle"}, admit=False)
        self.assertEqual(result["action"], "observation_only")
        self.assertIsNone(Q.QueueStore(self.tmp).read("f1"))
        self.assertEqual(self.queue.get("f1").status, "queued")


if __name__ == "__main__":
    unittest.main()
