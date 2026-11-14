"""The coordinator driver connects queue, landing, inbox and scheduler.

Every test runs against temporary state.  The worker is a fake; no model,
network or real dispatch is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import bee_events, event_consumer, factory_driver, job as jobs, leases, work_scheduler
from bundlebees_plugin import queue as queue_mod

SOURCE_REV = "a" * 40
ARTIFACT = "b" * 64
ATTEMPT = "att-driver-1"
JOB = "job-driver-1"
FILES = ["src/a.py", "tests/test_a.py"]


def _write_schema(root: Path) -> None:
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
    (schema_dir / "bee-landed.schema.json").write_text(json.dumps(schema), encoding="utf-8")


@pytest.fixture(autouse=True)
def _schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))


def _canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _queue_file(root: Path, features: dict) -> Path:
    path = root / "queue.json"
    path.write_text(json.dumps(features), encoding="utf-8")
    return path


def _lease(state: Path) -> dict:
    return leases.record_lease(
        state,
        ATTEMPT,
        {
            "source_rev": SOURCE_REV,
            "assigned_files": list(FILES),
            "acceptance": ["the driver test passes"],
        },
    )


def _subscribe(state: Path) -> None:
    bee_events.subscribe(state, {"identity": "coordinator", "file": "coordinator-inbox.jsonl"})


def _job(state: Path) -> None:
    jobs.record_authorization(
        state,
        job_id=JOB,
        statement="Build the factory driver test feature.",
        repo="mojomast/smoke-real",
        base_rev=SOURCE_REV,
        scope=list(FILES),
        bees=1,
        wall_clock_s=60,
        require_tty=False,
        interactive=False,
        by="tester",
    )
    jobs.create_job(
        state,
        job_id=JOB,
        summary="factory driver",
        tasks=[{"task_id": "T1", "objective": "Deliver the feature", "lease": list(FILES)}],
    )


class FakeWorker:
    """Dispatch once and land a canonical event; no model is called."""

    def __init__(self, outbox: Path, lease_record: dict) -> None:
        self.outbox = outbox
        self.lease = lease_record
        self.calls: list[str] = []
        self.event: dict | None = None

    def __call__(self, job_id: str) -> dict:
        self.calls.append(job_id)
        if self.event is None:
            self.event = bee_events.build_event(
                {"attempt_id": ATTEMPT, "state": "landed", "transition_id": "tr-driver-1"},
                feature_id="F-DRIVER",
                job_id=job_id,
                task_id="T1",
                coordinator={"identity": "coordinator", "session": "session-1"},
                backend="fake-worker",
                lease_sha256=self.lease["brief_sha256"],
                assigned_files=list(FILES),
                source_rev=SOURCE_REV,
                artifact_sha256=ARTIFACT,
                reference="refs/heads/feat/driver",
            )
            self.outbox.mkdir(parents=True, exist_ok=True)
            (self.outbox / "attempt.json").write_bytes(_canonical(self.event))
        return {"status": "running", "transitions": ["T1->dispatched"]}


def _files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if path.is_file()) if directory.is_dir() else []


def test_enqueue_dispatch_landing_inbox_verification_and_next_eligible_work(tmp_path: Path):
    state = tmp_path / "state"
    outbox = tmp_path / "outbox"
    state.mkdir()
    lease_record = _lease(state)
    _subscribe(state)
    _job(state)
    queue_path = _queue_file(tmp_path, {
        "f1": {"repo": "mojomast/smoke-real", "job_id": JOB, "title": "driver", "status": "queued"},
    })
    worker = FakeWorker(outbox, lease_record)
    driver = factory_driver.FactoryDriver(
        state, queue_path, tick=worker, launcher_outbox=outbox,
        subscribers=["coordinator"], startup=False,
    )

    first = driver.run_pass()

    assert first["queue"]["action"] == "running"
    assert worker.calls == [JOB]
    assert first["landing"]["processed"] == 1
    assert len(_files(state / "events" / "outbox")) == 1
    assert len(_files(state / "events" / "inbox" / "coordinator")) == 1
    assert len(_files(state / "events" / "acks" / "coordinator")) == 1
    assert first["events"][0]["state"] == "acked"
    verify_id = first["events"][0]["work_id"]
    assert verify_id.startswith("ws_verify_")
    assert verify_id in first["ready"]
    assert first["inference"] == 0

    work_scheduler.add_item(
        state, work_id="ws_next", title="next delivery", kind="feature",
        priority=1, depends_on=[verify_id],
    )
    assert "ws_next" not in [item["work_id"] for item in work_scheduler.ready_items(state)]

    work_scheduler.claim(state, verify_id, "verifier", "wt")
    work_scheduler.complete(state, verify_id, owner="verifier")

    second = driver.run_pass()

    assert "ws_next" in second["ready"]
    assert worker.calls == [JOB, JOB]
    assert len(work_scheduler.list_items(state)) == 2


def test_restart_is_idempotent_and_repairs_an_abandoned_claim(tmp_path: Path):
    state = tmp_path / "state"
    outbox = tmp_path / "outbox"
    state.mkdir()
    lease_record = _lease(state)
    _subscribe(state)
    _job(state)
    queue_path = _queue_file(tmp_path, {
        "f1": {"repo": "mojomast/smoke-real", "job_id": JOB, "title": "driver", "status": "queued"},
    })
    worker = FakeWorker(outbox, lease_record)

    first = factory_driver.run_pass(
        state, queue_path, tick=worker, launcher_outbox=outbox,
        subscribers=["coordinator"], startup=False,
    )
    assert first["events"][0]["state"] == "acked"

    claim_path = queue_mod.QueueStore(state).path("f1")
    record = json.loads(claim_path.read_text(encoding="utf-8"))
    record["pid"] = 10 ** 9
    claim_path.write_text(json.dumps(record), encoding="utf-8")

    restarted = factory_driver.run_pass(
        state, queue_path, tick=worker, launcher_outbox=outbox,
        subscribers=["coordinator"], startup=True,
    )

    assert restarted["startup"]["abandoned_claims"] == ["f1"]
    assert restarted["startup"]["repair"] is not None
    assert len(_files(state / "events" / "outbox")) == 1
    assert len(_files(state / "events" / "inbox" / "coordinator")) == 1
    assert len(_files(state / "events" / "acks" / "coordinator")) == 1
    assert [item["work_id"] for item in work_scheduler.list_items(state)] == [
        first["events"][0]["work_id"]
    ]
    assert not any(entry.get("state") == "acked" for entry in restarted["events"])


def test_startup_repairs_a_terminal_attempt_with_no_event(tmp_path: Path):
    state = tmp_path / "state"
    outbox = tmp_path / "outbox"
    state.mkdir()
    _lease(state)
    _subscribe(state)
    _job(state)
    job = jobs.load_job(state, JOB, recover=False)
    job["tasks"][0]["state"] = "failed"
    job["tasks"][0]["attempts"] = [ATTEMPT]
    jobs.save_job(state, job)

    queue_path = _queue_file(tmp_path, {})
    result = factory_driver.run_pass(
        state, queue_path, tick=lambda job_id: {"status": "idle"},
        launcher_outbox=outbox, subscribers=["coordinator"],
    )

    assert [item["attempt_id"] for item in result["startup"]["terminal_events"]] == [ATTEMPT]
    assert len(_files(state / "events" / "outbox")) == 1
    assert len(_files(state / "events" / "inbox" / "coordinator")) == 1
    assert len(_files(state / "events" / "acks" / "coordinator")) == 1
    assert len(work_scheduler.list_items(state)) == 1


def test_idle_makes_no_model_call(tmp_path: Path):
    state = tmp_path / "state"
    queue_path = _queue_file(tmp_path, {})
    calls: list[str] = []

    result = factory_driver.run_pass(
        state, queue_path, tick=lambda job_id: calls.append(job_id) or {"status": "idle"},
        subscribers=["coordinator"],
    )

    assert result["idle"] is True
    assert result["inference"] == 0
    assert result["model_calls"] == 0
    assert result["ready"] == []
    assert result["events"] == []
    assert calls == []


def test_wake_requests_coalesce_and_survive_restart(tmp_path: Path):
    wake = factory_driver.WakeCoalescer(tmp_path / "state")
    wake.request_wake("session-1", now=1000)
    turn = wake.begin_turn("session-1", now=1001)
    assert turn["started"] is True

    wake.request_wake("session-1", now=1002)
    wake.request_wake("session-1", now=1002)
    assert wake.begin_turn("session-1", now=1003)["started"] is False

    finished = wake.finish_turn("session-1", turn["turn"], now=1004)
    assert finished["pending"] is True
    assert finished["cursor"] == 1
    assert finished["generation"] == 3

    reloaded = factory_driver.WakeCoalescer(tmp_path / "state")
    assert reloaded.status("session-1")["generation"] == 3
    assert reloaded.due("session-1") is True

    next_turn = reloaded.begin_turn("session-1", now=1005)
    assert next_turn["started"] is True
    assert reloaded.begin_turn("session-1", now=1006)["started"] is False
    done = reloaded.finish_turn("session-1", next_turn["turn"], now=1007)
    assert done["pending"] is False
    assert reloaded.due("session-1") is False


def test_cli_once_emits_an_idle_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    state = tmp_path / "state"
    queue_path = _queue_file(tmp_path, {})

    code = factory_driver.main(["--state", str(state), "--queue", str(queue_path), "--once"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["idle"] is True
    assert payload["inference"] == 0
    assert payload["landing"] is None


def test_stack_manifest_records_the_pinned_versions():
    doc = Path(__file__).resolve().parents[1] / "docs" / "stack-manifest.md"
    text = doc.read_text(encoding="utf-8")

    for token in ("9c69d3a", "ec37c24", "8ffaf73", "0.23.0", "0.1.16", "3.12.3"):
        assert token in text
    assert 'git -C "$WORKFLOW_SRC" rev-parse HEAD' in text
    assert 'git -C "$HYVV_SRC" rev-parse HEAD' in text
    assert 'git -C "$BUNDLEBEES_SRC" rev-parse HEAD' in text
    assert "python3 --version" in text
