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
* the Hermes notification adapter refuses when no documented continuation
  command can be probed, and never reports success for a refused session.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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
# The notification adapter fails closed
# ---------------------------------------------------------------------------


class GatewayRunner:
    """A fake installed CLI that documents (or does not document) continuation."""

    def __init__(
        self,
        *,
        help_text: bytes = b"usage: hermes [--resume SESSION] [--continue]\n",
        help_returncode: int = 0,
        continuation_returncode: int = 0,
    ) -> None:
        self.help_text = help_text
        self.help_returncode = help_returncode
        self.continuation_returncode = continuation_returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout_s, prompt, cwd):
        self.calls.append(list(argv))
        if "--help" in argv:
            return subprocess.CompletedProcess(
                argv, self.help_returncode, stdout=self.help_text, stderr=b""
            )
        return subprocess.CompletedProcess(
            argv, self.continuation_returncode, stdout=b"", stderr=b""
        )


def test_missing_hermes_gateway_is_a_not_run_refusal(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(adapter.shutil, "which", lambda name: None)

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            {"event_id": "evt-1", "session_id": "coordinator-session", "message": "landed"}
        )

    assert caught.value.code == "hermes_notification_unavailable"
    assert "gateway api" in caught.value.fix.lower()


def test_notification_refuses_an_unrelated_session(tmp_path):
    runner = GatewayRunner()

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            {"event_id": "evt-1", "session_id": "someone-else", "message": "landed"},
            runner=runner,
            session="coordinator-session",
        )

    assert caught.value.code == "hermes_session_mismatch"
    assert runner.calls == []


def test_notification_without_a_documented_continuation_refuses(tmp_path):
    runner = GatewayRunner(help_text=b"usage: hermes [--version] [--help]\n")

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            {"event_id": "evt-1", "session_id": "coordinator-session", "message": "landed"},
            runner=runner,
        )

    assert caught.value.code == "hermes_notification_unavailable"
    assert all("--resume" not in call and "--continue" not in call for call in runner.calls)


def test_notification_uses_the_documented_continuation(tmp_path):
    runner = GatewayRunner()

    result = adapter.notify_coordinator(
        {"event_id": "evt-1", "session_id": "coordinator-session", "message": "landed"},
        runner=runner,
    )

    assert result["sent"] is True
    assert result["delivered"] is True
    assert result["event_id"] == "evt-1"
    assert result["session"] == "coordinator-session"
    assert result["continuation"] == "--resume"
    continuation = runner.calls[-1]
    assert continuation[0].endswith("hermes")
    assert continuation[1:4] == ["chat", "--resume", "coordinator-session"]
    assert continuation[-1] == "landed"
    assert result["command"][-1].startswith("<message sha256=")


def test_a_failed_continuation_is_never_reported_as_sent(tmp_path):
    runner = GatewayRunner(continuation_returncode=3)

    with pytest.raises(adapter.ProposalError) as caught:
        adapter.notify_coordinator(
            {"event_id": "evt-1", "session_id": "coordinator-session", "message": "landed"},
            runner=runner,
        )

    assert caught.value.code == "hermes_notification_failed"
