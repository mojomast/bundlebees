"""Acceptance tests for the blocking slop-lint workflow (ratchet: fail only on new findings vs the baseline). Owner approved blocking on 2026-11-23."""
import re
import unittest
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "slop-lint.yml"


class SlopLintWorkflowTests(unittest.TestCase):
    def text(self):
        self.assertTrue(WF.is_file(), "workflow file missing")
        return WF.read_text()

    def test_ac001_triggers_on_pull_request_and_push_to_main_only(self):
        t = self.text()
        self.assertRegex(t, r"(?m)^on:")
        self.assertIn("pull_request:", t)
        self.assertRegex(t, r"push:\s*\n\s*branches:\s*\[main\]")
        self.assertNotIn("pull_request_target", t)

    def test_ac002_permissions_are_read_only(self):
        t = self.text()
        m = re.search(r"(?m)^permissions:\s*\n((?:[ \t]+.*\n)+)", t)
        self.assertIsNotNone(m, "top-level permissions block required")
        body = m.group(1)
        self.assertIn("contents: read", body)
        self.assertNotRegex(body, r"write")

    def test_ac003_every_action_is_pinned_to_a_full_sha(self):
        uses = re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)", self.text())
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"@[0-9a-f]{40}$", u)

    def test_ac004_job_is_bounded_and_blocking_on_new_findings_only(self):
        t = self.text()
        m = re.search(r"timeout-minutes:\s*(\d+)", t)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 10)
        self.assertIn("--pass-on-unpruned", t)
        self.assertNotIn("--report-only", t)
        self.assertNotIn("continue-on-error", t)
        self.assertRegex(t, r"shell:\s*bash")

    def test_ac008_job_has_the_stable_required_check_name_and_pinned_python(self):
        t = self.text()
        self.assertRegex(t, r"(?m)^name:\s*slop-lint\s*$")
        self.assertRegex(t, r"(?m)^  slop-lint:\s*\n    name:\s*slop-lint\s*$")
        self.assertRegex(t, r"python-version:\s*\"3\.12\"")

    def test_ac009_baseline_is_owned_by_the_owner_via_codeowners(self):
        co = (WF.parents[1] / "CODEOWNERS").read_text()
        self.assertRegex(co, r"(?m)^/slop-baseline\.json\s+@mojomast\s*$")

    def test_ac005_runs_the_tool_against_the_committed_baseline(self):
        t = self.text()
        self.assertIn("python3 -m slop_lint check", t)
        self.assertIn("--baseline", t)

    def test_ac006_findings_go_to_the_job_summary_not_a_pr_comment(self):
        t = self.text()
        self.assertIn("GITHUB_STEP_SUMMARY", t)
        self.assertNotIn("github-script", t)
        self.assertNotIn("pull-requests", t)

    def test_ac007_no_secrets_and_no_network_installs(self):
        t = self.text()
        self.assertNotIn("secrets.", t)
        self.assertNotRegex(t, r"pip install|npm install|curl ")


if __name__ == "__main__":
    unittest.main()
