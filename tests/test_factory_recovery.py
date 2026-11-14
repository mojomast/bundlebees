"""Concurrency and crash recovery for the factory driver.

Every case runs against temporary state.  Worker ticks are fakes unless a test
explicitly starts a disposable driver process; no model or external network is
used.  The tests count durable effects (scheduler items, action records, inbox
acks and worker ticks) before and after duplicates and restarts so the
acceptance evidence is the state, not a log line.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bundlebees_plugin import (
    bee_events,
    event_consumer,
    factory_driver,
    queue as queue_mod,
    work_scheduler,
)
from tests.test_event_consumer import RecordingGateway

ROOT = Path(__file__).resolve().parents[1]
RAW_SESSION = "hermes/coordinator session:@A"


def _subscribe(state: Path, identity: str = "coordinator") -> None:
    bee_events.subscribe(state, {"identity": identity, "file": f"{identity}-inbox.jsonl"})


def _event(
    event_id: str,
    *,
    job_id: str,
    attempt_id: str,
    session: str = RAW_SESSION,
    identity: str = "coordinator",
) -> dict:
    return {
        "event_id": event_id,
        "id": event_id,
        "type": bee_events.EVENT_TYPE,
        "source": "test",
        "data": {
            "job_id": job_id,
            "task_id": "T1",
            "attempt_id": attempt_id,
            "state": "landed",
            "coordinator": {"identity": identity, "session": session},
            "lease": {"assigned_files": ["src/a.py"]},
        },
    }


def _publish(state: Path, event: dict, subscriber: str = "coordinator") -> None:
    event_consumer.publish_event(state, event, subscriber=subscriber)


def _files(directory: Path, pattern: str = "*.json") -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.rglob(pattern) if path.is_file())


def _counts(state: Path) -> dict[str, int]:
    """The four effect counters used by the recovery evidence assertions."""

    return {
        "scheduler_items": len(work_scheduler.list_items(state)),
        "actions": len(_files(state / "factory" / "actions")),
        "acks": len(
            [path for path in _files(state / "events" / "acks") if ".replays" not in path.parts]
        ),
        "inbox": len(_files(state / "events" / "inbox")),
    }


def _mapping(state: Path, work_id: str, job_id: str) -> None:
    directory = state / "factory" / "work-jobs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{work_id}.json").write_text(
        json.dumps({"work_id": work_id, "job_id": job_id, "attempt_id": ""}),
        encoding="utf-8",
    )


def _no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)


def test_a_landing_verifies_while_two_claimed_workers_stay_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _subscribe(state)
    _no_gateway(monkeypatch)

    work_scheduler.add_item(
        state, work_id="ws_b", title="worker B", kind="feature", priority=1,
        lease_files=["src/b.py"],
    )
    work_scheduler.add_item(
        state, work_id="ws_c", title="worker C", kind="feature", priority=2,
        lease_files=["src/c.py"],
    )
    work_scheduler.claim(state, "ws_b", "worker-b")
    work_scheduler.claim(state, "ws_c", "worker-c")

    _publish(state, _event("evt-a", job_id="job-a", attempt_id="att-a"))
    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        return {"status": "ok", "action": "ready_for_review"}

    driver = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    )
    result = driver.run_pass()

    assert result["events"][0]["state"] == "acked"
    assert result["events"][0]["work_state"] == "done"
    assert calls == ["job-a"]
    verify = next(
        item for item in work_scheduler.list_items(state) if item["work_id"] == "ws_verify_att-a"
    )
    assert verify["status"] == "done"
    active = {
        item["work_id"]: (item["status"], item["owner"])
        for item in work_scheduler.list_items(state)
        if item["work_id"] in {"ws_b", "ws_c"}
    }
    assert active == {"ws_b": ("claimed", "worker-b"), "ws_c": ("claimed", "worker-c")}

    status = driver.wake.status(RAW_SESSION)
    assert status["session"] == RAW_SESSION
    wake_files = _files(state / "factory" / "wakes")
    assert wake_files
    assert RAW_SESSION not in {path.name for path in wake_files}
    stored = json.loads(wake_files[0].read_text(encoding="utf-8"))
    assert stored["session"] == RAW_SESSION
    assert result["wake_delivery"][0]["session"] == RAW_SESSION
    assert result["wake_delivery"][0]["pending"] is True


def test_an_arrival_during_a_busy_turn_is_deferred_then_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _subscribe(state)
    _publish(state, _event("evt-busy", job_id="job-busy", attempt_id="att-busy"))
    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        return {"status": "ok", "action": "ready_for_review"}

    driver = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    )
    driver.wake.request_wake(RAW_SESSION, subscriber="coordinator")
    active = driver.wake.begin_turn(RAW_SESSION)
    assert active["started"] is True

    first = driver.run_pass()
    assert first["events"][0]["state"] == "acked"
    assert first["events"][0]["deferred"] is True
    assert first["wake_delivery"] == []
    assert driver.wake.status(RAW_SESSION)["pending"] is True

    driver.wake.finish_turn(RAW_SESSION, active["turn"])
    delivered: list[dict] = []

    def notify(event, **kwargs):
        delivered.append({"event": event, "kwargs": kwargs})
        return {"event_id": event["event_id"], "delivered": True}

    monkeypatch.setattr(factory_driver.hermes_adapter, "notify_coordinator", notify)
    second = driver.run_pass()

    assert second["events"][0]["state"] == "duplicate"
    assert second["wake_delivery"][0]["delivered"] is True
    assert second["wake_delivery"][0]["turn_finished"] is True
    assert len(delivered) == 1
    assert delivered[0]["kwargs"]["session"] == RAW_SESSION
    assert delivered[0]["kwargs"]["subscriber"]["identity"] == "coordinator"
    assert driver.wake.status(RAW_SESSION)["pending"] is False
    assert calls == ["job-busy"]


def test_duplicate_delivery_does_not_duplicate_verification_or_ack_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _subscribe(state)
    _no_gateway(monkeypatch)
    event = _event("evt-dup", job_id="job-dup", attempt_id="att-dup")
    _publish(state, event)
    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        return {"status": "ok", "action": "ready_for_review"}

    driver = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    )

    first = driver.run_pass()
    assert first["events"][0]["state"] == "acked"
    before = _counts(state)
    assert before == {"scheduler_items": 1, "actions": 1, "acks": 1, "inbox": 1}
    assert calls == ["job-dup"]

    duplicate_inbox = state / "events" / "inbox" / "coordinator" / "evt-dup-copy.json"
    duplicate_inbox.write_text(json.dumps(event), encoding="utf-8")

    second = driver.run_pass()
    third = driver.run_pass()

    assert second["events"][0]["state"] == "duplicate"
    assert second["events"][0]["work_state"] == "duplicate_skipped"
    assert third["events"][0]["state"] == "duplicate"
    assert calls == ["job-dup"]
    assert _counts(state)["scheduler_items"] == before["scheduler_items"]
    assert _counts(state)["actions"] == before["actions"]
    assert _counts(state)["acks"] == before["acks"]
    status = next(
        item["status"] for item in work_scheduler.list_items(state)
        if item["work_id"] == "ws_verify_att-dup"
    )
    assert status == "done"


def test_a_failed_worker_is_bounded_and_isolated_from_ready_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _no_gateway(monkeypatch)
    work_scheduler.add_item(state, work_id="ws_bad", title="bad", kind="tests", priority=0)
    work_scheduler.add_item(state, work_id="ws_good", title="good", kind="tests", priority=1)
    _mapping(state, "ws_bad", "job-bad")
    _mapping(state, "ws_good", "job-good")
    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        if job_id == "job-bad":
            raise RuntimeError("worker exploded")
        return {"status": "ok", "action": "ready_for_review"}

    driver = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False, max_work_attempts=2,
    )
    first = driver.run_pass()

    good = next(item for item in work_scheduler.list_items(state) if item["work_id"] == "ws_good")
    bad = next(item for item in work_scheduler.list_items(state) if item["work_id"] == "ws_bad")
    assert good["status"] == "done"
    assert bad["status"] == "claimed"
    assert first["failures"][0]["code"] == "RuntimeError"
    assert first["failures"][0]["fix"]
    assert calls.count("job-good") == 1

    second = driver.run_pass()
    bad = next(item for item in work_scheduler.list_items(state) if item["work_id"] == "ws_bad")
    assert bad["status"] == "pending"
    assert second["failures"][0]["exhausted"] is True
    assert calls.count("job-bad") == 2

    third = driver.run_pass()
    assert calls.count("job-bad") == 2
    assert next(
        item["status"] for item in work_scheduler.list_items(state) if item["work_id"] == "ws_good"
    ) == "done"
    assert not any(call == "job-bad" for call in calls[3:])


def test_real_driver_process_kill_before_ack_recovers_without_duplicate_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _subscribe(state)
    _no_gateway(monkeypatch)
    event = _event("evt-kill", job_id="job-kill", attempt_id="att-kill")
    _publish(state, event)

    child = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from bundlebees_plugin import factory_driver

real = factory_driver.deterministic_handler

def crash_after_effect(state_dir, event, **kwargs):
    real(state_dir, event, **kwargs)
    os._exit(23)

factory_driver.deterministic_handler = crash_after_effect
state = Path(sys.argv[1])
queue = Path(sys.argv[2])
driver = factory_driver.FactoryDriver(
    state, queue, tick=lambda job_id: {"status": "ok", "action": "ready_for_review"},
    subscribers=["coordinator"], startup=False,
)
driver.run_pass()
"""
    env = os.environ.copy()
    env.pop("HERMES_API_URL", None)
    env.pop("HERMES_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-c", child, str(state), str(tmp_path / "queue.json"), str(ROOT)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 23, proc.stderr

    assert _counts(state) == {
        "scheduler_items": 1, "actions": 1, "acks": 0, "inbox": 1,
    }
    assert next(
        item["status"] for item in work_scheduler.list_items(state)
        if item["work_id"] == "ws_verify_att-kill"
    ) == "pending"

    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        return {"status": "ok", "action": "ready_for_review"}

    restarted = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    ).run_pass()

    assert restarted["events"][0]["state"] == "acked"
    assert restarted["events"][0]["created"] is False
    assert restarted["events"][0]["duplicate_action"] is True
    assert calls == ["job-kill"]
    assert _counts(state) == {
        "scheduler_items": 1, "actions": 1, "acks": 1, "inbox": 1,
    }

    duplicate_inbox = state / "events" / "inbox" / "coordinator" / "evt-kill-copy.json"
    duplicate_inbox.write_text(json.dumps(event), encoding="utf-8")
    again = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    ).run_pass()

    assert again["events"][0]["state"] == "duplicate"
    assert calls == ["job-kill"]
    assert _counts(state) == {
        "scheduler_items": 1, "actions": 1, "acks": 1, "inbox": 2,
    }


def test_real_kill_after_the_scheduler_effect_before_the_action_record_is_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _subscribe(state)
    _no_gateway(monkeypatch)
    _publish(state, _event("evt-kill2", job_id="job-kill2", attempt_id="att-kill2"))

    child = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from bundlebees_plugin import factory_driver

real = factory_driver.work_scheduler.add_item

def crash_after_add(*args, **kwargs):
    real(*args, **kwargs)
    os._exit(24)

factory_driver.work_scheduler.add_item = crash_after_add
state = Path(sys.argv[1])
driver = factory_driver.FactoryDriver(
    state, Path(sys.argv[2]),
    tick=lambda job_id: {"status": "ok", "action": "ready_for_review"},
    subscribers=["coordinator"], startup=False,
)
driver.run_pass()
"""
    env = os.environ.copy()
    env.pop("HERMES_API_URL", None)
    env.pop("HERMES_API_KEY", None)
    proc = subprocess.run(
        [sys.executable, "-c", child, str(state), str(tmp_path / "queue.json"), str(ROOT)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 24, proc.stderr
    assert _counts(state) == {
        "scheduler_items": 1, "actions": 0, "acks": 0, "inbox": 1,
    }

    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        return {"status": "ok", "action": "ready_for_review"}

    recovered = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    ).run_pass()

    assert recovered["events"][0]["state"] == "acked"
    assert recovered["events"][0]["created"] is False
    assert calls == ["job-kill2"]
    assert _counts(state) == {
        "scheduler_items": 1, "actions": 1, "acks": 1, "inbox": 1,
    }


def test_unavailable_gateway_leaves_pending_work_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _subscribe(state)
    _no_gateway(monkeypatch)
    _publish(state, _event("evt-gw", job_id="job-gw", attempt_id="att-gw"))
    work_scheduler.add_item(state, work_id="ws_local", title="local", kind="tests", priority=1)
    _mapping(state, "ws_local", "job-local")
    calls: list[str] = []

    def tick(job_id: str) -> dict:
        calls.append(job_id)
        return {"status": "ok", "action": "ready_for_review"}

    first = factory_driver.FactoryDriver(
        state, tmp_path / "queue.json", tick=tick,
        subscribers=["coordinator"], startup=False,
    ).run_pass()

    assert first["events"][0]["state"] == "acked"
    assert first["wake_delivery"][0]["delivered"] is False
    assert first["wake_delivery"][0]["code"] == "hermes_notification_unavailable"
    assert first["wake_delivery"][0]["pending"] is True
    assert first["wake_delivery"][0]["fix"]
    assert next(
        item["status"] for item in work_scheduler.list_items(state) if item["work_id"] == "ws_local"
    ) == "done"
    assert calls == ["job-gw", "job-local"]

    with RecordingGateway() as gateway:
        monkeypatch.setenv("HERMES_API_URL", gateway.url)
        monkeypatch.setenv("HERMES_API_KEY", "test-key")
        recovered = factory_driver.FactoryDriver(
            state, tmp_path / "queue.json", tick=tick,
            subscribers=["coordinator"], startup=False,
        ).run_pass()

    assert recovered["wake_delivery"][0]["delivered"] is True
    assert recovered["wake_delivery"][0]["pending"] is False
    assert len(gateway.requests) == 1
    headers = {key.lower(): value for key, value in gateway.requests[0]["headers"].items()}
    assert headers["x-hermes-session-id"] == RAW_SESSION
    assert calls == ["job-gw", "job-local"]


def test_two_driver_processes_execute_one_work_item_once(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    counter = tmp_path / "ticks.log"
    work_scheduler.add_item(state, work_id="ws-race", title="race", kind="tests", priority=0)
    _mapping(state, "ws-race", "job-race")

    child = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from bundlebees_plugin import factory_driver

state = Path(sys.argv[1])
counter = Path(sys.argv[2])

def tick(job_id):
    with open(counter, "a", encoding="utf-8") as handle:
        handle.write(job_id + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    time.sleep(0.3)
    return {"status": "ok", "action": "ready_for_review"}

driver = factory_driver.FactoryDriver(
    state, state / "queue.json", tick=tick,
    subscribers=["coordinator"], startup=False,
)
driver.run_pass()
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child, str(state), str(counter), str(ROOT)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr

    lines = counter.read_text(encoding="utf-8").splitlines()
    assert lines == ["job-race"]
    item = next(item for item in work_scheduler.list_items(state) if item["work_id"] == "ws-race")
    assert item["status"] == "done"
