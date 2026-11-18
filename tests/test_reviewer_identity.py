"""Enrolled reviewer identities and signed review receipts (release gates E1/E4/E7/E8).

Requirement (2026-11-17 release decision): "An unsigned receipt or self-declared
reviewer cannot grant PASS. Authenticate a separately appointed reviewer for any
real review criterion."

Run: python -m pytest tests/test_reviewer_identity.py -q
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import reviewers  # noqa: E402

FIELDS = dict(contract_id="M2-canary-wave-tally",
              contract_sha256="4c7d06bd" + "0" * 56,
              revision="84fb4e20" + "0" * 32,
              outcome="passed",
              note="implemented per the frozen contract; tests unmodified")


class Enroll(unittest.TestCase):
    def test_enroll_writes_a_0600_key_and_a_record_without_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            rec = reviewers.enroll(d, "hermes-coordinator")
            key_path = Path(rec["key_path"])
            self.assertTrue(key_path.exists())
            mode = stat.S_IMODE(os.stat(key_path).st_mode)
            self.assertEqual(mode, 0o600, f"key mode {oct(mode)}")
            record_file = Path(d) / "reviewers" / "hermes-coordinator.json"
            stored = json.loads(record_file.read_text())
            self.assertNotIn("key", stored)
            self.assertEqual(stored["key_id"], rec["key_id"])
            self.assertEqual(len(rec["key_id"]), 16)

    def test_enroll_is_idempotent_and_adopts_an_existing_key(self):
        with tempfile.TemporaryDirectory() as d:
            first = reviewers.enroll(d, "r1")
            second = reviewers.enroll(d, "r1")
            self.assertEqual(first["key_id"], second["key_id"])
            self.assertIsNone(second["created_at"])


class Signatures(unittest.TestCase):
    def test_signed_review_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            rec = reviewers.enroll(d, "reviewer-x")
            key = Path(rec["key_path"]).read_bytes()
            record = dict(FIELDS, review_outcome="passed", reviewer_name="reviewer-x", reviewer_key_id=rec["key_id"])
            record["reviewer_signature"] = reviewers.sign_review(key, **FIELDS)
            verdict = reviewers.verify_review(d, record)
            self.assertTrue(verdict["signed"], verdict)
            self.assertEqual(verdict["key_id"], rec["key_id"])

    def test_changed_note_invalidates_the_signature(self):
        with tempfile.TemporaryDirectory() as d:
            rec = reviewers.enroll(d, "reviewer-x")
            key = Path(rec["key_path"]).read_bytes()
            record = dict(FIELDS, review_outcome="passed", reviewer_name="reviewer-x", reviewer_key_id=rec["key_id"])
            record["reviewer_signature"] = reviewers.sign_review(key, **FIELDS)
            record["note"] = "a different note than the reviewer signed"
            self.assertFalse(reviewers.verify_review(d, record)["signed"])

    def test_changed_revision_invalidates_the_signature(self):
        with tempfile.TemporaryDirectory() as d:
            rec = reviewers.enroll(d, "reviewer-x")
            key = Path(rec["key_path"]).read_bytes()
            record = dict(FIELDS, review_outcome="passed", reviewer_name="reviewer-x", reviewer_key_id=rec["key_id"])
            record["reviewer_signature"] = reviewers.sign_review(key, **FIELDS)
            record["revision"] = "f" * 40
            self.assertFalse(reviewers.verify_review(d, record)["signed"])

    def test_unenrolled_reviewer_is_not_authenticated(self):
        with tempfile.TemporaryDirectory() as d:
            record = dict(FIELDS, reviewer_name="stranger", reviewer_key_id="0" * 16,
                          reviewer_signature="ab" * 32)
            verdict = reviewers.verify_review(d, record)
            self.assertFalse(verdict["signed"])
            self.assertIn("not enrolled", verdict["reason"])

    def test_unsigned_review_stays_unauthenticated(self):
        with tempfile.TemporaryDirectory() as d:
            verdict = reviewers.verify_review(d, dict(FIELDS))
            self.assertFalse(verdict["signed"])


class SidecarAttestation(unittest.TestCase):
    """The reviewer signature lives BESIDE the coordinator record (a field inside it would
    invalidate the coordinator's own HMAC - proven the hard way on the live canary)."""

    def test_sidecar_signs_and_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            rec = reviewers.enroll(d, "reviewer-y")
            key = Path(rec["key_path"]).read_bytes()
            att = reviewers.sign_attestation(
                d, name="reviewer-y", key=key, contract_id="M2-canary-wave-tally",
                contract_sha256="a" * 64, revision="b" * 40, outcome="passed",
                note="looks right", evidence_signature="c" * 64)
            self.assertTrue((Path(d) / "reviewers" / "reviewer-y" / "M2-canary-wave-tally-bbbbbbbbbbbb.json").exists())
            self.assertTrue(reviewers.verify_attestation(d, att)["signed"])

    def test_sidecar_binds_the_coordinator_record_signature(self):
        with tempfile.TemporaryDirectory() as d:
            rec = reviewers.enroll(d, "reviewer-y")
            key = Path(rec["key_path"]).read_bytes()
            att = reviewers.sign_attestation(
                d, name="reviewer-y", key=key, contract_id="M2-canary-wave-tally",
                contract_sha256="a" * 64, revision="b" * 40, outcome="passed",
                note="looks right", evidence_signature="c" * 64)
            tampered = dict(att, evidence_signature="d" * 64)
            self.assertFalse(reviewers.verify_attestation(d, tampered)["signed"])


if __name__ == "__main__":
    unittest.main()
