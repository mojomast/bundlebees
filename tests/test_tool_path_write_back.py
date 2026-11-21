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
import os
import stat
import threading
from pathlib import Path

import pytest

from bundlebees_plugin import agent_consumer, bee_events, event_consumer, leases
from bundlebees_plugin.client import ClientError
from tests.fakes import FakeCtx, load_plugin

T0 = 1_800_000_000.0
SOURCE_REV = "a" * 40
LEASE_SHA = "b" * 64
USAGE = {"tokens": 1234, "cost_usd": 0.42}
ARTIFACT_SHA = "c" * 64


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


# ---------------------------------------------------------------------------
# RED: the real tool op handlers write the completion back
# ---------------------------------------------------------------------------
#
# These tests do not call ``record_tool_completion`` directly. They drive the
# production entrypoint — the ``bundlebees_attempt`` tool handler registered by
# ``tools.register_tools`` against a disposable plugin state dir — the way the
# coordinator's model would, with a launcher stub whose attempt sits in a
# caller-chosen state.


class LauncherStub:
    """A launcher client whose attempt sits in a chosen state."""

    def __init__(
        self,
        state: str = "succeeded",
        result: dict | None = None,
        result_error: Exception | None = None,
        cancel_state: str = "cancelled",
        review_state: str = "reviewed",
    ) -> None:
        self.state = state
        self.result_payload = result
        self.result_error = result_error
        self.cancel_state = cancel_state
        self.review_state = review_state
        self.calls: list[tuple[str, tuple, dict]] = []

    def get(self, attempt_id: str) -> dict:
        self.calls.append(("get", (attempt_id,), {}))
        return {"attempt_id": attempt_id, "task_id": "t1", "state": self.state}

    def result(self, attempt_id: str) -> dict:
        self.calls.append(("result", (attempt_id,), {}))
        if self.result_error is not None:
            raise self.result_error
        return dict(self.result_payload or {})

    def cancel(self, attempt_id: str) -> dict:
        self.calls.append(("cancel", (attempt_id,), {}))
        return {"attempt_id": attempt_id, "state": self.cancel_state}

    def review(self, attempt_id: str, accepted: bool, note=None) -> dict:
        self.calls.append(("review", (attempt_id,), {"accepted": accepted}))
        return {
            "attempt_id": attempt_id,
            "state": self.review_state,
            "accepted": accepted,
        }


def result_envelope() -> dict:
    """The result envelope a finished launcher returns for a successful bee."""
    return {
        "attempt_id": "att-tool-1",
        "state": "succeeded",
        "observed": {
            "changed_files": ["src/tool.py"],
            "usage": dict(USAGE),
            "sha256": ARTIFACT_SHA,
        },
        "checks": [{"name": "unit", "ok": True}],
    }


def tool_ctx(state_dir: Path):
    plugin = load_plugin()
    ctx = FakeCtx()
    ctx.bundlebees_state_dir = Path(state_dir)
    plugin.register(ctx)
    return ctx


def attempt_op(ctx, args: dict, client=None) -> dict:
    if client is not None:
        ctx.bundlebees_client = client
    raw = ctx.tools["bundlebees_attempt"]["handler"](args)
    assert isinstance(raw, str)
    return json.loads(raw)


def seeded_ctx(tmp_path: Path, attempt_id: str = "att-tool-1"):
    """A registered plugin whose state dir holds the dispatch-time lease."""
    ctx = tool_ctx(tmp_path)
    leases.record_lease(
        tmp_path,
        attempt_id,
        brief(branch="bee/tool-write-back"),
        now="2026-01-01T00:00:00Z",
    )
    # The review gate: the coordinator accepted this attempt into a wave.
    waves = tmp_path / "waves"
    waves.mkdir(parents=True, exist_ok=True)
    (waves / "w1.json").write_text(
        json.dumps({"wave_id": "w1", "accepted": True, "attempts": [attempt_id]}),
        encoding="utf-8",
    )
    return ctx


def test_attempt_get_with_a_terminal_success_writes_back_the_completion(tmp_path):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(state="succeeded", result=result_envelope())

    envelope = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)

    assert envelope["ok"] is True
    assert envelope["attempt"]["state"] == "succeeded"
    write_back = envelope["write_back"]
    assert write_back["emitted"] is True
    assert write_back["created"] is True
    assert write_back["state"] == "succeeded"
    event = outbox_events(tmp_path)[0]
    assert event["type"] == bee_events.EVENT_TYPE
    assert event["data"]["attempt_id"] == "att-tool-1"
    assert event["data"]["state"] == "succeeded"
    assert event["data"]["result"]["available"] is True
    assert event["data"]["result"]["artifact_sha256"] == ARTIFACT_SHA
    assert event["data"]["result"]["reference"] == "bee/tool-write-back"
    assert event["data"]["source_rev"] == SOURCE_REV
    record = sidecar(tmp_path, "att-tool-1")
    assert record is not None
    assert record["usage"] == USAGE


def test_attempt_get_with_a_terminal_failure_writes_back_without_a_result(tmp_path):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(
        state="failed", result_error=ClientError("no result yet", "no_result_yet")
    )

    envelope = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)

    assert envelope["ok"] is True
    assert "result" not in envelope
    write_back = envelope["write_back"]
    assert write_back["emitted"] is True
    data = outbox_events(tmp_path)[0]["data"]
    assert data["state"] == "failed"
    # The launcher's terminal state and the missing result stay separate facts:
    # a failed emission must never be read as a failed worker, and vice versa.
    assert data["result"]["available"] is False
    record = sidecar(tmp_path, "att-tool-1")
    assert record is not None
    assert "usage" not in record


def test_attempt_cancel_writes_back_the_cancellation(tmp_path):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(cancel_state="cancelled")

    envelope = attempt_op(
        ctx, {"op": "cancel", "attempt_id": "att-tool-1", "confirm": True}, client
    )

    assert envelope["ok"] is True
    assert envelope["state"] == "cancelled"
    assert envelope["write_back"]["emitted"] is True
    data = outbox_events(tmp_path)[0]["data"]
    assert data["state"] == "cancelled"
    assert data["result"]["available"] is False


def test_attempt_review_writes_back_only_when_the_response_is_terminal(tmp_path):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(review_state="succeeded")

    envelope = attempt_op(
        ctx,
        {"op": "review", "attempt_id": "att-tool-1", "confirm": True, "accepted": True},
        client,
    )

    assert envelope["ok"] is True
    assert envelope["write_back"]["emitted"] is True
    assert outbox_events(tmp_path)[0]["data"]["state"] == "succeeded"

    # A review whose response is not terminal writes nothing.
    other = tool_ctx(tmp_path / "second")
    leases.record_lease(
        tmp_path / "second", "att-tool-1", brief(), now="2026-01-01T00:00:00Z"
    )
    waves = tmp_path / "second" / "waves"
    waves.mkdir(parents=True, exist_ok=True)
    (waves / "w1.json").write_text(
        json.dumps({"wave_id": "w1", "accepted": True, "attempts": ["att-tool-1"]}),
        encoding="utf-8",
    )
    quiet = LauncherStub(review_state="reviewed")
    envelope = attempt_op(
        other,
        {"op": "review", "attempt_id": "att-tool-1", "confirm": True, "accepted": True},
        quiet,
    )
    assert envelope["ok"] is True
    assert "write_back" not in envelope
    assert outbox_events(tmp_path / "second") == []
    assert sidecar(tmp_path / "second", "att-tool-1") is None


def test_a_non_terminal_attempt_observation_writes_nothing(tmp_path):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(state="running")

    envelope = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)

    assert envelope["ok"] is True
    assert "write_back" not in envelope
    assert outbox_events(tmp_path) == []
    assert sidecar(tmp_path, "att-tool-1") is None


def test_duplicate_terminal_observations_emit_one_event(tmp_path):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(state="succeeded", result=result_envelope())

    first = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)
    second = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)
    # A different op observing the same terminal state dedupes too.
    cancel_client = LauncherStub(cancel_state="succeeded")
    third = attempt_op(
        ctx, {"op": "cancel", "attempt_id": "att-tool-1", "confirm": True}, cancel_client
    )

    assert first["write_back"]["emitted"] is True
    assert first["write_back"]["created"] is True
    assert second["write_back"]["emitted"] is True
    assert second["write_back"]["created"] is False
    assert third["write_back"]["created"] is False
    assert len(outbox_events(tmp_path)) == 1


def test_an_emission_write_failure_is_reported_and_recovers(tmp_path, monkeypatch):
    ctx = seeded_ctx(tmp_path)
    client = LauncherStub(state="succeeded", result=result_envelope())
    real_record = bee_events.record_landing

    def broken_record(state_dir, event):
        raise bee_events.EventError(
            "event_write_failed", "outbox write failed: OSError", "Check the state directory."
        )

    monkeypatch.setattr(agent_consumer.bee_events, "record_landing", broken_record)

    envelope = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)

    # The completion path survives the failed emission and reports it as its
    # own fact; the op itself does not become a failure.
    assert envelope["ok"] is True
    write_back = envelope["write_back"]
    assert write_back["emitted"] is False
    assert write_back["code"] == "event_write_failed"
    assert outbox_events(tmp_path) == []

    monkeypatch.setattr(agent_consumer.bee_events, "record_landing", real_record)
    retried = attempt_op(ctx, {"op": "get", "attempt_id": "att-tool-1"}, client)

    assert retried["write_back"]["emitted"] is True
    assert retried["write_back"]["created"] is True
    assert len(outbox_events(tmp_path)) == 1


def test_a_completion_without_a_coordinator_lease_is_refused_not_failed(tmp_path):
    ctx = tool_ctx(tmp_path)
    client = LauncherStub(state="succeeded", result=result_envelope())

    envelope = attempt_op(ctx, {"op": "get", "attempt_id": "att-orphan"}, client)

    assert envelope["ok"] is True
    write_back = envelope["write_back"]
    assert write_back["emitted"] is False
    assert write_back["code"] == "lease_facts_missing"
    assert outbox_events(tmp_path) == []


# ---------------------------------------------------------------------------
# RED: a planted prior record must not suppress the landing event
# ---------------------------------------------------------------------------


def planted_completion(attempt_id: str, event_id: str) -> dict:
    """A valid tool-completion record a prior writer could have left behind."""

    return {
        "schema": agent_consumer.TOOL_COMPLETION_SCHEMA,
        "attempt_id": attempt_id,
        "state": "succeeded",
        "branch": "bee/planted",
        "event_id": event_id,
    }


def write_planted(path: Path, record: dict, *, size: int | None = None) -> bytes:
    """Write *record* as JSON, optionally space-padded to an exact size."""

    raw = json.dumps(record).encode("utf-8")
    if size is not None:
        assert len(raw) <= size
        raw = raw + b" " * (size - len(raw))
    path.write_bytes(raw)
    return raw


def planted_outside(tmp_path: Path, name: str) -> Path:
    """A real directory outside the plugin state dir."""

    outside = tmp_path.parent / f"{tmp_path.name}-{name}"
    outside.mkdir()
    return outside


def test_a_symlinked_completions_directory_cannot_suppress_a_completion(tmp_path):
    attempt = "att-symdir"
    dispatch(tmp_path, attempt)
    planted_id = bee_events.EVENT_ID_PREFIX + "d" * 32
    outside = planted_outside(tmp_path, "symdir")
    planted_path = outside / f"{attempt}.json"
    planted = write_planted(planted_path, planted_completion(attempt, planted_id))
    (tmp_path / "tool-completions").symlink_to(outside, target_is_directory=True)

    result = agent_consumer.record_tool_completion(
        tmp_path, attempt, state="succeeded", now=T0
    )

    assert result["emitted"] is True
    assert result["created"] is True
    assert result["event_id"] != planted_id
    events = outbox_events(tmp_path)
    assert len(events) == 1
    assert events[0]["id"] == result["event_id"]
    assert all(event["id"] != planted_id for event in events)
    assert planted_path.read_bytes() == planted


def test_a_hardlinked_completion_record_cannot_suppress_a_completion(tmp_path):
    attempt = "att-hlck"
    dispatch(tmp_path, attempt)
    planted_id = bee_events.EVENT_ID_PREFIX + "e" * 32
    outside = planted_outside(tmp_path, "hlck")
    planted_path = outside / f"{attempt}.json"
    planted = write_planted(planted_path, planted_completion(attempt, planted_id))
    completions = tmp_path / "tool-completions"
    completions.mkdir()
    linked = completions / f"{attempt}.json"
    os.link(planted_path, linked)

    assert os.stat(linked).st_nlink == 2

    result = agent_consumer.record_tool_completion(
        tmp_path, attempt, state="succeeded", now=T0
    )

    assert result["emitted"] is True
    assert result["created"] is True
    assert result["event_id"] != planted_id
    events = outbox_events(tmp_path)
    assert len(events) == 1
    assert events[0]["id"] == result["event_id"]
    assert all(event["id"] != planted_id for event in events)
    assert planted_path.read_bytes() == planted


def test_a_completion_record_over_65536_bytes_is_not_a_prior_record(tmp_path):
    completions = tmp_path / "tool-completions"
    completions.mkdir()

    ok_id = bee_events.EVENT_ID_PREFIX + "a" * 32
    ok_path = completions / "att-bound-ok.json"
    ok_raw = write_planted(
        ok_path, planted_completion("att-bound-ok", ok_id), size=65536
    )
    assert len(ok_raw) == 65536

    at_bound = agent_consumer.record_tool_completion(
        tmp_path, "att-bound-ok", state="succeeded", now=T0
    )

    assert at_bound["emitted"] is True
    assert at_bound["created"] is False
    assert at_bound["event_id"] == ok_id
    assert outbox_events(tmp_path) == []

    dispatch(tmp_path, "att-bound-big")
    big_id = bee_events.EVENT_ID_PREFIX + "b" * 32
    big_path = completions / "att-bound-big.json"
    big_raw = write_planted(
        big_path, planted_completion("att-bound-big", big_id), size=65537
    )
    assert len(big_raw) == 65537

    over_bound = agent_consumer.record_tool_completion(
        tmp_path, "att-bound-big", state="succeeded", now=T0
    )

    assert over_bound["emitted"] is True
    assert over_bound["created"] is True
    assert over_bound["event_id"] != big_id
    events = outbox_events(tmp_path)
    assert len(events) == 1
    assert events[0]["id"] == over_bound["event_id"]
    assert events[0]["data"]["attempt_id"] == "att-bound-big"
    assert all(event["id"] != big_id for event in events)


def test_a_symlinked_completion_record_is_still_ignored(tmp_path):
    attempt = "att-lnkrec"
    dispatch(tmp_path, attempt)
    planted_id = bee_events.EVENT_ID_PREFIX + "f" * 32
    completions = tmp_path / "tool-completions"
    completions.mkdir()
    outside = planted_outside(tmp_path, "lnkrec")
    planted_path = outside / f"{attempt}.json"
    planted = write_planted(planted_path, planted_completion(attempt, planted_id))
    (completions / f"{attempt}.json").symlink_to(planted_path)

    result = agent_consumer.record_tool_completion(
        tmp_path, attempt, state="succeeded", now=T0
    )

    assert result["emitted"] is True
    assert result["created"] is True
    assert result["event_id"] != planted_id
    events = outbox_events(tmp_path)
    assert len(events) == 1
    assert events[0]["id"] == result["event_id"]
    assert all(event["id"] != planted_id for event in events)
    assert planted_path.read_bytes() == planted


# ---------------------------------------------------------------------------
# RED: the completion-record writer never leaves the state directory
# ---------------------------------------------------------------------------


def test_a_symlinked_completions_directory_is_refused_before_any_write(tmp_path):
    attempt = "att-write-symdir"
    dispatch(tmp_path, attempt)
    outside = tmp_path.parent / f"{tmp_path.name}-write-symdir"
    outside.mkdir()
    os.chmod(outside, 0o755)
    (tmp_path / "tool-completions").symlink_to(outside, target_is_directory=True)

    result = complete(tmp_path, attempt)

    assert result["emitted"] is True
    assert result["completion_record"] is None
    assert result["usage_recorded"] is False
    assert list(outside.iterdir()) == []
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert len(outbox_events(tmp_path)) == 1


def test_a_symlinked_completions_directory_conflict_is_refused_without_a_write(tmp_path):
    attempt = "att-write-symdir-conflict"
    dispatch(tmp_path, attempt)
    planted_id = bee_events.EVENT_ID_PREFIX + "2" * 32
    outside = tmp_path.parent / f"{tmp_path.name}-write-symdir-conflict"
    outside.mkdir()
    os.chmod(outside, 0o755)
    planted_path = outside / f"{attempt}.json"
    planted = write_planted(planted_path, planted_completion(attempt, planted_id))
    (tmp_path / "tool-completions").symlink_to(outside, target_is_directory=True)

    result = complete(tmp_path, attempt)

    assert result["emitted"] is True
    assert result["completion_record"] is None
    assert result["usage_recorded"] is False
    assert planted_path.read_bytes() == planted
    assert [path.name for path in outside.iterdir()] == [f"{attempt}.json"]
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert len(outbox_events(tmp_path)) == 1


def test_a_fifo_completion_record_never_blocks_the_write_back(tmp_path):
    attempt = "att-write-fifo"
    dispatch(tmp_path, attempt)
    completions = tmp_path / "tool-completions"
    completions.mkdir()
    fifo = completions / f"{attempt}.json"
    os.mkfifo(fifo)

    holder: dict[str, object] = {}

    def run() -> None:
        try:
            holder["result"] = complete(tmp_path, attempt)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion
            holder["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2.0)

    assert not thread.is_alive()
    assert "error" not in holder
    result = holder["result"]
    assert result["emitted"] is True
    assert result["completion_record"] is None
    assert result["usage_recorded"] is False
    assert len(outbox_events(tmp_path)) == 1
    assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    assert [path.name for path in completions.iterdir()] == [f"{attempt}.json"]


def test_a_plain_completions_directory_records_and_replays_idempotently(tmp_path):
    attempt = "att-write-control"
    dispatch(tmp_path, attempt)

    result = complete(tmp_path, attempt)

    assert result["emitted"] is True
    assert result["created"] is True
    assert result["completion_record"] == f"tool-completions/{attempt}.json"
    path = tmp_path / "tool-completions" / f"{attempt}.json"
    info = os.stat(path)
    assert stat.S_ISREG(info.st_mode)
    assert info.st_nlink == 1
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert len(outbox_events(tmp_path)) == 1

    replay = complete(tmp_path, attempt)

    assert replay["emitted"] is True
    assert replay["created"] is False
    assert replay["event_id"] == result["event_id"]
    assert len(outbox_events(tmp_path)) == 1


def test_an_identical_existing_record_is_not_rewritten(tmp_path):
    attempt = "att-write-identical"
    completions = tmp_path / "tool-completions"
    completions.mkdir()
    record = {
        "schema": agent_consumer.TOOL_COMPLETION_SCHEMA,
        "attempt_id": attempt,
        "state": "succeeded",
        "branch": "bee/tool-write-back",
        "feature_id": "bundlebees",
        "event_id": bee_events.EVENT_ID_PREFIX + "3" * 32,
        "recorded_at": "2027-01-15T08:00:00Z",
    }
    payload = bee_events._canonical(record)
    path = completions / f"{attempt}.json"
    path.write_bytes(payload)

    created = agent_consumer._write_completion_record(tmp_path, attempt, record)

    assert created is False
    assert path.read_bytes() == payload


def test_a_different_existing_record_is_refused_untouched(tmp_path):
    attempt = "att-write-different"
    dispatch(tmp_path, attempt)
    planted_id = bee_events.EVENT_ID_PREFIX + "4" * 32
    completions = tmp_path / "tool-completions"
    completions.mkdir()
    planted_record = planted_completion(attempt, planted_id)
    planted_record["state"] = "failed"
    path = completions / f"{attempt}.json"
    planted = write_planted(path, planted_record)

    result = complete(tmp_path, attempt)

    assert result["emitted"] is True
    assert result["completion_record"] is None
    assert result["usage_recorded"] is False
    assert path.read_bytes() == planted
    assert len(outbox_events(tmp_path)) == 1
