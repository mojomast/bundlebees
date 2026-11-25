"""RED test for the attempt-reconcile slice (AC-003).

A tool-path attempt that reached a terminal launcher state is never reconciled
into the local attempt store unless somebody polls the attempt, so
``bundlebees status`` keeps counting it in flight. The new interface is looked
up inside the test body, so this file is a pure behavioural RED on
``origin/main`` where ``reconcile.reconcile_tool_attempts`` does not exist yet.
"""

from __future__ import annotations

from pathlib import Path

from bundlebees_plugin import cli
from bundlebees_plugin import leases
from bundlebees_plugin import reconcile

SHA = "a" * 40


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


def test_reconcile_maps_terminal_launcher_attempts_and_leaves_running_ones(
    tmp_path: Path,
):
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
