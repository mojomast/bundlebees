"""Bundlebees enforces the pinned workflow feature-packet protocol."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bundlebees_plugin import job as jobs, packets

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()


@unittest.skipUnless(HAS_WORKFLOW or REQUIRE_WORKFLOW, "set WORKFLOW_SRC to the pinned workflow checkout")
class PacketEnforcement(unittest.TestCase):
    def setUp(self):
        if not HAS_WORKFLOW:
            self.fail("WORKFLOW_SRC must point at the pinned workflow checkout when REQUIRE_WORKFLOW=1")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        example = Path(WORKFLOW_SRC) / "schemas" / "examples" / "feature-exact.json"
        self.packet = json.loads(example.read_text(encoding="utf-8"))
        head = subprocess.run(["git", "-C", WORKFLOW_SRC, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.packet["workflow_ref"] = head
        self.packet["job_id"] = "job_p"
        jobs.record_authorization(self.state, job_id="job_p", statement="packet feature",
                                  repo="mojomast/smoke-real", base_rev="a" * 40,
                                  scope=["smoke/answer.py"], bees=1, wall_clock_s=600,
                                  require_tty=False, interactive=False, by="reviewer")

    def test_a_valid_packet_is_validated_and_bound_to_the_sealed_job(self):
        digest = packets.validate(self.packet)
        job = jobs.create_job(self.state, job_id="job_p", packet=self.packet, tasks=[{
            "task_id": "T1", "objective": "answer", "lease": ["smoke/answer.py"],
            "acceptance": {"criteria": ["c"], "tests": ["tests/test_smoke.py::test_answer_is_42"], "commands": ["pytest"]},
        }])
        self.assertEqual(job["goal"]["packet_digest"], digest)
        self.assertEqual(job["goal"]["workflow_ref"], self.packet["workflow_ref"])

    def test_a_mutated_contract_is_refused_before_the_job_exists(self):
        packet = copy.deepcopy(self.packet)
        packet["contract"]["outcome"]["summary"] = "mutated"
        with self.assertRaises(packets.PacketError) as caught:
            jobs.create_job(self.state, job_id="job_p", packet=packet, tasks=[{
                "task_id": "T1", "objective": "x", "lease": ["smoke/answer.py"],
                "acceptance": {"criteria": ["c"], "tests": ["t"], "commands": ["pytest"]}}])
        self.assertIn(caught.exception.code, {"contract_hash_mismatch", "contract_changed"})
        self.assertFalse(jobs.job_path(self.state, "job_p").exists())

    def test_a_resumed_job_cannot_change_packet_identity(self):
        jobs.create_job(self.state, job_id="job_p", packet=self.packet, tasks=[{
            "task_id": "T1", "objective": "answer", "lease": ["smoke/answer.py"],
            "acceptance": {"criteria": ["c"], "tests": ["tests/test_smoke.py::test_answer_is_42"], "commands": ["pytest"]}}])
        job = jobs.load_job(self.state, "job_p")
        job["goal"]["packet_digest"] = "f" * 64
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(self.state, job)
        self.assertEqual(caught.exception.code, "packet_identity_changed")

    def test_a_packet_for_another_job_is_refused(self):
        packet = copy.deepcopy(self.packet)
        packet["job_id"] = "job_other"
        with self.assertRaises(jobs.JobError) as caught:
            jobs.create_job(self.state, job_id="job_p", packet=packet, tasks=[{
                "task_id": "T1", "objective": "x", "lease": ["smoke/answer.py"],
                "acceptance": {"criteria": ["c"], "tests": ["t"], "commands": ["pytest"]}}])
        self.assertEqual(caught.exception.code, "packet_job_mismatch")

    def test_a_missing_workflow_checkout_fails_closed(self):
        with mock.patch.dict(os.environ, {"WORKFLOW_SRC": "", "REQUIRE_WORKFLOW": "1"}):
            with self.assertRaises(packets.PacketError) as caught:
                packets.validate(self.packet)
        self.assertEqual(caught.exception.code, "workflow_unavailable")

    def test_a_packet_pinned_to_a_revision_without_the_schema_is_refused(self):
        packet = copy.deepcopy(self.packet)
        packet["workflow_ref"] = "817eac479f441396562962e880c1af596638f399"  # predates the schema
        with self.assertRaises(packets.PacketError) as caught:
            packets.validate(packet)
        self.assertEqual(caught.exception.code, "workflow_ref_unsupported")


if __name__ == "__main__":
    unittest.main()
