"""The reconciliation report and the scheduler's git-evidence consumer.

The owner invariant — authorized+eligible work plus available capacity equals
advancement without owner messages — is only enforceable if it is measurable.
The report producer reads the durable state read-only (jobs, approvals,
campaign checkpoints, scheduler queue, feature claims) and emits the metric
set: total outstanding, ready, ready-waiting-despite-capacity, claimed, active
workers, awaiting verification/review/integration/deployment, blocked with
exact reasons, genuine human approvals required, the last successful automatic
progression, and the worker-completion → next-coordinator-action latency.

The consumer half turns a git-observer report into the scheduler's next
action: an integrated-not-merged PR (orbitdesktop #34 class) must never cause
a duplicate integration attempt — the disposition is recorded and the wait
ends — while patch-equivalent content stays unverified until a verifier
confirms it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bundlebees_plugin import campaign as campaign_state
from bundlebees_plugin import git_observer, job as jobs, queue as queue_mod, reconcile, work_scheduler

SHA = "817eac479f441396562962e880c1af596638f399"
T0 = 1_800_000_000.0


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _job(
    tmp_path: Path,
    job_id: str = "job_a",
    *,
    gates=("merge",),
    tasks=None,
) -> dict:
    jobs.record_authorization(
        tmp_path,
        job_id=job_id,
        statement="Produce the reconciliation report fixture evidence.",
        repo="mojomast/workflow",
        base_rev=SHA,
        scope=["tools/**"],
        bees=2,
        wall_clock_s=3600,
        gates=gates,
        interactive=True,
    )
    return jobs.create_job(
        tmp_path,
        job_id=job_id,
        summary="reconciliation fixture",
        tasks=tasks
        or [{"task_id": "T1", "objective": "only", "lease": ["tools/x.py"], "depends_on": []}],
    )


def _drive(tmp_path: Path, job_id: str, task_id: str, states) -> None:
    job = jobs.load_job(tmp_path, job_id)
    for state in states:
        jobs.transition(job, task_id, state, actor="reconciler", reason="fixture")
    jobs.save_job(tmp_path, job)


# ---------------------------------------------------------------------------
# The report producer: one metric per line of the invariant
# ---------------------------------------------------------------------------


def test_a_fresh_authorized_job_is_outstanding_ready_and_gated(tmp_path: Path):
    _job(tmp_path)

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["schema"] == reconcile.RECONCILIATION_SCHEMA
    assert report["generated_at"] == iso(T0)
    assert report["outstanding"] == {"total": 1, "ready": 1, "claimed": 0, "active_workers": 0}
    assert report["awaiting"] == {"verification": 0, "review": 0, "integration": 0, "deployment": 0}
    assert report["blocked"] == []
    # The merge gate is a genuine human approval the scheduler cannot mint.
    assert report["human_approvals_required"] == [{"job_id": "job_a", "gate": "merge"}]
    # Ready work waits, but a human approval is legitimately required first:
    # this is NOT waiting despite capacity, and no owner message is missing.
    assert report["ready_waiting_despite_capacity"] == 0
    assert report["invariant"]["advancement_expected_without_owner_messages"] is False
    assert report["invariant"]["owner_messages_required"] == 1
    assert report["last_successful_automatic_progression"] is None
    assert report["worker_completion_to_next_action"] is None


def test_granting_the_gate_makes_ready_work_wait_despite_capacity(tmp_path: Path):
    _job(tmp_path)
    jobs.record_approval(
        tmp_path,
        job_id="job_a",
        gate="merge",
        decision="granted",
        statement="Proceed.",
        require_tty=False,
        interactive=True,
        by="approver",
    )

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["human_approvals_required"] == []
    assert report["capacity"] == {"max_active": work_scheduler.MAX_ACTIVE, "in_use": 0, "available": work_scheduler.MAX_ACTIVE}
    assert report["ready_waiting_despite_capacity"] == 1
    assert report["invariant"]["advancement_expected_without_owner_messages"] is True
    assert report["invariant"]["owner_messages_required"] == 0


def test_worker_states_fill_disjoint_buckets_along_the_pipeline(tmp_path: Path):
    _job(tmp_path, gates=())
    jobs.record_approval(
        tmp_path, job_id="job_a", gate="merge", decision="granted", statement="Proceed.", require_tty=False, interactive=True
    )
    stages = [
        (("dispatching", "dispatched"), ("claimed", 1)),
        (("running",), ("active_workers", 1)),
        (("verifying",), ("verification", 1)),
        (("verified",), ("integration", 1)),
    ]
    driven: list[str] = []
    for states, (key, expected) in stages:
        driven.extend(states)
        _drive(tmp_path, "job_a", "T1", states)

        report = reconcile.reconciliation_report(tmp_path, now=T0)

        bucket = report["outstanding"] if key in {"claimed", "active_workers"} else report["awaiting"]
        assert bucket[key] == expected, (driven, key)
        assert report["outstanding"]["total"] == 1


def test_superseded_work_is_blocked_never_outstanding(tmp_path: Path):
    _job(tmp_path, gates=())
    _drive(tmp_path, "job_a", "T1", ("dispatching", "dispatched", "superseded"))

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["outstanding"]["total"] == 0
    rows = [row for row in report["blocked"] if row["id"] == "job_a:T1"]
    assert len(rows) == 1
    assert "superseded" in rows[0]["reason"] and "re-plan" in rows[0]["reason"]


def test_failed_and_rejected_work_names_its_exact_reason(tmp_path: Path):
    _job(tmp_path, gates=(), tasks=[{"task_id": "T1", "objective": "one", "lease": ["tools/x.py"], "depends_on": []}])
    _drive(tmp_path, "job_a", "T1", ("failed",))

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    row = next(row for row in report["blocked"] if row["id"] == "job_a:T1")
    assert "task T1 failed" in row["reason"]
    assert "recovery or human decision" in row["reason"]


def test_a_terminal_integration_stop_is_a_job_level_blocker(tmp_path: Path):
    _job(tmp_path, gates=())
    job = jobs.load_job(tmp_path, "job_a")
    job["integration"].update(terminal=True, state="stopped", evidence="two integration attempts failed")
    jobs.save_job(tmp_path, job)

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    row = next(row for row in report["blocked"] if row["id"] == "job:job_a")
    assert row["reason"].startswith("integration_terminal:")
    assert "two integration attempts failed" in row["reason"]


def test_acceptance_evidence_gaps_are_blocked_with_the_queue_reasons(tmp_path: Path):
    _job(tmp_path, gates=())
    _drive(tmp_path, "job_a", "T1", ("dispatching", "dispatched", "running", "verifying", "verified", "integrating", "accepted"))

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["outstanding"]["total"] == 0
    row = next(row for row in report["blocked"] if row["id"] == "job:job_a")
    assert row["reason"].startswith("job_evidence_incomplete:")
    assert "no accepted integration wave" in row["reason"]


def test_the_deployment_gate_is_counted_separately_from_other_approvals(tmp_path: Path):
    _job(tmp_path, gates=("merge", "deploy"))
    jobs.record_approval(
        tmp_path, job_id="job_a", gate="merge", decision="granted", statement="Proceed.", require_tty=False, interactive=True
    )

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["human_approvals_required"] == [{"job_id": "job_a", "gate": "deploy"}]
    assert report["awaiting"]["deployment"] == 1

    jobs.record_approval(
        tmp_path, job_id="job_a", gate="deploy", decision="granted", statement="Ship it.", require_tty=False, interactive=True
    )
    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["human_approvals_required"] == []
    assert report["awaiting"]["deployment"] == 0


def test_campaign_checkpoints_feed_review_and_blocked_counts(tmp_path: Path):
    _job(tmp_path, gates=())
    root = tmp_path / campaign_state.CAMPAIGN_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "camp-review.json").write_text(
        json.dumps({"schema": campaign_state.SCHEMA, "campaign_id": "camp-review", "job_id": "job_a", "phase": "ready_for_review"}),
        encoding="utf-8",
    )
    (root / "camp-blocked.json").write_text(
        json.dumps(
            {
                "schema": campaign_state.SCHEMA,
                "campaign_id": "camp-blocked",
                "job_id": "job_a",
                "phase": "blocked",
                "blocker": {"code": "task_failed", "detail": "T1 rejected"},
            }
        ),
        encoding="utf-8",
    )

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["awaiting"]["review"] == 1
    row = next(row for row in report["blocked"] if row["id"] == "campaign:camp-blocked")
    assert row["reason"].startswith("task_failed:")
    assert "T1 rejected" in row["reason"]


def test_scheduler_items_and_feature_claims_are_reported(tmp_path: Path):
    _job(tmp_path, gates=())
    work_scheduler.add_item(tmp_path, work_id="ws_one", kind="feature", now=iso(T0))
    work_scheduler.add_item(tmp_path, work_id="ws_two", kind="docs", now=iso(T0))
    work_scheduler.claim(tmp_path, "ws_two", "bee-1", now=iso(T0))
    store = queue_mod.QueueStore(tmp_path)
    store.write("feat-one", {"feature_id": "feat-one", "state": "claimed", "job_id": "job_a"})

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["scheduler"] == {"items": 2, "pending": 1, "claimed": 1, "blocked": 0, "done": 0}
    assert report["features"] == {"active_claims": 1}


def test_capacity_scarcity_stops_the_despite_capacity_count(tmp_path: Path):
    _job(tmp_path, gates=())
    jobs.record_approval(
        tmp_path, job_id="job_a", gate="merge", decision="granted", statement="Proceed.", require_tty=False, interactive=True
    )

    report = reconcile.reconciliation_report(tmp_path, now=T0, capacity=0)

    assert report["capacity"] == {"max_active": 0, "in_use": 0, "available": 0}
    assert report["ready_waiting_despite_capacity"] == 0
    assert report["invariant"]["advancement_expected_without_owner_messages"] is False

    report = reconcile.reconciliation_report(tmp_path, now=T0, capacity=1)
    assert report["ready_waiting_despite_capacity"] == 1


def test_in_flight_workers_consume_capacity(tmp_path: Path):
    _job(tmp_path, gates=())
    jobs.record_approval(
        tmp_path, job_id="job_a", gate="merge", decision="granted", statement="Proceed.", require_tty=False, interactive=True
    )
    _drive(tmp_path, "job_a", "T1", ("dispatching", "dispatched", "running"))

    report = reconcile.reconciliation_report(tmp_path, now=T0, capacity=1)

    assert report["capacity"] == {"max_active": 1, "in_use": 1, "available": 0}
    assert report["ready_waiting_despite_capacity"] == 0


def test_last_progression_and_completion_latency_are_measured(tmp_path: Path):
    _job(tmp_path, gates=())
    _drive(tmp_path, "job_a", "T1", ("dispatching", "dispatched", "running", "verifying", "verified"))
    job = jobs.load_job(tmp_path, "job_a")
    stamps = [T0, T0 + 10, T0 + 20, T0 + 30, T0 + 150]
    for event, at in zip(job["events"], stamps):
        event["at"] = iso(at)
    jobs.save_job(tmp_path, job)

    report = reconcile.reconciliation_report(tmp_path, now=T0 + 200)

    assert report["last_successful_automatic_progression"] == {
        "at": iso(T0 + 150),
        "job_id": "job_a",
        "task_id": "T1",
        "transition": "verifying->verified",
    }
    assert report["worker_completion_to_next_action"] == {
        "samples": 1,
        "max_s": 120.0,
        "latest_s": 120.0,
        "latest_next_action": "verifying->verified",
    }


def test_a_worker_completion_without_a_following_action_has_no_latency_sample(tmp_path: Path):
    _job(tmp_path, gates=())
    _drive(tmp_path, "job_a", "T1", ("dispatching", "dispatched", "running", "verifying"))

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    assert report["worker_completion_to_next_action"] is None


def test_unreadable_sources_are_blocked_with_exact_reasons(tmp_path: Path):
    _job(tmp_path, gates=())
    (tmp_path / "jobs" / "broken.json").write_text("{not json", encoding="utf-8")
    scheduler_dir = tmp_path / "coordinator"
    scheduler_dir.mkdir(parents=True, exist_ok=True)
    (scheduler_dir / "work.json").write_text("{corrupt", encoding="utf-8")
    queue_root = tmp_path / "queue"
    queue_root.mkdir(parents=True, exist_ok=True)
    (queue_root / "feat-x.json").write_text("garbage", encoding="utf-8")

    report = reconcile.reconciliation_report(tmp_path, now=T0)

    ids = {row["id"] for row in report["blocked"]}
    assert "job:broken" in ids
    assert "scheduler:queue" in ids
    assert "queue:claims" in ids
    assert any("job unreadable" in row["reason"] for row in report["blocked"])
    assert any("scheduler queue unreadable" in row["reason"] for row in report["blocked"])


def test_the_report_never_writes_anything(tmp_path: Path):
    _job(tmp_path, gates=("merge",))
    snapshot = {
        str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }

    reconcile.reconciliation_report(tmp_path, now=T0)

    after = {
        str(path.relative_to(tmp_path)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert after == snapshot
    assert set(after) == set(snapshot)


# ---------------------------------------------------------------------------
# The scheduler's git-evidence consumer
# ---------------------------------------------------------------------------


def _observer_report(*, drift=(), verdict=None) -> dict:
    integration = {
        "observed": verdict is not None,
        "main_ref": "main",
        "main_head": "1" * 40,
        "pr_head": "2" * 40,
        "head_ancestor_of_main": verdict == "integrated_not_pr_merged",
        "equivalent_on_main": None,
        "verdict": verdict or "unobserved",
    }
    return {
        "schema": git_observer.SCHEMA,
        "campaign_id": "campaign-34",
        "job_id": "job_a",
        "repo": "mojomast/orbitdesktop",
        "observed_at": iso(T0),
        "scope": ["git", "pr", "integration"] if verdict else ["git", "pr"],
        "expected": {"base_rev": SHA, "branch": "feat/x", "pull_request": 34},
        "actual": {"integration": integration, "open_heads": []},
        "drift": list(drift),
        "clean": not drift,
    }


def test_an_integrated_not_merged_report_stops_the_wait_and_bans_integration():
    report = _observer_report(
        drift=[{"kind": "integrated_not_pr_merged", "expected": {}, "actual": {}, "detail": ""}],
        verdict="integrated_not_pr_merged",
    )

    decision = reconcile.git_evidence_decision(report)

    assert decision["evidence"] == "integrated_not_pr_merged"
    assert decision["integration_permitted"] is False
    assert decision["stop_waiting"] is True
    assert decision["next_action"] == "record_disposition:integrated_not_pr_merged"
    assert "record the disposition" in decision["reason"]


def test_a_proposed_integration_is_suppressed_not_executed():
    report = _observer_report(verdict="integrated_not_pr_merged")

    decision = reconcile.git_evidence_decision(report, proposed="integrate:T1")

    assert decision["suppressed_next_action"] == "integrate:T1"
    assert decision["next_action"] != "integrate:T1"
    assert decision["next_action"].startswith("record_disposition:")


def test_a_clean_report_permits_the_proposed_integration():
    decision = reconcile.git_evidence_decision(_observer_report(), proposed="integrate:T1")

    assert decision["evidence"] == "clean"
    assert decision["integration_permitted"] is True
    assert decision["stop_waiting"] is False
    assert decision["suppressed_next_action"] == ""


def test_equivalent_content_is_not_a_disposition_and_keeps_waiting():
    report = _observer_report(
        drift=[{"kind": "equivalent_content_unverified", "expected": {}, "actual": {}, "detail": ""}],
        verdict="equivalent_content_unverified",
    )

    decision = reconcile.git_evidence_decision(report, proposed="integrate:T1")

    assert decision["integration_permitted"] is False
    assert decision["stop_waiting"] is False
    assert decision["next_action"] == "verify_integrated_content"
    assert decision["suppressed_next_action"] == "integrate:T1"
    assert "not proof of a merge" in decision["reason"]


def test_a_merged_pr_is_a_disposition_not_a_reintegration():
    report = _observer_report(drift=[{"kind": "pr_merged", "expected": {}, "actual": {}, "detail": ""}], verdict="merged")

    decision = reconcile.git_evidence_decision(report)

    assert decision["stop_waiting"] is True
    assert decision["next_action"] == "record_disposition:pr_merged"


def test_world_disagreement_needs_a_human_and_never_integrates():
    report = _observer_report(
        drift=[
            {"kind": "pr_closed", "expected": {}, "actual": {}, "detail": ""},
            {"kind": "branch_moved", "expected": {}, "actual": {}, "detail": ""},
        ]
    )

    decision = reconcile.git_evidence_decision(report, proposed="integrate:T1")

    assert decision["integration_permitted"] is False
    assert decision["stop_waiting"] is False
    assert decision["next_action"] == "needs_human:branch_moved,pr_closed"
    assert decision["suppressed_next_action"] == "integrate:T1"


def test_a_malformed_report_is_refused_not_guessed():
    with pytest.raises(reconcile.ReconcileError) as caught:
        reconcile.git_evidence_decision({"drift": "nope"})
    assert caught.value.code == "git_evidence_invalid"
    with pytest.raises(reconcile.ReconcileError) as caught:
        reconcile.git_evidence_decision("not a mapping")
    assert caught.value.code == "git_evidence_invalid"
