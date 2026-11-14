"""Coordinator-side landing event consumer.

The tests use a fake handler and a fake Hermes gateway runner: no model is
called and no network is touched.  They prove the properties the consumer
exists for:

* a claim is durable before the handler runs, and an ack exists only after the
  handler returns a mapping;
* a duplicate delivery of an acknowledged event never calls the handler again;
* a crash after the handler but before the ack is retried, and every retry is
  bounded;
* an event addressed to another subscriber is never claimed;
* replay is explicit and gives one more handling decision;
* the Hermes notification adapter wakes a stateless session through the
  documented gateway chat-completions endpoint, fails closed without an
  endpoint/key or on a non-2xx/timeout, and never leaks credentials.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events
from bundlebees_plugin import event_consumer as consumer
from bundlebees_plugin import hermes_adapter as adapter


def land(
    state: Path,
    event_id: str,
    *,
    subscriber: str = "coordinator",
    **fields,
) -> dict:
    event = {"event_id": event_id, "type": "landing", "message": "landed", **fields}
    return consumer.publish_event(state, event, subscriber=subscriber)


def inbox_files(state: Path) -> list[Path]:
    root = state / "events" / "inbox"
    return sorted(path for path in root.rglob("*.json") if path.is_file())


def ack_files(state: Path) -> list[Path]:
    root = state / "events" / "acks"
    return sorted(path for path in root.rglob("*.json") if path.is_file())


# ---------------------------------------------------------------------------
# Success, dedupe and summary
# ---------------------------------------------------------------------------


def test_a_successful_handler_is_acknowledged_exactly_once(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    calls = []

    def handler(event):
        calls.append(event)
        return {"accepted": True, "event_id": event["event_id"]}

    result = consumer.consume_once(state, "coordinator", handler, consumer="worker-a")

    assert result["state"] == "acked"
    assert result["duplicate"] is False
    assert result["attempts"] == 1
    assert result["result"] == {"accepted": True, "event_id": "evt-1"}
    assert len(calls) == 1
    ack = json.loads((state / "events/acks/coordinator/evt-1.json").read_text())
    assert ack["schema"] == consumer.ACK_SCHEMA
    assert ack["event_id"] == "evt-1"
    assert ack["subscriber"] == "coordinator"
    assert ack["result"] == {"accepted": True, "event_id": "evt-1"}
    assert ack["decision"] == ack["result"]


def test_duplicate_delivery_does_not_call_the_handler_again(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    calls = []

    def handler(event):
        calls.append(event)
        return {"n": len(calls)}

    first = consumer.consume_once(state, "coordinator", handler, consumer="worker-a")
    second = consumer.consume_once(state, "coordinator", handler, consumer="worker-b")

    assert first["state"] == "acked"
    assert second["state"] == "duplicate"
    assert second["duplicate"] is True
    assert second["result"] == {"n": 1}
    assert len(calls) == 1


def test_the_claim_is_durable_before_the_handler_runs(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    observed = {}

    def handler(event):
        path = state / "events/inbox/coordinator/evt-1.json"
        record = json.loads(path.read_text())
        observed.update(record["_delivery"])
        return {"ok": True}

    result = consumer.consume_once(state, "coordinator", handler, consumer="worker-a")

    assert result["state"] == "acked"
    assert observed["state"] == "claimed"
    assert observed["attempts"] == 1
    assert observed["claim"]["consumer"] == "worker-a"


def test_inbox_summary_counts_pending_and_acked_events(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    land(state, "evt-2")

    before = consumer.inbox_summary(state, "coordinator")
    assert before["total"] == 2
    assert before["pending"] == 2
    assert before["acked"] == 0

    consumer.consume_once(
        state, "coordinator", lambda event: {"ok": event["event_id"]}, consumer="worker-a"
    )
    after = consumer.inbox_summary(state, "coordinator")
    assert after["total"] == 2
    assert after["pending"] == 1
    assert after["acked"] == 1
    assert after["subscribers"]["coordinator"]["acked"] == 1


# ---------------------------------------------------------------------------
# Crash, retry and bounded failure
# ---------------------------------------------------------------------------


def test_crash_after_handler_before_ack_is_retried(tmp_path, monkeypatch):
    state = tmp_path / "state"
    land(state, "evt-1")
    calls = []

    def handler(event):
        calls.append(event)
        return {"n": len(calls)}

    real_ack = consumer._ack

    def crash_before_ack(*args, **kwargs):
        raise RuntimeError("simulated crash after the handler")

    monkeypatch.setattr(consumer, "_ack", crash_before_ack)
    crashed = consumer.consume_once(state, "coordinator", handler, consumer="worker-a")

    assert crashed["state"] == "retryable"
    assert crashed["error"] == "RuntimeError"
    assert len(calls) == 1
    assert ack_files(state) == []

    monkeypatch.setattr(consumer, "_ack", real_ack)
    retried = consumer.consume_once(state, "coordinator", handler, consumer="worker-b")

    assert retried["state"] == "acked"
    assert retried["result"] == {"n": 2}
    assert len(calls) == 2
    assert len(ack_files(state)) == 1


def test_a_handler_failure_is_retried_once_then_bounded(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    calls = []

    def handler(event):
        calls.append(event)
        raise ValueError("cannot handle this event")

    first = consumer.consume_once(state, "coordinator", handler, consumer="worker-a")
    second = consumer.consume_once(state, "coordinator", handler, consumer="worker-b")
    third = consumer.consume_once(state, "coordinator", handler, consumer="worker-c")

    assert first["state"] == "retryable"
    assert first["attempts"] == 1
    assert second["state"] == "failed"
    assert second["attempts"] == consumer.MAX_ATTEMPTS
    assert third is None
    assert len(calls) == consumer.MAX_ATTEMPTS
    assert ack_files(state) == []
    summary = consumer.inbox_summary(state, "coordinator")
    assert summary["failed"] == 1
    assert summary["refused"] == 1


def test_a_failed_event_does_not_block_a_later_event(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    land(state, "evt-2")
    calls = []

    def handler(event):
        calls.append(event["event_id"])
        if event["event_id"] == "evt-1":
            raise RuntimeError("first event is poison")
        return {"ok": True}

    consumer.consume_once(state, "coordinator", handler, consumer="worker-a")
    consumer.consume_once(state, "coordinator", handler, consumer="worker-b")
    third = consumer.consume_once(state, "coordinator", handler, consumer="worker-c")

    assert third["event_id"] == "evt-2"
    assert third["state"] == "acked"
    assert calls == ["evt-1", "evt-1", "evt-2"]


def test_a_handler_must_return_a_mapping_to_be_acknowledged(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")

    first = consumer.consume_once(
        state, "coordinator", lambda event: "not a decision", consumer="worker-a"
    )
    second = consumer.consume_once(
        state, "coordinator", lambda event: None, consumer="worker-b"
    )

    assert first["state"] == "retryable"
    assert first["error"] == "handler_result_invalid"
    assert second["state"] == "failed"
    assert ack_files(state) == []


# ---------------------------------------------------------------------------
# Replay and subscriber isolation
# ---------------------------------------------------------------------------


def test_replay_acked_allows_one_more_handling_decision(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")
    calls = []

    def handler(event):
        calls.append(event)
        return {"n": len(calls)}

    consumer.consume_once(state, "coordinator", handler, consumer="worker-a")
    replay = consumer.replay_acked(state, "coordinator", "evt-1")
    replayed = consumer.consume_once(state, "coordinator", handler, consumer="worker-b")

    assert replay["replayed"] is True
    assert replay["event_id"] == "evt-1"
    assert replayed["state"] == "acked"
    assert replayed["result"] == {"n": 2}
    assert len(calls) == 2
    assert json.loads(
        (state / "events/acks/coordinator/evt-1.json").read_text()
    )["result"] == {"n": 2}


def test_replay_of_an_unacked_event_refuses(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")

    replay = consumer.replay_acked(state, "coordinator", "evt-1")

    assert replay["replayed"] is False
    assert replay["reason"] == "not_acked"


def test_an_event_for_another_subscriber_is_never_claimed(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1", subscriber="someone-else")
    calls = []

    result = consumer.consume_once(
        state, "coordinator", lambda event: calls.append(event) or {"ok": True},
        consumer="worker-a",
    )

    assert result is None
    assert calls == []
    assert consumer.inbox_summary(state, "coordinator")["total"] == 0


def test_a_flat_inbox_event_naming_another_subscriber_is_ignored(tmp_path):
    state = tmp_path / "state"
    inbox = state / "events/inbox"
    inbox.mkdir(parents=True)
    (inbox / "evt-1.json").write_text(
        json.dumps({"event_id": "evt-1", "subscriber": "someone-else"}), encoding="utf-8"
    )
    calls = []

    result = consumer.consume_once(
        state, "coordinator", lambda event: calls.append(event) or {"ok": True},
        consumer="worker-a",
    )

    assert result is None
    assert calls == []


def test_a_flat_inbox_event_naming_the_subscriber_is_consumed(tmp_path):
    state = tmp_path / "state"
    inbox = state / "events/inbox"
    inbox.mkdir(parents=True)
    (inbox / "evt-1.json").write_text(
        json.dumps({"event_id": "evt-1", "subscriber": "coordinator", "message": "hi"}),
        encoding="utf-8",
    )
    calls = []

    result = consumer.consume_once(
        state, "coordinator", lambda event: calls.append(event) or {"ok": True},
        consumer="worker-a",
    )

    assert result["state"] == "acked"
    assert calls and calls[0]["message"] == "hi"


def test_unsafe_identifiers_are_refused(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")

    with pytest.raises(consumer.EventConsumerError) as caught:
        consumer.consume_once(
            state, "../escape", lambda event: {"ok": True}, consumer="worker-a"
        )
    assert caught.value.code == "identifier_invalid"

    with pytest.raises(consumer.EventConsumerError) as caught:
        consumer.replay_acked(state, "coordinator", "../evt-1")
    assert caught.value.code == "identifier_invalid"


def test_the_consumer_writes_only_inbox_and_acks(tmp_path):
    state = tmp_path / "state"
    land(state, "evt-1")

    consumer.consume_once(
        state, "coordinator", lambda event: {"ok": True}, consumer="worker-a"
    )

    written = sorted(
        path.relative_to(state).as_posix()
        for path in state.rglob("*")
        if path.is_file()
    )
    assert written
    assert all(
        name.startswith("events/inbox/") or name.startswith("events/acks/")
        for name in written
    )


# ---------------------------------------------------------------------------
# The notification adapter uses the documented gateway wake path
# ---------------------------------------------------------------------------


class RecordingGateway:
    """A loopback HTTP server that records one wake request and answers it."""

    def __init__(self, *, status: int = 200, delay: float = 0.0, response: bytes = b'{"ok":true}') -> None:
        self.status = status
        self.delay = delay
        self.response = response
        self.requests: list[dict] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> "RecordingGateway":
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                gateway.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers.items()),
                        "body": body,
                    }
                )
                if gateway.delay:
                    time.sleep(gateway.delay)
                try:
                    self.send_response(gateway.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(gateway.response)))
                    self.end_headers()
                    self.wfile.write(gateway.response)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args) -> None:  # noqa: D401 - silence the test server
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *exc) -> None:
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def wake_event(**overrides) -> dict:
    event = {
        "event_id": "evt-1",
        "type": "landing",
        "session_id": "coordinator-session",
        "job_id": "job-1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "state": "landed",
        "source_rev": "deadbeef",
        "result_reference": "recovery/job-1/task-1.1.proposal.json",
        "message": "landed",
        "token": "event-token-should-never-be-sent",
        "private_path": "/home/cua/private/state",
    }
    event.update(overrides)
    return event


def test_notification_posts_to_the_gateway_wake_endpoint(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with RecordingGateway() as gateway:
        result = adapter.notify_coordinator(
            wake_event(),
            endpoint=gateway.url,
            api_key="test-key",
            session="coordinator-session",
        )

    assert result["sent"] is True
    assert result["delivered"] is True
    assert result["transport"] == "gateway"
    assert result["path"] == "/v1/chat/completions"
    assert result["status"] == 200
    assert result["event_id"] == "evt-1"
    assert result["session"] == "coordinator-session"

    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request["path"] == "/v1/chat/completions"
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert headers["x-hermes-session-id"] == "coordinator-session"
    assert headers["authorization"] == "Bearer test-key"
    body = json.loads(request["body"])
    assert body["stream"] is False
    assert body["model"]
    assert body["messages"][0]["role"] == "user"
    content = json.loads(body["messages"][0]["content"])
    assert content["event_id"] == "evt-1"
    assert content["type"] == "landing"


def test_notification_reads_endpoint_and_key_from_the_environment(monkeypatch):
    with RecordingGateway() as gateway:
        monkeypatch.setenv("HERMES_API_URL", gateway.url)
        monkeypatch.setenv("HERMES_API_KEY", "environment-key")
        result = adapter.notify_coordinator(wake_event(), session="coordinator-session")

    assert result["sent"] is True
    assert len(gateway.requests) == 1
    headers = {key.lower(): value for key, value in gateway.requests[0]["headers"].items()}
    assert headers["authorization"] == "Bearer environment-key"


def test_a_non_2xx_gateway_response_is_never_sent(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with RecordingGateway(status=503) as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                wake_event(),
                endpoint=gateway.url,
                api_key="test-key",
                session="coordinator-session",
            )

    assert caught.value.code == "hermes_notification_failed"
    assert "503" in caught.value.message
    assert len(gateway.requests) == 1


def test_a_timed_out_gateway_request_is_never_sent(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with RecordingGateway(delay=0.5) as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                wake_event(),
                endpoint=gateway.url,
                api_key="test-key",
                session="coordinator-session",
                timeout_s=0.1,
            )

    assert caught.value.code == "hermes_notification_failed"
    assert "did not answer" in caught.value.message
    assert len(gateway.requests) == 1


def test_notification_refuses_an_unrelated_session(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with RecordingGateway() as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                wake_event(session_id="someone-else"),
                endpoint=gateway.url,
                api_key="test-key",
                session="coordinator-session",
            )

    assert caught.value.code == "hermes_session_mismatch"
    assert gateway.requests == []


def test_configured_session_requires_an_event_session(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with RecordingGateway() as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                wake_event(session_id=""),
                endpoint=gateway.url,
                api_key="test-key",
                session="coordinator-session",
            )

    assert caught.value.code == "hermes_session_mismatch"
    assert gateway.requests == []


def test_missing_endpoint_is_a_not_run_refusal(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(wake_event(), session="coordinator-session")

    assert caught.value.code == "hermes_notification_unavailable"
    assert "HERMES_API_URL" in caught.value.message


def test_missing_key_is_a_not_run_refusal(monkeypatch):
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            wake_event(),
            endpoint="http://127.0.0.1:1",
            session="coordinator-session",
        )

    assert caught.value.code == "hermes_notification_unavailable"
    assert "HERMES_API_KEY" in caught.value.message


def test_a_non_http_endpoint_is_refused(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            wake_event(),
            endpoint="file:///tmp/socket",
            api_key="test-key",
            session="coordinator-session",
        )

    assert caught.value.code == "hermes_notification_unavailable"
    assert "http(s)" in caught.value.message


def test_the_payload_contains_only_sanitized_event_fields(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    event = wake_event(
        message="secret happened at /home/cua/private/state and token=do-not-send",
        token="event-token-should-never-be-sent",
        private_path="/home/cua/private/state",
        result_reference="/home/cua/private/result.json",
    )
    with RecordingGateway() as gateway:
        adapter.notify_coordinator(
            event,
            endpoint=gateway.url,
            api_key="test-key",
            session="coordinator-session",
        )

    raw_body = gateway.requests[0]["body"]
    body = json.loads(raw_body)
    content_text = body["messages"][0]["content"]
    content = json.loads(content_text)

    assert set(content) == {
        "event_id",
        "type",
        "job_id",
        "task_id",
        "attempt_id",
        "state",
        "source_rev",
        "result_reference",
    }
    assert content["event_id"] == "evt-1"
    assert content["result_reference"] == "result.json"
    assert "event-token-should-never-be-sent" not in raw_body.decode("utf-8")
    assert "/home/cua" not in raw_body.decode("utf-8")
    assert "private_path" not in content_text
    assert "do-not-send" not in raw_body.decode("utf-8")


def test_successful_delivery_does_not_log_or_return_credentials(monkeypatch, caplog):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    caplog.set_level(logging.DEBUG)
    key = "sk-live-should-never-appear-123456"

    with RecordingGateway() as gateway:
        result = adapter.notify_coordinator(
            wake_event(),
            endpoint=gateway.url,
            api_key=key,
            session="coordinator-session",
        )

    rendered = json.dumps(result)
    assert key not in rendered
    assert gateway.url not in rendered
    assert key not in caplog.text


def test_a_refused_delivery_does_not_log_or_return_credentials(monkeypatch, caplog):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    caplog.set_level(logging.DEBUG)
    key = "sk-live-should-never-appear-987654"

    with RecordingGateway(status=401) as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                wake_event(),
                endpoint=gateway.url,
                api_key=key,
                session="coordinator-session",
            )

    assert caught.value.code == "hermes_notification_failed"
    assert key not in caplog.text
    assert key not in str(caught.value)
    assert gateway.url not in str(caught.value)


def test_an_injected_transport_is_used_without_network(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    calls = []

    def runner(url, *, headers, body, timeout_s):
        calls.append({"url": url, "headers": headers, "body": body, "timeout_s": timeout_s})
        return 200, b'{"ok":true}'

    result = adapter.notify_coordinator(
        wake_event(),
        endpoint="http://127.0.0.1:9/v1",
        api_key="test-key",
        session="coordinator-session",
        runner=runner,
    )

    assert result["sent"] is True
    assert calls[0]["headers"]["X-Hermes-Session-Id"] == "coordinator-session"
    assert calls[0]["url"] == "http://127.0.0.1:9/v1/chat/completions"


def test_an_injected_transport_timeout_is_never_sent(monkeypatch):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    def runner(url, *, headers, body, timeout_s):
        raise TimeoutError("simulated timeout")

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            wake_event(),
            endpoint="http://127.0.0.1:9",
            api_key="test-key",
            session="coordinator-session",
            runner=runner,
        )

    assert caught.value.code == "hermes_notification_failed"


# ---------------------------------------------------------------------------
# Canonical hyvv landing events and authoritative subscriber routing
# ---------------------------------------------------------------------------

SUBSCRIBER_SECRET = "subscriber-secret-should-never-be-sent"


def hyvv_event(**overrides) -> dict:
    """A real ``rizzstack.bee.landed/1`` event as the hyvv bridge emits it."""

    event = {
        "feature_id": "BEE-1",
        "job_id": "job-1",
        "task_id": "task-1",
        "coordinator": {"identity": "coordinator-1", "session": "run-1"},
        "backend": {"harness": "hyvv", "model": "deepseek-v4.1-flash"},
        "source_rev": "a" * 40,
        "lease": {
            "sha256": "b" * 64,
            "assigned_files": ["src/a.py", "tests/test_a.py"],
        },
        "result": {
            "available": True,
            "artifact_sha256": "c" * 64,
            "reference": "refs/heads/feat/bee-events",
        },
    }
    event.update(overrides)
    return bee_events.build_event(
        {"attempt_id": "att-bee-1", "state": "landed", "transition_id": "tr-bee-1"},
        **event,
    )


def hermes_subscription(state: Path, *, identity: str = "coordinator-1",
                        session: str = "run-1") -> dict:
    return bee_events.subscribe(
        state,
        {
            "identity": identity,
            "url": "https://gateway.invalid/hook",
            "secret": SUBSCRIBER_SECRET,
            "session": session,
        },
    )


def test_a_canonical_hyvv_event_wakes_its_authoritative_subscriber(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    state = tmp_path / "state"
    hermes_subscription(state)
    event = hyvv_event()

    with RecordingGateway() as gateway:
        result = adapter.notify_coordinator(
            event,
            endpoint=gateway.url,
            api_key="test-key",
            subscriber="coordinator-1",
            state_dir=state,
        )

    assert result["sent"] is True
    assert result["session"] == "run-1"
    request = gateway.requests[0]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert headers["x-hermes-session-id"] == "run-1"

    raw_body = request["body"]
    content = json.loads(
        json.loads(raw_body)["messages"][0]["content"]
    )
    assert content == {
        "event_id": event["id"],
        "type": "rizzstack.bee.landed.v1",
        "job_id": "job-1",
        "task_id": "task-1",
        "attempt_id": "att-bee-1",
        "state": "landed",
        "source_rev": "a" * 40,
        "result_reference": "refs/heads/feat/bee-events",
    }
    assert SUBSCRIBER_SECRET not in raw_body.decode("utf-8")
    assert "assigned_files" not in raw_body.decode("utf-8")


def test_a_canonical_event_for_another_coordinator_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    state = tmp_path / "state"
    hermes_subscription(state)
    event = hyvv_event(coordinator={"identity": "coordinator-2", "session": "run-1"})

    with RecordingGateway() as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                event,
                endpoint=gateway.url,
                api_key="test-key",
                subscriber="coordinator-1",
                state_dir=state,
            )

    assert caught.value.code == "hermes_session_mismatch"
    assert gateway.requests == []


def test_a_canonical_event_without_a_session_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    state = tmp_path / "state"
    hermes_subscription(state)
    event = hyvv_event(coordinator={"identity": "coordinator-1"})

    with RecordingGateway() as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                event,
                endpoint=gateway.url,
                api_key="test-key",
                subscriber="coordinator-1",
                state_dir=state,
            )

    assert caught.value.code == "hermes_session_mismatch"
    assert gateway.requests == []


def test_a_root_only_legacy_event_cannot_bind_a_subscriber(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    state = tmp_path / "state"
    hermes_subscription(state)
    legacy = wake_event(session_id="run-1")

    with RecordingGateway() as gateway:
        with pytest.raises(adapter.ProposalError) as caught:
            adapter.notify_coordinator(
                legacy,
                endpoint=gateway.url,
                api_key="test-key",
                subscriber="coordinator-1",
                state_dir=state,
            )

    assert caught.value.code == "hermes_session_mismatch"
    assert gateway.requests == []
