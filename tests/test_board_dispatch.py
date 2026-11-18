"""Board -> dispatcher bridge: ready cards with an approved brief are stocked.

The bridge is the only thing that writes a queue feature for board work.  It
reads the ready column through the public ``hermes kanban`` CLI, requires a
durable owner approval record that carries the brief, validates that brief with
the strict brief validator, requires the named job document to exist, and then
writes exactly two durable artifacts:

* one ``queue.json`` feature (the live docket envelope is preserved), and
* one ``factory/dispatch-approvals/<work_id>.json`` record.

Every write is idempotent (a replay changes nothing and never overwrites), and a
card without an approval record is never stocked: the bridge fails closed.

RED before the fix: :mod:`bundlebees_plugin.board_dispatch` does not exist, so
every test below fails with an import error.  GREEN after it lands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bundlebees_plugin import board_dispatch
from bundlebees_plugin import factory_driver
from bundlebees_plugin import job as job_mod

SOURCE_REV = "73162e0c72cef888487d313a4905a243d6832f08"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _card(task_id: str, title: str = "approved slice") -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "body": "",
        "assignee": None,
        "status": "ready",
        "priority": 5,
    }


def _runner(cards: list[dict[str, Any]], calls: list[list[str]] | None = None):
    def run(argv: list[str]) -> str:
        if calls is not None:
            calls.append(list(argv))
        return json.dumps(cards)

    return run


def _brief(task_id: str = "t_ready_1", **overrides: Any) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "objective": f"Do the approved slice for {task_id}.",
        "project": "workflow",
        "branch": "feat/approved-slice",
        "source_rev": SOURCE_REV,
        "assigned_files": ["rizzstack_memory/retention.py"],
        "acceptance": {
            "criteria": ["the slice works"],
            "tests": ["tests/test_memory_retention.py"],
        },
    }
    brief.update(overrides)
    return brief


def _write_job(
    state: Path,
    job_id: str,
    *,
    task_id: str = "T1",
    lease: tuple[str, ...] = ("rizzstack_memory/retention.py",),
) -> None:
    """Create a real sealed authorization + job through the production store."""

    job_mod.record_authorization(
        state,
        job_id=job_id,
        statement="test fixture authorization for one leased slice",
        repo="mojomast/workflow",
        base_rev=SOURCE_REV,
        scope=list(lease),
        bees=1,
        wall_clock_s=3600,
        require_tty=False,
    )
    job_mod.create_job(
        state,
        job_id=job_id,
        tasks=[
            {
                "task_id": task_id,
                "objective": "fixture slice",
                "project": "workflow",
                "lease": list(lease),
                "acceptance": {"criteria": ["fixture"]},
            }
        ],
    )


def _approval(
    state: Path,
    task_id: str,
    *,
    job_id: str = "job_slice_1",
    repo: str = "mojomast/workflow",
    approved: bool = True,
    brief: dict[str, Any] | None = None,
    **overrides: Any,
) -> Path:
    path = board_dispatch.approval_path(state, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "schema": board_dispatch.APPROVAL_SCHEMA,
        "task_id": task_id,
        "repo": repo,
        "job_id": job_id,
        "approved": approved,
        "approved_by": "clanker01",
        "approved_at": "2026-11-18T21:00:00Z",
        "brief": _brief(task_id) if brief is None else brief,
    }
    body.update(overrides)
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _queue(state: Path, features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body = {
        "schema": "bundlebees.queue/1",
        "generated_at": "2026-11-18T04:27:23Z",
        "features": features or [],
        "note": "live docket envelope, does not belong to the bridge",
    }
    path = state / "queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return body


def _stock(state: Path, cards: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return board_dispatch.stock(state, runner=_runner(cards), now="2026-11-18T22:30:00Z", **kwargs)


def _features(state: Path) -> list[dict[str, Any]]:
    return json.loads((state / "queue.json").read_text(encoding="utf-8"))["features"]


# ---------------------------------------------------------------------------
# The happy path and its idempotency
# ---------------------------------------------------------------------------


def test_ready_card_with_approval_is_stocked(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1")

    report = _stock(state, [_card("t_ready_1")])

    assert report["stocked"] == [
        {"task_id": "t_ready_1", "work_id": "bd_t_ready_1", "job_id": "job_slice_1"}
    ]
    assert report["skipped"] == []
    features = _features(state)
    assert [(row["feature_id"], row["job_id"], row["repo"], row["status"]) for row in features] == [
        ("t_ready_1", "job_slice_1", "mojomast/workflow", "queued")
    ]
    approval = factory_driver.read_dispatch_approval(state, "bd_t_ready_1")
    assert approval is not None
    assert approval["approved"] is True
    assert approval["scope"] == ["rizzstack_memory/retention.py"]
    assert approval["job_id"] == "job_slice_1"
    assert approval["approved_by"] == "clanker01"


def test_second_run_changes_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1")

    first = _stock(state, [_card("t_ready_1")])
    assert first["stocked"]
    queue_before = (state / "queue.json").read_bytes()
    approval_before = factory_driver.dispatch_approval_path(state, "bd_t_ready_1").read_bytes()

    second = _stock(state, [_card("t_ready_1")])

    assert second["stocked"] == []
    assert second["unchanged"] == [
        {"task_id": "t_ready_1", "work_id": "bd_t_ready_1", "job_id": "job_slice_1"}
    ]
    assert (state / "queue.json").read_bytes() == queue_before
    assert factory_driver.dispatch_approval_path(state, "bd_t_ready_1").read_bytes() == approval_before


# ---------------------------------------------------------------------------
# Fail closed: no approval, a false approval, an invalid brief, a missing job
# ---------------------------------------------------------------------------


def test_card_without_approval_is_never_stocked(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")

    report = _stock(state, [_card("t_ready_1")])

    assert report["stocked"] == []
    assert report["skipped"] == [
        {"task_id": "t_ready_1", "title": "approved slice", "reason": "approval_absent", "detail": ""}
    ]
    assert _features(state) == []
    assert not factory_driver.dispatch_approval_path(state, "bd_t_ready_1").exists()


def test_false_approval_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1", approved=False)

    report = _stock(state, [_card("t_ready_1")])

    assert report["stocked"] == []
    assert report["skipped"][0]["reason"] == "approval_invalid"
    assert _features(state) == []


def test_invalid_brief_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1", brief=_brief("t_ready_1", assigned_files=[]))

    report = _stock(state, [_card("t_ready_1")])

    assert report["stocked"] == []
    assert report["skipped"][0]["reason"] == "brief_invalid"
    assert _features(state) == []


def test_missing_job_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _approval(state, "t_ready_1")

    report = _stock(state, [_card("t_ready_1")])

    assert report["stocked"] == []
    assert report["skipped"][0]["reason"] == "job_absent"
    assert _features(state) == []


# ---------------------------------------------------------------------------
# A conflicting feature is never overwritten
# ---------------------------------------------------------------------------


def test_existing_feature_with_another_job_is_a_conflict(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(
        state,
        [
            {
                "feature_id": "t_ready_1",
                "repo": "mojomast/workflow",
                "job_id": "job_other",
                "title": "someone else's slice",
                "status": "queued",
            }
        ],
    )
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1")
    before = (state / "queue.json").read_bytes()

    report = _stock(state, [_card("t_ready_1")])

    assert report["stocked"] == []
    assert report["skipped"][0]["reason"] == "feature_conflict"
    assert (state / "queue.json").read_bytes() == before


# ---------------------------------------------------------------------------
# Envelope, CLI shape and dry-run
# ---------------------------------------------------------------------------


def test_envelope_and_existing_features_are_preserved(tmp_path: Path) -> None:
    state = tmp_path / "state"
    existing = {
        "feature_id": "smoke-feature",
        "repo": "mojomast/rizzstack-smoke",
        "job_id": "smoke-job",
        "title": "smoke",
        "status": "running",
    }
    _queue(state, [existing])
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1")

    _stock(state, [_card("t_ready_1")])

    document = json.loads((state / "queue.json").read_text(encoding="utf-8"))
    assert document["schema"] == "bundlebees.queue/1"
    assert document["generated_at"] == "2026-11-18T04:27:23Z"
    assert document["note"] == "live docket envelope, does not belong to the bridge"
    assert existing in document["features"]
    ids = [row["feature_id"] for row in document["features"]]
    assert ids == ["smoke-feature", "t_ready_1"]


def test_ready_column_is_read_through_the_public_cli(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1")
    calls: list[list[str]] = []

    board_dispatch.stock(
        state, runner=_runner([_card("t_ready_1")], calls), now="2026-11-18T22:30:00Z"
    )

    assert calls == [["hermes", "kanban", "list", "--status", "ready", "--json"]]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)
    _write_job(state, "job_slice_1")
    _approval(state, "t_ready_1")
    before = (state / "queue.json").read_bytes()

    report = _stock(state, [_card("t_ready_1")], dry_run=True)

    assert report["stocked"] == []
    assert report["would_stock"]
    assert report["intended"]
    assert (state / "queue.json").read_bytes() == before
    assert not factory_driver.dispatch_approval_path(state, "bd_t_ready_1").exists()


def test_unreadable_board_is_one_refusal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _queue(state)

    def broken(argv: list[str]) -> str:
        raise OSError("kanban is not installed")

    with pytest.raises(board_dispatch.BoardDispatchError) as raised:
        board_dispatch.stock(state, runner=broken, now="2026-11-18T22:30:00Z")
    assert raised.value.code == "board_unreadable"
