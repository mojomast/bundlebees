"""Child process used by the crash tests.

Runs one reconciler tick against a disk-backed fake launcher and dies at a named
point with `os._exit`, so the parent test observes a *real* process termination
rather than a simulated exception.

    python tests/crash_child.py <state_dir> <job_id> <phase> <journal.jsonl> <posts.jsonl>

Phases:
  before_post   die at the top of post_attempt, after recording the attempt
  after_post    record the attempt in the journal, then die before returning
  after_confirm run _dispatch_one (intent + in-memory transition) and die before
                the job is saved — the crash window between intent confirmation
                and persisted state
  multi         two tasks: dispatch the first, die during the second POST
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

T0 = 1_800_000_000.0


def _append(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class DiskLauncher:
    idempotent = True

    def __init__(self, phase: str, journal: Path, posts: Path) -> None:
        self.phase = phase
        self.journal = journal
        self.posts = posts
        self.calls = 0

    def _journal(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def list_attempts(self) -> list[dict]:
        return self._journal()

    def get_attempt(self, attempt_id: str) -> dict | None:
        return next((entry for entry in self._journal() if entry["attempt_id"] == attempt_id), None)

    def post_attempt(self, brief: dict) -> dict:
        self.calls += 1
        _append(self.posts, {"phase": self.phase, "brief": dict(brief)})
        if self.phase == "before_post":
            os._exit(9)
        entry = {
            "attempt_id": f"att_{self.calls}",
            "state": "succeeded",
            "created_at": rec._iso(T0),
            "brief": dict(brief),
        }
        _append(self.journal, entry)
        if self.phase == "after_post":
            os._exit(9)
        if self.phase == "multi" and self.calls == 2:
            os._exit(9)
        return entry


def main(argv: list[str]) -> int:
    state, job_id, phase, journal, posts = argv[1:6]
    state_dir = Path(state)
    launcher = DiskLauncher(phase, Path(journal), Path(posts))

    if phase == "after_confirm":
        job = jobs.load_job(state_dir, job_id)
        task = job["tasks"][0]
        rec._dispatch_one(state_dir, job, task, rec._Adapter(launcher), T0, [])
        os._exit(9)

    rec.tick(state_dir, job_id, launcher=launcher, now=T0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
