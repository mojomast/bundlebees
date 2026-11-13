"""Accepted-commit transport: publish locally, provision through a governed mirror."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bundlebees_plugin import transport, verify_local

HYVV_SRC = os.environ.get("HYVV_SRC", "")
REQUIRE_HYVV = os.environ.get("REQUIRE_HYVV") == "1"
HAS_HYVV = bool(HYVV_SRC) and Path(HYVV_SRC, "provisioning.py").exists()
REPO = "mojomast/smoke-real"


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


@unittest.skipUnless(HAS_HYVV or REQUIRE_HYVV, "set HYVV_SRC to a hyvv checkout")
class TransportTests(unittest.TestCase):
    def setUp(self):
        if not HAS_HYVV:
            self.fail("HYVV_SRC must point at the hyvv checkout when REQUIRE_HYVV=1")
        sys.path.insert(0, HYVV_SRC)
        self.addCleanup(lambda: sys.path.remove(HYVV_SRC))
        self.hyvv = importlib.import_module("provisioning")

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

        self.work = self.tmp / "work"
        self.work.mkdir()
        git(["init", "-q", "-b", "main"], self.work)
        (self.work / "README.md").write_text("base\n")
        git(["add", "-A"], self.work)
        git(["commit", "-q", "-m", "root"], self.work)
        self.root = git(["rev-parse", "HEAD"], self.work).stdout.strip()

        git(["checkout", "-q", "-b", "feat"], self.work)
        (self.work / "smoke").mkdir()
        (self.work / "smoke" / "answer.py").write_text("def answer():\n    return 42\n")
        git(["add", "-A"], self.work)
        git(["commit", "-q", "-m", "verified implementation"], self.work)
        self.verified = git(["rev-parse", "HEAD"], self.work).stdout.strip()

        (self.work / "INTEGRATED").write_text("accepted\n")
        git(["add", "-A"], self.work)
        git(["commit", "-q", "-m", "integration"], self.work)
        self.accepted = git(["rev-parse", "HEAD"], self.work).stdout.strip()

        # The original origin knows only main: the accepted commit is not there.
        self.origin = self.tmp / "origin.git"
        git(["init", "-q", "--bare", str(self.origin)], self.tmp)
        git(["push", "-q", str(self.origin), f"{self.root}:refs/heads/main"], self.work)
        remote = git(["ls-remote", str(self.origin)], self.tmp).stdout
        self.assertNotIn(self.accepted, remote)

        self.mirror = self.tmp / "mirror.git"
        git(["init", "-q", "--bare", str(self.mirror)], self.tmp)

        self.state = self.tmp / "state"
        self.state.mkdir()
        record = {"attempt_id": "att_1", "ok": True, "sha": self.verified, "base_rev": self.root,
                  "files": ["smoke/answer.py"], "tests": ["tests/test_smoke.py"],
                  "suite": {"exit_code": 0}, "gitleaks": {"exit_code": 0}, "mode": "commit-local"}
        path = verify_local.record_path(self.state, "att_1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

        self.job = {"job_id": "job_a", "goal": {"repo": REPO, "base_rev": self.root},
                    "tasks": [{"task_id": "T1", "state": "accepted", "attempts": ["att_1"]}],
                    "integration": {"accepted_waves": [{"wave": 1, "commit": self.accepted, "attempts": ["att_1"]}],
                                    "last_commit": self.accepted}}

    def publish(self):
        with mock.patch.object(transport.jobs, "load_job", lambda state_dir, job_id: self.job):
            return transport.publish(self.state, "job_a", repo=self.work, mirror=self.mirror,
                                     repo_identity=REPO, root_rev=self.root)

    def test_the_accepted_commit_is_published_once_and_recovered_on_restart(self):
        receipt = self.publish()

        self.assertEqual(receipt["source_sha"], self.accepted)
        self.assertIn(f"refs/bb/accepted/job_a/{self.accepted}", receipt["mirror_ref"])
        advertised = git(["ls-remote", str(self.mirror), receipt["mirror_ref"]], self.tmp).stdout
        self.assertIn(self.accepted, advertised)

        again = self.publish()
        self.assertTrue(again["recovered"])
        self.assertEqual(again["mirror_ref"], receipt["mirror_ref"])
        refs = git(["for-each-ref", "--format=%(refname)"], self.mirror).stdout.splitlines()
        self.assertEqual(len([r for r in refs if r.startswith("refs/bb/accepted/")]), 1)

    def test_a_ref_that_already_points_elsewhere_is_a_hard_refusal(self):
        ref = f"refs/bb/accepted/job_a/{self.accepted}"
        git(["push", "-q", str(self.mirror), f"{self.root}:{ref}"], self.work)

        with self.assertRaises(transport.TransportError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "ref_conflict")

    def test_unverified_or_unrelated_work_is_never_published(self):
        self.job["integration"]["accepted_waves"] = []
        with self.assertRaises(transport.TransportError) as caught:
            self.publish()
        self.assertEqual(caught.exception.code, "dependency_not_accepted")

    def test_an_unreachable_mirror_fails_closed(self):
        shutil.rmtree(self.mirror)
        with self.assertRaises(transport.TransportError) as caught:
            self.publish()
        self.assertIn(caught.exception.code, {"mirror_unreachable", "publish_failed"})

    def test_hyvv_provisions_through_the_mirror_at_the_accepted_sha(self):
        receipt = self.publish()
        fixtures = self.tmp / "fixtures"
        fixtures.mkdir()
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({
            "schema": "rizzstack.provisioning/1",
            "profiles": {"smoke": {"repo_patterns": [REPO], "workspace_root": str(fixtures),
                                   "mirrors": {REPO: f"file://{self.mirror}"}}},
        }))
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"projects": {}}))
        projects: dict = {}
        prov = self.hyvv.Provisioner(policy=self.hyvv.load_policy(policy),
                                     journal=self.hyvv.Journal(self.tmp / "journal"),
                                     registry=self.hyvv.RegistryStore(config, cache=projects),
                                     credential=lambda repo: "host-helper")

        out = prov.provision({"repo": REPO, "source_rev": self.accepted, "profile": "smoke",
                              "job_id": "job_b", "idempotency_key": "prov_b", "project": "smoke-real"})

        self.assertEqual(out["resolved_revision"], self.accepted)
        self.assertEqual(out["transport"], "mirror")
        workspace = Path(out["workspace"])
        self.assertEqual(git(["rev-parse", "HEAD"], workspace).stdout.strip(), self.accepted)
        self.assertEqual(git(["symbolic-ref", "-q", "HEAD"], workspace, check=False).returncode, 1)
        self.assertEqual(git(["status", "--porcelain"], workspace).stdout.strip(), "")
        self.assertIn("return 42", (workspace / "smoke" / "answer.py").read_text())
        self.assertEqual(json.loads(config.read_text())["projects"]["smoke-real"]["source_rev"], self.accepted)
        self.assertEqual(out["receipt_digest"] if "receipt_digest" in out else receipt["source_sha"], self.accepted)

    def test_a_commit_absent_from_the_mirror_is_refused(self):
        fixtures = self.tmp / "fixtures"
        fixtures.mkdir()
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({
            "schema": "rizzstack.provisioning/1",
            "profiles": {"smoke": {"repo_patterns": [REPO], "workspace_root": str(fixtures),
                                   "mirrors": {REPO: f"file://{self.mirror}"}}},
        }))
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"projects": {}}))
        prov = self.hyvv.Provisioner(policy=self.hyvv.load_policy(policy),
                                     journal=self.hyvv.Journal(self.tmp / "journal"),
                                     registry=self.hyvv.RegistryStore(config, cache={}),
                                     credential=lambda repo: "host-helper")

        with self.assertRaises(self.hyvv.ProvisionError) as caught:
            prov.provision({"repo": REPO, "source_rev": self.accepted, "profile": "smoke",
                            "job_id": "job_b", "idempotency_key": "prov_b", "project": "smoke-real"})
        self.assertIn(caught.exception.code, {"checkout_failed", "fetch_failed"})


if __name__ == "__main__":
    unittest.main()
