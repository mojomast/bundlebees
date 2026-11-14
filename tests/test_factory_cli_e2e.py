"""M1 CLI entry point: real job tick and provisioning binding, one pass at a time."""

from __future__ import annotations

import sys
import unittest

from bundlebees_plugin import client as client_mod
from bundlebees_plugin import factory_driver
from bundlebees_plugin import job as jobs
from bundlebees_plugin import provisioning as provisioning_mod
from bundlebees_plugin import queue, return_report

from tests import test_queue_factory_e2e as gatec


class FactoryDriverCliReturn(unittest.TestCase):
    def setUp(self):
        self.fixture = gatec.GateC(
            "test_the_feature_is_built_verified_integrated_and_waiting_for_review"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_cli_binds_real_tick_and_provisioning(self):
        fx = self.fixture
        originals = (client_mod.LauncherClient, provisioning_mod.from_client)
        client_mod.LauncherClient = lambda *args, **kwargs: fx.launcher
        provisioning_mod.from_client = lambda config, client: fx.provisioner
        self.addCleanup(setattr, client_mod, "LauncherClient", originals[0])
        self.addCleanup(setattr, provisioning_mod, "from_client", originals[1])
        import os

        previous = os.environ.get("BUNDLEBEES_LAUNCHER_TOKEN")
        os.environ["BUNDLEBEES_LAUNCHER_TOKEN"] = "cli-test-token"
        self.addCleanup(
            lambda: os.environ.pop("BUNDLEBEES_LAUNCHER_TOKEN", None)
            if previous is None
            else os.environ.__setitem__("BUNDLEBEES_LAUNCHER_TOKEN", previous)
        )

        args = [
            "--state", str(fx.state),
            "--queue", str(fx.state / "queue.json"),
            "--launcher-url", "http://127.0.0.1:9",
            "--repo", str(fx.tmp / "work"),
            "--gitleaks", gatec.GITLEAKS,
            "--provisioning",
            "--provisioning-host", fx.host_name,
            "--once",
            "--suite-command", sys.executable, "-m", "pytest", "-q",
        ]
        for _ in range(8):
            self.assertEqual(factory_driver.main(args), 0)
            item = queue.QueueStore(fx.state).read("f1")
            if item and item.get("state") == "ready_for_review":
                break

        item = queue.QueueStore(fx.state).read("f1")
        self.assertEqual(item.get("state"), "ready_for_review")
        job = jobs.load_job(fx.state, "job_c")
        self.assertTrue(job["integration"].get("accepted_waves"))
        self.assertEqual(len(fx.launcher.posts), 1)
        manifest = return_report.build(fx.state, "job_c", repo=fx.tmp / "work")
        self.assertEqual(manifest["receiver_decision"]["status"], "pending")
        self.assertTrue(all(row["status"] == "PASS" for row in manifest["acceptance"]))


if __name__ == "__main__":
    unittest.main()
