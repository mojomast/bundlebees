"""P2: job identity — the intent packet and the sealed digest are one record.

``create_job(packet=...)`` must pin the protocol revision, persist the exact
packet bytes and seal their digest. ``save_job`` must never launder a dropped or
edited digest into a valid job, and must never treat a damaged existing document
as "no previous job" and overwrite it. Legacy packet-free jobs keep working.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from bundlebees_plugin import job as jobs, packets

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()

REPO = "mojomast/smoke-real"
SHA = "a" * 40


def task() -> dict:
    return {"task_id": "T1", "objective": "answer", "lease": ["smoke/answer.py"],
            "acceptance": {"criteria": ["answer() == 42"],
                           "tests": ["tests/test_smoke.py::test_answer_is_42"], "commands": ["pytest"]}}


def authorize(state: Path) -> dict:
    return jobs.record_authorization(state, job_id="job_p", statement="packet feature", repo=REPO,
                                     base_rev=SHA, scope=["smoke/answer.py"], bees=1, wall_clock_s=600,
                                     require_tty=False, interactive=False, by="reviewer")


class LegacyJobs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        authorize(self.state)

    def test_a_packet_free_job_round_trips_and_saves_again(self):
        job = jobs.create_job(self.state, job_id="job_p", tasks=[task()])
        self.assertEqual(job["goal"]["packet_digest"], "")
        loaded = jobs.load_job(self.state, "job_p")
        loaded["goal"]["summary"] = "still legacy"
        jobs.save_job(self.state, loaded)
        self.assertEqual(jobs.load_job(self.state, "job_p")["goal"]["summary"], "still legacy")
        self.assertFalse((self.state / "packets").exists())

    def test_a_legacy_job_cannot_gain_a_packet_identity_later(self):
        job = jobs.create_job(self.state, job_id="job_p", tasks=[task()])
        job["goal"]["packet_digest"] = "f" * 64
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(self.state, job)
        self.assertEqual(caught.exception.code, "packet_identity_changed")
        self.assertEqual(jobs.load_job(self.state, "job_p")["goal"]["packet_digest"], "")

    def test_save_job_distinguishes_absent_from_unreadable_and_unsealed(self):
        # absent: a first save is allowed
        body = {"job_id": "job_p", "goal": {"repo": REPO, "base_rev": SHA, "packet_digest": ""},
                "tasks": [], "events": []}
        jobs.save_job(self.state, body)
        self.assertTrue(jobs.job_path(self.state, "job_p").exists())

        # unsealed: the document exists, the signing key does not
        path = jobs.job_path(self.state, "job_p")
        before = path.read_bytes()
        (self.state / "lease.key").unlink()
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(self.state, body)
        self.assertEqual(caught.exception.code, "job_unsealed")
        with self.assertRaises(jobs.JobError) as caught:
            jobs.load_job(self.state, "job_p")
        self.assertEqual(caught.exception.code, "job_unsealed")
        self.assertEqual(path.read_bytes(), before, "an unsealed job is never overwritten")

        # unreadable: a corrupt existing document raises and is left in place
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        authorize(other)
        jobs.save_job(other, body)
        corrupt = jobs.job_path(other, "job_p")
        corrupt.write_text("not json")
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(other, body)
        self.assertEqual(caught.exception.code, "job_invalid")
        self.assertEqual(corrupt.read_text(), "not json")

    def test_a_missing_job_is_still_reported_as_missing(self):
        with self.assertRaises(jobs.JobError) as caught:
            jobs.load_job(self.state, "job_absent")
        self.assertEqual(caught.exception.code, "job_missing")


@unittest.skipUnless(HAS_WORKFLOW or REQUIRE_WORKFLOW, "set WORKFLOW_SRC to the pinned workflow checkout")
class PacketIdentity(unittest.TestCase):
    def setUp(self):
        if not HAS_WORKFLOW:
            self.fail("WORKFLOW_SRC must point at the pinned workflow checkout when REQUIRE_WORKFLOW=1")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        authorize(self.state)
        example = Path(WORKFLOW_SRC) / "schemas" / "examples" / "feature-exact.json"
        self.packet = json.loads(example.read_text(encoding="utf-8"))
        head = subprocess.run(["git", "-C", WORKFLOW_SRC, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.packet["workflow_ref"] = head
        self.packet["job_id"] = "job_p"

    def create(self, **overrides):
        kwargs = {"job_id": "job_p", "tasks": [task()], "packet": copy.deepcopy(self.packet)}
        kwargs.update(overrides)
        return jobs.create_job(self.state, **kwargs)

    def test_the_workflow_ref_argument_must_equal_the_packets_pin_exactly(self):
        with self.assertRaises(jobs.JobError) as caught:
            self.create(workflow_ref="f" * 40)
        self.assertEqual(caught.exception.code, "workflow_ref_mismatch")
        self.assertFalse(jobs.job_path(self.state, "job_p").exists())
        self.assertFalse(packets.packet_path(self.state, "job_p").exists())

        explicit = self.create(workflow_ref=self.packet["workflow_ref"])
        self.assertEqual(explicit["goal"]["workflow_ref"], self.packet["workflow_ref"])

    def test_an_omitted_workflow_ref_inherits_the_packets_pin(self):
        job = self.create()
        self.assertEqual(job["goal"]["workflow_ref"], self.packet["workflow_ref"])

    def test_the_packet_is_persisted_byte_for_byte_and_its_digest_is_sealed(self):
        job = self.create()
        path = self.state / "packets" / "job_p.json"
        self.assertTrue(path.exists())
        payload = path.read_bytes()
        self.assertEqual(payload, packets.canonical_bytes(self.packet))
        self.assertEqual(hashlib.sha256(payload).hexdigest(), job["goal"]["packet_digest"])
        self.assertEqual(json.loads(payload), self.packet)

        with self.assertRaises(jobs.JobError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "job_exists")

    def test_a_different_packet_for_an_admitted_job_is_a_conflict(self):
        self.create()
        changed = copy.deepcopy(self.packet)
        changed["request"]["summary"] = "mutated"
        with self.assertRaises(packets.PacketError) as caught:
            packets.store_packet(self.state, "job_p", changed)
        self.assertEqual(caught.exception.code, "packet_conflict")
        self.assertEqual(json.loads((self.state / "packets" / "job_p.json").read_text()), self.packet)

    def test_save_job_refuses_a_dropped_packet_digest(self):
        self.create()
        job = jobs.load_job(self.state, "job_p")
        sealed = job["goal"]["packet_digest"]
        job["goal"].pop("packet_digest")
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(self.state, job)
        self.assertEqual(caught.exception.code, "packet_digest_missing")
        self.assertEqual(jobs.load_job(self.state, "job_p")["goal"]["packet_digest"], sealed)

    def test_save_job_refuses_an_altered_digest_and_leaves_the_record_intact(self):
        self.create()
        before = jobs.job_path(self.state, "job_p").read_bytes()
        job = jobs.load_job(self.state, "job_p")
        job["goal"]["packet_digest"] = "f" * 64
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(self.state, job)
        self.assertEqual(caught.exception.code, "packet_identity_changed")
        self.assertEqual(jobs.job_path(self.state, "job_p").read_bytes(), before)

    def test_save_job_refuses_a_rewritten_packet_file(self):
        self.create()
        path = self.state / "packets" / "job_p.json"
        path.chmod(0o600)
        changed = copy.deepcopy(self.packet)
        changed["request"]["summary"] = "swapped after admission"
        path.write_bytes(packets.canonical_bytes(changed))
        job = jobs.load_job(self.state, "job_p")
        with self.assertRaises(jobs.JobError) as caught:
            jobs.save_job(self.state, job)
        self.assertEqual(caught.exception.code, "packet_altered")

    def test_a_packet_for_another_repository_is_refused(self):
        packet = copy.deepcopy(self.packet)
        packet["repository"]["identity"] = "mojomast/other"
        with self.assertRaises(jobs.JobError) as caught:
            self.create(packet=packet)
        self.assertEqual(caught.exception.code, "packet_repo_mismatch")


if __name__ == "__main__":
    unittest.main()
