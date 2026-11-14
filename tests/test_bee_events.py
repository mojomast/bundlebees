"""Acceptance tests for bundlebees_plugin.bee_events (durable landing outbox).

Every test uses a real temporary state directory.  No broker, database, server
or network is involved; delivery is a crash-safe file copy with deterministic
event identities so a consumer can deduplicate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events as events


LEASE = "a" * 64
ARTIFACT = "b" * 64
SECRET = "tok" + "en: " + "fixture" + "-value-" + "9" * 8


def _attempt(**over):
    value = {"attempt_id": "att_bee_1", "state": "accepted", "transition_id": "tr_0001"}
    value.update(over)
    return value


def _event(**over):
    return events.build_event(
        _attempt(),
        feature_id="BEE-1",
        job_id="job_1",
        task_id="task_1",
        coordinator="coordinator-1",
        lease_sha256=LEASE,
        artifact_sha256=ARTIFACT,
        reference="refs/heads/feat/bee-events",
        backend="hermes",
        **over,
    )


def _outbox(state: Path):
    return Path(state) / "events" / "outbox"


def _inbox(state: Path, identity: str = "ops"):
    return Path(state) / "events" / "inbox" / identity


def _acks(state: Path, identity: str = "ops"):
    return Path(state) / "events" / "acks" / identity


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _code(excinfo):
    return getattr(excinfo.value, "code", None)


@pytest.fixture(autouse=True)
def _clear_replay_cache():
    events._clear_delivery_replay_cache()
    yield
    events._clear_delivery_replay_cache()


def _signed_headers(secret: str, body: bytes, *, now=None, delivery_id=None):
    moment = time.time() if now is None else now
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Rizzstack-Timestamp": str(int(moment)),
        "X-Rizzstack-Signature": f"sha256={digest}",
    }
    if delivery_id is not None:
        headers["X-Rizzstack-Delivery-Id"] = delivery_id
    return headers


def _header(headers, name):
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class _CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append({"headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        return


@contextmanager
def _http_capture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _write_pinned_schema(root: Path, *, required_backend: bool = True) -> Path:
    schema_dir = root / "schemas"
    schema_dir.mkdir(parents=True)
    data_required = ["schema", "attempt_id", "state", "transition_id"]
    if required_backend:
        data_required.append("backend")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": True,
        "required": ["specversion", "id", "source", "type", "data"],
        "properties": {
            "specversion": {"const": "1.0"},
            "id": {"type": "string", "pattern": r"^evt_[0-9a-f]{32}$"},
            "source": {"type": "string", "minLength": 1},
            "type": {"const": events.SCHEMA},
            "subject": {"type": "string"},
            "time": {"type": "string"},
            "datacontenttype": {"const": "application/json"},
            "data": {
                "type": "object",
                "required": data_required,
                "properties": {
                    "schema": {"const": events.SCHEMA},
                    "event_id": {"type": "string"},
                    "attempt_id": {"type": "string"},
                    "state": {"type": "string"},
                    "transition_id": {"type": "string"},
                    "feature_id": {"type": ["string", "null"]},
                    "job_id": {"type": ["string", "null"]},
                    "task_id": {"type": ["string", "null"]},
                    "coordinator": {"type": ["string", "null"]},
                    "lease_sha256": {"type": ["string", "null"]},
                    "artifact_sha256": {"type": ["string", "null"]},
                    "reference": {},
                    "backend": {"type": ["string", "null"]},
                },
            },
        },
    }
    path = schema_dir / "bee-landed.schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Identity and CloudEvents construction
# ---------------------------------------------------------------------------


def test_event_id_is_deterministic_and_transition_sensitive():
    first = events.event_id_for("att_1", "accepted", "tr_1")
    assert first == events.event_id_for("att_1", "accepted", "tr_1")
    assert first.startswith("evt_")
    assert first != events.event_id_for("att_1", "failed", "tr_1")
    assert first != events.event_id_for("att_1", "accepted", "tr_2")
    assert "/" not in first and ".." not in first


def test_build_event_is_cloud_events_1_and_carries_the_landing_facts():
    event = _event()
    assert event["specversion"] == "1.0"
    assert event["type"] == events.SCHEMA
    assert event["id"] == events.event_id_for("att_bee_1", "accepted", "tr_0001")
    assert event["subject"] == "attempt/att_bee_1"
    assert event["time"].endswith("Z")
    assert event["datacontenttype"] == "application/json"
    data = event["data"]
    assert data["schema"] == events.SCHEMA
    assert data["attempt_id"] == "att_bee_1"
    assert data["state"] == "accepted"
    assert data["transition_id"] == "tr_0001"
    assert data["feature_id"] == "BEE-1"
    assert data["job_id"] == "job_1"
    assert data["task_id"] == "task_1"
    assert data["lease_sha256"] == LEASE
    assert data["artifact_sha256"] == ARTIFACT
    assert data["reference"] == "refs/heads/feat/bee-events"
    assert data["backend"] == "hermes"


def test_build_event_refuses_credentials():
    with pytest.raises(events.EventError) as exc:
        events.build_event(_attempt(), reference=SECRET)
    assert _code(exc) == "event_secret"
    assert "fixture-value" not in str(exc.value)


def test_build_event_refuses_private_and_absolute_paths():
    for reference in (
        "/home/alice/private/repo",
        "~/work/secret",
        "C:\\Users\\alice\\secret",
        "../outside/repo",
        "/tmp/state/outbox.json",
    ):
        with pytest.raises(events.EventError) as exc:
            events.build_event(_attempt(), reference=reference)
        assert _code(exc) == "event_private", reference


def test_build_event_refuses_an_oversized_reference():
    with pytest.raises(events.EventError) as exc:
        events.build_event(_attempt(), reference="x" * (events.MAX_EVENT_BYTES + 1))
    assert _code(exc) == "event_oversized"


def test_build_event_refuses_an_invalid_digest():
    with pytest.raises(events.EventError) as exc:
        events.build_event(_attempt(), lease_sha256="not-a-digest")
    assert _code(exc) == "event_invalid"


def test_build_event_accepts_object_attempts_and_explicit_ids():
    class Attempt:
        attempt_id = "att_object"
        state = "verified"
        transition_id = "tr_object"

    event = events.build_event(Attempt(), backend="hermes")
    assert event["data"]["attempt_id"] == "att_object"
    assert event["data"]["state"] == "verified"

    explicit = events.build_event(
        attempt_id="att_explicit", state="accepted", transition_id="tr_explicit"
    )
    assert explicit["data"]["attempt_id"] == "att_explicit"


def test_dedupe_by_event_id_keeps_the_first_copy():
    event = _event()
    assert events.dedupe_by_event_id([event, event, event["id"]]) == [event]


# ---------------------------------------------------------------------------
# Pinned-schema validation
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


def test_validate_event_accepts_a_valid_event_with_the_pinned_schema(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))
    assert events.validate_event(_event()) is True


def test_validate_event_refuses_a_pinned_schema_violation(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path, required_backend=True)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))
    event = _event()
    event["data"].pop("backend")
    with pytest.raises(events.EventError) as exc:
        events.validate_event(event)
    assert _code(exc) == "event_schema_invalid"


def test_validate_event_uses_the_stdlib_schema_fallback(tmp_path, monkeypatch):
    _write_pinned_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))
    monkeypatch.setitem(sys.modules, "jsonschema", None)
    assert events.validate_event(_event()) is True
    event = _event()
    event["data"]["attempt_id"] = ""
    with pytest.raises(events.EventError) as exc:
        events.validate_event(event)
    assert _code(exc) in {"event_invalid", "event_schema_invalid"}


def test_validate_event_refuses_an_id_that_does_not_match_the_facts():
    event = _event()
    event["id"] = events.event_id_for("att_bee_1", "failed", "tr_0001")
    with pytest.raises(events.EventError) as exc:
        events.validate_event(event)
    assert _code(exc) == "event_id_mismatch"


# ---------------------------------------------------------------------------
# Outbox: create-only, fsynced, idempotent, conflict-safe
# ---------------------------------------------------------------------------


def test_record_landing_is_create_only_fsynced_and_private(tmp_path, monkeypatch):
    state = tmp_path / "state"
    fsyncs = []
    real_fsync = os.fsync

    def spy(fd):
        fsyncs.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(events.os, "fsync", spy)
    event = _event()
    stored = events.record_landing(state, event)

    path = _outbox(state) / f"{event['id']}.json"
    assert path.is_file()
    assert _read(path)["id"] == event["id"]
    assert stored["id"] == event["id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert len(fsyncs) >= 2, "both the file and its directory must be fsynced"


def test_record_landing_is_idempotent_for_identical_bytes(tmp_path):
    state = tmp_path / "state"
    event = _event()
    first = events.record_landing(state, event)
    before = (_outbox(state) / f"{event['id']}.json").read_bytes()
    second = events.record_landing(state, event)
    assert first["created"] is True
    assert second["created"] is False
    assert (_outbox(state) / f"{event['id']}.json").read_bytes() == before
    assert len(list(_outbox(state).glob("*.json"))) == 1


def test_record_landing_refuses_a_conflicting_duplicate(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    changed = json.loads(json.dumps(event))
    changed["data"]["reference"] = "refs/heads/other"
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, changed)
    assert _code(exc) == "event_conflict"
    assert _read(_outbox(state) / f"{event['id']}.json")["data"]["reference"] == "refs/heads/feat/bee-events"


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


def test_record_landing_refuses_an_oversized_or_secret_event(tmp_path):
    state = tmp_path / "state"
    oversized = _event()
    oversized["data"]["reference"] = "x" * (events.MAX_EVENT_BYTES + 1)
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, oversized)
    assert _code(exc) == "event_oversized"

    secret = _event()
    secret["data"]["reference"] = SECRET
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, secret)
    assert _code(exc) == "event_secret"

    extra = _event()
    extra["note"] = SECRET
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, extra)
    assert _code(exc) == "event_secret"

    bad_source = _event()
    bad_source["source"] = "https://user:pass@example.invalid/x"
    with pytest.raises(events.EventError) as exc:
        events.record_landing(state, bad_source)
    assert _code(exc) == "event_private"
    assert not _outbox(state).exists() or list(_outbox(state).iterdir()) == []


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def test_subscribe_is_durable_idempotent_and_unsubscribe_removes_it(tmp_path):
    state = tmp_path / "state"
    subscriber = {"identity": "ops", "file": "inbox.jsonl", "session": "run_1"}
    first = events.subscribe(state, subscriber)
    before = (state / "events" / "subscribers" / "ops.json").read_bytes()
    second = events.subscribe(state, subscriber)
    assert first["created"] is True
    assert second["created"] is False
    assert (state / "events" / "subscribers" / "ops.json").read_bytes() == before
    assert first["file"] == "inbox.jsonl"
    assert first["session"] == "run_1"

    assert events.unsubscribe(state, "ops") is True
    assert not (state / "events" / "subscribers" / "ops.json").exists()
    assert events.unsubscribe(state, "ops") is False


def test_subscribe_refuses_conflicting_settings_and_unsafe_urls(tmp_path):
    state = tmp_path / "state"
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    with pytest.raises(events.EventError) as exc:
        events.subscribe(state, {"identity": "ops", "file": "different.jsonl"})
    assert _code(exc) == "subscriber_conflict"

    with pytest.raises(events.EventError) as exc:
        events.subscribe(state, {"identity": "hook", "url": "https://user:pass@example.invalid/x"})
    assert _code(exc) == "subscriber_invalid"

    with pytest.raises(events.EventError) as exc:
        events.subscribe(state, {"identity": "../escape", "file": "x"})
    assert _code(exc) == "subscriber_invalid"


def test_subscribe_accepts_an_http_url_without_credentials(tmp_path):
    state = tmp_path / "state"
    record = events.subscribe(state, {"identity": "hook", "url": "https://example.invalid/hook"})
    assert record["url"] == "https://example.invalid/hook"
    assert record["file"] is None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_deliver_pending_writes_an_atomic_inbox_record(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})

    result = events.deliver_pending(state)
    path = _inbox(state) / f"{event['id']}.json"
    record = _read(path)
    assert result["delivered"] == 1 and result["failed"] == 0
    assert record["schema"] == events.DELIVERY_SCHEMA
    assert record["delivery_id"].startswith("dlv_")
    assert record["event_id"] == event["id"]
    assert record["subscriber"] == "ops"
    assert record["attempts"] == 1
    assert record["delivered_at"].endswith("Z")
    assert record["event"]["id"] == event["id"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]


def test_delivery_is_idempotent_and_never_duplicates_an_event(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    events.deliver_pending(state)
    before = (_inbox(state) / f"{event['id']}.json").read_bytes()

    second = events.deliver_pending(state)
    assert second["delivered"] == 0
    assert second["skipped"] == 1
    assert (_inbox(state) / f"{event['id']}.json").read_bytes() == before
    assert len(list(_inbox(state).glob("*.json"))) == 1


def test_delivery_retries_a_failed_write_and_counts_attempts(tmp_path, monkeypatch):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})

    original = events._create_only_json
    calls = {"n": 0}

    def flaky(path, value):
        if path.parent.parent.name == "inbox" and path.parent.name == "ops" and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated crash before link")
        return original(path, value)

    monkeypatch.setattr(events, "_create_only_json", flaky)
    first = events.deliver_pending(state)
    assert first["delivered"] == 0
    assert first["failed"] == 1
    assert not (_inbox(state) / f"{event['id']}.json").exists()
    assert events.pending_deliveries(state) == [{"subscriber": "ops", "event_id": event["id"]}]

    second = events.deliver_pending(state)
    assert second["delivered"] == 1
    record = _read(_inbox(state) / f"{event['id']}.json")
    assert record["attempts"] == 2


def test_delivery_fans_out_to_every_subscriber_once(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "a"})
    events.subscribe(state, {"identity": "audit", "file": "b"})

    result = events.deliver_pending(state)
    assert result["delivered"] == 2
    assert (_inbox(state, "ops") / f"{event['id']}.json").is_file()
    assert (_inbox(state, "audit") / f"{event['id']}.json").is_file()
    assert events.deliver_pending(state)["delivered"] == 0


# ---------------------------------------------------------------------------
# Acknowledgement, replay and crash recovery
# ---------------------------------------------------------------------------


def test_acknowledge_is_durable_and_idempotent(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    events.deliver_pending(state)

    first = events.acknowledge(state, "ops", event["id"])
    second = events.acknowledge(state, "ops", event["id"])
    path = _acks(state) / f"{event['id']}.json"
    assert path.is_file()
    assert first["created"] is True
    assert second["created"] is False
    assert _read(path)["schema"] == events.ACK_SCHEMA
    assert _read(path)["delivery_id"] == first["delivery_id"]
    assert _read(path)["acknowledged_at"].endswith("Z")


def test_acknowledge_refuses_an_undelivered_event(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    with pytest.raises(events.EventError) as exc:
        events.acknowledge(state, "ops", event["id"])
    assert _code(exc) == "delivery_missing"


def test_replay_is_allowed_only_after_ack_and_clears_the_ack(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    events.deliver_pending(state)
    delivered_at = _read(_inbox(state) / f"{event['id']}.json")["delivered_at"]

    with pytest.raises(events.EventError) as exc:
        events.replay(state, event["id"], "ops")
    assert _code(exc) == "replay_refused"

    events.acknowledge(state, "ops", event["id"])
    replayed = events.replay(state, event["id"], "ops", now=time.time() + 10)
    assert replayed["attempts"] == 2
    assert replayed["delivered_at"] != delivered_at
    assert not (_acks(state) / f"{event['id']}.json").exists()
    assert _read(_inbox(state) / f"{event['id']}.json")["attempts"] == 2


def test_replay_is_allowed_when_the_delivery_is_missing(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    assert not (_inbox(state) / f"{event['id']}.json").exists()
    record = events.replay(state, event["id"], "ops")
    assert record["attempts"] == 1
    assert (_inbox(state) / f"{event['id']}.json").is_file()


def test_replay_refuses_an_unknown_event(tmp_path):
    state = tmp_path / "state"
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    with pytest.raises(events.EventError) as exc:
        events.replay(state, "evt_" + "0" * 32, "ops")
    assert _code(exc) == "event_missing"


def test_crash_recovery_scan_finds_outbox_events_with_no_delivery(tmp_path, monkeypatch):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    assert events.pending_deliveries(state) == [{"subscriber": "ops", "event_id": event["id"]}]

    original = events._create_only_json

    def crash(path, value):
        if path.parent.parent.name == "inbox":
            raise OSError("process died before delivery")
        return original(path, value)

    monkeypatch.setattr(events, "_create_only_json", crash)
    assert events.deliver_pending(state)["failed"] == 1
    assert events.pending_deliveries(state) == [{"subscriber": "ops", "event_id": event["id"]}]

    monkeypatch.setattr(events, "_create_only_json", original)
    recovered = events.recover_pending(state)
    assert recovered["delivered"] == 1
    assert events.pending_deliveries(state) == []
    assert _read(_inbox(state) / f"{event['id']}.json")["attempts"] == 2


def test_delivery_payload_does_not_leak_the_state_directory(tmp_path):
    state = tmp_path / "private-state-dir"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "ops", "file": "inbox.jsonl"})
    events.deliver_pending(state)
    text = (_inbox(state) / f"{event['id']}.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "private-state-dir" not in text


# ---------------------------------------------------------------------------
# HTTP delivery: HMAC authentication, replay protection and bounds
# ---------------------------------------------------------------------------


def test_subscribe_http_secret_is_private_and_never_returned(tmp_path):
    state = tmp_path / "state"
    secret = "fixture-http-secret-" + "7" * 12
    subscriber = {
        "identity": "hook",
        "url": "https://example.invalid/hook",
        "secret": secret,
    }
    first = events.subscribe(state, subscriber)
    assert first["created"] is True
    assert first["url"] == "https://example.invalid/hook"
    assert "secret" not in first
    assert secret not in json.dumps(first)

    stored = _read(state / "events" / "subscribers" / "hook.json")
    assert stored["secret"] == secret
    assert stat.S_IMODE((state / "events" / "subscribers" / "hook.json").stat().st_mode) == 0o600

    second = events.subscribe(state, subscriber)
    assert second["created"] is False
    assert "secret" not in second
    assert secret not in json.dumps(second)


def test_http_delivery_signs_the_raw_body_and_verifies(tmp_path):
    secret = "fixture-delivery-secret-" + "8" * 12
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)

    with _http_capture() as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        events.subscribe(state, {"identity": "hook", "url": url, "secret": secret})
        result = events.deliver_pending(state)

        assert result["delivered"] == 1
        assert result["failed"] == 0
        assert secret not in json.dumps(result)
        assert len(server.requests) == 1
        request = server.requests[0]
        body = request["body"]
        assert body == events._canonical(event)
        assert secret.encode("utf-8") not in body

        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        assert _header(request["headers"], "X-Rizzstack-Signature") == f"sha256={expected}"
        assert _header(request["headers"], "X-Rizzstack-Delivery-Id").startswith("dlv_")
        assert events.verify_delivery(request["headers"], body, secret, time.time()) is True

    record = _read(_inbox(state, "hook") / f"{event['id']}.json")
    assert record["attempts"] == 1
    assert secret not in json.dumps(record)


def test_verify_delivery_rejects_a_tampered_body():
    secret = "fixture-tamper-secret-" + "9" * 12
    body = b'{"event":"original"}'
    headers = _signed_headers(secret, body, delivery_id="dlv_tamper_1")

    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, body + b" ", secret, time.time())
    assert _code(exc) == "delivery_signature_invalid"


def test_verify_delivery_rejects_a_duplicate_delivery_id_replay():
    secret = "fixture-replay-secret-" + "a" * 12
    body = b'{"event":"replay"}'
    headers = _signed_headers(secret, body, delivery_id="dlv_replay_1")

    assert events.verify_delivery(headers, body, secret, time.time()) is True
    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, body, secret, time.time())
    assert _code(exc) == "delivery_replayed"


def test_verify_delivery_rejects_a_stale_timestamp():
    secret = "fixture-stale-secret-" + "b" * 12
    body = b'{"event":"stale"}'
    now = time.time()
    headers = _signed_headers(secret, body, now=now - 360, delivery_id="dlv_stale_1")

    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, body, secret, now, tolerance_s=300)
    assert _code(exc) == "delivery_stale"


def test_verify_delivery_rejects_a_missing_secret():
    secret = "fixture-missing-secret-" + "c" * 12
    body = b'{"event":"missing-secret"}'
    headers = _signed_headers(secret, body, delivery_id="dlv_no_secret_1")

    for missing in (None, ""):
        with pytest.raises(events.EventError) as exc:
            events.verify_delivery(headers, body, missing, time.time())
        assert _code(exc) == "secret_missing"


def test_http_delivery_without_a_secret_remains_pending(tmp_path):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(state, {"identity": "hook", "url": "http://127.0.0.1:9/hook"})

    result = events.deliver_pending(state)
    assert result["delivered"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "secret_missing"
    assert result["pending"] == 1
    assert not (_inbox(state, "hook") / f"{event['id']}.json").exists()


def test_verify_delivery_refuses_an_oversized_body():
    secret = "fixture-oversize-secret-" + "d" * 12
    body = b"x" * (events.MAX_HTTP_BODY_BYTES + 1)
    headers = _signed_headers(secret, body, delivery_id="dlv_oversize_1")

    with pytest.raises(events.EventError) as exc:
        events.verify_delivery(headers, body, secret, time.time())
    assert _code(exc) == "delivery_oversized"


def test_http_delivery_refuses_a_body_over_the_limit(tmp_path, monkeypatch):
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    events.subscribe(
        state,
        {"identity": "hook", "url": "http://127.0.0.1:9/hook", "secret": "fixture-limit"},
    )
    monkeypatch.setattr(events, "MAX_HTTP_BODY_BYTES", 32)

    result = events.deliver_pending(state)
    assert result["delivered"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "delivery_oversized"
    assert result["pending"] == 1


def test_http_delivery_retries_a_transient_failure(tmp_path, monkeypatch):
    secret = "fixture-retry-secret-" + "e" * 12
    state = tmp_path / "state"
    event = _event()
    events.record_landing(state, event)
    monkeypatch.setattr(events, "HTTP_RETRY_BACKOFF_S", 0)

    calls = {"n": 0}
    real_urlopen = events.urlrequest.urlopen

    def flaky(request, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise events.urlerror.URLError("simulated transient failure")
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(events.urlrequest, "urlopen", flaky)

    with _http_capture() as server:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        events.subscribe(state, {"identity": "hook", "url": url, "secret": secret})
        result = events.deliver_pending(state)

        assert result["delivered"] == 1
        assert result["failed"] == 0
        assert calls["n"] == 2
        assert len(server.requests) == 1
