"""gate-authority-vendor-g1: one base-pinned gate, byte-identical in seven repos.

This file is vendored byte-identically into mojomast/rizzstack, workflow, hyvv,
bundlebees, orbitdesktop, orbit-control-plane and uirizz.  It proves two things
in whichever repository it runs:

1. A2, byte identity.  The gate script, the base-pinned ``gate-authority``
   workflow, the review re-run workflow and the gate's own unit tests and
   fixtures hash to the pins below, so every copy is the same bytes as
   rizzstack's.  The pins change only through a new owner-approved contract.
   The required status check each repository's ruleset names is
   ``gate-authority`` (the job's name in ``gate-authority.yml``).

2. A3, coordinator reviews are incidents.  Any review GitHub records as
   submitted by ``rizzstack-coordinator[bot]`` is reported (an ``INCIDENT`` line
   and an ``::error`` annotation naming ``coordinator_review_incident``) and is
   never counted: not as an approval, not as a rejection, not as anything else.
   Reviews from every other login behave exactly as before.

Run: python3 -m pytest -q -p no:cacheprovider tests/test_gate_g1.py
  or: python3 -m unittest discover -s tests -p test_gate_g1.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "lease_tamper.py"
GATE_WORKFLOW = ROOT / ".github" / "workflows" / "gate-authority.yml"
RERUN_WORKFLOW = ROOT / ".github" / "workflows" / "review-rerun.yml"

#: sha256 of every vendored file, identical in all seven repositories.
VENDORED_SHA256 = {
    ".github/scripts/lease_tamper.py": "9f35981c8ba9a3e2153fedef61046a402aced3025e483ac877d2b7bda9ca02fa",
    ".github/workflows/gate-authority.yml": "05b3fc444717813dd3659a53e02efb1a45af9fa13ab03adc4fd8489b2498cf3f",
    ".github/workflows/review-rerun.yml": "f69a14746af07bbb6737ef7fbe587e7965d6b568d7d88321ee80d5486be45fc8",
    "tests/test_lease_tamper.py": "f071bd2d0510baad58d95a929e805407fda4213796d14f9a18e1ff9e56739d55",
    "tests/fixtures/shared_policy.sha256": "3989f913be2fdee0b66466d437fd7491905441ea3c103a1a984e9539b736b328",
    "tests/fixtures/review_lines.json": "ac2de684dd22fceed5aa2f18ac6d7d6f8c101c0fd67a619d2a2c6a663642311b",
    "tests/fixtures/carry_scenarios.py": "33435d738b84742c8e2b5cfa3e153cf20921fb7a1985f808bbde68603941165e",
}

#: The status check Kyle requires in each repository's main ruleset after G1.
REQUIRED_CHECK = "gate-authority"

REPO_SLUG = "mojomast/rizzstack"
PR = 12
BRANCH = "feat/g1-fixture"
REVIEWER = "rizzstack-reviewer[bot]"
COORDINATOR = "rizzstack-coordinator[bot]"
OWNER = "mojomast"
INCIDENT = "lease-tamper: INCIDENT (reason=coordinator_review_incident): "
ANNOTATION = "::error title=lease-tamper coordinator_review_incident::"


class GitRepo:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "g1@example.invalid")
        self.git("config", "user.name", "G1 Tests")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        proc = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s failed: %s%s" % (" ".join(args), proc.stdout, proc.stderr))
        return proc.stdout.strip()

    def write(self, relative, text):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


def _contract(source_rev):
    return {
        "contract_version": "acdd/1",
        "id": "g1-fixture",
        "source_rev": source_rev,
        "scope": {
            "assigned_files": ["src/slice.py"],
            "test_files": ["tests/test_frozen.py"],
            "prohibited": [],
            "read_only": [],
        },
        "review": {"status": "approved", "approved_by": "owner (test fixture)"},
    }


def _contract_sha(document):
    body = {key: value for key, value in document.items() if key != "review"}
    text = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(root, slice_pr):
    """``(repo, base, head)``; with ``slice_pr`` a frozen base lease names the branch."""
    repo = GitRepo(root)
    repo.write("src/slice.py", "base\n")
    repo.write("tests/test_frozen.py", "base\n")
    repo.write("README.md", "base\n")
    source_rev = repo.commit("base")
    if slice_pr:
        document = _contract(source_rev)
        sha = _contract_sha(document)
        repo.write(".rizzstack/contracts/%s.json" % sha, json.dumps(document, indent=2, sort_keys=True) + "\n")
        lease = {
            "id": "g1-fixture",
            "lease_version": "rizzstack.lease/1",
            "contract_id": "g1-fixture",
            "contract_sha256": sha,
            "source_rev": source_rev,
            "branch": BRANCH,
            "scope": {key: list(value) for key, value in document["scope"].items()},
            "review": {"status": "frozen", "approved_by": "coordinator (test fixture)"},
        }
        repo.write(".rizzstack/leases/g1-fixture.json", json.dumps(lease, indent=2) + "\n")
    else:
        repo.write("docs/note.md", "base\n")
    base = repo.commit("base branch")
    repo.git("checkout", "-q", "-b", BRANCH)
    repo.write("src/slice.py", "implementation\n")
    head = repo.commit("the change")
    return repo, base, head


class FakeReviews:
    """A loopback stand-in for GET /repos/<o>/<r>/pulls/<n>/reviews."""

    def __init__(self):
        self.reviews = []
        self.status = 200
        self.requests = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                api.requests.append(self.path)
                clean = self.path.split("?")[0]
                if clean == "/repos/%s/pulls/%d/reviews" % (REPO_SLUG, PR):
                    status, payload = api.status, (api.reviews if api.status == 200 else {"message": "forced"})
                else:
                    status, payload = 404, {"message": "no route for " + clean}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def review(login, head, state="COMMENTED", body=None, review_id=1, when="2026-11-28T10:00:00Z"):
    item = {
        "id": review_id,
        "state": state,
        "commit_id": head,
        "submitted_at": when,
        "body": body if body is not None else "APPROVE (independent review) at %s" % head,
    }
    if login is not None:
        item["user"] = {"login": login}
    return item


class VendoredGateBytesTest(unittest.TestCase):
    """A2: this repository carries rizzstack's gate, byte for byte."""

    def test_each_vendored_file_matches_its_g1_pin(self):
        for relative, expected in sorted(VENDORED_SHA256.items()):
            with self.subTest(path=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file(), "missing vendored file %s" % relative)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, relative)

    def test_the_required_check_is_the_gate_authority_job(self):
        self.assertTrue(GATE_WORKFLOW.is_file(), "missing .github/workflows/gate-authority.yml")
        text = GATE_WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^name: %s$" % REQUIRED_CHECK)
        self.assertRegex(text, r"(?m)^  %s:\n    name: %s$" % (REQUIRED_CHECK, REQUIRED_CHECK))
        self.assertRegex(text, r"(?m)^  pull_request_target:$")
        self.assertNotRegex(text, r"(?m)^  pull_request:")
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("python3 .github/scripts/lease_tamper.py", text)

    def test_a_review_reruns_the_base_pinned_gate(self):
        self.assertTrue(RERUN_WORKFLOW.is_file(), "missing .github/workflows/review-rerun.yml")
        text = RERUN_WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^  pull_request_review:$")
        self.assertIn("actions/workflows/gate-authority.yml/runs?event=pull_request_target", text)


class _GateCase(unittest.TestCase):
    slice_pr = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-g1-"))
        self.api = FakeReviews()
        self.repo, self.base, self.head = build(self.tmp / "repo", self.slice_pr)

    def tearDown(self):
        self.api.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_gate(self, api=True):
        command = [sys.executable, str(SCRIPT), "--repo", str(self.repo.path), "--base", self.base,
                   "--head", self.head, "--branch", BRANCH, "--pull-request", str(PR)]
        env = dict(os.environ)
        for key in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_URL",
                    "GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME"):
            env.pop(key, None)
        if api:
            env.update(GITHUB_REPOSITORY=REPO_SLUG, GITHUB_API_URL=self.api.url, GITHUB_TOKEN="test-token")
        proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
        return proc, proc.stdout + proc.stderr

    def incidents(self, output):
        return [line for line in output.splitlines() if line.startswith(INCIDENT)]

    def annotations(self, output):
        return [line for line in output.splitlines() if line.startswith(ANNOTATION)]

    def assert_passes(self, proc, output):
        self.assertEqual(proc.returncode, 0, output)

    def assert_refused(self, proc, output, reason="non_slice_requires_independent_review"):
        self.assertEqual(proc.returncode, 1, output)
        self.assertIn("reason=%s" % reason, output)


class CoordinatorIncidentNonSliceTest(_GateCase):
    """A3 on a non-slice pull request (it needs a reviewer-app review at its head)."""

    def test_a_coordinator_approval_line_is_an_incident_and_never_counts(self):
        self.api.reviews = [review(COORDINATOR, self.head, review_id=501)]
        proc, output = self.run_gate()
        self.assert_refused(proc, output)
        self.assertEqual(len(self.incidents(output)), 1, output)
        self.assertIn("review 501 by rizzstack-coordinator[bot] on PR #12", self.incidents(output)[0])
        self.assertEqual(len(self.annotations(output)), 1, output)

    def test_a_coordinator_approved_state_is_an_incident_and_never_counts(self):
        self.api.reviews = [review(COORDINATOR, self.head, state="APPROVED", body="", review_id=502)]
        proc, output = self.run_gate()
        self.assert_refused(proc, output)
        self.assertEqual(len(self.incidents(output)), 1, output)
        self.assertIn("(state APPROVED, commit %s)" % self.head[:12], self.incidents(output)[0])

    def test_a_coordinator_rejection_is_an_incident_and_never_counts(self):
        for state, body in (("CHANGES_REQUESTED", "no"),
                            ("COMMENTED", "CHANGES REQUESTED (stop) at %s" % self.head)):
            with self.subTest(state=state):
                self.api.reviews = [
                    review(REVIEWER, self.head, review_id=601, when="2026-11-28T10:00:00Z"),
                    review(COORDINATOR, self.head, state=state, body=body, review_id=602,
                           when="2026-11-28T11:00:00Z"),
                ]
                proc, output = self.run_gate()
                self.assert_passes(proc, output)
                self.assertEqual(len(self.incidents(output)), 1, output)
                self.assertIn("review 602 ", self.incidents(output)[0])

    def test_a_coordinator_comment_or_dismissed_review_is_still_an_incident(self):
        for state, body in (("COMMENTED", "looks fine to me"), ("DISMISSED", "")):
            with self.subTest(state=state):
                self.api.reviews = [
                    review(REVIEWER, self.head, review_id=701),
                    review(COORDINATOR, self.head, state=state, body=body, review_id=702,
                           when="2026-11-28T11:00:00Z"),
                ]
                proc, output = self.run_gate()
                self.assert_passes(proc, output)
                self.assertEqual(len(self.incidents(output)), 1, output)
                self.assertIn("(state %s," % state, self.incidents(output)[0])

    def test_login_case_variants_of_the_coordinator_are_incidents(self):
        for login in ("RIZZSTACK-COORDINATOR[BOT]", "Rizzstack-Coordinator[bot]", " rizzstack-coordinator[bot] "):
            with self.subTest(login=login):
                self.api.reviews = [review(login, self.head, review_id=801)]
                proc, output = self.run_gate()
                self.assert_refused(proc, output)
                self.assertEqual(len(self.incidents(output)), 1, output)

    def test_every_coordinator_review_is_reported_once_and_in_order(self):
        self.api.reviews = [
            review(COORDINATOR, self.head, review_id=901, when="2026-11-28T09:00:00Z"),
            review(REVIEWER, self.head, review_id=902, when="2026-11-28T10:00:00Z"),
            review(COORDINATOR, self.head, state="CHANGES_REQUESTED", body="x", review_id=903,
                   when="2026-11-28T11:00:00Z"),
        ]
        proc, output = self.run_gate()
        self.assert_passes(proc, output)
        found = self.incidents(output)
        self.assertEqual(len(found), 2, output)
        self.assertIn("review 901 ", found[0])
        self.assertIn("review 903 ", found[1])
        self.assertEqual(len(self.annotations(output)), 2, output)

    def test_incident_fields_cannot_inject_workflow_commands(self):
        bad = review(COORDINATOR, self.head, state="COMMENTED\n::warning::x", review_id="9\n::error::y")
        self.api.reviews = [bad]
        proc, output = self.run_gate()
        self.assert_refused(proc, output)
        found = self.incidents(output)
        self.assertEqual(len(found), 1, output)
        self.assertNotIn("::warning::", output)
        self.assertEqual([line for line in output.splitlines() if line.startswith("::error::y")], [])

    # -- guards: unchanged behaviour for every other login --------------------

    def test_reviews_by_other_logins_are_not_incidents(self):
        self.api.reviews = [
            review(None, self.head, body="APPROVE (independent review) at %s" % self.head, review_id=1),
            review("someone-else", self.head, body="nice", review_id=2),
            review(OWNER, self.head, body="ok", review_id=3),
            review(REVIEWER, self.head, review_id=4, body="APPROVE (independent review) at %s\n"
                   "mentions rizzstack-coordinator[bot] in the text" % self.head),
        ]
        proc, output = self.run_gate()
        self.assert_passes(proc, output)
        self.assertEqual(self.incidents(output), [], output)
        self.assertEqual(self.annotations(output), [], output)

    def test_a_reviewer_rejection_after_its_approval_still_governs(self):
        self.api.reviews = [
            review(REVIEWER, self.head, review_id=11, when="2026-11-28T10:00:00Z"),
            review(REVIEWER, self.head, state="CHANGES_REQUESTED", body="no", review_id=12,
                   when="2026-11-28T11:00:00Z"),
        ]
        proc, output = self.run_gate()
        self.assert_refused(proc, output)
        self.assertEqual(self.incidents(output), [], output)

    def test_no_review_is_refused_without_an_incident(self):
        proc, output = self.run_gate()
        self.assert_refused(proc, output)
        self.assertEqual(self.incidents(output), [], output)


class CoordinatorIncidentSliceTest(_GateCase):
    """A3 on a slice: the slice result is unchanged; the incident is still reported."""

    slice_pr = True

    def test_a_coordinator_review_on_a_slice_is_reported_and_the_slice_still_passes(self):
        self.api.reviews = [review(COORDINATOR, self.head, review_id=1001)]
        proc, output = self.run_gate()
        self.assert_passes(proc, output)
        self.assertIn("lease-tamper: OK:", output)
        self.assertEqual(len(self.incidents(output)), 1, output)
        self.assertIn("review 1001 ", self.incidents(output)[0])

    def test_a_slice_whose_reviews_cannot_be_read_fails_closed(self):
        self.api.status = 500
        proc, output = self.run_gate()
        self.assertEqual(proc.returncode, 1, output)
        self.assertTrue(any("/pulls/%d/reviews" % PR in path for path in self.api.requests), self.api.requests)

    # -- guards ---------------------------------------------------------------

    def test_a_slice_without_api_credentials_still_passes(self):
        proc, output = self.run_gate(api=False)
        self.assert_passes(proc, output)
        self.assertIn("lease-tamper: OK:", output)
        self.assertEqual(self.incidents(output), [], output)

    def test_a_slice_with_only_other_reviews_has_no_incident(self):
        self.api.reviews = [review(REVIEWER, self.head, review_id=1101), review(OWNER, self.head, review_id=1102)]
        proc, output = self.run_gate()
        self.assert_passes(proc, output)
        self.assertEqual(self.incidents(output), [], output)


if __name__ == "__main__":
    unittest.main()
