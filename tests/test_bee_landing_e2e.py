"""Deterministic end-to-end proof of one bee landing.

Three attempts (A/B/C) hold disjoint scheduler leases.  A lands while B and C
stay claimed.  The landing is recorded once in the durable outbox, delivered to
one subscriber inbox, and consumed once as an accepted decision.  The landing
also preempts one planning item at its durable checkpoint so a verification
task can be claimed; afterwards the preempted item resumes from that checkpoint.

Only files under ``tmp_path`` are written.  The handler is a fake and no model,
worker, network or production code is touched.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from bundlebees_plugin import bee_events, event_consumer, work_scheduler

SUBSCRIBER = "coordinator"
LEASE_A = "src/attempt_a.py"
LEASE_B = "src/attempt_b.py"
LEASE_C = "src/attempt_c.py"
LEASE_VERIFY = "tests/verify_landing.py"
LEASE_PRIOR = "docs/prior_plan.md"
ARTIFACT = hashlib.sha256(b"artifact-a").hexdigest()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_files(directory) -> list:
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_bee_landing_end_to_end(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    attempt_a = {"attempt_id": "att-a", "state": "landed", "transition_id": "tr-land-a"}
    attempt_b = {"attempt_id": "att-b", "state": "running", "transition_id": "tr-run-b"}
    attempt_c = {"attempt_id": "att-c", "state": "running", "transition_id": "tr-run-c"}

    work = {
        "ws_attempt_a": {
            "bee": "bee-a",
            "task": "task-a",
            "lease": LEASE_A,
            "note": "A checkpoint",
        },
        "ws_attempt_b": {
            "bee": "bee-b",
            "task": "task-b",
            "lease": LEASE_B,
            "note": "B checkpoint",
        },
        "ws_attempt_c": {
            "bee": "bee-c",
            "task": "task-c",
            "lease": LEASE_C,
            "note": "C checkpoint",
        },
    }

    prior = {
        "work_id": "ws_prior",
        "bee": "planner",
        "task": "prior-plan",
        "lease": LEASE_PRIOR,
        "note": "prior checkpoint",
    }

    for offset, (work_id, record) in enumerate(work.items()):
        work_scheduler.add_item(
            state,
            work_id=work_id,
            title=f"{record['task']} for {record['bee']}",
            kind="research",
            priority=5,
            lease_files=[record["lease"]],
            now=1_700_000_000 + offset,
        )

    work_scheduler.add_item(
        state,
        work_id=prior["work_id"],
        title=f"{prior['task']} for {prior['bee']}",
        kind="research",
        priority=9,
        lease_files=[prior["lease"]],
        now=1_700_000_050,
    )

    work_scheduler.add_item(
        state,
        work_id="ws_verify_landing",
        title="Verify the landing",
        kind="tests",
        priority=0,
        lease_files=[LEASE_VERIFY],
        now=1_700_000_100,
    )

    for offset, (work_id, record) in enumerate(work.items()):
        claimed = work_scheduler.claim(
            state, work_id, record["bee"], max_active=4, now=1_700_000_200 + offset
        )
        assert claimed["status"] == "claimed"
        assert claimed["owner"] == record["bee"]
        assert claimed["lease_files"] == [record["lease"]]
        checkpointed = work_scheduler.checkpoint(
            state,
            work_id,
            record["note"],
            evidence=[f"{record['task']}:durable"],
            now=1_700_000_300 + offset,
        )
        assert checkpointed["checkpoint"]["note"] == record["note"]

    prior_claimed = work_scheduler.claim(
        state, prior["work_id"], prior["bee"], max_active=5, now=1_700_000_250
    )
    assert prior_claimed["status"] == "claimed"
    assert prior_claimed["owner"] == prior["bee"]
    prior_checkpointed = work_scheduler.checkpoint(
        state,
        prior["work_id"],
        prior["note"],
        evidence=[f"{prior['task']}:durable"],
        now=1_700_000_350,
    )
    assert prior_checkpointed["checkpoint"]["note"] == prior["note"]

    items = {item["work_id"]: item for item in work_scheduler.list_items(state)}
    lease_sets = {work_id: set(item["lease_files"]) for work_id, item in items.items()}
    lease_ids = list(lease_sets)
    for index, left in enumerate(lease_ids):
        for right in lease_ids[index + 1:]:
            assert lease_sets[left].isdisjoint(lease_sets[right])
    assert items["ws_verify_landing"]["status"] == "pending"

    with pytest.raises(work_scheduler.SchedulerError) as blocked:
        work_scheduler.claim(
            state, "ws_verify_landing", "verifier", max_active=4, now=1_700_000_400
        )
    assert blocked.value.code == "capacity_exhausted"

    terminal_at = time.perf_counter()
    terminal_wall = time.time()
    event = bee_events.build_event(
        attempt_a,
        feature_id="BEE-LAND",
        job_id="job-land",
        task_id="task-a",
        coordinator=SUBSCRIBER,
        lease_sha256=_sha(LEASE_A),
        artifact_sha256=ARTIFACT,
        reference="refs/heads/feat/bee-a",
        backend="fake-worker",
        now=1_700_000_500,
    )
    first_record = bee_events.record_landing(state, event)
    replay_record = bee_events.record_landing(state, event)
    event_at = time.perf_counter()
    event_wall = time.time()

    assert first_record["created"] is True
    assert replay_record["created"] is False
    outbox = _json_files(state / "events" / "outbox")
    assert len(outbox) == 1
    assert outbox[0].stem == event["id"]
    outbox_ids = {path.stem for path in outbox}
    for other_attempt, other_transition in (
        (attempt_b["attempt_id"], attempt_b["transition_id"]),
        (attempt_c["attempt_id"], attempt_c["transition_id"]),
    ):
        assert (
            bee_events.event_id_for(other_attempt, "landed", other_transition)
            not in outbox_ids
        )

    landed_item = work_scheduler.complete(
        state, "ws_attempt_a", owner="bee-a", now=1_700_000_550
    )
    assert landed_item["status"] == "done"

    before_preempt = work_scheduler.list_items(state)
    preempt = work_scheduler.preempt_for_event(
        state,
        {"kind": "landing", "max_active": 3, "lease_files": [LEASE_A]},
        now=1_700_000_600,
    )
    assert preempt["action"] == "checkpoint"
    assert preempt["safe"] is True
    assert preempt["interrupts_non_idempotent"] is False
    assert preempt["then"] == "release"
    assert preempt["work_id"] == prior["work_id"]
    assert preempt["owner"] == prior["bee"]
    assert preempt["checkpoint"]["note"] == prior["note"]
    assert work_scheduler.list_items(state) == before_preempt

    with pytest.raises(work_scheduler.SchedulerError) as still_blocked:
        work_scheduler.claim(
            state, "ws_verify_landing", "verifier", max_active=3, now=1_700_000_650
        )
    assert still_blocked.value.code == "capacity_exhausted"

    released = work_scheduler.release(
        state, prior["work_id"], owner=prior["bee"], now=1_700_000_700
    )
    assert released["status"] == "pending"
    assert released["owner"] == ""
    assert released["checkpoint"]["note"] == prior["note"]

    verified = work_scheduler.claim(
        state, "ws_verify_landing", "verifier", max_active=3, now=1_700_000_800
    )
    verification_at = time.perf_counter()
    verification_wall = time.time()
    assert verified["status"] == "claimed"
    assert verified["owner"] == "verifier"
    work_scheduler.checkpoint(
        state,
        "ws_verify_landing",
        "landing verified",
        evidence=[event["id"]],
        now=1_700_000_900,
    )
    work_scheduler.complete(
        state, "ws_verify_landing", owner="verifier", now=1_700_001_000
    )

    resumed = work_scheduler.resume(state, prior["work_id"], now=1_700_001_100)
    assert resumed["status"] == "pending"
    assert resumed["reason"] == "ready_to_claim"
    assert resumed["checkpoint"]["note"] == prior["note"]
    assert resumed["checkpoint"]["evidence"] == [f"{prior['task']}:durable"]

    reclaimed = work_scheduler.claim(
        state, prior["work_id"], prior["bee"], max_active=4, now=1_700_001_200
    )
    assert reclaimed["status"] == "claimed"
    assert reclaimed["owner"] == prior["bee"]
    assert reclaimed["checkpoint"]["note"] == prior["note"]

    final_items = {item["work_id"]: item for item in work_scheduler.list_items(state)}
    assert final_items["ws_attempt_a"]["status"] == "done"
    for work_id in ("ws_attempt_b", "ws_attempt_c"):
        assert final_items[work_id]["status"] == "claimed"
        assert final_items[work_id]["owner"] == work[work_id]["bee"]
        assert final_items[work_id]["checkpoint"]["note"] == work[work_id]["note"]
    assert final_items["ws_verify_landing"]["status"] == "done"
    assert final_items[prior["work_id"]]["status"] == "claimed"

    bee_events.subscribe(
        state, {"identity": SUBSCRIBER, "file": "coordinator-inbox.jsonl"}
    )
    delivery = bee_events.deliver_pending(state)
    delivery_at = time.perf_counter()
    delivery_wall = time.time()
    assert delivery["delivered"] == 1
    assert delivery["failed"] == 0
    inbox = _json_files(state / "events" / "inbox" / SUBSCRIBER)
    assert len(inbox) == 1
    assert inbox[0].stem == event["id"]
    delivered_event = _read(inbox[0])
    assert delivered_event["event_id"] == event["id"]
    assert delivered_event["subscriber"] == SUBSCRIBER
    assert delivered_event["event"]["id"] == event["id"]
    assert delivered_event["attempts"] == 1

    handled = []

    def handler(payload):
        handled.append(payload)
        return {"accepted": True, "event_id": payload.get("id") or payload["event_id"]}

    real_ack = event_consumer._ack

    def crash_between_handler_and_ack(*args, **kwargs):
        raise RuntimeError("simulated crash after the handler, before the ack")

    monkeypatch.setattr(event_consumer, "_ack", crash_between_handler_and_ack)
    crashed = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="coordinator-a"
    )
    assert crashed["state"] == "retryable"
    assert crashed["attempts"] == 1
    assert crashed["duplicate"] is False
    assert len(handled) == 1
    assert _json_files(state / "events" / "acks" / SUBSCRIBER) == []

    monkeypatch.setattr(event_consumer, "_ack", real_ack)
    acked = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="coordinator-b"
    )
    ack_at = time.perf_counter()
    ack_wall = time.time()
    assert acked["state"] == "acked"
    assert acked["duplicate"] is False
    assert acked["attempts"] == 2
    assert acked["result"]["accepted"] is True
    assert len(handled) == 2
    acks = _json_files(state / "events" / "acks" / SUBSCRIBER)
    assert len(acks) == 1
    ack_record = _read(acks[0])
    assert ack_record["event_id"] == event["id"]
    assert ack_record["subscriber"] == SUBSCRIBER
    assert ack_record["attempts"] == 2
    assert ack_record["result"] == {"accepted": True, "event_id": event["id"]}

    duplicate_delivery = bee_events.deliver_pending(state)
    assert duplicate_delivery["delivered"] == 0
    assert duplicate_delivery["skipped"] == 1
    duplicate_handle = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="coordinator-c"
    )
    assert duplicate_handle["state"] == "duplicate"
    assert duplicate_handle["duplicate"] is True
    assert len(handled) == 2
    assert [(call.get("id") or call.get("event_id")) for call in handled] == [event["id"], event["id"]]

    assert len(_json_files(state / "events" / "outbox")) == 1
    assert len(_json_files(state / "events" / "inbox" / SUBSCRIBER)) == 1
    assert len(_json_files(state / "events" / "acks" / SUBSCRIBER)) == 1
    summary = event_consumer.inbox_summary(state, SUBSCRIBER)
    assert summary["total"] == 1
    assert summary["acked"] == 1

    latencies = {
        "terminal_to_event_ms": round((event_at - terminal_at) * 1000.0, 3),
        "event_to_delivery_ms": round((delivery_at - event_at) * 1000.0, 3),
        "delivery_to_ack_ms": round((ack_at - delivery_at) * 1000.0, 3),
        "landing_to_verification_eligibility_ms": round(
            (verification_at - terminal_at) * 1000.0, 3
        ),
    }
    for name, value in latencies.items():
        assert value >= 0.0, name

    timestamps = {
        "terminal": terminal_wall,
        "event": event_wall,
        "delivery": delivery_wall,
        "ack": ack_wall,
        "verification_eligibility": verification_wall,
    }
    with capsys.disabled():
        print("LANDING_TIMESTAMPS " + json.dumps(timestamps, sort_keys=True))
        print("LANDING_LATENCIES_MS " + json.dumps(latencies, sort_keys=True))
