"""RED-then-GREEN: an accepted landing produces one durable completion event.

Run this file alone before and after the producer wiring:

    python -m pytest -q tests/test_completion_event_emit.py

RED (pre-change): no ``events/inbox/coordinator-actions/<event_id>.json`` is
written for an accepted landing, so the first assertion in
``test_an_accepted_landing_publishes_one_completion_event`` fails. GREEN (with
the landing bridge's completion producer): exactly one deterministic inbox
event is written before the source is acknowledged, a replay is a create-only
no-op, and an inbox write failure leaves the source unacknowledged.

The fixtures are canonical ``rizzstack.bee.landed/1`` events correlated with a
real coordinator lease. They contain identifiers and repository-relative refs
only; no credential, endpoint or absolute home path is placed in an event payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events, event_consumer, landing_bridge, leases

SOURCE_REV = "a" * 40
ARTIFACT = "b" * 64
ATTEMPT = "att-emit-1"
JOB = "job-emit-1"
TASK = "task-emit-1"
FILES = ["src/emit.py", "tests/test_emit.py"]
T0 = 1_700_000_000.0


def _write_pinned_schema(root: Path) -> Path:
    schema_dir = root / "schemas"
    schema_dir.mkdir(parents=True)
    data = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema", "feature_id", "job_id", "task_id", "attempt_id",
            "coordinator", "backend", "state", "source_rev", "lease", "result",
        ],
        "properties": {
            "schema": {"const": bee_events.SCHEMA},
            "feature_id": {"type": "string"},
            "job_id": {"type": "string"},
            "task_id": {"type": "string"},
            "attempt_id": {"type": "string", "minLength": 1},
            "coordinator": {
                "type": "object", "additionalProperties": False,
                "required": ["identity"],
                "properties": {"identity": {"type": "string"}, "session": {"type": "string"}},
            },
            "backend": {
                "type": "object", "additionalProperties": False,
                "required": ["harness"],
                "properties": {"harness": {"type": "string"}, "model": {"type": "string"}},
            },
            "state": {"type": "string", "minLength": 1},
            "source_rev": {"type": "string", "minLength": 1},
            "lease": {
                "type": "object", "additionalProperties": False,
                "required": ["sha256", "assigned_files"],
                "properties": {
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "assigned_files": {"type": "array", "items": {"type": "string"}},
                },
            },
            "result": {
                "type": "object", "additionalProperties": False,
                "required": ["available"],
                "properties": {
                    "available": {"type": "boolean"},
                    "artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "reference": {"type": "string"},
                },
            },
        },
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["specversion", "id", "source", "type", "data"],
        "properties": {
            "specversion": {"const": "1.0"},
            "id": {"type": "string", "pattern": r"^urn:rizzstack:bee-landed:[0-9a-f]{32}$"},
            "source": {"type": "string", "minLength": 1},
            "type": {"const": bee_events.EVENT_TYPE},
            "time": {"type": "string"},
            "datacontenttype": {"const": "application/json"},
            "data": data,
        },
    }
    path = schema_dir / "bee-landed.schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _pinned_schema(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))


def _record_lease(state: Path) -> dict:
    if (state / "leases" / f"{ATTEMPT}.json").exists():
        return leases.load_lease(state, ATTEMPT)
    return leases.record_lease(
        state,
        ATTEMPT,
        {
            "source_rev": SOURCE_REV,
            "assigned_files": list(FILES),
            "acceptance": ["the completion emitter test passes"],
        },
    )


def _event(
    state: Path,
    *,
    state_name: str = "landed",
    transition_id: str = "tr-emit-1",
    result: dict | None = None,
) -> dict:
    lease = _record_lease(state)
    return bee_events.build_event(
        {
            "attempt_id": ATTEMPT,
            "state": state_name,
            "transition_id": transition_id,
        },
        feature_id="BEE-EMIT",
        job_id=JOB,
        task_id=TASK,
        coordinator={"identity": "coordinator-1", "session": "run-1"},
        backend={"harness": "hyvv", "model": "deepseek-v4.1-flash"},
        source_rev=SOURCE_REV,
        lease={
            "sha256": lease.get("brief_sha256") or lease.get("sha256"),
            "assigned_files": list(FILES),
        },
        result=result
        or {
            "available": True,
            "artifact_sha256": ARTIFACT,
            "reference": "refs/heads/feat/emit",
        },
    )


def _write_source(source: Path, name: str, event: dict) -> Path:
    source.mkdir(parents=True, exist_ok=True)
    path = source / name
    path.write_bytes(bee_events._canonical(event))
    return path


def _inbox_dir(state: Path) -> Path:
    return event_consumer.inbox_dir(state, event_consumer.DEFAULT_COMPLETION_SUBSCRIBER)


def _json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _action_path(state: Path, action_id: str) -> Path:
    return (
        state / "coordinator" / "completion-actions" / f"{action_id}.json"
    )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _assert_no_private_payload_text(record: dict) -> None:
    for text in _strings(record):
        assert not text.startswith("/")
        assert not text.startswith("~")
        assert "://" not in text


def test_an_accepted_landing_publishes_one_completion_event(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    event = _event(state, state_name="landed")
    _write_source(source, "attempt.json", event)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 1
    assert result["failed"] == 0
    files = _json_files(_inbox_dir(state))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    expected_id = event_consumer.completion_event_id(ATTEMPT, "landed")
    assert files[0].stem == expected_id
    assert record["event_id"] == expected_id
    assert record["attempt_id"] == ATTEMPT
    assert record["state"] == "landed"
    assert record["subscriber"] == event_consumer.DEFAULT_COMPLETION_SUBSCRIBER
    _assert_no_private_payload_text(record)

    consumed = event_consumer.consume_completions(state, now=T0)

    assert consumed is not None
    assert consumed["state"] == "acked"
    assert consumed["result"]["actions"] == [
        "verify:att-emit-1",
        "process-completion:att-emit-1",
    ]
    assert _action_path(state, "verify:att-emit-1").exists()
    assert _action_path(state, "process-completion:att-emit-1").exists()


def test_replay_of_the_same_attempt_and_state_is_a_create_only_no_op(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    first_event = _event(state, state_name="landed", transition_id="tr-emit-1")
    _write_source(source, "first.json", first_event)
    assert landing_bridge.consume_once(state, source)["processed"] == 1
    assert event_consumer.consume_completions(state, now=T0)["state"] == "acked"

    # Same attempt and terminal state, different landing transition: the
    # canonical landing ids differ, but the completion event id must not.
    replay_event = _event(state, state_name="landed", transition_id="tr-emit-2")
    assert replay_event["id"] != first_event["id"]
    _write_source(source, "second.json", replay_event)
    replay = landing_bridge.consume_once(state, source)

    assert replay["failed"] == 0
    files = _json_files(_inbox_dir(state))
    assert len(files) == 1
    assert files[0].stem == event_consumer.completion_event_id(ATTEMPT, "landed")
    assert len(_json_files(state / "coordinator" / "completion-actions")) == 2

    again = event_consumer.consume_completions(state, now=T0 + 1)
    assert again["state"] == "duplicate"
    assert again["duplicate"] is True
    assert len(_json_files(state / "coordinator" / "completion-actions")) == 2


def test_a_non_verify_eligible_terminal_state_still_publishes_once(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    event = _event(
        state,
        state_name="failed",
        transition_id="tr-emit-failed",
        result={"available": False},
    )
    _write_source(source, "failed.json", event)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 1
    assert result["failed"] == 0
    files = _json_files(_inbox_dir(state))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["event_id"] == event_consumer.completion_event_id(ATTEMPT, "failed")
    assert record["state"] == "failed"

    consumed = event_consumer.consume_completions(state, now=T0)

    assert consumed["state"] == "acked"
    assert consumed["result"]["actions"] == ["process-completion:att-emit-1"]
    assert not _action_path(state, "verify:att-emit-1").exists()

    # A replay in the same terminal state does not add an inbox event or action.
    replay_event = _event(
        state,
        state_name="failed",
        transition_id="tr-emit-failed-2",
        result={"available": False},
    )
    _write_source(source, "failed-replay.json", replay_event)
    assert landing_bridge.consume_once(state, source)["failed"] == 0
    assert len(_json_files(_inbox_dir(state))) == 1
    again = event_consumer.consume_completions(state, now=T0 + 1)
    assert again["state"] == "duplicate"
    assert len(_json_files(state / "coordinator" / "completion-actions")) == 1


def test_inbox_write_failure_leaves_the_landing_unacked_and_retry_succeeds(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    event = _event(state, state_name="landed")
    _write_source(source, "attempt.json", event)
    ack_meta = source / landing_bridge.ACK_DIR / ".acks" / "attempt.json.ack.json"

    original_publish = event_consumer.publish_event

    def fail_publish(*args, **kwargs):
        raise OSError("simulated inbox write failure")

    monkeypatch.setattr(landing_bridge.event_consumer, "publish_event", fail_publish)

    failed = landing_bridge.consume_once(state, source)

    assert failed["failed"] == 1
    assert failed["processed"] == 0
    assert (source / "attempt.json").exists()
    assert not ack_meta.exists()
    assert not (source / landing_bridge.ACK_DIR / "attempt.json").exists()
    assert _json_files(_inbox_dir(state)) == []

    monkeypatch.setattr(landing_bridge.event_consumer, "publish_event", original_publish)

    recovered = landing_bridge.consume_once(state, source)

    assert recovered["processed"] == 1
    assert recovered["failed"] == 0
    assert ack_meta.exists()
    assert (source / landing_bridge.ACK_DIR / "attempt.json").exists()
    assert len(_json_files(_inbox_dir(state))) == 1
