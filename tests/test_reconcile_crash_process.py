"""Crash tests that kill a real process, not a simulated exception.

The child (`tests/crash_child.py`) runs one tick against a disk-backed fake
launcher and dies with `os._exit` at a named point. The parent then runs a tick
against the same on-disk state and asserts that nothing was dispatched twice and
nothing was adopted without proof.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

from tests.test_reconcile import make_job, T0

ROOT = Path(__file__).resolve().parents[1]
CHILD = ROOT / "tests" / "crash_child.py"


class FileLauncher:
    """The parent-side launcher: same journal, same attempt ids."""

    idempotent = True

    def __init__(self, journal: Path, posts: Path) -> None:
        self.journal = journal
        self.posts = posts

    def _journal(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines() if line.strip()]

    def list_attempts(self) -> list[dict]:
        return self._journal()

    def get_attempt(self, attempt_id: str) -> dict | None:
        return next((entry for entry in self._journal() if entry["attempt_id"] == attempt_id), None)

    def post_attempt(self, brief: dict) -> dict:
        with self.posts.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"phase": "parent", "brief": dict(brief)}) + "\n")
        entry = {
            "attempt_id": f"att_parent_{len(self._journal())}",
            "state": "succeeded",
            "created_at": rec._iso(T0 + 2),
            "brief": dict(brief),
        }
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry


def _posts(path: Path) -> int:
    return len([line for line in path.read_text().splitlines() if line.strip()]) if path.exists() else 0


def _run_child(tmp_path: Path, job_id: str, phase: str):
    journal = tmp_path / "journal.jsonl"
    posts = tmp_path / "posts.jsonl"
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        [sys.executable, str(CHILD), str(tmp_path), job_id, phase, str(journal), str(posts)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, FileLauncher(journal, posts), journal, posts


class ProcessCrash:
    __test__ = True
    def test_killed_before_the_post_never_posts_twice(self, tmp_path):
        make_job(tmp_path)
        proc, launcher, journal, posts = _run_child(tmp_path, "job_a", "before_post")

        assert proc.returncode == 9, proc.stderr
        assert _posts(posts) == 1          # the child recorded its attempt
        assert launcher.list_attempts() == []  # but nothing reached the journal

        # The intent on disk is uncertainty, not a retry licence.
        waited = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=10, max_uncertain_ticks=50)
        assert waited.status == "reconcile_wait"
        assert _posts(posts) == 1

        escalated = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 11, grace_s=10, max_uncertain_ticks=50)
        assert escalated.status == "escalated"
        assert _posts(posts) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "failed"

    def test_killed_after_the_post_is_adopted_without_a_second_post(self, tmp_path):
        make_job(tmp_path)
        proc, launcher, journal, posts = _run_child(tmp_path, "job_a", "after_post")

        assert proc.returncode == 9, proc.stderr
        assert len(launcher.list_attempts()) == 1
        assert _posts(posts) == 1

        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert adopted.action == "adopt"
        assert _posts(posts) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["attempts"] == ["att_1"]

    def test_killed_after_intent_confirmation_adopts_the_confirmed_attempt(self, tmp_path):
        make_job(tmp_path)
        proc, launcher, journal, posts = _run_child(tmp_path, "job_a", "after_confirm")

        assert proc.returncode == 9, proc.stderr
        assert _posts(posts) == 1
        # The job was never saved, so disk still says "pending".
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "pending"

        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert adopted.action == "adopt"
        assert adopted.detail.startswith("adopted att_")
        assert _posts(posts) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatched"

    def test_killed_during_a_multi_task_batch_posts_nothing_twice(self, tmp_path):
        make_job(
            tmp_path,
            tasks=[
                {"task_id": "T1", "objective": "first", "lease": ["tools/x.py"],
                 "acceptance": {"criteria": ["a"], "tests": ["tests/x.py"]}},
                {"task_id": "T2", "objective": "second", "lease": ["tools/y.py"],
                 "acceptance": {"criteria": ["b"], "tests": ["tests/y.py"]}},
            ],
        )
        proc, launcher, journal, posts = _run_child(tmp_path, "job_a", "multi")

        assert proc.returncode == 9, proc.stderr
        assert _posts(posts) == 2  # both POSTs were issued before the kill

        # T1 landed and was confirmed; T2's intent exists with its journal entry.
        first = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)
        second = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 2)

        assert _posts(posts) == 2
        states = [t["state"] for t in jobs.load_job(tmp_path, "job_a")["tasks"]]
        assert states == ["dispatched", "dispatched"]
        assert {first.action, second.action} <= {"adopt", "reap", "dispatch", "verify"}

    def test_a_killed_tick_does_not_wedge_the_job(self, tmp_path):
        """The lock left by the dead child is stolen by the next process."""

        make_job(tmp_path)
        proc, launcher, journal, posts = _run_child(tmp_path, "job_a", "after_post")
        assert proc.returncode == 9, proc.stderr

        lock = rec.lock_path(tmp_path, "job_a")
        assert lock.exists(), "the killed child should have left its lock behind"

        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert adopted.status == "ok"
        assert "stale lock" in adopted.detail
        assert not lock.exists()
