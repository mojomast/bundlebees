"""End-to-end landing-bridge tests with real temporary state and source dirs.

The source files are canonical hyvv-style bytes: one ``rizzstack.bee.landed/1``
CloudEvents object per JSON file.  The coordinator lease is written by the real
:mod:`bundlebees_plugin.leases` code, so correlation, HMAC verification and the
outbox/delivery durability checks run for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events, landing_bridge, leases

SOURCE_REV = "a" * 40
OTHER_REV = "d" * 40
ARTIFACT = "b" * 64
ATTEMPT = "att-bridge-1"
JOB = "job-bridge-1"
TASK = "task-bridge-1"
FILES = ["src/a.py", "tests/test_a.py"]


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
        "$schema": "https://json-schema.org/draft/2020-12/schema",
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


def _record_lease(state: Path, *, attempt: str = ATTEMPT, source_rev: str = SOURCE_REV,
                  files: list[str] | None = None) -> dict:
    return leases.record_lease(
        state,
        attempt,
        {
            "source_rev": source_rev,
            "assigned_files": list(files if files is not None else FILES),
            "acceptance": ["the landing bridge test passes"],
        },
    )


def _event(*, lease_record: dict | None = None, attempt: str = ATTEMPT,
           source_rev: str = SOURCE_REV, files: list[str] | None = None,
           reference: str = "refs/heads/feat/bridge", **over) -> dict:
    if lease_record is None:
        lease_record = {"brief_sha256": "e" * 64}
    kwargs = {
        "feature_id": "BEE-BRIDGE",
        "job_id": JOB,
        "task_id": TASK,
        "coordinator": {"identity": "coordinator-1", "session": "run-1"},
        "backend": {"harness": "hyvv", "model": "deepseek-v4.1-flash"},
        "source_rev": source_rev,
        "lease": {
            "sha256": lease_record.get("brief_sha256") or lease_record.get("sha256"),
            "assigned_files": list(files if files is not None else lease_record.get("assigned_files", FILES)),
        },
        "result": {
            "available": True,
            "artifact_sha256": ARTIFACT,
            "reference": reference,
        },
    }
    kwargs.update(over)
    return bee_events.build_event(
        {"attempt_id": attempt, "state": "landed", "transition_id": "tr-bridge-1"},
        **kwargs,
    )


def _write_source(source: Path, name: str, event: dict) -> Path:
    source.mkdir(parents=True, exist_ok=True)
    path = source / name
    path.write_bytes(bee_events._canonical(event))
    return path


def _subscribe(state: Path, identity: str = "coordinator") -> None:
    bee_events.subscribe(state, {"identity": identity, "file": "inbox.jsonl"})


def _outbox(state: Path) -> Path:
    return state / "events" / "outbox"


def _inbox(state: Path, identity: str = "coordinator") -> Path:
    return state / "events" / "inbox" / identity


def _json_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if path.is_file())


# ---------------------------------------------------------------------------
# Happy path and durable acknowledgement
# ---------------------------------------------------------------------------


def test_bridge_records_and_delivers_then_moves_the_source(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    original = _write_source(source, "attempt.json", event)
    _subscribe(state)
    original_bytes = original.read_bytes()

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 1
    assert result["failed"] == 0
    assert result["pending"] == 0
    assert len(_json_files(_outbox(state))) == 1
    assert _json_files(_outbox(state))[0].name == f"{event['id']}.json"
    assert len(_json_files(_inbox(state))) == 1
    assert not original.exists()
    assert (source / landing_bridge.ACK_DIR / "attempt.json").read_bytes() == original_bytes
    ack = json.loads(
        (source / landing_bridge.ACK_DIR / ".acks" / "attempt.json.ack.json").read_text()
    )
    assert ack["event_ids"] == [event["id"]]
    assert ack["delivery"]["delivered"] == 1

    again = landing_bridge.consume_once(state, source)
    assert again["processed"] == 0
    assert again["failed"] == 0


def test_bridge_verifies_the_job_attempt_when_a_job_record_is_present(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    jobs = state / "jobs"
    jobs.mkdir(parents=True)
    (jobs / f"{JOB}.json").write_text(
        json.dumps(
            {
                "job_id": JOB,
                "goal": {"base_rev": SOURCE_REV},
                "tasks": [{"task_id": TASK, "attempts": [ATTEMPT], "lease": FILES}],
            }
        ),
        encoding="utf-8",
    )
    event = _event(lease_record={"sha256": "f" * 64})
    _write_source(source, "job-attempt.json", event)
    _subscribe(state)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 1
    assert result["failed"] == 0
    assert result["sources"][0]["identities"][0]["records"] == ["job"]


# ---------------------------------------------------------------------------
# Fail-closed correlation
# ---------------------------------------------------------------------------


def test_bridge_refuses_a_source_rev_mismatch_and_records_nothing(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state, source_rev=OTHER_REV)
    event = _event(lease_record=lease, source_rev=SOURCE_REV)
    _write_source(source, "mismatch.json", event)
    _subscribe(state)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "source_rev_mismatch"
    assert not _outbox(state).exists() or _json_files(_outbox(state)) == []
    assert (source / "mismatch.json").exists()


def test_bridge_refuses_a_lease_digest_or_file_mismatch(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)

    bad_digest = _event(lease_record=lease)
    bad_digest["data"]["lease"]["sha256"] = "0" * 64
    _write_source(source, "digest.json", bad_digest)

    bad_files = _event(lease_record=lease)
    bad_files["data"]["lease"]["assigned_files"] = ["src/other.py"]
    _write_source(source, "files.json", bad_files)

    result = landing_bridge.consume_once(state, source)

    assert result["failed"] == 2
    assert {item["error"] for item in result["failures"]} == {"lease_mismatch"}
    assert not _outbox(state).exists() or _json_files(_outbox(state)) == []
    assert (source / "digest.json").exists()
    assert (source / "files.json").exists()


def test_bridge_refuses_an_absent_coordinator_identity(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    event = _event(lease_record={"brief_sha256": "e" * 64})
    _write_source(source, "absent.json", event)

    result = landing_bridge.consume_once(state, source)

    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "identity_missing"
    assert not _outbox(state).exists() or _json_files(_outbox(state)) == []
    assert (source / "absent.json").exists()


def test_bridge_refuses_an_event_without_coordinator_identity(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    event["data"]["coordinator"] = {}
    _write_source(source, "no-coordinator.json", event)

    result = landing_bridge.consume_once(state, source)

    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "event_invalid"
    assert (source / "no-coordinator.json").exists()


# ---------------------------------------------------------------------------
# Duplicate and conflicting bytes
# ---------------------------------------------------------------------------


def test_duplicate_source_bytes_are_a_no_op(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    _write_source(source, "first.json", event)
    _subscribe(state)

    assert landing_bridge.consume_once(state, source)["processed"] == 1
    outbox_before = (_outbox(state) / f"{event['id']}.json").read_bytes()
    inbox_before = (_inbox(state) / f"{event['id']}.json").read_bytes()

    # Same bytes under the same name after acknowledgement.
    _write_source(source, "first.json", event)
    duplicate = landing_bridge.consume_once(state, source)
    assert duplicate["duplicates"] == 1
    assert (_outbox(state) / f"{event['id']}.json").read_bytes() == outbox_before
    assert (_inbox(state) / f"{event['id']}.json").read_bytes() == inbox_before

    # Same event under another name: reached through the durable outbox again,
    # but it never creates a second outbox or delivery record.
    _write_source(source, "second.json", event)
    other_name = landing_bridge.consume_once(state, source)
    assert other_name["failed"] == 0
    assert len(_json_files(_outbox(state))) == 1
    assert len(_json_files(_inbox(state))) == 1


def test_conflicting_bytes_for_the_same_event_are_refused(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    _write_source(source, "original.json", event)
    _subscribe(state)
    assert landing_bridge.consume_once(state, source)["processed"] == 1

    changed = _event(lease_record=lease, reference="refs/heads/feat/other")
    _write_source(source, "changed.json", changed)
    result = landing_bridge.consume_once(state, source)

    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "event_conflict"
    assert (_outbox(state) / f"{event['id']}.json").exists()
    assert json.loads((_outbox(state) / f"{event['id']}.json").read_text())["data"]["result"]["reference"] == "refs/heads/feat/bridge"
    assert (source / "changed.json").exists()


def test_reusing_an_acknowledged_source_name_with_new_bytes_is_refused(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    _write_source(source, "same-name.json", event)
    _subscribe(state)
    assert landing_bridge.consume_once(state, source)["processed"] == 1

    changed = _event(lease_record=lease, reference="refs/heads/feat/other")
    _write_source(source, "same-name.json", changed)
    result = landing_bridge.consume_once(state, source)

    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "source_conflict"
    assert (source / "same-name.json").exists()
    assert (source / landing_bridge.ACK_DIR / "same-name.json").exists()


# ---------------------------------------------------------------------------
# Crash/restart replay
# ---------------------------------------------------------------------------


def test_crash_before_record_replays_without_loss(tmp_path, monkeypatch):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    _write_source(source, "crash-before.json", event)
    _subscribe(state)

    real_record = bee_events.record_landing
    calls = {"n": 0}

    def crash_once(path, value):
        if calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated crash before the outbox write")
        return real_record(path, value)

    monkeypatch.setattr(bee_events, "record_landing", crash_once)
    failed = landing_bridge.consume_once(state, source)
    assert failed["failed"] == 1
    assert (source / "crash-before.json").exists()
    assert list(_outbox(state).glob("*.json")) == []

    monkeypatch.setattr(bee_events, "record_landing", real_record)
    recovered = landing_bridge.consume_once(state, source)
    assert recovered["processed"] == 1
    assert len(_json_files(_outbox(state))) == 1
    assert len(_json_files(_inbox(state))) == 1
    assert not (source / "crash-before.json").exists()


def test_crash_after_delivery_before_ack_does_not_redeliver(tmp_path, monkeypatch):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    _write_source(source, "crash-after-delivery.json", event)
    _subscribe(state)

    real_write_ack = landing_bridge._write_ack
    calls = {"n": 0}

    def crash_once(path, record):
        if calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated crash before the ack")
        return real_write_ack(path, record)

    monkeypatch.setattr(landing_bridge, "_write_ack", crash_once)
    failed = landing_bridge.consume_once(state, source)
    assert failed["failed"] == 1
    assert (source / "crash-after-delivery.json").exists()
    assert len(_json_files(_outbox(state))) == 1
    assert len(_json_files(_inbox(state))) == 1
    inbox_before = (_inbox(state) / f"{event['id']}.json").read_bytes()

    monkeypatch.setattr(landing_bridge, "_write_ack", real_write_ack)
    recovered = landing_bridge.consume_once(state, source)
    assert recovered["processed"] == 1
    assert len(_json_files(_outbox(state))) == 1
    assert len(_json_files(_inbox(state))) == 1
    assert (_inbox(state) / f"{event['id']}.json").read_bytes() == inbox_before
    assert not (source / "crash-after-delivery.json").exists()


def test_crash_after_ack_before_move_finishes_on_restart(tmp_path, monkeypatch):
    state = tmp_path / "state"
    source = tmp_path / "hyvv-outbox"
    lease = _record_lease(state)
    event = _event(lease_record=lease)
    _write_source(source, "crash-before-move.json", event)
    _subscribe(state)

    real_move = landing_bridge._move_source
    calls = {"n": 0}

    def crash_once(path, raw):
        if calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated crash before the source move")
        return real_move(path, raw)

    monkeypatch.setattr(landing_bridge, "_move_source", crash_once)
    failed = landing_bridge.consume_once(state, source)
    assert failed["failed"] == 1
    assert (source / "crash-before-move.json").exists()
    assert (source / landing_bridge.ACK_DIR / ".acks" / "crash-before-move.json.ack.json").exists()
    assert len(_json_files(_inbox(state))) == 1

    monkeypatch.setattr(landing_bridge, "_move_source", real_move)
    recovered = landing_bridge.consume_once(state, source)
    assert recovered["duplicates"] == 1
    assert not (source / "crash-before-move.json").exists()
    assert len(_json_files(_outbox(state))) == 1
    assert len(_json_files(_inbox(state))) == 1
