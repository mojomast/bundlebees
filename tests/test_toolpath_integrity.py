"""RED tests for the tool-path integrity slice.

Three failure classes this file pins, all observed in bundlebees:

* dispatch accepts a glob lease (``schemas/examples/**``) while
  ``bee_events._canonical_assigned_files`` refuses ``*``, so the lease is
  recorded, the bee runs, and the terminal write-back can never land;
* a tool-path attempt that reached a terminal launcher state is never
  reconciled into the local attempt store unless somebody polls the attempt,
  so ``bundlebees status`` keeps counting it in flight;
* verify discovers a missing or diverged feature branch only after the bee has
  already run.

The tests are intentionally independent: the new interface is looked up inside
the test body, so the one regression criterion (the job-path reconcile tick)
still runs and passes on ``origin/main`` while the new behaviour is RED.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events
from bundlebees_plugin import cli
from bundlebees_plugin import job as jobs
from bundlebees_plugin import leases
from bundlebees_plugin import reconcile
from bundlebees_plugin import tools

SHA = "a" * 40
T0 = 1_800_000_000.0


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _brief(**overrides):
    brief = {
        "objective": "Harden one tool-path integrity behaviour",
        "project": "proj",
        "branch": "feat/toolpath-integrity",
        "source_rev": SHA,
        "assigned_files": ["bundlebees_plugin/tools.py"],
        "commands": ["python3 -m pytest -q tests/test_toolpath_integrity.py"],
        "acceptance": ["the named behaviour is pinned"],
        "budget": {"timeout_s": 600, "max_tool_calls": 20},
    }
    brief.update(overrides)
    return brief


class DispatchClient:
    """The minimal launcher surface dispatch touches, with no network."""

    def __init__(self, attempts=None):
        self.created: list[dict] = []
        self.attempts = list(attempts or [])
        self.projects_data = [{"key": "proj"}]

    def projects(self):
        return list(self.projects_data)

    def attempts_raw(self):
        return [dict(item) for item in self.attempts]

    def create(self, brief, model=None, kanban_task_id=None):
        self.created.append({"brief": dict(brief), "model": model})
        return {
            "attempt_id": "att_created_0001",
            "task_id": "task_0001",
            "state": "queued",
        }


def _dispatch(client, brief, state_dir, settings=None):
    args = {"brief": brief, "_settings": dict(settings or {})}
    return json.loads(tools._dispatch(client, args, Path(state_dir)))


# ---------------------------------------------------------------------------
# AC-001: a glob lease is refused by name and nothing is recorded/dispatched
# ---------------------------------------------------------------------------


def test_dispatch_refuses_a_glob_lease_with_a_named_error(tmp_path: Path):
    patterns = ["schemas/examples/**", "bundlebees_plugin/?.py", "src/[ab].py"]

    for index, pattern in enumerate(patterns):
        state = tmp_path / f"state_{index}"
        client = DispatchClient()

        payload = _dispatch(client, _brief(assigned_files=[pattern]), state)

        assert payload.get("ok") is False, payload
        assert payload.get("code") == "lease_glob_unsupported", payload
        assert client.created == [], "a glob lease must never start a bee"
        assert not (state / "leases").exists(), "a refused dispatch records no lease"


# ---------------------------------------------------------------------------
# AC-002: the same validator still accepts ordinary repository paths
# ---------------------------------------------------------------------------


def test_dispatch_still_accepts_normal_repository_relative_paths(tmp_path: Path):
    validator = getattr(bee_events, "validate_lease_files", None)
    assert callable(validator), (
        "bee_events.validate_lease_files must be the one lease validator shared "
        "by dispatch and the landing write-back"
    )

    paths = [
        "bundlebees_plugin/tools.py",
        "docs/guide.v2.md",
        "tools/a+b/c@d.txt",
        "docs/release.notes/2026.11.md",
        "tests/test_toolpath_integrity.py",
    ]

    assert validator(list(paths)) == paths

    client = DispatchClient()
    payload = _dispatch(client, _brief(assigned_files=paths), tmp_path / "state")

    assert payload.get("ok") is True, payload
    assert len(client.created) == 1, "ordinary paths must still dispatch"
    assert (tmp_path / "state" / "leases" / "att_created_0001.json").exists()


# ---------------------------------------------------------------------------
# AC-003: reconcile a terminal launcher attempt into the local attempt store
# ---------------------------------------------------------------------------


class ReconcileBackend:
    """A fake launcher exposing attempts_raw() and create(); never any network."""

    def __init__(self, attempts):
        self._attempts = list(attempts)
        self.created: list[dict] = []

    def attempts_raw(self):
        return [dict(item) for item in self._attempts]

    def create(self, brief, **kwargs):
        self.created.append(dict(brief))
        raise AssertionError("reconcile must never create a new attempt")


def _lease(state: Path, attempt_id: str) -> None:
    leases.record_lease(
        state,
        attempt_id,
        _brief(
            branch=f"feat/{attempt_id}",
            assigned_files=[f"src/{attempt_id}.py"],
        ),
    )


def _states(state: Path) -> dict[str, str]:
    return {row["attempt_id"]: row["state"] for row in cli._attempt_rows(state)}


def _reconcile_fixture(tmp_path: Path):
    state = tmp_path / "state"
    terminal = (
        "att_succeeded_01",
        "att_failed_02",
        "att_cancelled_03",
    )
    for attempt_id in terminal:
        _lease(state, attempt_id)
    _lease(state, "att_running_04")
    backend = ReconcileBackend(
        [
            {"attempt_id": "att_succeeded_01", "state": "succeeded"},
            {"attempt_id": "att_failed_02", "state": "failed"},
            {"attempt_id": "att_cancelled_03", "state": "cancelled"},
            {"attempt_id": "att_running_04", "state": "running"},
        ]
    )
    return state, backend, terminal


def _assert_dispatched_before(state: Path) -> None:
    assert _states(state) == {
        "att_succeeded_01": "dispatched",
        "att_failed_02": "dispatched",
        "att_cancelled_03": "dispatched",
        "att_running_04": "dispatched",
    }


def _assert_reconcile_summary(summary, terminal, backend) -> None:
    assert summary["checked"] == 4
    assert set(summary["reconciled"]) == set(terminal)
    assert set(summary["unchanged"]) == {"att_running_04"}
    assert backend.created == [], "reconcile must never re-dispatch"


def _assert_terminal_states_after(state: Path) -> None:
    assert _states(state) == {
        "att_succeeded_01": "unverified",
        "att_failed_02": "failed",
        "att_cancelled_03": "failed",
        "att_running_04": "dispatched",
    }


def test_reconcile_maps_terminal_launcher_attempts_and_leaves_running_ones(tmp_path: Path):
    sync = getattr(reconcile, "reconcile_tool_attempts", None)
    assert callable(sync), (
        "reconcile.reconcile_tool_attempts(state_dir, backend) must sweep "
        "terminal launcher attempts into the local attempt store"
    )

    state, backend, terminal = _reconcile_fixture(tmp_path)
    _assert_dispatched_before(state)

    summary = sync(state, backend)

    _assert_reconcile_summary(summary, terminal, backend)
    _assert_terminal_states_after(state)


# ---------------------------------------------------------------------------
# AC-004: dispatch preflights the feature branch and refuses before any bee
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A real repository: C1 on main, C2 on main, feat existing at C1."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "base")
    first = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "feat/toolpath-integrity")
    (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "next")
    second = _git(repo, "rev-parse", "HEAD")
    return repo, first, second


def test_dispatch_refuses_a_missing_or_diverged_feature_branch(tmp_path: Path):
    repo, first, second = _repository(tmp_path)
    settings = {"worktree": str(repo)}

    missing = DispatchClient()
    payload = _dispatch(
        missing,
        _brief(branch="feat/absent", source_rev=second),
        tmp_path / "missing",
        settings,
    )
    assert payload.get("ok") is False, payload
    assert payload.get("code") == "branch_not_found", payload
    assert missing.created == [], "a missing feature branch must not start a bee"

    diverged = DispatchClient()
    payload = _dispatch(
        diverged,
        _brief(branch="feat/toolpath-integrity", source_rev=second),
        tmp_path / "diverged",
        settings,
    )
    assert payload.get("ok") is False, payload
    assert payload.get("code") == "branch_diverged", payload
    assert diverged.created == [], "a diverged feature branch must not start a bee"

    accepted = DispatchClient()
    payload = _dispatch(
        accepted,
        _brief(branch="feat/toolpath-integrity", source_rev=first),
        tmp_path / "accepted",
        settings,
    )
    assert payload.get("ok") is True, payload
    assert len(accepted.created) == 1, "a branch that descends must still dispatch"


# ---------------------------------------------------------------------------
# AC-005 (regression): the durable job path is unchanged
# ---------------------------------------------------------------------------


class JobLauncher:
    """A launcher implementing exactly the job reconciler's four members."""

    idempotent = True

    def __init__(self):
        self.entries: list[dict] = []
        self.posts: list[dict] = []

    def list_attempts(self):
        return list(self.entries)

    def get_attempt(self, attempt_id):
        return next(
            (entry for entry in self.entries if entry["attempt_id"] == attempt_id),
            None,
        )

    def post_attempt(self, brief):
        self.posts.append(dict(brief))
        entry = {
            "attempt_id": f"att_job_{len(self.entries) + 1}",
            "state": "succeeded",
            "created_at": iso(T0),
            "brief": dict(brief),
        }
        self.entries.append(entry)
        return entry


def _job(tmp_path: Path) -> None:
    jobs.record_authorization(
        tmp_path,
        job_id="job_a",
        statement="Keep the durable job reconcile path unchanged.",
        repo="example/bundlebees",
        base_rev=SHA,
        scope=["tests/test_toolpath_integrity.py"],
        bees=1,
        wall_clock_s=3600,
        interactive=True,
    )
    jobs.create_job(
        tmp_path,
        job_id="job_a",
        tasks=[
            {
                "task_id": "T1",
                "objective": "keep the job path unchanged",
                "lease": ["tests/test_toolpath_integrity.py"],
                "acceptance": {
                    "criteria": ["the job path still dispatches once"],
                    "tests": ["tests/test_toolpath_integrity.py"],
                },
            }
        ],
    )


def test_job_path_reconcile_tick_is_unchanged(tmp_path: Path):
    _job(tmp_path)
    launcher = JobLauncher()
    verified: list = []

    result = reconcile.tick(
        tmp_path,
        "job_a",
        launcher=launcher,
        verifier=lambda context: verified.append(context) or {"ok": True},
        now=T0,
    )

    assert result.action == "dispatch"
    assert result.status == "ok"
    assert result.transitions == ("T1->dispatched",)
    assert len(launcher.posts) == 1
    assert launcher.posts[0]["objective"] == "keep the job path unchanged"
    assert verified == []
