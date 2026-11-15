"""Durable campaign continuity state and restart reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from bundlebees_plugin import campaign, factory_driver, job as jobs
from bundlebees_plugin import queue as queue_mod

SHA = "a" * 40
JOB = "campaign-job"


def _setup(tmp_path: Path) -> dict:
    jobs.record_authorization(
        tmp_path,
        job_id=JOB,
        statement="Build a campaign with two dependent features.",
        repo="mojomast/campaign-fixture",
        base_rev=SHA,
        scope=["src/**", "tests/**"],
        bees=2,
        wall_clock_s=3600,
        require_tty=False,
        interactive=False,
        by="tester",
    )
    jobs.create_job(
        tmp_path,
        job_id=JOB,
        summary="campaign fixture",
        tasks=[
            {"task_id": "T1", "objective": "first", "lease": ["src/a.py"], "depends_on": []},
            {"task_id": "T2", "objective": "second", "lease": ["src/b.py"], "depends_on": ["T1"]},
        ],
    )
    return campaign.create_campaign(
        tmp_path,
        campaign_id="campaign-a",
        job_id=JOB,
        objective="Build the campaign fixture",
        branch="feat/campaign",
        pull_request=7,
    )


def _set_task(state: Path, task_id: str, task_state: str) -> None:
    job = jobs.load_job(state, JOB)
    for task in job["tasks"]:
        if task["task_id"] == task_id:
            task["state"] = task_state
    jobs.save_job(state, job)


def _mark_job_accepted(state: Path, job_id: str = JOB) -> None:
    """Attach the minimum real verify/integration evidence the queue accepts."""
    job = jobs.load_job(state, job_id)
    attempts = []
    for task in job["tasks"]:
        task["state"] = "accepted"
        attempt_id = f"att-{task['task_id']}"
        task["attempts"] = [attempt_id]
        attempts.append(attempt_id)
        record = {
            "ok": True,
            "sha": "b" * 40,
            "base_rev": SHA,
            "gitleaks": {"exit_code": 0},
            "suite": {"exit_code": 0},
        }
        path = state / "verify-results" / f"{attempt_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    job["integration"] = {
        "wave": "w1",
        "state": "accepted",
        "last_commit": "b" * 40,
        "accepted_waves": [
            {"wave": 1, "commit": "b" * 40, "attempts": attempts}
        ],
    }
    jobs.save_job(state, job)


def test_reconcile_exposes_ready_work_without_dispatch(tmp_path: Path):
    record = _setup(tmp_path)

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "ready_to_advance"
    assert result["next_action"] == "dispatch:T1"
    assert result["active_workers"] == []
    assert result["dependencies"] == {"T1": [], "T2": ["T1"]}
    assert result["task_counts"]["total"] == 2
    assert result["revision"] == 2


def test_reconcile_is_idempotent_after_restart(tmp_path: Path):
    record = _setup(tmp_path)
    first = campaign.reconcile(tmp_path, record["campaign_id"])
    path = campaign._path(tmp_path, record["campaign_id"])
    before = path.read_bytes()

    second = campaign.reconcile(tmp_path, record["campaign_id"])

    assert second["revision"] == first["revision"]
    assert path.read_bytes() == before


def test_reconcile_adopts_in_flight_work_without_a_second_action(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "dispatched")

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "running"
    assert result["active_workers"][0]["task_id"] == "T1"
    assert result["active_workers"][0]["state"] == "dispatched"
    assert result["next_action"] == "wait_for_worker"
    assert "T1" in result["wait_condition"]


def test_all_tasks_accepted_without_job_evidence_is_not_campaign_complete(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "accepted")
    _set_task(tmp_path, "T2", "accepted")

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "blocked"
    assert result["next_action"] == "repair_job_evidence"
    assert result["job_accepted"] is False
    assert result["job_acceptance_reasons"]
    plan = campaign.resume_plan(tmp_path, record["campaign_id"])
    assert plan["should_continue"] is False
    assert plan["requires_human"] is True
    assert plan["requires_campaign_closeout"] is False


def test_all_tasks_accepted_with_job_evidence_is_job_complete(tmp_path: Path):
    record = _setup(tmp_path)
    _mark_job_accepted(tmp_path)

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "job_complete"
    assert result["next_action"] == "prepare_campaign_closeout"
    assert "product evidence" in result["wait_condition"]
    assert result["job_accepted"] is True
    assert result["job_acceptance_reasons"] == []
    plan = campaign.resume_plan(tmp_path, record["campaign_id"])
    assert plan["should_continue"] is False
    assert plan["requires_human"] is False
    assert plan["requires_campaign_closeout"] is True


def test_failed_work_is_a_blocker_not_a_retry(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "failed")

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "blocked"
    assert result["blocker"]["code"] == "task_failed"
    assert result["next_action"] == "resolve:T1"


def test_a_changed_authorization_snapshot_is_refused(tmp_path: Path):
    record = _setup(tmp_path)
    path = campaign._path(tmp_path, record["campaign_id"])
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["authorization"]["repo"] = "mojomast/other"
    path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(campaign.CampaignError) as caught:
        campaign.reconcile(tmp_path, record["campaign_id"])
    assert caught.value.code == "campaign_job_mismatch"


def test_duplicate_campaign_creation_is_refused(tmp_path: Path):
    record = _setup(tmp_path)
    with pytest.raises(campaign.CampaignError) as caught:
        campaign.create_campaign(
            tmp_path,
            campaign_id=record["campaign_id"],
            job_id=JOB,
            objective="duplicate",
        )
    assert caught.value.code == "campaign_exists"


def test_unknown_campaign_is_a_named_refusal(tmp_path: Path):
    with pytest.raises(campaign.CampaignError) as caught:
        campaign.resume_plan(tmp_path, "missing")
    assert caught.value.code == "campaign_missing"



def test_factory_driver_reconciles_a_campaign_without_a_second_dispatch(tmp_path: Path):
    record = _setup(tmp_path)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text("{}", encoding="utf-8")
    dispatched: list[str] = []

    def tick(job_id: str):
        dispatched.append(job_id)
        return {"status": "idle"}

    driver = factory_driver.FactoryDriver(
        tmp_path,
        queue_mod.JsonQueue(queue_path),
        tick=tick,
        startup=False,
        campaign_id=record["campaign_id"],
    )
    first = driver.run_pass()
    second = factory_driver.FactoryDriver(
        tmp_path,
        queue_mod.JsonQueue(queue_path),
        tick=tick,
        startup=False,
        campaign_id=record["campaign_id"],
    ).run_pass()

    assert first["campaign"]["phase"] == "ready_to_advance"
    assert second["campaign"]["phase"] == "ready_to_advance"
    assert first["campaign"]["revision"] == second["campaign"]["revision"]
    assert dispatched == []


def test_the_checkpoint_matches_its_published_schema(tmp_path: Path):
    record = _setup(tmp_path)
    reconciled = campaign.reconcile(tmp_path, record["campaign_id"])
    schema_path = Path("bundlebees_plugin/schemas/campaign-state.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(record)
    Draft202012Validator(schema).validate(reconciled)


def test_a_job_with_no_tasks_is_a_blocker_not_an_idle_loop(tmp_path: Path):
    record = _setup(tmp_path)
    job = jobs.load_job(tmp_path, JOB)
    job["tasks"] = []
    jobs.save_job(tmp_path, job)

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "blocked"
    assert result["blocker"]["code"] == "no_tasks"
    assert campaign.resume_plan(tmp_path, record["campaign_id"])["should_continue"] is False


def test_a_queued_campaign_advances_through_driver_restart(tmp_path: Path):
    """Deterministic driver/queue trial: no real worker is involved.

    The CI-only GateC test exercises the same driver with the real job tick and
    a simulated worker patch. This one keeps the queue and driver real while
    making task transitions deterministic, so it can catch a wiring regression
    without a sandbox or a worker host.
    """
    record = _setup(tmp_path)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps({
        "f1": {"repo": "mojomast/campaign-fixture", "job_id": JOB, "status": "queued"}
    }), encoding="utf-8")
    queue = queue_mod.JsonQueue(queue_path)
    calls: list[str] = []

    def tick(job_id: str):
        calls.append(job_id)
        job = jobs.load_job(tmp_path, job_id)
        states = {task["task_id"]: task["state"] for task in job["tasks"]}
        if states["T1"] == "pending":
            _set_task(tmp_path, "T1", "dispatched")
            return {"status": "running"}
        if states["T1"] == "dispatched":
            _set_task(tmp_path, "T1", "accepted")
            return {"status": "running"}
        if states["T2"] == "pending":
            _set_task(tmp_path, "T2", "dispatched")
            return {"status": "running"}
        if states["T2"] == "dispatched":
            _set_task(tmp_path, "T2", "accepted")
            _mark_job_accepted(tmp_path)
            return {"status": "running"}
        return {"status": "idle"}

    for _ in range(6):
        result = factory_driver.FactoryDriver(
            tmp_path, queue, tick=tick, startup=False, campaign_id=record["campaign_id"]
        ).run_pass()
        item = queue_mod.QueueStore(tmp_path).read("f1")
        if item and item.get("state") == "ready_for_review":
            break

    item = queue_mod.QueueStore(tmp_path).read("f1")
    assert item and item.get("state") == "ready_for_review"
    assert len(calls) == 4
    plan = campaign.resume_plan(tmp_path, record["campaign_id"])
    assert plan["phase"] == "job_complete"
    assert plan["requires_campaign_closeout"] is True


def test_campaign_can_be_created_without_a_branch(tmp_path: Path):
    _setup(tmp_path)
    record = campaign.create_campaign(
        tmp_path,
        campaign_id="campaign-no-branch",
        job_id=JOB,
        objective="Optional branch",
    )
    assert record["branch"] == ""
    assert record["phase"] == "authorized"


def test_terminal_integration_is_a_blocker_not_a_worker_wait(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "verified")
    _set_task(tmp_path, "T2", "pending")
    job = jobs.load_job(tmp_path, JOB)
    job["integration"] = {
        "wave": "w1",
        "terminal": True,
        "state": "stopped",
        "stopped": "integration_failed",
        "evidence": "two integration attempts failed",
    }
    jobs.save_job(tmp_path, job)

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "blocked"
    assert result["blocker"]["code"] == "integration_terminal"
    assert result["next_action"] == "resolve_integration"
    assert campaign.resume_plan(tmp_path, record["campaign_id"])["should_continue"] is False


def test_verified_work_waits_for_integration_not_a_worker(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "verified")
    _set_task(tmp_path, "T2", "pending")

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "running"
    assert result["next_action"] == "integrate:T1"
    assert "integration step pending" in result["wait_condition"]
    assert "wait_for_worker" != result["next_action"]
    # A verified row is waiting for integration; no worker is in flight for it.
    assert result["active_workers"] == []
    assert result["integration_queue"] == [
        {"task_id": "T1", "state": "verified", "attempts": []}
    ]
    assert result["task_counts"]["integrating"] == 1
    assert result["task_counts"]["active"] == 1


def test_a_superseded_task_is_a_replan_blocker_not_a_task_failure(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "accepted")
    _set_task(tmp_path, "T2", "superseded")

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "blocked"
    assert result["blocker"]["code"] == "task_superseded"
    assert result["blocker"]["task_ids"] == ["T2"]
    assert result["next_action"] == "replan:T2"
    # A cancelled row is not a failed row: it can never be retried.
    assert result["task_counts"]["failed"] == 0
    assert result["task_counts"]["superseded"] == 1
    assert campaign.resume_plan(tmp_path, record["campaign_id"])["should_continue"] is False


def test_a_terminal_integration_escalation_blocks_the_queue_card(tmp_path: Path):
    record = _setup(tmp_path)
    job = jobs.load_job(tmp_path, JOB)
    job["tasks"][0]["state"] = "verified"
    job["integration"] = {
        "wave": "w1",
        "terminal": True,
        "state": "stopped",
        "stopped": "integration_failed",
        "evidence": "two integration attempts failed",
    }
    jobs.save_job(tmp_path, job)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps({
        "f1": {"repo": "mojomast/campaign-fixture", "job_id": JOB, "status": "queued"}
    }), encoding="utf-8")

    result = queue_mod.run_once(
        tmp_path,
        queue_mod.JsonQueue(queue_path),
        tick=lambda job_id: {"status": "escalated", "action": "integrate",
                             "detail": "two integration attempts failed"},
    )

    assert result["action"] == "blocked"
    assert queue_mod.JsonQueue(queue_path).get("f1").status == "blocked"
    assert queue_mod.QueueStore(tmp_path).read("f1")["state"] == "blocked"
    reconcile = campaign.reconcile(tmp_path, record["campaign_id"])
    assert reconcile["phase"] == "blocked"
    assert reconcile["blocker"]["code"] == "integration_terminal"
