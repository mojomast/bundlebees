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


# ---------------------------------------------------------------------------
# HTTP delivery signing, freshness and replay
# ---------------------------------------------------------------------------

DELIVERY_ID = "dlv_" + "d" * 32
DELIVERY_EVENT_ID = events.event_id_for("att_bee_1", "landed", "tr_0001")


def _delivery_body():
    return events._http_body(_event())


def _delivery_headers(body, *, now, delivery_id=DELIVERY_ID, secret=SECRET):
    record = {"delivery_id": delivery_id, "event_id": DELIVERY_EVENT_ID}
    return events._http_headers(record, body, secret, now=now)


def _verified_dir(state: Path):
    return Path(state) / "events" / "verified"


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, _amount=None):
        return b""


def test_signature_binds_delivery_id_timestamp_and_exact_body(tmp_path):
    body = _delivery_body()
    headers = _delivery_headers(body, now=1000)
    assert events.verify_delivery(headers, body, SECRET, now=1000) is True
    # Verification is side-effect free until the caller durably accepts.
    assert not _verified_dir(tmp_path).exists()

    changed_body = dict(_event())
    changed_body["data"]["result"]["reference"] = "refs/heads/other"
    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, events._http_body(changed_body), SECRET, now=1000)
    assert _code(exc) == "delivery_signature_invalid"

    changed_timestamp = dict(headers)
    changed_timestamp[events.TIMESTAMP_HEADER] = "1001"
    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(changed_timestamp, body, SECRET, now=1001)
    assert _code(exc) == "delivery_signature_invalid"

    changed_id = dict(headers)
    changed_id[events.DELIVERY_ID_HEADER] = "dlv_" + "e" * 32
    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(changed_id, body, SECRET, now=1000)
    assert _code(exc) == "delivery_signature_invalid"


def test_verify_requires_a_versioned_signature_over_the_headers():
    body = _delivery_body()
    headers = _delivery_headers(body, now=1000)

    unversioned = dict(headers)
    unversioned[events.SIGNATURE_HEADER] = headers[events.SIGNATURE_HEADER].split("=", 2)[2]
    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(unversioned, body, SECRET, now=1000)
    assert _code(exc) == "delivery_signature_invalid"

    missing_id = dict(headers)
    missing_id.pop(events.DELIVERY_ID_HEADER)
    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(missing_id, body, SECRET, now=1000)
    assert _code(exc) == "delivery_id_missing"


def test_verify_rejects_stale_timestamps_and_honours_tolerance():
    body = _delivery_body()
    headers = _delivery_headers(body, now=1000)

    assert events.verify_delivery(headers, body, SECRET, now=1010, tolerance_s=10) is True
    for now in (989, 1011):
        with pytest.raises(events.EventError) as exc:
            events.verify_delivery(headers, body, SECRET, now=now, tolerance_s=10)
        assert _code(exc) == "delivery_stale"

    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, body, SECRET, now=1000 + events.DEFAULT_TIMESTAMP_TOLERANCE_S + 1)
    assert _code(exc) == "delivery_stale"


def test_exact_replay_of_an_accepted_delivery_is_refused(tmp_path):
    body = _delivery_body()
    headers = _delivery_headers(body, now=1000)

    receipt = events.accept_delivery(tmp_path, headers, body, SECRET, now=1000)
    assert receipt["created"] is True
    assert receipt["duplicate"] is False
    assert receipt["delivery_id"] == DELIVERY_ID
    marker_files = list(_verified_dir(tmp_path).glob("*.json"))
    assert len(marker_files) == 1

    with pytest.raises(events.EventError) as exc:
        events.accept_delivery(tmp_path, headers, body, SECRET, now=1000)
    assert _code(exc) == "delivery_replayed"

    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, body, SECRET, now=1000, state_dir=tmp_path)
    assert _code(exc) == "delivery_replayed"


def test_verify_does_not_persist_a_replay_marker_before_acceptance(tmp_path):
    body = _delivery_body()
    headers = _delivery_headers(body, now=1000)

    assert events.verify_delivery(headers, body, SECRET, now=1000, state_dir=tmp_path) is True
    assert not _verified_dir(tmp_path).exists()

    # A fresh retry is still accepted because no acceptance was recorded.
    retried = _delivery_headers(body, now=1001)
    receipt = events.accept_delivery(tmp_path, retried, body, SECRET, now=1001)
    assert receipt["created"] is True
    assert len(list(_verified_dir(tmp_path).glob("*.json"))) == 1


def test_crash_before_acceptance_leaves_the_delivery_retryable(tmp_path):
    body = _delivery_body()
    effects = []

    def crash_before_accept(_receipt):
        raise RuntimeError("simulated crash before durable acceptance")

    with pytest.raises(RuntimeError):
        events.accept_delivery(
            tmp_path,
            _delivery_headers(body, now=1000),
            body,
            SECRET,
            now=1000,
            on_accept=crash_before_accept,
        )
    assert not _verified_dir(tmp_path).exists()

    retried = _delivery_headers(body, now=1001)
    receipt = events.accept_delivery(
        tmp_path, retried, body, SECRET, now=1001, on_accept=effects.append
    )
    assert receipt["created"] is True
    assert len(effects) == 1
    assert len(list(_verified_dir(tmp_path).glob("*.json"))) == 1


def test_legitimate_retransmission_returns_the_durable_receipt_once(tmp_path):
    body = _delivery_body()
    effects = []
    first = events.accept_delivery(
        tmp_path,
        _delivery_headers(body, now=1000),
        body,
        SECRET,
        now=1000,
        on_accept=effects.append,
    )
    assert first["created"] is True and len(effects) == 1
    marker_path = next(_verified_dir(tmp_path).glob("*.json"))
    marker_bytes = marker_path.read_bytes()

    retransmitted = _delivery_headers(body, now=1001)
    second = events.accept_delivery(
        tmp_path, retransmitted, body, SECRET, now=1001, on_accept=effects.append
    )
    assert second["created"] is False
    assert second["duplicate"] is True
    assert second["delivery_id"] == first["delivery_id"]
    assert second["accepted_at"] == first["accepted_at"]
    assert len(effects) == 1
    assert marker_path.read_bytes() == marker_bytes


def test_durable_receipt_survives_losing_the_in_memory_cache(tmp_path):
    body = _delivery_body()
    first = events.accept_delivery(
        tmp_path, _delivery_headers(body, now=1000), body, SECRET, now=1000
    )
    events._clear_delivery_replay_cache()

    retransmitted = _delivery_headers(body, now=1001)
    second = events.accept_delivery(tmp_path, retransmitted, body, SECRET, now=1001)
    assert second["duplicate"] is True
    assert second["accepted_at"] == first["accepted_at"]


def test_http_retries_refresh_timestamp_and_keep_the_same_delivery_id(monkeypatch):
    body = _delivery_body()
    record = {"delivery_id": DELIVERY_ID, "event_id": DELIVERY_EVENT_ID}
    captured = []
    clock = iter([1000, 1001])
    state = {"attempt": 0}

    def fake_urlopen(request, timeout=None):
        captured.append(dict(request.headers))
        state["attempt"] += 1
        if state["attempt"] == 1:
            raise events.urlerror.URLError("simulated lost response")
        return _Response()

    monkeypatch.setattr(events.time, "time", lambda: next(clock))
    monkeypatch.setattr(events.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(events.urlrequest, "urlopen", fake_urlopen)

    initial = events._http_headers(record, body, SECRET, now=1000)
    events._send_http(
        "https://example.invalid/hook",
        body,
        initial,
        refresh=lambda: events._http_headers(record, body, SECRET),
    )

    assert len(captured) == 2
    delivery_ids = {events._header_value(item, events.DELIVERY_ID_HEADER) for item in captured}
    timestamps = {events._header_value(item, events.TIMESTAMP_HEADER) for item in captured}
    assert delivery_ids == {DELIVERY_ID}
    assert len(timestamps) == 2
    assert events.verify_delivery(captured[0], body, SECRET, now=1000) is True
    assert events.verify_delivery(captured[1], body, SECRET, now=1001) is True


def test_http_retry_after_an_ambiguous_acceptance_gets_a_receipt(tmp_path, monkeypatch):
    body = _delivery_body()
    record = {"delivery_id": DELIVERY_ID, "event_id": DELIVERY_EVENT_ID}
    effects = []
    clock = iter([1000, 1001])
    state = {"attempt": 0}

    def fake_urlopen(request, timeout=None):
        state["attempt"] += 1
        if state["attempt"] == 1:
            events.accept_delivery(
                tmp_path,
                dict(request.headers),
                request.data,
                SECRET,
                now=1000,
                on_accept=effects.append,
            )
            raise events.urlerror.URLError("simulated lost response after acceptance")
        result = events.accept_delivery(
            tmp_path,
            dict(request.headers),
            request.data,
            SECRET,
            now=1001,
            on_accept=effects.append,
        )
        assert result["duplicate"] is True
        return _Response()

    monkeypatch.setattr(events.time, "time", lambda: next(clock))
    monkeypatch.setattr(events.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(events.urlrequest, "urlopen", fake_urlopen)

    initial = events._http_headers(record, body, SECRET, now=1000)
    events._send_http(
        "https://example.invalid/hook",
        body,
        initial,
        refresh=lambda: events._http_headers(record, body, SECRET),
    )

    assert state["attempt"] == 2
    assert len(effects) == 1
    assert len(list(_verified_dir(tmp_path).glob("*.json"))) == 1
