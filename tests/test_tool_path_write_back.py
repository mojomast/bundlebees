"""Tool-path write-back: a completed ``bundlebees_dispatch`` attempt lands in the outbox.

The defect: attempts dispatched through the tool path never write completion
events to the plugin's outbox. Only the job-CLI path writes canonical
``rizzstack.bee.landed.v1`` events, and that emission is coupled to the job
lifecycle, so a tool-path attempt keeps its dispatch-time state forever: the
fleet section shows stale rows, the usage ingest never sees the attempt, and
the reconciliation sweep cannot distinguish completed from unpushed.

The fix under test: when a tool-path attempt's state reaches a terminal value
(``succeeded``, ``failed``, ``cancelled``, ``interrupted``), the same canonical
landing event is written through the same outbox path — attempt id, state,
feature/branch and the usage payload the result envelope carries — and the
emission is best-effort: a failed outbox write is reported, never raised into
the attempt's completion path.

RED-first: every test here fails on ``main``, where the write-back entry point
does not exist, and passes on ``feat/tool-path-write-back``.

No network, no launcher, no model: the only durable surfaces used are the
coordinator lease ``bundlebees_dispatch`` would have written and the plugin's
own outbox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import agent_consumer, bee_events, event_consumer, leases

T0 = 1_800_000_000.0
SOURCE_REV = "a" * 40
LEASE_SHA = "b" * 64
USAGE = {"tokens": 1234, "cost_usd": 0.42}


def brief(**overrides) -> dict:
    base = {
        "objective": "write the tool-path completion back to the outbox",
        "project": "bundlebees",
        "branch": "bee/tool-write-back",
        "source_rev": SOURCE_REV,
        "assigned_files": ["src/tool.py"],
        "acceptance": {"criteria": ["the outbox carries the landing"]},
    }
    base.update(overrides)
    return base


def dispatch(tmp_path: Path, attempt_id: str = "att-tool-1") -> dict:
    """The coordinator-side record ``bundlebees_dispatch`` leaves behind."""

    return leases.record_lease(tmp_path, attempt_id, brief(), now="2026-01-01T00:00:00Z")


def outbox_events(state: Path) -> list[dict]:
    root = state / "events" / "outbox"
    if not root.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


def sidecar(state: Path, attempt_id: str) -> dict | None:
    path = state / "tool-completions" / f"{attempt_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def complete(root: Path, attempt_id: str = "att-tool-1", **overrides) -> dict:
    kwargs = {
        "state": "succeeded",
        "branch": "bee/tool-write-back",
        "model": "deepseek-v4.1-flash",
        "usage": USAGE,
        "now": T0,
    }
    kwargs.update(overrides)
    return agent_consumer.record_tool_completion(root, attempt_id, **kwargs)


# ---------------------------------------------------------------------------
# RED: the terminal completion lands in the outbox
# ---------------------------------------------------------------------------


def test_a_terminal_tool_path_completion_lands_in_the_outbox(tmp_path):
    lease = dispatch(tmp_path)
    result = complete(tmp_path)

    assert result["emitted"] is True
    assert result["created"] is True
    events = outbox_events(tmp_path)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == bee_events.EVENT_TYPE
    assert event["id"] == bee_events.event_id_for("att-tool-1", "succeeded", result["transition_id"])
    data = event["data"]
    assert data["attempt_id"] == "att-tool-1"
    assert data["state"] == "succeeded"
    # The branch and the feature are visible without opening the lease.
    assert data["result"]["reference"] == "bee/tool-write-back"
    assert data["result"]["available"] is True
    assert data["feature_id"]
    # Lease facts come from the coordinator lease the dispatch recorded.
    assert data["lease"]["sha256"] == lease["brief_sha256"]
    assert data["lease"]["assigned_files"] == ["src/tool.py"]
    assert data["source_rev"] == SOURCE_REV
    assert data["backend"]["model"] == "deepseek-v4.1-flash"


def test_the_event_is_a_completion_the_existing_consumer_accepts(tmp_path):
    dispatch(tmp_path)
    complete(tmp_path)

    event = outbox_events(tmp_path)[0]
    classified = event_consumer.classify_completion(event)
    assert classified["completion"] is True
    assert classified["attempt_id"] == "att-tool-1"
    assert classified["state"] == "succeeded"
    assert classified["result_reference"] == "bee/tool-write-back"


def test_every_terminal_state_writes_back_and_non_terminal_never_does(tmp_path):
    dispatch(tmp_path)

    for index, state in enumerate(("succeeded", "failed", "cancelled", "interrupted")):
        attempt_id = f"att-{state}"
        leases.record_lease(
            tmp_path, attempt_id, brief(branch=f"bee/{state}"), now="2026-01-01T00:00:00Z"
        )
        result = complete(
            tmp_path,
            attempt_id=attempt_id,
            state=state,
            branch=f"bee/{state}",
            usage=USAGE if state == "succeeded" else None,
            now=T0 + index,
        )
        assert result["emitted"] is True, state
        stored = outbox_events(tmp_path)
        assert {event["data"]["state"] for event in stored} >= {state}

    # A non-terminal state is a no-op: nothing is written, nothing is claimed.
    before = outbox_events(tmp_path)
    result = complete(tmp_path, attempt_id="att-tool-1", state="running", now=T0 + 99)
    assert result["emitted"] is False
    assert result["reason"] == "state_not_terminal"
    assert outbox_events(tmp_path) == before
    assert sidecar(tmp_path, "att-tool-1") is None


def test_a_second_identical_completion_is_a_deduplicated_no_op(tmp_path):
    dispatch(tmp_path)
    first = complete(tmp_path)
    second = complete(tmp_path)

    assert first["emitted"] is True and first["created"] is True
    assert second["emitted"] is True and second["created"] is False
    assert len(outbox_events(tmp_path)) == 1


# ---------------------------------------------------------------------------
# The usage payload the result envelope carries
# ---------------------------------------------------------------------------


def test_usage_is_recorded_with_the_completion_when_present(tmp_path):
    dispatch(tmp_path)
    result = complete(tmp_path)

    assert result["usage_recorded"] is True
    record = sidecar(tmp_path, "att-tool-1")
    assert record["attempt_id"] == "att-tool-1"
    assert record["state"] == "succeeded"
    assert record["branch"] == "bee/tool-write-back"
    assert record["event_id"] == result["event_id"]
    assert record["usage"] == USAGE


def test_a_completion_without_usage_records_no_usage(tmp_path):
    dispatch(tmp_path)
    result = complete(tmp_path, usage=None)

    assert result["emitted"] is True
    assert result["usage_recorded"] is False
    record = sidecar(tmp_path, "att-tool-1")
    assert record is not None
    assert "usage" not in record


# ---------------------------------------------------------------------------
# Lease facts are enough when the caller only knows the ids
# ---------------------------------------------------------------------------


def test_lease_facts_fill_the_event_when_the_caller_supplies_only_ids(tmp_path):
    dispatch(tmp_path)

    result = agent_consumer.record_tool_completion(
        tmp_path, "att-tool-1", state="succeeded", now=T0
    )

    assert result["emitted"] is True
    data = outbox_events(tmp_path)[0]["data"]
    assert data["attempt_id"] == "att-tool-1"
    assert data["state"] == "succeeded"
    assert data["source_rev"] == SOURCE_REV
    assert data["lease"]["assigned_files"] == ["src/tool.py"]
    assert data["feature_id"] == "bundlebees"
    assert data["result"]["reference"] == "bee/tool-write-back"


def test_a_completion_without_any_lease_facts_is_refused_not_raised(tmp_path):
    result = agent_consumer.record_tool_completion(
        tmp_path, "att-orphan", state="succeeded", now=T0
    )

    assert result["emitted"] is False
    assert result["code"]
    assert outbox_events(tmp_path) == []


def test_a_terminal_attempt_without_a_recorded_lease_uses_supplied_facts(tmp_path):
    result = agent_consumer.record_tool_completion(
        tmp_path,
        "att-foreign",
        state="failed",
        branch="bee/foreign",
        lease_sha256=LEASE_SHA,
        source_rev=SOURCE_REV,
        assigned_files=["src/foreign.py"],
        now=T0,
    )

    assert result["emitted"] is True
    data = outbox_events(tmp_path)[0]["data"]
    assert data["state"] == "failed"
    assert data["lease"]["sha256"] == LEASE_SHA
    assert data["source_rev"] == SOURCE_REV
    assert data["result"]["reference"] == "bee/foreign"
    assert data["result"]["available"] is False


# ---------------------------------------------------------------------------
# The emission never breaks the attempt completion path
# ---------------------------------------------------------------------------


def test_a_failed_outbox_write_is_reported_never_raised(tmp_path, monkeypatch):
    dispatch(tmp_path)

    def broken_record(state_dir, event):
        raise bee_events.EventError(
            "event_write_failed", "outbox write failed: OSError", "Check the state directory."
        )

    monkeypatch.setattr(agent_consumer.bee_events, "record_landing", broken_record)

    result = complete(tmp_path)

    assert result["emitted"] is False
    assert result["code"] == "event_write_failed"
    assert outbox_events(tmp_path) == []
    # The completion path continues: the refusal is a value, not an exception.
    # The usage sidecar is not written for an event that never landed.
    assert sidecar(tmp_path, "att-tool-1") is None


def test_a_failed_outbox_write_retries_cleanly(tmp_path, monkeypatch):
    dispatch(tmp_path)
    real_record = bee_events.record_landing

    def broken_record(state_dir, event):
        raise bee_events.EventError(
            "event_write_failed", "outbox write failed: OSError", "retry"
        )

    monkeypatch.setattr(agent_consumer.bee_events, "record_landing", broken_record)
    assert complete(tmp_path)["emitted"] is False

    monkeypatch.setattr(agent_consumer.bee_events, "record_landing", real_record)
    result = complete(tmp_path)

    assert result["emitted"] is True and result["created"] is True
    assert len(outbox_events(tmp_path)) == 1


def test_an_unwritable_state_directory_is_refused_never_raised(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    # A regular file where the events tree must be created: the outbox write
    # cannot succeed and must not escalate into the completion path.
    (blocked / "events").write_text("not a directory", encoding="utf-8")

    result = agent_consumer.record_tool_completion(
        blocked, "att-tool-1", state="succeeded", lease_sha256=LEASE_SHA,
        source_rev=SOURCE_REV, assigned_files=["src/x.py"], now=T0,
    )

    assert result["emitted"] is False
    assert result["code"]


# ---------------------------------------------------------------------------
# The write-back never widens or corrupts the durable shapes
# ---------------------------------------------------------------------------


def test_the_event_keeps_exactly_the_canonical_shape(tmp_path):
    dispatch(tmp_path)
    complete(tmp_path, artifact_sha256="c" * 64)

    event = outbox_events(tmp_path)[0]
    bee_events._validate_event_local(event)  # would raise on any drift
    data = event["data"]
    assert set(data) <= set(bee_events._DATA_KEYS)
    assert data["result"]["artifact_sha256"] == "c" * 64


def test_an_unsafe_attempt_id_is_refused_before_any_write(tmp_path):
    result = agent_consumer.record_tool_completion(
        tmp_path, "../escape", state="succeeded", now=T0
    )

    assert result["emitted"] is False
    assert not list(tmp_path.rglob("*.json"))
