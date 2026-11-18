"""Owner authority enrollment (release gates E2/E5).

Requirement: "First feedback write revalidates authority and expiry at the writer boundary...
Operator enrolls owner authority."

Run: python -m unittest tests.test_owner_authority -v
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import owners  # noqa: E402


class OwnerEnrollment(unittest.TestCase):
    def test_enroll_writes_0600_key_and_scoped_record(self):
        with tempfile.TemporaryDirectory() as d:
            rec = owners.enroll(d, "clanker01 (owner)", scope=["board", "feedback"], expires="never")
            self.assertEqual(stat.S_IMODE(os.stat(rec["key_path"]).st_mode), 0o600)
            self.assertEqual(rec["scope"], ["board", "feedback"])
            self.assertEqual(rec["expires_at"], owners.NEVER)
            stored = json.loads((Path(d) / "owners" / "clanker01-owner.json").read_text())
            self.assertNotIn("key", stored)

    def test_unknown_scope_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(owners.OwnerAuthorityError):
                owners.enroll(d, "x", scope=["everything"])

    def test_past_expiry_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(owners.OwnerAuthorityError):
                owners.enroll(d, "x", scope=["board"], expires="2020-01-01")

    def test_signed_board_write_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            owners.enroll(d, "clanker01 (owner)", scope=["board"], expires="never")
            action = owners.sign(d, name="clanker01 (owner)", action="board.write", payload={"note": "hi"})
            self.assertTrue(owners.verify(d, action, scope="board")["valid"])

    def test_wrong_scope_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            owners.enroll(d, "clanker01 (owner)", scope=["board"], expires="never")
            action = owners.sign(d, name="clanker01 (owner)", action="board.write", payload={"note": "hi"})
            verdict = owners.verify(d, action, scope="feedback")
            self.assertFalse(verdict["valid"])
            self.assertIn("scope", verdict["reason"])

    def test_expiry_is_checked_at_the_writer_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            owners.enroll(d, "owner-2", scope=["board"], expires="never")
            action = owners.sign(d, name="owner-2", action="board.write", payload={})
            later = owners.NEVER + 1
            verdict = owners.verify(d, action, scope="board", now=later)
            self.assertFalse(verdict["valid"])
            self.assertIn("expired", verdict["reason"])

    def test_tampered_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            owners.enroll(d, "owner-3", scope=["board"], expires="never")
            action = owners.sign(d, name="owner-3", action="board.write", payload={"note": "hi"})
            tampered = dict(action, payload_sha256="0" * 64)
            self.assertFalse(owners.verify(d, tampered, scope="board")["valid"])


if __name__ == "__main__":
    unittest.main()
