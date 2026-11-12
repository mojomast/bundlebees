"""Approved tests for evidence ordering and stale RED (slice L4, written first, RED).

Found while dogfooding acdd-lint on the workflow reference demo: records
written in the same second (same "at") were ordered by file name, so
green.json sorted before red.json and a valid history reported red_missing.
Also from review: a RED recorded against an older contract hash is stale
and must not satisfy RED-before-GREEN for the current contract.
"""

from __future__ import annotations

import unittest

from tests.support import frozen, record


def codes(problems):
    return sorted({p["code"] for p in problems})


class OrderingTests(unittest.TestCase):
    def setUp(self):
        from acdd_lint import evidence
        self.m = evidence
        self.c = frozen()

    def test_same_second_records_use_phase_order_not_file_name(self):
        at = "2026-11-12T18:00:00Z"
        red = record(self.c, "red", "behavioral_failure", at=at)
        green = record(self.c, "green", "passed", at=at)
        refactor = record(self.c, "refactor", "passed", at=at)
        history = [("green.json", green), ("red.json", red), ("refactor.json", refactor)]
        self.assertEqual(self.m.check_history(self.c, history), [])

    def test_time_still_wins_over_phase(self):
        green = record(self.c, "green", "passed", at="2026-11-12T18:00:00Z")
        red = record(self.c, "red", "behavioral_failure", at="2026-11-12T18:00:05Z")
        problems = self.m.check_history(self.c, [("0001-red.json", red), ("0002-green.json", green)])
        self.assertIn("red_missing", codes(problems))

    def test_stale_red_does_not_satisfy_red_before_green(self):
        stale_red = record(self.c, "red", "behavioral_failure", at="2026-11-12T18:01:00Z", contract_sha256="f" * 64)
        green = record(self.c, "green", "passed", at="2026-11-12T18:02:00Z")
        problems = self.m.check_history(self.c, [("0001-red.json", stale_red), ("0002-green.json", green)])
        self.assertIn("evidence_stale", codes(problems))
        self.assertIn("red_missing", codes(problems))

    def test_stale_failed_red_is_reported_once_as_stale(self):
        stale_bad = record(self.c, "red", "unrelated_failure", at="2026-11-12T18:00:00Z", contract_sha256="f" * 64)
        red = record(self.c, "red", "behavioral_failure", at="2026-11-12T18:01:00Z")
        green = record(self.c, "green", "passed", at="2026-11-12T18:02:00Z")
        problems = self.m.check_history(self.c, [("0001-red.json", stale_bad), ("0002-red.json", red), ("0003-green.json", green)])
        self.assertEqual(codes(problems), ["evidence_stale"])


if __name__ == "__main__":
    unittest.main()
