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


def test_all_accepted_is_ready_for_review_not_campaign_acceptance(tmp_path: Path):
    record = _setup(tmp_path)
    _set_task(tmp_path, "T1", "accepted")
    _set_task(tmp_path, "T2", "accepted")

    result = campaign.reconcile(tmp_path, record["campaign_id"])

    assert result["phase"] == "ready_for_review"
    assert result["next_action"] == "await_receiver_review"
    assert "human receiver decision" in result["wait_condition"]
    plan = campaign.resume_plan(tmp_path, record["campaign_id"])
    assert plan["should_continue"] is False
    assert plan["requires_human"] is True


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
