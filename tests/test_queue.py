"""Queue intake, durable claim and one-pass consumption."""

from __future__ import annotations

import json
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
