"""One pid-liveness contract shared by every durable claim protocol."""

from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory
from unittest import mock

from bundlebees_plugin import agent_work
from bundlebees_plugin import event_consumer
from bundlebees_plugin import factory_driver
from bundlebees_plugin import grants
from bundlebees_plugin import liveness
from bundlebees_plugin import queue
from bundlebees_plugin import reconcile

#: Larger than a signed 32-bit pid, which some kernels reject with OverflowError.
TOO_LARGE = 2 ** 31

DELEGATES = (
    ("queue", queue._pid_alive),
    ("factory_driver", factory_driver._pid_alive),
    ("reconcile", reconcile._holder_alive),
    ("agent_work", agent_work._holder_alive),
    ("event_consumer", event_consumer._holder_alive),
    ("grants", grants.pid_alive),
)


class _EmptyQueue:
    def list_items(self):
        return []


class PidAliveUnitTests(unittest.TestCase):
    def test_a_pid_too_large_for_os_kill_is_not_alive(self):
        self.assertIs(liveness.pid_alive(TOO_LARGE), False)

    def test_our_own_process_is_alive(self):
        self.assertIs(liveness.pid_alive(os.getpid()), True)

    def test_bool_and_malformed_pids_are_not_alive(self):
        self.assertIs(liveness.pid_alive(True), False)
        self.assertIs(liveness.pid_alive(False), False)
        self.assertIs(liveness.pid_alive("123"), False)
        self.assertIs(liveness.pid_alive(1.0), False)
        self.assertIs(liveness.pid_alive(None), False)

    def test_zero_and_negative_pids_are_not_alive(self):
        self.assertIs(liveness.pid_alive(0), False)
        self.assertIs(liveness.pid_alive(-1), False)

    def test_process_lookup_error_means_not_alive(self):
        with mock.patch.object(liveness.os, "kill", side_effect=ProcessLookupError):
            self.assertIs(liveness.pid_alive(os.getpid()), False)

    def test_permission_error_means_alive(self):
        with mock.patch.object(liveness.os, "kill", side_effect=PermissionError):
            self.assertIs(liveness.pid_alive(os.getpid()), True)

    def test_os_error_means_not_alive(self):
        with mock.patch.object(liveness.os, "kill", side_effect=OSError):
            self.assertIs(liveness.pid_alive(os.getpid()), False)

    def test_overflow_error_means_not_alive(self):
        with mock.patch.object(liveness.os, "kill", side_effect=OverflowError):
            self.assertIs(liveness.pid_alive(os.getpid()), False)


class DelegationTests(unittest.TestCase):
    def test_every_module_shares_the_same_function(self):
        for name, delegate in DELEGATES:
            with self.subTest(module=name):
                self.assertIs(delegate, liveness.pid_alive)

    def test_every_module_rejects_the_too_large_pid(self):
        for name, delegate in DELEGATES:
            with self.subTest(module=name):
                self.assertIs(delegate(TOO_LARGE), False)


class StartupReconcileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = self._tmp.name

    def test_a_too_large_pid_is_abandoned_without_raising(self):
        queue.QueueStore(self.state).write(
            "feature-corrupt",
            {"state": "running", "job_id": "", "pid": TOO_LARGE},
        )

        report = factory_driver.startup_reconcile(self.state, _EmptyQueue())

        self.assertEqual(report["abandoned_claims"], ["feature-corrupt"])
        self.assertEqual(report["errors"], [])

    def test_a_live_pid_is_not_abandoned(self):
        queue.QueueStore(self.state).write(
            "feature-live",
            {"state": "running", "job_id": "", "pid": os.getpid()},
        )

        report = factory_driver.startup_reconcile(self.state, _EmptyQueue())

        self.assertEqual(report["abandoned_claims"], [])
        self.assertEqual(report["errors"], [])


if __name__ == "__main__":
    unittest.main()
