"""Hardening tests for the M2 findings: identity, identifiers, verdicts,
durable escalation, stale locks and integration unblocking."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

from tests.test_reconcile import FakeLauncher, make_job, T0, iso

ACCEPTANCE = {"criteria": ["the thing works"], "tests": ["tests/x.py"]}


def task(task_id="T1", objective="do the thing", lease=None, acceptance=None, **extra):
    body = {
        "task_id": task_id,
        "objective": objective,
        "lease": lease or ["tools/x.py"],
        "acceptance": acceptance if acceptance is not None else dict(ACCEPTANCE),
    }
    body.update(extra)
    return body


class Identifiers:
    __test__ = True
    @pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".", "with space", "nul\x00byte", ""])
    def test_malformed_task_id_is_refused_before_any_path_is_built(self, tmp_path, bad):
        from bundlebees_plugin.job import JobError

        launcher = FakeLauncher()
        with pytest.raises((rec.ReconcileError, JobError)) as exc:
            make_job(tmp_path, tasks=[task(task_id=bad)])
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        code = getattr(exc.value, "code", "")
        assert code in {"identifier_invalid", "job_invalid"}
        assert launcher.posts == []

    @pytest.mark.parametrize("bad", ["../job", "a/b", "..", ""])
    def test_malformed_job_id_is_refused(self, tmp_path, bad):
        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, bad, launcher=FakeLauncher(), now=T0)

        assert exc.value.code == "identifier_invalid"

    def test_intent_path_stays_inside_the_state_dir(self, tmp_path):
        path = rec.intent_path(tmp_path, "job_a", "T1", 1)
        assert tmp_path.resolve() in path.resolve().parents

        with pytest.raises(rec.ReconcileError):
            rec.intent_path(tmp_path, "job_a", "../../etc/passwd", 1)


class Identity:
    __test__ = True
    def _dispatch_uncertain(self, tmp_path, launcher):
        make_job(tmp_path)
        launcher.script = ["uncertain"]
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0, grace_s=600, max_uncertain_ticks=50)
        return launcher

    def _with_entry(self, launcher, **overrides):
        entry = {
            "attempt_id": "att_x",
            "state": "succeeded",
            "created_at": iso(T0),
            "brief": dict(launcher.posts[0]),
        }
        entry.update(overrides)
        launcher.entries.append(entry)
        return launcher

    def test_incomplete_brief_is_not_proof(self, tmp_path):
        launcher = self._dispatch_uncertain(tmp_path, FakeLauncher())
        self._with_entry(launcher, brief={"objective": ""})

        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50)

        assert result.status == "reconcile_wait"
        assert len(launcher.posts) == 1

    def test_missing_attempt_id_is_not_proof(self, tmp_path):
        launcher = self._dispatch_uncertain(tmp_path, FakeLauncher())
        self._with_entry(launcher, attempt_id="")

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50).status == "reconcile_wait"

    def test_missing_timestamp_is_not_proof(self, tmp_path):
        launcher = self._dispatch_uncertain(tmp_path, FakeLauncher())
        self._with_entry(launcher)
        del launcher.entries[0]["created_at"]

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50).status == "reconcile_wait"

    def test_invalid_timestamp_is_not_proof(self, tmp_path):
        launcher = self._dispatch_uncertain(tmp_path, FakeLauncher())
        self._with_entry(launcher, created_at="not-a-timestamp")

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50).status == "reconcile_wait"

    def test_unrelated_attempt_does_not_match(self, tmp_path):
        launcher = self._dispatch_uncertain(tmp_path, FakeLauncher())
        self._with_entry(launcher, brief={"objective": "something else", "source_rev": "0" * 40, "assigned_files": ["tools/x.py"]})

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50).status == "reconcile_wait"

    def test_a_complete_entry_is_adopted(self, tmp_path):
        launcher = self._dispatch_uncertain(tmp_path, FakeLauncher())
        self._with_entry(launcher)

        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50)

        assert adopted.action == "adopt"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["attempts"] == ["att_x"]


class Verdicts:
    __test__ = True
    def _verifying_job(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        return launcher

    @pytest.mark.parametrize("bad", ["yes", 1, "true", None, []])
    def test_truthy_non_boolean_ok_is_refused(self, tmp_path, bad):
        launcher = self._verifying_job(tmp_path)

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(
                tmp_path,
                "job_a",
                launcher=launcher,
                verifier=lambda ctx: {"ok": bad, "evidence": "x"},
                now=T0 + 1,
            )

        assert exc.value.code == "verifier_invalid"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatched"

    def test_malformed_verifier_result_is_refused(self, tmp_path):
        launcher = self._verifying_job(tmp_path)

        with pytest.raises(rec.ReconcileError):
            rec.tick(tmp_path, "job_a", launcher=launcher, verifier=lambda ctx: "ok", now=T0 + 1)

    def test_passing_verdict_without_evidence_is_refused(self, tmp_path):
        launcher = self._verifying_job(tmp_path)

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=launcher, verifier=lambda ctx: {"ok": True}, now=T0 + 1)

        assert exc.value.code == "verifier_invalid"

    def test_boolean_false_rejects_with_evidence(self, tmp_path):
        launcher = self._verifying_job(tmp_path)

        result = rec.tick(
            tmp_path,
            "job_a",
            launcher=launcher,
            verifier=lambda ctx: {"ok": False, "evidence": "test failed"},
            now=T0 + 1,
        )

        assert result.action == "verify"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "rejected"


class Escalation:
    __test__ = True
    def test_escalation_is_persisted_once_and_never_reclaimed(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["ambiguous"]

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0).status == "reconcile_wait"
        first = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)
        assert first.status == "escalated"
        assert first.evidence == "dispatch_ambiguous"

        task_state = jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"]
        assert task_state == "failed"

        second = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 2)
        assert second.status == "idle"
        escalations = jobs.load_job(tmp_path, "job_a").get("escalations") or []
        assert len(escalations) == 1
        assert len(launcher.posts) == 1

    def test_uncertainty_exhaustion_is_terminal(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain"]

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0, grace_s=10, max_uncertain_ticks=50)
        escalated = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 11, grace_s=10, max_uncertain_ticks=50)

        assert escalated.status == "escalated"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "failed"
        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 12).status == "idle"
        assert len(launcher.posts) == 1


class Integration:
    __test__ = True
    def _two_task_job(self, tmp_path):
        make_job(
            tmp_path,
            tasks=[
                task(task_id="T1"),
                task(task_id="T2", objective="second", lease=["tools/y.py"], depends_on=["T1"]),
            ],
        )

    def test_accepted_wave_accepts_tasks_and_unblocks_dependants(self, tmp_path):
        self._two_task_job(tmp_path)
        launcher = FakeLauncher()
        verifier = lambda ctx: {"ok": True, "evidence": "green"}

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)                      # T1 dispatched
        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=verifier, now=T0 + 1)  # T1 verified
        result = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=lambda job: {"ok": True, "evidence": "suite green"}, now=T0 + 2)

        assert result.action == "integrate"
        reloaded = jobs.load_job(tmp_path, "job_a")
        assert [t["state"] for t in reloaded["tasks"]] == ["accepted", "pending"]
        assert reloaded["integration"]["state"] == "accepted"

        # The dependant now dispatches without a human nudge.
        nxt = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert nxt.action == "dispatch"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][1]["state"] == "dispatched"

    def test_truthy_integrator_ok_is_refused(self, tmp_path):
        make_job(tmp_path)  # one task, so a verified wave exists
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=lambda ctx: {"ok": True, "evidence": "green"}, now=T0 + 1)

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=launcher, integrator=lambda job: {"ok": "yes"}, now=T0 + 2)

        assert exc.value.code == "integrator_invalid"


class Brief:
    __test__ = True
    def test_task_without_acceptance_is_refused_before_any_post(self, tmp_path):
        make_job(tmp_path, tasks=[{"task_id": "T1", "objective": "x", "lease": ["tools/x.py"]}])
        launcher = FakeLauncher()

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        assert exc.value.code == "brief_invalid"
        assert launcher.posts == []

    def test_brief_matches_the_production_validator(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        from bundlebees_plugin.brief import validate_brief

        brief = launcher.posts[0]
        validate_brief(brief)  # must not raise
        assert brief["acceptance"]["tests"] == ["tests/x.py"]
        assert brief["source_rev"] == "817eac479f441396562962e880c1af596638f399"


class StaleLock:
    __test__ = True
    def test_a_dead_holders_lock_is_stolen_and_the_job_resumes(self, tmp_path):
        make_job(tmp_path)
        lock = rec.lock_path(tmp_path, "job_a")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 999_999_999, "at": iso(T0)}))

        result = rec.tick(tmp_path, "job_a", launcher=FakeLauncher(), now=T0 + 1)

        assert result.status == "ok"
        assert "stale lock" in result.detail
        assert not lock.exists()
        assert list(lock.parent.glob("*.stale.*"))

    def test_a_live_holders_lock_is_respected(self, tmp_path):
        make_job(tmp_path)
        lock = rec.lock_path(tmp_path, "job_a")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "at": iso(T0)}))

        result = rec.tick(tmp_path, "job_a", launcher=FakeLauncher(), now=T0 + 1)

        assert result.status == "locked"
        assert lock.exists()

    def test_an_old_lock_is_stolen_even_if_the_pid_is_reused(self, tmp_path):
        make_job(tmp_path)
        lock = rec.lock_path(tmp_path, "job_a")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": os.getpid(), "at": iso(T0)}))

        result = rec.tick(tmp_path, "job_a", launcher=FakeLauncher(), now=T0 + 10_000, stale_lock_s=300)

        assert result.status == "ok"
        assert "stale lock" in result.detail
