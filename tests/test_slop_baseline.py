"""Approved acceptance tests: report-only slop-lint baseline adopted in this repo. Coordinator-written; do not edit.

Deliberately does NOT fail on new findings: adoption is report-only; a human decides later about enforcement."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "slop-lint"
BASELINE = REPO / "slop-baseline.json"


class BaselineAdoptionTests(unittest.TestCase):
    def test_ac001_tool_is_present(self):
        self.assertTrue((TOOL / "slop_lint" / "cli.py").is_file())

    def test_ac002_baseline_exists_and_is_valid_json_of_counts(self):
        self.assertTrue(BASELINE.is_file(), "slop-baseline.json missing at repo root")
        data = json.loads(BASELINE.read_text())
        self.assertIsInstance(data, dict)
        self.assertTrue(data, "baseline must not be empty for a repo with existing findings")

    def test_ac003_paths_are_relative_posix_and_not_machine_specific(self):
        text = BASELINE.read_text()
        for needle in ("/home/", "/workspace/", "C:\\\\", "\\\\"):
            self.assertNotIn(needle, text)
        self.assertNotIn("../", text, "baseline must be generated with paths relative to the repo root for this repo's CI layout; see CI command in the brief")

    def test_ac004_check_report_only_does_not_exit_2_and_has_no_new_findings_at_adoption(self):
        env = dict(os.environ, PYTHONPATH=str(TOOL))
        p = subprocess.run([sys.executable, "-m", "slop_lint", "check", ".", "--baseline", "slop-baseline.json",
                            "--report-only", "--pass-on-unpruned"], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-400:])


if __name__ == "__main__":
    unittest.main()
