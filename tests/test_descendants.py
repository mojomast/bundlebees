"""Long job ids and bounded descendant authorization."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from bundlebees_plugin import descendants as D
from bundlebees_plugin import integrate_job

SHORT = "a" * 40


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


class WaveLimit(unittest.TestCase):
    def test_a_96_char_job_id_still_yields_a_valid_unique_wave_id(self):
        long_a = "job-" + "x" * 92
        long_b = "job-" + "x" * 91 + "y"
        a = integrate_job.job_wave_id({"job_id": long_a, "integration": {"wave": "w1"}})
        b = integrate_job.job_wave_id({"job_id": long_b, "integration": {"wave": "w1"}})
        self.assertLessEqual(len(a), 64)
        self.assertLessEqual(len(b), 64)
        self.assertNotEqual(a, b)
        self.assertEqual(a, integrate_job.job_wave_id({"job_id": long_a, "integration": {"wave": "w1"}}))

    def test_a_previous_per_job_record_is_adopted_atomically(self):
        with TemporaryDirectory() as tmp:
            waves = Path(tmp) / "waves"
            waves.mkdir()
            long_id = "job-" + "x" * 92
            old = f"{long_id}.w1"
            new = integrate_job.job_wave_id({"job_id": long_id, "integration": {"wave": "w1"}})
            (waves / f"{old}.json").write_text(json.dumps({"attempts": ["att_1"], "head_sha": SHORT}))

            adopted = integrate_job.adopt_legacy_wave(Path(tmp), wave=new, legacy=old, attempts=["att_1"])

            self.assertTrue(adopted)
            self.assertTrue((waves / f"{new}.json").exists())
            self.assertTrue((waves / f"{old}.json").exists(), "the old record is preserved")


class DescendantPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(["init", "-q", "-b", "main"], self.repo)
        (self.repo / "a.txt").write_text("a")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "root"], self.repo)
        self.root = git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        (self.repo / "b.txt").write_text("b")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "descendant"], self.repo)
        self.candidate = git(["rev-parse", "HEAD"], self.repo).stdout.strip()

        git(["checkout", "-q", "--orphan", "unrelated"], self.repo)
        (self.repo / "c.txt").write_text("c")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "unrelated"], self.repo)
        self.unrelated = git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        git(["checkout", "-q", "main"], self.repo)

        self.policy = D.DescendantPolicy(root_rev=self.root, repo="mojomast/smoke-real",
                                         scopes=("smoke/",), max_bees=1, max_wall_clock_s=1800,
                                         dependencies=("job_a",))
        self.state = self.tmp / "state"
        self.state.mkdir()
        from bundlebees_plugin import verify_local
        path = verify_local.record_path(self.state, "att_1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"attempt_id": "att_1", "ok": True, "sha": self.root,
                                    "base_rev": self.root, "suite": {"exit_code": 0},
                                    "gitleaks": {"exit_code": 0}}))
        self.accepted_job = {"tasks": [{"state": "accepted", "attempts": ["att_1"]}],
                             "integration": {"last_commit": self.candidate,
                                             "accepted_waves": [{"wave": 1, "commit": self.candidate,
                                                                 "attempts": ["att_1"]}]}}

    def derive(self, **overrides):
        kwargs = dict(candidate_rev=self.candidate, repo="mojomast/smoke-real", worktree=self.repo,
                      assigned_files=["smoke/double.py"], state_dir=self.state, bees=1, wall_clock_s=900)
        kwargs.update(overrides)
        with mock.patch.object(D.jobs, "load_job", lambda state_dir, job_id: self.accepted_job):
            return D.derive(self.policy, **kwargs)

    def test_a_verified_descendant_is_accepted_with_provenance(self):
        derived = self.derive()
        self.assertEqual(derived["base_rev"], self.candidate)
        self.assertEqual(derived["lineage"], {"job_a": self.candidate})
        self.assertEqual(derived["descendant"]["root_rev"], self.root)

    def test_an_unrelated_commit_is_refused(self):
        with self.assertRaises(D.DescendantError) as caught:
            self.derive(candidate_rev=self.unrelated)
        self.assertEqual(caught.exception.code, "not_a_descendant")

    def test_the_dependency_must_name_the_same_commit(self):
        accepted = {"tasks": [{"state": "accepted", "attempts": ["att_1"]}],
                    "integration": {"last_commit": self.root,
                                    "accepted_waves": [{"wave": 1, "commit": self.root, "attempts": ["att_1"]}]}}
        with mock.patch.object(D.jobs, "load_job", lambda state_dir, job_id: accepted):
            with self.assertRaises(D.DescendantError) as caught:
                D.derive(self.policy, candidate_rev=self.candidate, repo="mojomast/smoke-real",
                         worktree=self.repo, assigned_files=["smoke/double.py"], state_dir=self.state,
                         bees=1, wall_clock_s=900)
        self.assertEqual(caught.exception.code, "lineage_mismatch")

    def test_a_dependency_without_accepted_evidence_blocks(self):
        with mock.patch.object(D.jobs, "load_job", lambda state_dir, job_id: {"integration": {}}):
            with self.assertRaises(D.DescendantError) as caught:
                D.derive(self.policy, candidate_rev=self.candidate, repo="mojomast/smoke-real",
                         worktree=self.repo, assigned_files=["smoke/double.py"], state_dir=self.state,
                         bees=1, wall_clock_s=900)
        self.assertEqual(caught.exception.code, "dependency_not_accepted")

    def test_a_wider_scope_or_budget_is_refused(self):
        with self.assertRaises(D.DescendantError) as caught:
            self.derive(assigned_files=["src/secret.py"])
        self.assertEqual(caught.exception.code, "scope_widened")
        with self.assertRaises(D.DescendantError) as caught:
            self.derive(bees=2)
        self.assertEqual(caught.exception.code, "budget_exceeded")

    def test_another_repository_is_refused(self):
        with self.assertRaises(D.DescendantError) as caught:
            self.derive(repo="mojomast/other")
        self.assertEqual(caught.exception.code, "repo_mismatch")


if __name__ == "__main__":
    unittest.main()
