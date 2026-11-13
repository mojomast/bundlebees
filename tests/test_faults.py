"""The fault interface itself: inert by default, armed only in-process."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from bundlebees_plugin import faults

ROOT = Path(__file__).resolve().parents[1]


class FaultsContract(unittest.TestCase):
    def tearDown(self) -> None:
        faults.clear()

    def test_no_hook_means_no_effect(self):
        self.assertEqual(faults.registered(), ())
        self.assertIsNone(faults.checkpoint("tick.after_dispatch", job_id="job_a"))
        self.assertIsNone(faults.checkpoint("anything.at.all", payload={"x": 1}))

    def test_a_hook_receives_the_payload_as_keywords(self):
        seen = []
        faults.register("wave.after_persist", lambda **payload: seen.append(payload))
        faults.checkpoint("wave.after_persist", job_id="job_a", wave="job_a.w1")
        self.assertEqual(seen, [{"job_id": "job_a", "wave": "job_a.w1"}])

    def test_only_the_named_checkpoint_fires(self):
        seen = []
        faults.register("transport.after_publish", lambda **payload: seen.append(payload))
        faults.checkpoint("tick.after_dispatch", job_id="job_a")
        self.assertEqual(seen, [])
        faults.checkpoint("transport.after_publish", job_id="job_a")
        self.assertEqual(len(seen), 1)

    def test_registration_is_per_process(self):
        faults.register("queue.before_ack", lambda **payload: None)
        script = "from bundlebees_plugin import faults; print(faults.registered()); print(faults.checkpoint('queue.before_ack', x=1))"
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              cwd=str(ROOT), env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["()", "None"],
                         "a fresh process must not inherit an armed checkpoint")

    def test_a_second_hook_for_one_name_is_refused(self):
        faults.register("tick.after_dispatch", lambda **payload: None)
        with self.assertRaises(faults.FaultError):
            faults.register("tick.after_dispatch", lambda **payload: None)

    def test_names_are_validated_but_checkpoint_never_raises(self):
        with self.assertRaises(faults.FaultError):
            faults.register("", lambda **payload: None)
        with self.assertRaises(faults.FaultError):
            faults.register("has space", lambda **payload: None)
        self.assertIsNone(faults.checkpoint(""))
        self.assertIsNone(faults.unregister("never.registered"))


if __name__ == "__main__":
    unittest.main()
