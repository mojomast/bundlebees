"""W6 completions x F6 coordinator action queue: the composed consumer.

The tests use only durable files: the canonical bee-landed outbox, a file
subscriber inbox, and the coordinator action queue. No model is called, no
network is touched, and the Hermes wake path is only ever spied on to prove it
is not needed. They prove the properties the composition exists for:

* a terminal bee-landed event becomes exactly one `verify:<attempt>` and one
  `process-completion:<attempt>` action-queue item, keyed by the F6 idempotent
  keys, with the completion recorded once by attempt_id + state;
* duplicate delivery, explicit replay, a crash between the effect and the
  acknowledgement, and a coordinator restart each produce zero duplicate
  actions;
* the queue is unavailable -> the event stays buffered with a bounded backoff
  and is processed once the queue returns;
* malformed events are skipped with a counted reason and never enqueue;
* action items are claimed under a fencing token; a stale token is refused;
* the pipeline advances without Hermes being awake: the wake path is never
  touched;
* the composition's bundled store and the merged coordinator action queue
  (#54) coexist in one state directory without corrupting each other, and the
  explicit adapter publishes conformant records into the merged queue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events
from bundlebees_plugin import coordinator_actions as ca
from bundlebees_plugin import event_consumer as consumer
from bundlebees_plugin import hermes_adapter as adapter

T0 = 1_700_000_000.0
SUBSCRIBER = "coordinator-actions"


def canonical_event(
    attempt_id: str = "att-bee-1",
    state: str = "landed",
    transition_id: str = "tr-bee-1",
    **overrides,
) -> dict:
    fields = {
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
    fields.update(overrides)
    return bee_events.build_event(
        {"attempt_id": attempt_id, "state": state, "transition_id": transition_id},
        **fields,
    )


def land(state: Path, event: dict, *, identity: str = SUBSCRIBER) -> dict:
    """Record one canonical landing and deliver it to the file subscriber."""

    bee_events.record_landing(state, event)
    bee_events.subscribe(state, {"identity": identity, "file": "coordinator-actions"})
    delivery = bee_events.deliver_pending(state)
    assert delivery["failed"] == 0
    return event


def action_path(state: Path, action_id: str) -> Path:
    """The composition's bundled item store, beside the merged queue's directory."""

    return state / "coordinator" / "completion-actions" / f"{action_id}.json"


def read_action(state: Path, action_id: str) -> dict:
    return json.loads(action_path(state, action_id).read_text(encoding="utf-8"))


def inbox_records(state: Path) -> list[dict]:
    root = state / "events" / "inbox" / SUBSCRIBER
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


# ---------------------------------------------------------------------------
# The normal flow: terminal event -> action-queue items
# ---------------------------------------------------------------------------


def test_a_landed_completion_enqueues_verify_and_process_completion(tmp_path):
    state = tmp_path / "state"
    event = canonical_event()
    # An older producer may attach arbitrary fields to the event it delivers;
    # the fixture supplies one so the projection assertion below is load-bearing.
    delivered = {**event, "worker_note": "left the bench notes in the run log"}
    consumer.publish_event(state, delivered, subscriber=SUBSCRIBER)

    result = consumer.consume_completions(state, now=T0)

    assert result["state"] == "acked"
    assert result["duplicate"] is False
    actions = result["result"]["actions"]
    assert actions == ["verify:att-bee-1", "process-completion:att-bee-1"]

    verify = read_action(state, "verify:att-bee-1")
    assert verify["schema"] == "rizzstack.coordinator-action/1"
    assert verify["kind"] == "verify"
    assert verify["attempt_id"] == "att-bee-1"
    assert verify["status"] == "pending"
    assert verify["fence"] == 0
    assert verify["dedupe_key"] == "att-bee-1:landed"
    assert verify["event_id"] == event["id"]
    assert verify["event_state"] == "landed"
    assert verify["job_id"] == "job-1"
    assert verify["task_id"] == "task-1"
    assert verify["lease_files"] == ["src/a.py", "tests/test_a.py"]
    assert verify["result_available"] is True
    assert verify["result_reference"] == "refs/heads/feat/bee-events"

    completion = read_action(state, "process-completion:att-bee-1")
    assert completion["kind"] == "process-completion"
    assert completion["attempt_id"] == "att-bee-1"
    assert completion["status"] == "pending"

    ledger = json.loads(
        (state / "coordinator" / "completions" / "att-bee-1:landed.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["schema"] == "rizzstack.completion-ledger/1"
    assert ledger["attempt_id"] == "att-bee-1"
    assert ledger["state"] == "landed"
    assert ledger["actions"] == ["verify:att-bee-1", "process-completion:att-bee-1"]
    ack = json.loads(
        (state / "events" / "acks" / SUBSCRIBER / f"{event['id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert ack["result"]["accepted"] is True

    # Only allowlisted completion facts are copied: the event carried
    # worker_note, the stored item must not.
    assert "worker_note" in delivered
    assert "worker_note" not in verify


def test_a_failed_completion_enqueues_only_process_completion(tmp_path):
    state = tmp_path / "state"
    land(state, canonical_event(state="failed", transition_id="tr-fail",
                                result={"available": False}))

    result = consumer.consume_completions(state, now=T0)

    assert result["state"] == "acked"
    assert result["result"]["actions"] == ["process-completion:att-bee-1"]
    assert not action_path(state, "verify:att-bee-1").exists()
    assert read_action(state, "process-completion:att-bee-1")["status"] == "pending"


def test_the_pipeline_advances_without_hermes_being_awake(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    state = tmp_path / "state"
    land(state, canonical_event())

    def no_wake(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the action-queue consumer must not wake Hermes")

    monkeypatch.setattr(adapter, "notify_coordinator", no_wake)
    result = consumer.consume_completions(state, now=T0)
    assert result["state"] == "acked"
    assert len(result["result"]["actions"]) == 2


# ---------------------------------------------------------------------------
# Exactly once: duplicates, replay, crash window, restart
# ---------------------------------------------------------------------------


def test_duplicate_delivery_creates_no_second_action(tmp_path):
    state = tmp_path / "state"
    event = land(state, canonical_event())

    first = consumer.consume_completions(state, now=T0)
    second = consumer.consume_completions(state, now=T0 + 1)

    assert first["state"] == "acked"
    assert second["state"] == "duplicate"
    assert second["duplicate"] is True
    assert second["result"]["actions"] == [
        "verify:att-bee-1",
        "process-completion:att-bee-1",
    ]
    summary = consumer.actions_summary(state)
    assert summary["counts"] == {"total": 2, "pending": 2, "claimed": 0, "done": 0,
                                 "failed": 0}
    assert len(list((state / "coordinator" / "completions").glob("*.json"))) == 1
    assert event["id"]


def test_replay_of_one_event_creates_no_second_action(tmp_path):
    state = tmp_path / "state"
    event = land(state, canonical_event())

    consumer.consume_completions(state, now=T0)
    replay = consumer.replay_acked(state, SUBSCRIBER, event["id"])
    assert replay["replayed"] is True

    again = consumer.consume_completions(state, now=T0 + 1)
    assert again["state"] == "acked"
    assert again["result"]["duplicate"] is True
    assert again["result"]["reason"] == "completion_already_processed"
    assert again["result"]["actions"] == [
        "verify:att-bee-1",
        "process-completion:att-bee-1",
    ]
    summary = consumer.actions_summary(state)
    assert summary["counts"]["total"] == 2


def test_a_crash_after_the_effect_before_the_ack_resolves_to_one_action_set(tmp_path):
    state = tmp_path / "state"
    event = land(state, canonical_event())
    real = consumer.action_queue_handler(state, now=T0)

    def crashing(event_payload):
        real(event_payload)
        raise RuntimeError("coordinator died after the enqueue")

    first = consumer.consume_completions(
        state, now=T0, handler=crashing, max_attempts=2, retry_backoff_s=0
    )
    assert first["state"] == "retryable"

    second = consumer.consume_completions(
        state, now=T0 + 1, max_attempts=2, retry_backoff_s=0
    )
    assert second["state"] == "acked"
    # The replay resolves to the recorded decision: nothing new is created.
    assert second["result"]["duplicate"] is True
    assert second["result"]["reason"] == "completion_already_processed"

    summary = consumer.actions_summary(state)
    assert summary["counts"] == {"total": 2, "pending": 2, "claimed": 0, "done": 0,
                                 "failed": 0}
    ack = json.loads(
        (state / "events" / "acks" / SUBSCRIBER / f"{event['id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert ack["attempts"] == 2


def test_dedupe_by_attempt_and_state_across_two_events(tmp_path):
    state = tmp_path / "state"
    first = land(state, canonical_event(transition_id="tr-1"))
    second_event = land(state, canonical_event(transition_id="tr-2"))
    assert first["id"] != second_event["id"]

    one = consumer.consume_completions(state, now=T0)
    two = consumer.consume_completions(state, now=T0 + 1)

    assert one["result"]["duplicate"] is False
    assert two["state"] == "acked"
    assert two["result"]["duplicate"] is True
    assert two["result"]["reason"] == "completion_already_processed"
    summary = consumer.actions_summary(state)
    assert summary["counts"]["total"] == 2
    assert summary["counts"]["pending"] == 2


# ---------------------------------------------------------------------------
# Failure modes: queue down, malformed events, exhausted retries
# ---------------------------------------------------------------------------


def test_queue_unavailable_buffers_the_event_for_a_bounded_retry(tmp_path):
    state = tmp_path / "state"
    event = land(state, canonical_event())

    def queue_down(*args, **kwargs):
        raise consumer.ActionQueueError("queue_unavailable", "the queue is unreachable")

    down = consumer.consume_completions(
        state, now=T0, enqueue=queue_down, max_attempts=2
    )
    assert down["state"] == "retryable"
    assert down["error_code"] == "queue_unavailable"
    assert down["retry_after"]
    assert not (state / "events" / "acks" / SUBSCRIBER / f"{event['id']}.json").exists()

    # The outbox event is still durable, and the delivery waits for the backoff.
    assert (state / "events" / "outbox" / f"{event['id']}.json").exists()
    delivery = inbox_records(state)[0]
    assert delivery["_delivery"]["state"] == "retryable"

    early = consumer.consume_completions(
        state, now=T0 + 10, enqueue=queue_down, max_attempts=2
    )
    assert early is None  # still inside the bounded backoff

    up = consumer.consume_completions(state, now=T0 + 31, max_attempts=2)
    assert up["state"] == "acked"
    assert up["result"]["actions"] == [
        "verify:att-bee-1",
        "process-completion:att-bee-1",
    ]


def test_a_sustained_outage_marks_the_event_failed_and_requeue_is_explicit(tmp_path):
    state = tmp_path / "state"
    event = land(state, canonical_event())

    def queue_down(*args, **kwargs):
        raise consumer.ActionQueueError("queue_unavailable", "the queue is unreachable")

    first = consumer.consume_completions(
        state, now=T0, enqueue=queue_down, max_attempts=2
    )
    second = consumer.consume_completions(
        state, now=T0 + 31, enqueue=queue_down, max_attempts=2
    )
    assert first["state"] == "retryable"
    assert second["state"] == "failed"
    assert second["error_code"] == "queue_unavailable"

    stuck = consumer.consume_completions(
        state, now=T0 + 62, max_attempts=2
    )
    assert stuck is None  # failed events are never re-claimed silently

    requeued = consumer.requeue_failed(state, SUBSCRIBER, event["id"])
    assert requeued["requeued"] is True
    delivery = inbox_records(state)[0]
    assert delivery["_delivery"]["state"] == "retryable"

    up = consumer.consume_completions(state, now=T0 + 63, max_attempts=2)
    assert up["state"] == "acked"
    assert len(up["result"]["actions"]) == 2


def test_malformed_and_non_terminal_events_are_skipped_with_counted_reasons(tmp_path):
    state = tmp_path / "state"
    bad_attempts = [
        {"event_id": "evt-missing-attempt", "type": "landing", "state": "landed"},
        {"event_id": "evt-bad-attempt", "type": "landing", "state": "landed",
         "attempt_id": "att/../escape"},
        {"event_id": "evt-bad-state", "type": "landing", "attempt_id": "att-bee-2"},
        {"event_id": "evt-not-terminal", "type": "landing", "attempt_id": "att-bee-3",
         "state": "dispatched"},
    ]
    for event in bad_attempts:
        consumer.publish_event(state, event, subscriber=SUBSCRIBER)

    for _ in bad_attempts:
        result = consumer.consume_completions(state, now=T0)
        assert result["state"] == "acked"
        assert result["result"]["skipped"] is True

    skips = consumer.skipped_events(state)
    counts = skips["counts"]
    assert counts["malformed_attempt_missing"] == 1
    assert counts["malformed_attempt_id"] == 1
    assert counts["malformed_state_missing"] == 1
    assert counts["not_terminal"] == 1
    assert consumer.actions_summary(state)["counts"]["total"] == 0


# ---------------------------------------------------------------------------
# Fencing: claim, stale consumer, bounded retries
# ---------------------------------------------------------------------------


def test_claim_assigns_a_fencing_token_and_a_second_claim_is_busy(tmp_path):
    state = tmp_path / "state"
    consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-1", event_state="landed", now=T0,
    )

    first = consumer.claim_action(state, "verify:att-bee-1", consumer="turn-1", now=T0)
    assert first["status"] == "claimed"
    assert first["fence"] == 1
    assert first["item"]["claimed_by"] == "turn-1"

    second = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-2", now=T0 + 1
    )
    assert second["status"] == "busy"
    assert second["item"]["fence"] == 1

    adopted = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-2", now=T0 + consumer.DEFAULT_CLAIM_TTL_S + 1
    )
    assert adopted["status"] == "claimed"
    assert adopted["fence"] == 2  # the dead claim was adopted, the fence moved


def test_a_stale_consumer_is_refused_on_complete_and_fail(tmp_path):
    state = tmp_path / "state"
    consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-1", event_state="landed", now=T0,
    )
    stale = consumer.claim_action(state, "verify:att-bee-1", consumer="turn-1", now=T0)
    fresh = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-2",
        now=T0 + consumer.DEFAULT_CLAIM_TTL_S + 1,
    )
    assert stale["fence"] == 1
    assert fresh["fence"] == 2

    with pytest.raises(consumer.ActionQueueError) as complete_exc:
        consumer.complete_action(
            state, "verify:att-bee-1", fence=stale["fence"], result={"ok": True},
            now=T0 + consumer.DEFAULT_CLAIM_TTL_S + 2,
        )
    assert complete_exc.value.code == "stale_fence"

    with pytest.raises(consumer.ActionQueueError) as fail_exc:
        consumer.fail_action(
            state, "verify:att-bee-1", fence=stale["fence"], error="late failure",
            now=T0 + consumer.DEFAULT_CLAIM_TTL_S + 2,
        )
    assert fail_exc.value.code == "stale_fence"

    done = consumer.complete_action(
        state, "verify:att-bee-1", fence=fresh["fence"], result={"ok": True},
        now=T0 + consumer.DEFAULT_CLAIM_TTL_S + 2,
    )
    assert done["status"] == "done"
    item = read_action(state, "verify:att-bee-1")
    assert item["status"] == "done"
    assert item["result"] == {"ok": True}
    assert item["claimed_by"] == ""


def test_fail_releases_for_a_bounded_retry_then_marks_failed(tmp_path):
    state = tmp_path / "state"
    consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-1", event_state="landed", now=T0,
    )

    first = consumer.claim_action(state, "verify:att-bee-1", consumer="turn-1", now=T0)
    released = consumer.fail_action(
        state, "verify:att-bee-1", fence=first["fence"], error="coordinator busy",
        now=T0, retry_backoff_s=30.0,
    )
    assert released["status"] == "pending"
    assert released["retry_after"]
    item = read_action(state, "verify:att-bee-1")
    assert item["status"] == "pending"
    assert item["next_attempt_at"] == released["retry_after"]

    early = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-2", now=T0 + 1
    )
    assert early["status"] == "backoff"  # the released item waits out its backoff

    second = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-2", now=T0 + 31
    )
    assert second["fence"] == 2
    exhausted = consumer.fail_action(
        state, "verify:att-bee-1", fence=second["fence"], error="still busy",
        now=T0 + 31,
    )
    assert exhausted["status"] == "failed"
    assert exhausted["exhausted"] is True
    item = read_action(state, "verify:att-bee-1")
    assert item["status"] == "failed"

    refused = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-3", now=T0 + 32
    )
    assert refused["status"] == "failed"


def test_enqueue_is_idempotent_for_the_same_key_and_refuses_conflicts(tmp_path):
    state = tmp_path / "state"
    first = consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-1", event_state="landed", now=T0,
    )
    assert first["created"] is True

    again = consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-2", event_state="landed", now=T0 + 1,
    )
    assert again["created"] is False
    assert again["duplicate"] is True
    assert again["item"]["event_id"] == "evt-1"  # the first decision stands

    with pytest.raises(consumer.ActionQueueError) as conflict:
        consumer.enqueue_action(
            state, "verify:att-bee-1", kind="verify", attempt_id="att-other",
            event_id="evt-3", event_state="landed", now=T0 + 2,
        )
    assert conflict.value.code == "action_conflict"

    with pytest.raises(consumer.ActionQueueError) as kind:
        consumer.enqueue_action(
            state, "review:att-bee-1", kind="review", attempt_id="att-bee-1",
            now=T0 + 2,
        )
    assert kind.value.code == "action_kind_invalid"

    claim = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-1", now=T0 + 3
    )
    done = consumer.complete_action(
        state, "verify:att-bee-1", fence=claim["fence"], result={"ok": True}, now=T0 + 3
    )
    assert done["status"] == "done"
    resurrect = consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-4", event_state="landed", now=T0 + 4,
    )
    assert resurrect["created"] is False
    assert resurrect["item"]["status"] == "done"  # done work is never resurrected


def test_actions_summary_counts_by_status(tmp_path):
    state = tmp_path / "state"
    consumer.enqueue_action(
        state, "verify:att-1", kind="verify", attempt_id="att-1", now=T0
    )
    consumer.enqueue_action(
        state, "process-completion:att-2", kind="process-completion",
        attempt_id="att-2", now=T0,
    )
    claim = consumer.claim_action(state, "verify:att-1", consumer="turn-1", now=T0)
    consumer.complete_action(state, "verify:att-1", fence=claim["fence"], now=T0 + 1)

    summary = consumer.actions_summary(state)
    assert summary["counts"] == {"total": 2, "pending": 1, "claimed": 0, "done": 1,
                                 "failed": 0}


# ---------------------------------------------------------------------------
# Coexistence: the bundled store and the merged #54 queue in one state dir
# ---------------------------------------------------------------------------


def test_the_bundled_store_and_the_merged_queue_coexist_in_one_state_dir(tmp_path):
    state = tmp_path / "state"

    # The merged coordinator action queue (#54) writes its own records into
    # coordinator/actions; nothing else may live there.
    merged = ca.enqueue(
        state, "act_merge_1", action_key="merge-1", kind="merge", repo="org/a",
        now=T0,
    )
    assert merged["schema"] == "bundlebees.coordinator-actions/1"
    assert merged["status"] == "queued"

    # The composition's bundled store writes its own shape beside it.
    created = consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_state="landed", now=T0,
    )
    assert created["created"] is True
    stored = read_action(state, "verify:att-bee-1")
    assert stored["schema"] == "rizzstack.coordinator-action/1"
    assert stored["status"] == "pending"

    # Each store reads exactly its own records: the merged queue's reader
    # refuses foreign records, so a composition item in its directory would
    # corrupt it — the separate directories keep both readable.
    assert [record["action_id"] for record in ca.list_actions(state)] == ["act_merge_1"]
    assert consumer.actions_summary(state)["counts"]["total"] == 1

    # Both entry points claim and complete their own item without interference.
    merged_claim = ca.claim(state, "act_merge_1", owner="bee-a", now=T0 + 1)
    composed_claim = consumer.claim_action(
        state, "verify:att-bee-1", consumer="turn-1", now=T0 + 1
    )
    assert merged_claim["fence_seq"] == 1
    assert composed_claim["fence"] == 1

    ca.complete(state, "act_merge_1", merged_claim["fence_seq"], now=T0 + 2)
    consumer.complete_action(
        state, "verify:att-bee-1", fence=composed_claim["fence"], now=T0 + 2
    )

    assert ca.load(state, "act_merge_1")["status"] == "done"
    assert read_action(state, "verify:att-bee-1")["status"] == "done"

    # The fence counters never cross: the composition's claims mint nothing in
    # the merged queue's durable counter, and its locks are its own.
    assert ca.fence_value(state) == 1
    assert consumer.actions_summary(state)["counts"] == {
        "total": 1, "pending": 0, "claimed": 0, "done": 1, "failed": 0,
    }


# ---------------------------------------------------------------------------
# The explicit adapter: conformant records through the merged queue
# ---------------------------------------------------------------------------


def test_the_adapter_enqueues_conformant_records_into_the_merged_queue(tmp_path):
    state = tmp_path / "state"

    outcome = consumer.coordinator_actions_enqueue(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_id="evt-1", event_state="landed", job_id="job-1", task_id="task-1",
        dedupe_key="att-bee-1:landed", now=T0,
    )
    assert outcome["created"] is True
    assert outcome["action_id"] == "verify_att-bee-1"  # colon-free: the queue's grammar
    assert outcome["action_key"] == "verify:att-bee-1"  # the key is kept verbatim

    record = ca.load(state, "verify_att-bee-1")
    assert record["schema"] == "bundlebees.coordinator-actions/1"
    assert record["action_id"] == "verify_att-bee-1"
    assert record["action_key"] == "verify:att-bee-1"
    assert record["kind"] == "verify"
    assert record["status"] == "queued"
    assert record["fence_seq"] == 0
    assert record["workflow_ref"] == ""  # the legacy unpinned shape, field present
    assert record["payload"]["attempt_id"] == "att-bee-1"
    assert record["payload"]["dedupe_key"] == "att-bee-1:landed"

    replay = consumer.coordinator_actions_enqueue(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1", now=T0 + 1
    )
    assert replay["created"] is False
    assert replay["duplicate"] is True
    assert replay["item"] == record  # the first decision stands

    # The merged queue's own machinery claims and completes the adapted item.
    claim = ca.claim(state, "verify_att-bee-1", owner="turn-1", now=T0 + 2)
    assert claim["fence_seq"] == 1
    done = ca.complete(state, "verify_att-bee-1", claim["fence_seq"], now=T0 + 3)
    assert done["status"] == "done"

    resurrect = consumer.coordinator_actions_enqueue(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1", now=T0 + 4
    )
    assert resurrect["created"] is False
    assert resurrect["item"]["status"] == "done"  # done work is never resurrected

    with pytest.raises(consumer.ActionQueueError) as mismatch:
        consumer.coordinator_actions_enqueue(
            state, "verify:someone-else", kind="verify", attempt_id="att-bee-1",
            now=T0 + 5,
        )
    assert mismatch.value.code == "action_id_mismatch"


def test_the_handler_runs_end_to_end_through_the_merged_queue_adapter(tmp_path):
    state = tmp_path / "state"
    land(state, canonical_event())

    result = consumer.consume_completions(
        state, now=T0, enqueue=consumer.coordinator_actions_enqueue
    )
    assert result["state"] == "acked"
    assert result["result"]["actions"] == [
        "verify_att-bee-1",
        "process-completion_att-bee-1",
    ]

    # The merged queue reads exactly two conformant records of its own schema.
    records = ca.list_actions(state)
    assert [record["action_id"] for record in records] == [
        "process-completion_att-bee-1",
        "verify_att-bee-1",
    ]
    assert all(record["schema"] == "bundlebees.coordinator-actions/1" for record in records)
    assert all(record["status"] == "queued" for record in records)
    assert all(record["action_key"].startswith(("verify:", "process-completion:")) for record in records)

    ledger = json.loads(
        (state / "coordinator" / "completions" / "att-bee-1:landed.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["actions"] == ["verify_att-bee-1", "process-completion_att-bee-1"]

    # A duplicate delivery replays the recorded decision, not new items.
    again = consumer.consume_completions(
        state, now=T0 + 1, enqueue=consumer.coordinator_actions_enqueue
    )
    assert again["state"] == "duplicate"
    assert again["result"]["actions"] == [
        "verify_att-bee-1",
        "process-completion_att-bee-1",
    ]
    assert len(ca.list_actions(state)) == 2


# ---------------------------------------------------------------------------
# Claim-state discipline: an unclaimed or failed item cannot be completed
# ---------------------------------------------------------------------------


def test_complete_and_fail_refuse_an_item_that_is_not_claimed(tmp_path):
    state = tmp_path / "state"
    consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_state="landed", now=T0,
    )

    # A pending (never claimed) item: even the trivially matching fence 0
    # completes or fails nothing.
    with pytest.raises(consumer.ActionQueueError) as pending_complete:
        consumer.complete_action(state, "verify:att-bee-1", fence=0, now=T0)
    assert pending_complete.value.code == "not_claimed"

    with pytest.raises(consumer.ActionQueueError) as pending_fail:
        consumer.fail_action(state, "verify:att-bee-1", fence=0, error="premature", now=T0)
    assert pending_fail.value.code == "not_claimed"
    assert read_action(state, "verify:att-bee-1")["status"] == "pending"

    # A released item: the former owner's still-current fence completes nothing.
    claim = consumer.claim_action(state, "verify:att-bee-1", consumer="turn-1", now=T0)
    released = consumer.fail_action(
        state, "verify:att-bee-1", fence=claim["fence"], error="coordinator busy",
        now=T0, retry_backoff_s=0.0,
    )
    assert released["status"] == "pending"
    with pytest.raises(consumer.ActionQueueError) as released_complete:
        consumer.complete_action(
            state, "verify:att-bee-1", fence=claim["fence"], now=T0 + 1
        )
    assert released_complete.value.code == "not_claimed"

    # A terminally failed item: no owner can resurrect it through completion.
    second = consumer.claim_action(state, "verify:att-bee-1", consumer="turn-2", now=T0 + 2)
    exhausted = consumer.fail_action(
        state, "verify:att-bee-1", fence=second["fence"], error="still busy", now=T0 + 2
    )
    assert exhausted["status"] == "failed"
    with pytest.raises(consumer.ActionQueueError) as failed_complete:
        consumer.complete_action(
            state, "verify:att-bee-1", fence=second["fence"], now=T0 + 3
        )
    assert failed_complete.value.code == "not_claimed"
    assert read_action(state, "verify:att-bee-1")["status"] == "failed"


# ---------------------------------------------------------------------------
# Store outages surface as the queue's typed error
# ---------------------------------------------------------------------------


def test_a_store_outage_surfaces_as_the_queue_error(monkeypatch, tmp_path):
    state = tmp_path / "state"

    def disk_full(path, value):
        raise OSError(28, "No space left on device")

    # The create-only publish inside enqueue_action fails as the queue's error.
    with monkeypatch.context() as mp:
        mp.setattr(consumer, "_create_only_json", disk_full)
        with pytest.raises(consumer.ActionQueueError) as enqueue_exc:
            consumer.enqueue_action(
                state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
                event_state="landed", now=T0,
            )
        assert enqueue_exc.value.code == "queue_unavailable"
    assert not action_path(state, "verify:att-bee-1").exists()

    # The completion's rewrite fails as the queue's error while the store is
    # unwritable; the claim itself succeeded before the outage.
    consumer.enqueue_action(
        state, "verify:att-bee-1", kind="verify", attempt_id="att-bee-1",
        event_state="landed", now=T0,
    )
    claim = consumer.claim_action(state, "verify:att-bee-1", consumer="turn-1", now=T0)
    assert claim["status"] == "claimed"
    with monkeypatch.context() as mp:
        mp.setattr(consumer, "_write_json", disk_full)
        with pytest.raises(consumer.ActionQueueError) as complete_exc:
            consumer.complete_action(
                state, "verify:att-bee-1", fence=claim["fence"], now=T0 + 1
            )
        assert complete_exc.value.code == "queue_unavailable"

    # Once the store is writable again the same completion succeeds.
    done = consumer.complete_action(
        state, "verify:att-bee-1", fence=claim["fence"], now=T0 + 2
    )
    assert done["status"] == "done"


# ---------------------------------------------------------------------------
# Narrow crash windows inside the handler
# ---------------------------------------------------------------------------


def test_a_crash_between_the_two_enqueues_still_yields_one_action_set(tmp_path):
    state = tmp_path / "state"
    land(state, canonical_event())

    def crashing_after_verify(event_payload):
        classified = consumer.classify_completion(event_payload)
        attempt = str(classified["attempt_id"])
        consumer.enqueue_action(
            state, consumer.action_id_for("verify", attempt), kind="verify",
            attempt_id=attempt, event_state=str(classified["state"]), now=T0,
        )
        raise RuntimeError("coordinator died between the two enqueues")

    first = consumer.consume_completions(
        state, now=T0, handler=crashing_after_verify, max_attempts=2, retry_backoff_s=0
    )
    assert first["state"] == "retryable"
    assert action_path(state, "verify:att-bee-1").exists()
    assert not action_path(state, "process-completion:att-bee-1").exists()

    second = consumer.consume_completions(state, now=T0 + 1, max_attempts=2, retry_backoff_s=0)
    assert second["state"] == "acked"
    assert second["result"]["duplicate"] is False
    # The first effect is reused, not duplicated; the missing one is created.
    assert second["result"]["actions"] == [
        "verify:att-bee-1",
        "process-completion:att-bee-1",
    ]

    summary = consumer.actions_summary(state)
    assert summary["counts"] == {"total": 2, "pending": 2, "claimed": 0, "done": 0,
                                 "failed": 0}
    assert len(list((state / "coordinator" / "completions").glob("*.json"))) == 1


def test_a_crash_before_the_ledger_still_yields_one_action_set(tmp_path):
    state = tmp_path / "state"
    land(state, canonical_event())

    def effects_then_crash(event_payload):
        classified = consumer.classify_completion(event_payload)
        attempt = str(classified["attempt_id"])
        state_name = str(classified["state"])
        kinds = (
            ["verify", "process-completion"]
            if state_name in consumer.VERIFY_ELIGIBLE_STATES
            else ["process-completion"]
        )
        for kind in kinds:
            consumer.enqueue_action(
                state, consumer.action_id_for(kind, attempt), kind=kind,
                attempt_id=attempt, event_state=state_name, now=T0,
            )
        raise RuntimeError("coordinator died before the ledger")

    first = consumer.consume_completions(
        state, now=T0, handler=effects_then_crash, max_attempts=2, retry_backoff_s=0
    )
    assert first["state"] == "retryable"
    # Both effects are durable; the accepted decision is not recorded yet.
    assert len(list((state / "coordinator" / "completions").glob("*.json"))) == 0

    second = consumer.consume_completions(state, now=T0 + 1, max_attempts=2, retry_backoff_s=0)
    assert second["state"] == "acked"
    assert second["result"]["duplicate"] is False
    assert second["result"]["completion_created"] is True
    assert second["result"]["actions"] == [
        "verify:att-bee-1",
        "process-completion:att-bee-1",
    ]

    summary = consumer.actions_summary(state)
    assert summary["counts"] == {"total": 2, "pending": 2, "claimed": 0, "done": 0,
                                 "failed": 0}
    assert len(list((state / "coordinator" / "completions").glob("*.json"))) == 1
