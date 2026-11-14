"""M1 acceptance tests: durable jobs that narrow a human authorization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import job as jobs
from bundlebees_plugin.job import JobError

SHA = "817eac479f441396562962e880c1af596638f399"
OTHER_SHA = "28e7a4863bc0da34af16062e36a8b55b2f4c6721"


def authorize(state: Path, job_id: str = "job_a", **overrides) -> dict:
    kwargs = dict(
        job_id=job_id,
        statement="Add contract source-rev validation to acdd-lint, nothing else.",
        repo="mojomast/workflow",
        base_rev=SHA,
        scope=["tools/acdd-lint/**"],
        bees=3,
        wall_clock_s=3600,
        interactive=True,
    )
    kwargs.update(overrides)
    return jobs.record_authorization(state, **kwargs)


def two_tasks():
    return [
        {
            "task_id": "T1",
            "objective": "reject malformed source_rev",
            "lease": ["tools/acdd-lint/acdd_lint/checks_source_rev.py"],
        },
        {
            "task_id": "T2",
            "objective": "reject unreachable source_rev",
            "lease": ["tools/acdd-lint/acdd_lint/checks_reachability.py"],
            "depends_on": ["T1"],
        },
    ]


def walk_to_accepted(job: dict, task_id: str) -> dict:
    for state in (
        "dispatching",
        "dispatched",
        "running",
        "verifying",
        "verified",
        "integrating",
        "accepted",
    ):
        jobs.transition(job, task_id, state, actor="reconciler")
    return job


# AC-001 -------------------------------------------------------------------


def test_round_trip_preserves_every_field_and_seals_the_document(tmp_path):
    auth = authorize(tmp_path)
    job = jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())

    loaded = jobs.load_job(tmp_path, "job_a")

    assert loaded["goal"]["base_rev"] == auth["base_rev"]
    assert [t["task_id"] for t in loaded["tasks"]] == ["T1", "T2"]
    assert loaded["tasks"][1]["depends_on"] == ["T1"]
    assert loaded["tasks"][0]["state"] == "pending"
    assert loaded["authorization"]["auth_id"] == auth["auth_id"]

    raw = json.loads(jobs.job_path(tmp_path, "job_a").read_text())
    assert set(raw) == {"body", "mac"}
    assert raw["body"]["job_id"] == job["job_id"]


# AC-002 -------------------------------------------------------------------


def test_a_hand_edited_field_is_refused(tmp_path):
    authorize(tmp_path)
    jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())
    path = jobs.job_path(tmp_path, "job_a")

    document = json.loads(path.read_text())
    document["body"]["tasks"][0]["lease"] = ["tools/acdd-lint/acdd_lint/anything.py"]
    path.write_text(json.dumps(document))

    with pytest.raises(JobError) as exc:
        jobs.load_job(tmp_path, "job_a")
    assert exc.value.code == "job_tampered"


def test_a_hand_edited_authorization_is_refused(tmp_path):
    authorize(tmp_path)
    path = jobs.authorization_path(tmp_path, "job_a")
    document = json.loads(path.read_text())
    document["body"]["budget"]["bees"] = 99
    path.chmod(0o600)  # recorded authorizations are 0400; a human editor would chmod too
    path.write_text(json.dumps(document))

    with pytest.raises(JobError) as exc:
        jobs.load_authorization(tmp_path, "job_a")
    assert exc.value.code == "authorization_tampered"


# AC-003 -------------------------------------------------------------------


def test_an_illegal_transition_is_refused(tmp_path):
    authorize(tmp_path)
    job = jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())

    with pytest.raises(JobError) as exc:
        jobs.transition(job, "T1", "accepted", actor="reconciler")
    assert exc.value.code == "job_bad_transition"

    jobs.transition(job, "T1", "dispatching", actor="reconciler")
    with pytest.raises(JobError):
        jobs.transition(job, "T1", "pending", actor="reconciler")


def test_every_transition_is_recorded_with_actor_and_time(tmp_path):
    authorize(tmp_path)
    job = jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())
    jobs.transition(job, "T1", "dispatching", actor="reconciler", reason="deps met")

    event = job["events"][-1]
    assert event["transition"] == "pending->dispatching"
    assert event["actor"] == "reconciler"
    assert event["reason"] == "deps met"
    assert event["at"].endswith("Z")


# AC-004 -------------------------------------------------------------------


def test_a_task_cannot_dispatch_before_its_dependency_is_accepted(tmp_path):
    authorize(tmp_path)
    job = jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())

    with pytest.raises(JobError) as exc:
        jobs.transition(job, "T2", "dispatching", actor="reconciler")
    assert exc.value.code == "task_dependency_unmet"

    walk_to_accepted(job, "T1")
    jobs.transition(job, "T2", "dispatching", actor="reconciler")
    assert job["tasks"][1]["state"] == "dispatching"


# AC-005 -------------------------------------------------------------------


def test_a_lease_outside_the_authorized_scope_is_refused(tmp_path):
    authorize(tmp_path)
    with pytest.raises(JobError) as exc:
        jobs.create_job(
            tmp_path,
            job_id="job_a",
            tasks=[{"task_id": "T1", "lease": ["README.md"]}],
        )
    assert exc.value.code == "lease_out_of_scope"


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.py", "tools/../../etc/x"])
def test_absolute_and_escaping_paths_are_refused(tmp_path, bad):
    authorize(tmp_path)
    with pytest.raises(JobError) as exc:
        jobs.create_job(
            tmp_path,
            job_id="job_a",
            tasks=[{"task_id": "T1", "lease": [bad]}],
        )
    assert exc.value.code in {"lease_out_of_scope", "job_invalid"}


def test_scope_globs_cover_nested_paths(tmp_path):
    authorize(tmp_path)
    job = jobs.create_job(
        tmp_path,
        job_id="job_a",
        tasks=[{"task_id": "T1", "lease": ["tools/acdd-lint/acdd_lint/deep/checks_x.py"]}],
    )
    assert job["tasks"][0]["lease"] == ["tools/acdd-lint/acdd_lint/deep/checks_x.py"]


# AC-006 -------------------------------------------------------------------


def test_more_tasks_than_the_authorized_bees_is_refused(tmp_path):
    authorize(tmp_path, bees=1)
    with pytest.raises(JobError) as exc:
        jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())
    assert exc.value.code == "authorization_widened"


def test_a_job_without_an_authorization_is_refused(tmp_path):
    with pytest.raises(JobError) as exc:
        jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())
    assert exc.value.code == "authorization_missing"


def test_a_different_base_rev_is_refused(tmp_path):
    authorize(tmp_path)
    job = jobs.create_job(tmp_path, job_id="job_a", tasks=two_tasks())
    authorization = jobs.load_authorization(tmp_path, "job_a")
    job["goal"]["base_rev"] = OTHER_SHA

    with pytest.raises(JobError) as exc:
        jobs._enforce_narrowing(job, authorization)
    assert exc.value.code == "authorization_widened"


def test_a_second_authorization_for_one_job_is_refused(tmp_path):
    authorize(tmp_path)
    with pytest.raises(JobError) as exc:
        authorize(tmp_path)
    assert exc.value.code == "authorization_exists"


# AC-007 -------------------------------------------------------------------


def test_authorization_without_a_tty_is_refused(tmp_path):
    with pytest.raises(JobError) as exc:
        authorize(tmp_path, interactive=False)
    assert exc.value.code == "authorization_not_interactive"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_approval_without_a_tty_is_refused(tmp_path):
    authorize(tmp_path)
    with pytest.raises(JobError) as exc:
        jobs.record_approval(
            tmp_path,
            job_id="job_a",
            gate="merge",
            decision="granted",
            statement="merge it",
            interactive=False,
        )
    assert exc.value.code == "approval_not_interactive"


def test_approval_records_the_human_decision(tmp_path):
    authorize(tmp_path)
    approval = jobs.record_approval(
        tmp_path,
        job_id="job_a",
        gate="merge",
        decision="granted",
        statement="Approved: merge PR #31.",
        interactive=True,
    )
    assert approval["decision"] == "granted"
    assert "merge" in approval["statement"]


def test_a_short_placeholder_statement_is_refused(tmp_path):
    with pytest.raises(JobError) as exc:
        authorize(tmp_path, statement="go")
    assert exc.value.code == "authorization_incomplete"
