"""M1: the factory driver, not the test body, reaches a validated RETURN."""

from __future__ import annotations

import json
import unittest

from bundlebees_plugin import factory_driver, job as jobs, queue, return_report

from tests import test_queue_factory_e2e as gatec


class FactoryDriverReturn(unittest.TestCase):
    def setUp(self):
        self.fixture = gatec.GateC("test_the_feature_is_built_verified_integrated_and_waiting_for_review")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_driver_owns_queue_to_validated_return(self):
        fx = self.fixture
        driver = factory_driver.FactoryDriver(
            fx.state, fx.queue, tick=fx.tick, startup=False)
        passes = []
        for _ in range(8):
            result = driver.run_pass()
            passes.append(result)
            item = queue.QueueStore(fx.state).read("f1")
            if item and item.get("state") == "ready_for_review":
                break
        item = queue.QueueStore(fx.state).read("f1")
        self.assertIsNotNone(item)
        self.assertEqual(item.get("state"), "ready_for_review",
                         json.dumps({"passes": passes,
                                     "tasks": [(t.get("task_id"), t.get("state"), t.get("evidence"))
                                               for t in jobs.load_job(fx.state, "job_c")["tasks"]],
                                     "integration": jobs.load_job(fx.state, "job_c").get("integration")},
                                    indent=1, default=str))
        job = jobs.load_job(fx.state, "job_c")
        self.assertTrue(job["integration"].get("accepted_waves"))
        self.assertEqual(len(fx.launcher.posts), 1,
                         "the driver must dispatch exactly once")
        manifest = return_report.build(fx.state, "job_c", repo=fx.tmp / "work")
        self.assertEqual(manifest["schema"], "rizzstack.return/1")
        self.assertEqual(manifest["receiver_decision"]["status"], "pending")
        self.assertTrue((fx.state / "jobs" / "job_c" / "RETURN.md").exists())
        statuses = {row["id"]: row["status"] for row in manifest["acceptance"]}
        self.assertTrue(statuses and all(value == "PASS" for value in statuses.values()), statuses)
        before = (len(fx.launcher.posts), job["tasks"][0]["state"])
        again = driver.run_pass()
        self.assertEqual(len(fx.launcher.posts), before[0])
        self.assertEqual(jobs.load_job(fx.state, "job_c")["tasks"][0]["state"], before[1])
        self.assertIn(again["action"], {"idle", "ready_for_review"})


if __name__ == "__main__":
    unittest.main()
