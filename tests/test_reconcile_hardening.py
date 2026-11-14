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

from tests.test_reconcile import FakeLauncher, make_job, SHA, T0, iso

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
        assert reloaded["integration"]["accepted_waves"][0]["wave"] == 1

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
    def test_a_dead_holders_lock_is_recovered_without_a_steal(self, tmp_path):
        make_job(tmp_path)
        lock = rec.lock_path(tmp_path, "job_a")
        lock.parent.mkdir(parents=True, exist_ok=True)
        # A file left by a dead process: no one holds the flock, so it is free.
        lock.write_text(json.dumps({"pid": 999_999_999, "at": iso(T0)}))

        result = rec.tick(tmp_path, "job_a", launcher=FakeLauncher(), now=T0 + 1)

        assert result.status == "ok"
        assert "recovered lock" in result.detail
        assert lock.exists()  # never unlinked: deleting it would allow a second inode

    def test_a_live_holder_is_never_displaced_even_when_the_lock_looks_old(self, tmp_path):
        make_job(tmp_path)
        held = rec._acquire_lock(tmp_path, "job_a", T0)
        assert held is not None

        # Far beyond any timeout: age alone must not take a live holder's lock.
        result = rec.tick(tmp_path, "job_a", launcher=FakeLauncher(), now=T0 + 100_000, stale_lock_s=1)

        assert result.status == "locked"
        held.release()

    def test_two_concurrent_recovery_processes_admit_exactly_one(self, tmp_path):
        import subprocess, sys, os
        script = (
            "import sys, time;"
            "from pathlib import Path;"
            "from bundlebees_plugin import reconcile as rec;"
            "lock = rec._acquire_lock(Path(sys.argv[1]), 'job_a', 1_800_000_000.0);"
            "print('acquired' if lock else 'blocked');"
            "time.sleep(1.0 if lock else 0.0)"
        )
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        procs = [
            subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], env=env, stdout=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        outputs = [proc.communicate(timeout=30)[0].strip() for proc in procs]
        assert sorted(outputs) == ["acquired", "blocked"], outputs


class IdentityAcrossJobs:
    __test__ = True

    def _uncertain(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain"]
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0, grace_s=600, max_uncertain_ticks=50)
        return launcher

    def test_a_matching_attempt_from_another_job_is_not_adopted(self, tmp_path):
        launcher = self._uncertain(tmp_path)

        brief = dict(launcher.posts[0])
        brief["job_id"] = "job_other"
        launcher.entries.append(
            {"attempt_id": "att_foreign", "state": "succeeded", "created_at": iso(T0), "brief": brief}
        )

        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50)

        assert result.status == "reconcile_wait"
        assert len(launcher.posts) == 1

    def test_a_matching_attempt_for_another_task_is_not_adopted(self, tmp_path):
        launcher = self._uncertain(tmp_path)

        brief = dict(launcher.posts[0])
        brief["task_id"] = "T9"
        launcher.entries.append(
            {"attempt_id": "att_foreign", "state": "succeeded", "created_at": iso(T0), "brief": brief}
        )

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50).status == "reconcile_wait"

    def test_a_confirmed_intent_is_not_adopted_from_a_mismatched_record(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()

        # Build the crash state directly: task pending, confirmed intent on disk,
        # and a journal record whose brief belongs to something else.
        intent = {
            "task_id": "T1",
            "attempt_no": 1,
            "idempotency_key": "abc",
            "status": "dispatched",
            "written_at": T0,
            "first_uncertain_at": None,
            "uncertain_ticks": 0,
            "attempt_id": "att_wrong",
            "fingerprint": {
                "objective": "reject malformed source_rev",
                "source_rev": SHA,
                "assigned_files": ["tools/x.py"],
            },
        }
        rec.intent_path(tmp_path, "job_a", "T1", 1).parent.mkdir(parents=True, exist_ok=True)
        rec.intent_path(tmp_path, "job_a", "T1", 1).write_text(json.dumps(intent))
        launcher.entries.append(
            {
                "attempt_id": "att_wrong",
                "state": "succeeded",
                "created_at": iso(T0),
                "brief": {"objective": "different work", "source_rev": SHA, "assigned_files": ["tools/x.py"]},
            }
        )

        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1, grace_s=600, max_uncertain_ticks=50)

        assert result.action != "adopt"
        assert launcher.posts == []


class CorruptIntent:
    __test__ = True
    def test_a_corrupt_intent_fails_closed_instead_of_dispatching(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        path = rec.intent_path(tmp_path, "job_a", "T1", 1)
        path.write_text("{not json")

        job = jobs.load_job(tmp_path, "job_a")
        job["tasks"][0]["state"] = "pending"
        job["tasks"][0]["intent"] = None
        jobs.save_job(tmp_path, job)

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert exc.value.code == "intent_corrupt"
        assert len(launcher.posts) == 1

    def test_an_empty_intent_file_fails_closed(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        rec.intent_path(tmp_path, "job_a", "T1", 1).write_text("")
        job = jobs.load_job(tmp_path, "job_a")
        job["tasks"][0]["state"] = "pending"
        job["tasks"][0]["intent"] = None
        jobs.save_job(tmp_path, job)

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert exc.value.code == "intent_corrupt"
        assert len(launcher.posts) == 1


class MultiWave:
    __test__ = True
    def test_a_dependent_task_completes_its_own_wave(self, tmp_path):
        make_job(
            tmp_path,
            tasks=[
                task(task_id="T1"),
                task(task_id="T2", objective="second", lease=["tools/y.py"], depends_on=["T1"]),
            ],
        )
        launcher = FakeLauncher()
        verifier = lambda ctx: {"ok": True, "evidence": "green"}
        integrator = lambda job: {"ok": True, "evidence": "suite green"}

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)                                  # T1 dispatch
        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=verifier, now=T0 + 1)           # T1 verify
        first = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=integrator, now=T0 + 2)
        assert first.action == "integrate"

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)                              # T2 dispatch
        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=verifier, now=T0 + 4)           # T2 verify
        second = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=integrator, now=T0 + 5)

        assert second.action == "integrate"
        reloaded = jobs.load_job(tmp_path, "job_a")
        assert [t["state"] for t in reloaded["tasks"]] == ["accepted", "accepted"]
        assert [w["wave"] for w in reloaded["integration"]["accepted_waves"]] == [1, 2]


class IntegrationBudget:
    __test__ = True
    def test_failure_budget_is_terminal_and_recorded_once(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        verifier = lambda ctx: {"ok": True, "evidence": "green"}
        calls = []

        def failing(job):
            calls.append(1)
            return {"ok": False, "evidence": "suite red"}

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=verifier, now=T0 + 1)
        first = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=failing, now=T0 + 2)
        second = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=failing, now=T0 + 3)
        third = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=failing, now=T0 + 4)

        assert first.status == "ok"
        assert second.status == "escalated"
        assert third.status == "idle"
        assert len(calls) == 2
        escalations = jobs.load_job(tmp_path, "job_a").get("escalations") or []
        assert [e["code"] for e in escalations].count("integration_failed") == 1


class PartialWave:
    __test__ = True
    def test_a_partial_wave_is_refused_without_authorization(self, tmp_path):
        make_job(
            tmp_path,
            tasks=[task(task_id="T1"), task(task_id="T2", objective="second", lease=["tools/y.py"])],
        )
        launcher = FakeLauncher()
        calls = []

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)  # both dispatch
        # T1 verifies, T2 fails.
        job = jobs.load_job(tmp_path, "job_a")
        job["tasks"][0]["state"] = "dispatched"
        job["tasks"][1]["state"] = "dispatched"
        jobs.save_job(tmp_path, job)

        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=lambda ctx: {"ok": ctx["task"]["task_id"] == "T1", "evidence": "one green, one red"}, now=T0 + 1)
        rec.tick(tmp_path, "job_a", launcher=launcher, verifier=lambda ctx: {"ok": ctx["task"]["task_id"] == "T1", "evidence": "one green, one red"}, now=T0 + 2)
        result = rec.tick(tmp_path, "job_a", launcher=launcher, integrator=lambda job: calls.append(1) or {"ok": True, "evidence": "x"}, now=T0 + 3)

        assert result.status == "escalated"
        assert result.evidence == "wave_incomplete"
        assert calls == []
        assert (jobs.load_job(tmp_path, "job_a")["integration"] or {}).get("terminal") is True


class RunningReap:
    __test__ = True
    def test_running_can_transition_to_verifying_without_a_self_transition(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        job = jobs.load_job(tmp_path, "job_a")
        jobs.transition(job, "T1", "running", actor="reconciler")
        jobs.save_job(tmp_path, job)

        result = rec.tick(tmp_path, "job_a", launcher=launcher, verifier=lambda ctx: {"ok": True, "evidence": "green"}, now=T0 + 1)

        assert result.transitions == ("T1->verifying", "T1->verified")
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "verified"
