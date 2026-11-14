"""Canonical landing-event tests.

Every test uses a real temporary directory when durability matters.  The event
under test is exactly ``rizzstack.bee-landed/1``: CloudEvents type
``rizzstack.bee.landed.v1`` with the canonical ``data`` object and no legacy
fields.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events as events

LEASE = "a" * 64
ARTIFACT = "b" * 64
SOURCE_REV = "c" * 40
SECRET = "tok" + "en: " + "fixture" + "-value-" + "9" * 8


def _attempt(**over):
    value = {"attempt_id": "att_bee_1", "state": "landed", "transition_id": "tr_0001"}
    value.update(over)
    return value


def _event(**over):
    kwargs = {
        "feature_id": "BEE-1",
        "job_id": "job_1",
        "task_id": "task_1",
        "coordinator": {"identity": "coordinator-1", "session": "run_1"},
        "backend": {"harness": "hermes", "model": "deepseek-v4.1-flash"},
        "source_rev": SOURCE_REV,
        "lease": {"sha256": LEASE, "assigned_files": ["src/a.py", "tests/test_a.py"]},
        "result": {
            "available": True,
            "artifact_sha256": ARTIFACT,
            "reference": "refs/heads/feat/bee-events",
        },
        "correlation_id": "corr_1",
        "causation_id": "cause_1",
    }
    kwargs.update(over)
    return events.build_event(_attempt(), **kwargs)


def _outbox(state: Path):
    return Path(state) / "events" / "outbox"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _code(excinfo):
    return getattr(excinfo.value, "code", None)


def _write_pinned_schema(root: Path) -> Path:
    schema_dir = root / "schemas"
    schema_dir.mkdir(parents=True)
    data_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(
            [
                "schema",
                "feature_id",
                "job_id",
                "task_id",
                "attempt_id",
                "coordinator",
                "backend",
                "state",
                "source_rev",
                "lease",
                "result",
            ]
        ),
        "properties": {
            "schema": {"const": events.SCHEMA},
            "feature_id": {"type": "string"},
            "job_id": {"type": "string"},
            "task_id": {"type": "string"},
            "attempt_id": {"type": "string", "minLength": 1},
            "coordinator": {
                "type": "object",
                "additionalProperties": False,
                "required": ["identity"],
                "properties": {
                    "identity": {"type": "string", "minLength": 1},
                    "session": {"type": "string", "minLength": 1},
                },
            },
            "backend": {
                "type": "object",
                "additionalProperties": False,
                "required": ["harness"],
                "properties": {
                    "harness": {"type": "string", "minLength": 1},
                    "model": {"type": "string", "minLength": 1},
                },
            },
            "state": {"type": "string", "minLength": 1},
            "source_rev": {"type": "string", "minLength": 1},
            "lease": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sha256", "assigned_files"],
                "properties": {
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "assigned_files": {"type": "array", "items": {"type": "string"}},
                },
            },
            "result": {
                "type": "object",
                "additionalProperties": False,
                "required": ["available"],
                "properties": {
                    "available": {"type": "boolean"},
                    "artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "reference": {"type": "string", "minLength": 1},
                },
            },
            "correlation_id": {"type": "string", "minLength": 1},
            "causation_id": {"type": "string", "minLength": 1},
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
            "type": {"const": events.EVENT_TYPE},
            "time": {"type": "string"},
            "datacontenttype": {"const": "application/json"},
            "data": data_schema,
        },
    }
    path = schema_dir / "bee-landed.schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Identity and canonical construction
# ---------------------------------------------------------------------------


def test_event_id_is_the_canonical_urn_and_is_transition_sensitive():
    seed = "att_1:landed:tr_1".encode("utf-8")
    expected = "urn:rizzstack:bee-landed:" + hashlib.sha256(seed).hexdigest()[:32]
    assert events.event_id_for("att_1", "landed", "tr_1") == expected
    assert events.event_id_for("att_1", "landed", "tr_1") == expected
    assert events.event_id_for("att_1", "failed", "tr_1") != expected
    assert events.event_id_for("att_1", "landed", "tr_2") != expected


def test_build_event_emits_only_the_canonical_shape():
    event = _event()
    assert set(event) == {
        "specversion",
        "id",
        "source",
        "type",
        "time",
        "datacontenttype",
        "data",
    }
    assert event["specversion"] == "1.0"
    assert event["type"] == "rizzstack.bee.landed.v1"
    assert event["source"] == "rizzstack://bundlebees"
    assert event["datacontenttype"] == "application/json"
    assert event["id"] == events.event_id_for("att_bee_1", "landed", "tr_0001")

    data = event["data"]
    assert set(data) == {
        "schema",
        "feature_id",
        "job_id",
        "task_id",
        "attempt_id",
        "coordinator",
        "backend",
        "state",
        "source_rev",
        "lease",
        "result",
        "correlation_id",
        "causation_id",
    }
    assert data["schema"] == "rizzstack.bee-landed/1"
    assert data["attempt_id"] == "att_bee_1"
    assert data["state"] == "landed"
    assert data["source_rev"] == SOURCE_REV
    assert data["coordinator"] == {"identity": "coordinator-1", "session": "run_1"}
    assert data["backend"] == {"harness": "hermes", "model": "deepseek-v4.1-flash"}
    assert data["lease"] == {"sha256": LEASE, "assigned_files": ["src/a.py", "tests/test_a.py"]}
    assert data["result"] == {
        "available": True,
        "artifact_sha256": ARTIFACT,
        "reference": "refs/heads/feat/bee-events",
    }
    assert "transition_id" not in data
    assert "lease_sha256" not in data
    assert "artifact_sha256" not in data
    assert "reference" not in data


def test_optional_coordinator_backend_and_result_fields_are_omitted():
    event = _event(
        coordinator={"identity": "coordinator-1"},
        backend={"harness": "hermes"},
        result={"available": False},
        correlation_id=None,
        causation_id=None,
    )
    data = event["data"]
    assert data["coordinator"] == {"identity": "coordinator-1"}
    assert data["backend"] == {"harness": "hermes"}
    assert data["result"] == {"available": False}
    assert "correlation_id" not in data
    assert "causation_id" not in data


def test_legacy_aliases_fold_into_the_canonical_objects():
    event = events.build_event(
        _attempt(),
        feature_id="BEE-2",
        job_id="job_2",
        task_id="task_2",
        coordinator="coordinator-2",
        backend="launcher",
        source_rev=SOURCE_REV,
        lease_sha256=LEASE,
        assigned_files=["src/b.py"],
        artifact_sha256=ARTIFACT,
        reference="refs/heads/feat/b",
    )
    data = event["data"]
    assert data["coordinator"] == {"identity": "coordinator-2"}
    assert data["backend"] == {"harness": "launcher"}
    assert data["lease"] == {"sha256": LEASE, "assigned_files": ["src/b.py"]}
    assert data["result"] == {
        "available": True,
        "artifact_sha256": ARTIFACT,
        "reference": "refs/heads/feat/b",
    }


def test_build_event_refuses_incomplete_or_unsafe_canonical_facts():
    with pytest.raises(events.EventError) as exc:
        events.build_event(_attempt(), backend={"harness": "hermes"}, source_rev=SOURCE_REV,
                           lease={"sha256": LEASE, "assigned_files": []},
                           result={"available": False})
    assert _code(exc) == "event_invalid"

    with pytest.raises(events.EventError) as exc:
        _event(coordinator={"session": "run_1"})
    assert _code(exc) == "event_invalid"

    with pytest.raises(events.EventError) as exc:
        _event(backend={"model": "deepseek"})
    assert _code(exc) == "event_invalid"

    with pytest.raises(events.EventError) as exc:
        _event(lease={"sha256": "not-a-digest", "assigned_files": []})
    assert _code(exc) == "event_invalid"

    for path in ("/etc/passwd", "../outside.py", "C:\\secret.py", "src/../../escape.py"):
        with pytest.raises(events.EventError) as exc:
            _event(lease={"sha256": LEASE, "assigned_files": [path]})
        assert _code(exc) == "event_invalid"

    with pytest.raises(events.EventError) as exc:
        _event(result={"available": True, "reference": SECRET})
    assert _code(exc) == "event_secret"


# ---------------------------------------------------------------------------
# Pinned-schema validation and tampering
# ---------------------------------------------------------------------------


def test_validate_event_fails_closed_without_workflow_src(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    with pytest.raises(events.EventError) as exc:
        events.validate_event(_event())
    assert _code(exc) == "workflow_schema_unavailable"


def test_validate_event_fails_closed_when_the_pinned_schema_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path / "empty-workflow"))
    with pytest.raises(events.EventError) as exc:
        events.validate_event(_event())
    assert _code(exc) == "workflow_schema_unavailable"


def test_validate_event_accepts_canonical_bytes_with_the_pinned_schema(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))
    event = _event()
    assert events.validate_event(event) is True
    # Validation accepts raw canonical bytes too.
    assert events.validate_event(json.dumps(event, sort_keys=True).encode("utf-8")) is True


def test_validate_event_refuses_tampered_envelopes_and_data(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))

    cases = []
    wrong_type = _event()
    wrong_type["type"] = "rizzstack.bee.landed.v2"
    cases.append((wrong_type, "event_invalid"))

    wrong_schema = _event()
    wrong_schema["data"]["schema"] = "rizzstack.bee.landed/1"
    cases.append((wrong_schema, "event_invalid"))

    bad_id = _event()
    bad_id["id"] = "evt_" + "0" * 32
    cases.append((bad_id, "event_invalid"))

    extra_data = _event()
    extra_data["data"]["transition_id"] = "tr_0001"
    cases.append((extra_data, "event_invalid"))

    extra_envelope = _event()
    extra_envelope["legacy_lease_sha256"] = LEASE
    cases.append((extra_envelope, "event_invalid"))

    extra_result = _event()
    extra_result["data"]["result"]["note"] = "extra"
    cases.append((extra_result, "event_invalid"))

    missing_attempt = _event()
    missing_attempt["data"].pop("attempt_id")
    cases.append((missing_attempt, "event_invalid"))

    for event, code in cases:
        with pytest.raises(events.EventError) as exc:
            events.validate_event(event)
        assert _code(exc) == code, event


def test_validate_event_lets_the_pinned_schema_reject_a_null_source_rev(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))
    event = _event(source_rev=None)
    assert event["data"]["source_rev"] is None
    with pytest.raises(events.EventError) as exc:
        events.validate_event(event)
    assert _code(exc) == "event_schema_invalid"


# ---------------------------------------------------------------------------
# Durable outbox
# ---------------------------------------------------------------------------


def test_record_landing_is_create_only_fsynced_and_idempotent(tmp_path, monkeypatch):
    state = tmp_path / "state"
    fsyncs = []
    real_fsync = os.fsync

    def spy(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(events.os, "fsync", spy)
    event = _event()
    first = events.record_landing(state, event)
    path = _outbox(state) / f"{event['id']}.json"
    assert first["created"] is True
    assert _read(path)["id"] == event["id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert len(fsyncs) >= 2

    before = path.read_bytes()
    second = events.record_landing(state, event)
    assert second["created"] is False
    assert path.read_bytes() == before
    assert len(list(_outbox(state).glob("*.json"))) == 1


def test_record_landing_refuses_conflicting_bytes_under_the_same_id(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    changed = json.loads(json.dumps(event))
    changed["data"]["result"]["reference"] = "refs/heads/other"
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, changed)
    assert _code(exc) == "event_conflict"
    stored = _read(_outbox(state) / f"{event['id']}.json")
    assert stored["data"]["result"]["reference"] == "refs/heads/feat/bee-events"


def test_a_failed_outbox_publish_leaves_no_partial_file(tmp_path, monkeypatch):
    state = tmp_path / "state"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(events.os, "link", boom)
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, _event())
    assert _code(exc) == "event_write_failed"
    outbox = _outbox(state)
    if outbox.exists():
        assert list(outbox.iterdir()) == []
