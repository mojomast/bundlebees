"""Coverage reconciliation tests — the COVERED_BY_DRAFT distinction is the point.

Requirement (owner, 2026-11-17): a task implemented in an unmerged draft must NOT be
redispatched, but must NOT be treated as landed either. It is linked, validated and
kept distinct from acceptance.

Run: python -m pytest tests/test_coverage_reconciliation.py -q
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import coverage as cov  # noqa: E402


def task(task_id="RAILS-R05", repo="bundlebees", files=("a.py", "tests/test_a.py"), **kw):
    return cov.Task.build(task_id=task_id, repo=repo, assigned_files=list(files),
                          tests=list(kw.pop("tests", ("tests/test_a.py",))),
                          contract="specs/rails.md@9850efa", **kw)


def draft(repo="bundlebees", number=82, sha="83ffa378" + "0" * 32, files=("a.py", "tests/test_a.py"), **kw):
    return cov.Candidate.build(repo=repo, number=number, head_sha=sha,
                               merged=kw.pop("merged", False), files=list(files), **kw)


class Classification(unittest.TestCase):
    def test_open_draft_covering_the_lease_is_covered_by_draft_not_landed(self):
        rec = cov.classify(task(), [draft()])
        self.assertEqual(rec["disposition"], cov.COVERED_BY_DRAFT)
        self.assertNotEqual(rec["disposition"], cov.LANDED)
        self.assertEqual(rec["covered_by"], "bundlebees#82")
        self.assertTrue(rec["requires_validation"])
        self.assertIn("NOT accepted", rec["reason"])

    def test_merged_candidate_is_landed(self):
        rec = cov.classify(task(), [draft(merged=True, sha="9" * 40)])
        self.assertEqual(rec["disposition"], cov.LANDED)
        self.assertEqual(rec["landed_by"], "bundlebees#82")

    def test_partial_draft_lists_exactly_the_missing_files(self):
        rec = cov.classify(task(files=("a.py", "b.py", "tests/test_a.py")),
                           [draft(files=("a.py", "tests/test_a.py"))])
        self.assertEqual(rec["disposition"], cov.PARTIAL)
        self.assertEqual(rec["missing_files"], ["b.py"])

    def test_no_candidate_is_missing_and_dispatchable(self):
        rec = cov.classify(task(files=("z.py",)), [draft()])
        self.assertEqual(rec["disposition"], cov.MISSING)

    def test_live_attempt_wins_over_coverage(self):
        rec = cov.classify(task(attempt_ids=("att_live",), attempt_states={"att_live": "running"}),
                           [draft()])
        self.assertEqual(rec["disposition"], cov.IN_FLIGHT)
        self.assertEqual(rec["in_flight_attempts"], ["att_live"])

    def test_finished_attempt_does_not_block_coverage(self):
        rec = cov.classify(task(attempt_ids=("att_done",), attempt_states={"att_done": "failed"}),
                           [draft()])
        self.assertEqual(rec["disposition"], cov.COVERED_BY_DRAFT)

    def test_draft_without_evidence_is_flagged_for_validation(self):
        rec = cov.classify(task(), [draft(evidence="")])
        self.assertIn("validate", rec["evidence"])

    def test_superseded_declaration_by_candidate(self):
        rec = cov.classify(task(), [draft(supersedes=("RAILS-R05",))])
        self.assertEqual(rec["disposition"], cov.SUPERSEDED)

    def test_blocked_wins_over_missing_but_not_over_landed(self):
        rec = cov.classify(task(files=("z.py",), blocked_by=("RAILS-R01",)), [draft()])
        self.assertEqual(rec["disposition"], cov.BLOCKED)
        landed = cov.classify(task(files=("a.py",), blocked_by=("RAILS-R01",)), [draft(merged=True)])
        self.assertEqual(landed["disposition"], cov.LANDED)

    def test_repo_mismatch_is_not_coverage(self):
        rec = cov.classify(task(repo="hyvv"), [draft(repo="bundlebees")])
        self.assertEqual(rec["disposition"], cov.MISSING)


class Report(unittest.TestCase):
    def test_report_counts_and_idempotency(self):
        tasks = [task("RAILS-R05"), task("RAILS-R06", files=("b.py", "tests/test_b.py")),
                 task("RAILS-R17C", repo="hyvv", files=("c.py",))]
        cands = [draft(), draft(repo="hyvv", number=26, files=("c.py",), sha="35a4d0cf" + "0" * 32)]
        first = cov.reconcile(tasks, cands)
        second = cov.reconcile(tasks, cands)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["counts"][cov.COVERED_BY_DRAFT], 2)
        self.assertEqual(first["counts"][cov.MISSING], 1)
        self.assertEqual(first["dispatchable"], ["RAILS-R06"])
        self.assertEqual(first["duplicate_dispatches_prevented"], 2)

    def test_table_renders_every_task(self):
        report = cov.reconcile([task()], [draft()])
        text = cov.format_table(report)
        self.assertIn("RAILS-R05", text)
        self.assertIn("COVERED_BY_DRAFT", text)


if __name__ == "__main__":
    unittest.main()
