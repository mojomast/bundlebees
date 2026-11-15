"""Local trials for the restart harness: processes, deadline, proofs, records.

Nothing here starts a launcher, a worker or a host service. The only child
processes are short-lived local scripts and the clearly labelled rehearsal
driver, which refuses to run outside a rehearsal state directory.
"""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bundlebees_plugin import campaign, job as jobs, queue as queue_mod, reconcile, revision, verify_local
from bundlebees_plugin import trial_harness as th

PY = sys.executable
SHA = "a" * 40
REPO = "mojomast/trial-fixture"
ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures: durable job state without a worker, a repo or a launcher
# ---------------------------------------------------------------------------


def make_job(
    state: Path,
    *,
    job_id: str = "job_t",
    campaign_id: str = "camp_t",
    tasks: list[dict[str, Any]] | None = None,
    bees: int = 1,
) -> dict[str, Any]:
    state.mkdir(parents=True, exist_ok=True)
    jobs.record_authorization(
        state,
        job_id=job_id,
        statement="Local trial fixture; nothing outside the state directory is touched.",
        repo=REPO,
        base_rev=SHA,
        scope=["src/**", "tests/**"],
        bees=bees,
        wall_clock_s=600,
        require_tty=False,
        interactive=False,
        by="test",
    )
    jobs.create_job(
        state,
        job_id=job_id,
        summary="trial fixture",
        tasks=tasks
        or [
            {
                "task_id": "T1",
                "objective": "one slice",
                "lease": ["src/a.py"],
                "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"], "commands": ["pytest"]},
            }
        ],
    )
    if campaign_id:
        campaign.create_campaign(
            state,
            campaign_id=campaign_id,
            job_id=job_id,
            objective="local trial fixture",
            branch="feat/trial",
        )
    return {"state": state, "job_id": job_id, "campaign_id": campaign_id}


def dispatch_record(state: Path, job_id: str, *, task_id: str = "T1", attempt_id: str = "att-1",
                    attempt_no: int = 1, status: str = "dispatched") -> Path:
    path = reconcile.intent_path(state, job_id, task_id, attempt_no)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "attempt_no": attempt_no,
                "idempotency_key": f"key-{attempt_no}",
                "status": status,
                "attempt_id": attempt_id,
                "written_at": 1.0 + attempt_no,
                "fingerprint": {"job_id": job_id, "task_id": task_id},
            }
        ),
        encoding="utf-8",
    )
    return path


def complete_job(state: Path, job_id: str, *, attempt_id: str = "att-1") -> dict[str, Any]:
    """Record exactly one dispatch, verification and accepted wave."""

    job = jobs.load_job(state, job_id, recover=False)
    task_id = str(job["tasks"][0]["task_id"])
    jobs.transition(job, task_id, "dispatching", actor="test", reason="dispatch")
    jobs.transition(job, task_id, "dispatched", actor="test", reason="dispatch")
    job["tasks"][0]["attempts"] = [attempt_id]
    dispatch_record(state, job_id, task_id=task_id, attempt_id=attempt_id)
    jobs.save_job(state, job)

    record = {
        "ok": True,
        "sha": "b" * 40,
        "base_rev": SHA,
        "gitleaks": {"exit_code": 0},
        "suite": {"exit_code": 0},
    }
    path = verify_local.record_path(state, attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    for step in ("running", "verifying", "verified"):
        jobs.transition(job, task_id, step, actor="test", reason=step)
    wave_id = revision.wave_id_for(job_id, 1)
    revision._write_wave(
        state,
        wave_id,
        {"wave_id": wave_id, "accepted": True, "head_sha": "b" * 40, "attempts": [attempt_id],
         "problems": {}, "at": "2026-01-01T00:00:00Z"},
    )
    integration = job.setdefault("integration", {"wave": "w1"})
    integration.update(
        state="pending",
        last_commit="b" * 40,
        accepted_waves=[{"wave": 1, "commit": "b" * 40, "evidence": "test", "attempts": [attempt_id]}],
        wave=2,
    )
    jobs.transition(job, task_id, "integrating", actor="test", reason="wave accepted")
    jobs.transition(job, task_id, "accepted", actor="test", reason="wave accepted")
    jobs.save_job(state, job)
    assert queue_mod.job_accepted_reasons(state, job_id) == []
    return job


def campaign_job_complete(state: Path, job_id: str, campaign_id: str) -> bool:
    return campaign.resume_plan(state, campaign_id)["phase"] == "job_complete"


# ---------------------------------------------------------------------------
# Process control: the recorded PID is the process, and it is confirmed dead
# ---------------------------------------------------------------------------


CHILD_SLEEPER = """
import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
"""

CHILD_IGNORES_SIGTERM = """
import os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(60)
"""


def write_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def wait_for_file(path: Path, *, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.005)
    raise AssertionError(f"{path} was not written")


class ProcessControl:
    __test__ = True

    def test_the_recorded_pid_is_the_actual_child_and_shutdown_is_confirmed(self, tmp_path):
        pid_file = tmp_path / "pid"
        script = write_script(tmp_path, "sleeper.py", CHILD_SLEEPER)
        log = tmp_path / "driver.log"
        clock = th.SystemClock()
        argv = (PY, str(script), str(pid_file))

        record = th.start_driver(
            argv, run=1, cwd=tmp_path, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            log_path=log, clock=clock, validate_argv=False,
        )
        try:
            child_pid = int(wait_for_file(pid_file))
            assert record.pid == child_pid, "the recorded pid must be the child itself, not a wrapper"
            assert th._proc_cmdline(record.pid) == argv
            assert record.identity_verified is True
            assert record.returncode is None

            report = th.stop_driver(
                record,
                deadline=th.Deadline.start(30.0, clock=clock),
                clock=clock,
                term_grace_s=5.0,
                kill_grace_s=2.0,
            )

            assert report.request_signal == "SIGTERM"
            assert report.returncode is not None, "returncode must be set before shutdown is confirmed"
            assert report.shutdown_confirmed is True
            assert report.code == "stopped"
            assert [item["pid"] for item in report.signals] == [record.pid]
            assert record.proc.poll() is not None
            with pytest.raises(ProcessLookupError):
                os.kill(record.pid, 0)
        finally:
            record.close_log()
            if record.returncode is None:  # pragma: no cover - defensive cleanup
                record.proc.kill()

    def test_an_arbitrary_python_script_is_not_the_driver(self, tmp_path):
        script = write_script(tmp_path, "not-the-driver.py", CHILD_SLEEPER)
        with pytest.raises(th.TrialRefused) as caught:
            th.start_driver(
                (PY, str(script)),
                run=1,
                cwd=tmp_path,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                log_path=tmp_path / "log",
                clock=th.SystemClock(),
            )
        assert caught.value.code == "driver_argv_not_the_driver"

    def test_a_shell_pipeline_is_refused_before_any_process_is_started(self, tmp_path):
        with pytest.raises(th.TrialRefused) as shell:
            th.validate_driver_argv(["bash", "-c", "python -m bundlebees_plugin.factory_driver"])
        assert shell.value.code == "driver_argv_is_a_shell"

        with pytest.raises(th.TrialRefused) as pipeline:
            th.validate_driver_argv(
                [PY, "-m", "bundlebees_plugin.factory_driver", "|", "tee", "log"]
            )
        assert pipeline.value.code == "driver_argv_is_a_pipeline"

        with pytest.raises(th.TrialRefused) as wrong:
            th.validate_driver_argv([PY, "-m", "some.other.module"])
        assert wrong.value.code == "driver_argv_not_the_driver"

    def test_a_process_that_is_not_the_started_argv_is_never_recorded(self, tmp_path, monkeypatch):
        script = write_script(tmp_path, "sleeper.py", CHILD_SLEEPER)
        pid_file = tmp_path / "pid"
        monkeypatch.setattr(th, "_proc_cmdline", lambda pid: ("/bin/bash", "-c", "python -m x"))
        with pytest.raises(th.TrialRefused) as caught:
            th.start_driver(
                (PY, str(script), str(pid_file)),
                run=1,
                cwd=tmp_path,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                log_path=tmp_path / "log",
                clock=th.SystemClock(),
                validate_argv=False,
            )
        assert caught.value.code == "driver_identity_mismatch"

    def test_cleanup_cannot_outlive_the_deadline_and_escalates_to_sigkill(self, tmp_path):
        pid_file = tmp_path / "pid"
        script = write_script(tmp_path, "stubborn.py", CHILD_IGNORES_SIGTERM)
        clock = th.SystemClock()
        record = th.start_driver(
            (PY, str(script), str(pid_file)),
            run=1,
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            log_path=tmp_path / "log",
            clock=clock,
            validate_argv=False,
        )
        try:
            assert int(wait_for_file(pid_file)) == record.pid
            deadline = th.Deadline.start(1.5, clock=clock)
            started = time.monotonic()
            report = th.stop_driver(
                record, deadline=deadline, clock=clock, term_grace_s=30.0, kill_grace_s=30.0
            )
            elapsed = time.monotonic() - started

            assert report.returncode is not None
            assert report.code == "stopped_after_deadline_sigkill"
            assert report.shutdown_confirmed is True
            assert [item["reason"] for item in report.signals] == ["trial_stop", "deadline_expired"]
            assert elapsed < 3.5, f"cleanup ran {elapsed:.2f}s against a 1.5s deadline"
            for wait in report.waits:
                assert wait["budget_s"] <= wait["remaining_s"] + 1e-9, wait
        finally:
            record.close_log()
            if record.returncode is None:  # pragma: no cover
                record.proc.kill()
                record.proc.wait(timeout=5)

    def test_an_expired_deadline_never_waits_for_the_child(self, tmp_path):
        pid_file = tmp_path / "pid"
        script = write_script(tmp_path, "stubborn2.py", CHILD_IGNORES_SIGTERM)
        clock = th.ManualClock(1000.0)
        record = th.start_driver(
            (PY, str(script), str(pid_file)),
            run=1,
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            log_path=tmp_path / "log",
            clock=clock,
            validate_argv=False,
        )
        try:
            assert int(wait_for_file(pid_file)) == record.pid
            deadline = th.Deadline.start(10.0, clock=clock)
            clock.advance(11.0)
            report = th.stop_driver(
                record, deadline=deadline, clock=clock, term_grace_s=30.0, kill_grace_s=30.0
            )
            assert report.waits == [], "an expired deadline must not wait at all"
            assert [item["reason"] for item in report.signals] == ["deadline_expired"]
            assert report.code == "stopped_after_deadline_sigkill"
            assert report.returncode is not None
            assert report.shutdown_confirmed is True
        finally:
            try:
                record.proc.kill()
                record.proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):  # pragma: no cover
                pass
            record.close_log()


# ---------------------------------------------------------------------------
# One deadline across both runs
# ---------------------------------------------------------------------------


def rehearsal_trial(
    tmp_path: Path,
    *,
    clock: th.Clock | None = None,
    completion_polls: int = 1,
    trial_kwargs: dict[str, Any] | None = None,
    **plan_overrides: Any,
) -> th.RestartTrial:
    """A rehearsal trial whose simulated worker writes the downtime result."""

    state = tmp_path / "state"
    fixture = th.build_rehearsal_state(state)
    trial_dir = state / "trials" / "rehearsal"
    trial_dir.mkdir(parents=True, exist_ok=True)
    worker_result = trial_dir / "worker-result.json"

    def complete_worker() -> None:
        worker_result.write_text(
            json.dumps({"attempt_id": th.REHEARSAL_ATTEMPT, "state": "succeeded"}),
            encoding="utf-8",
        )

    active_clock = clock if clock is not None else th.SystemClock()
    values: dict[str, Any] = dict(
        state_dir=state,
        job_id=fixture["job_id"],
        campaign_id=fixture["campaign_id"],
        checkout=th.Checkout(
            root=ROOT,
            contexts=th.default_contexts(rehearsal=True, coordinator_python=PY),
        ),
        driver_argv=th.rehearsal_driver_argv(state, trial_dir=trial_dir, pass_number=1),
        restart_argv=th.rehearsal_driver_argv(state, trial_dir=trial_dir, pass_number=2),
        rehearsal=True,
        trial_id="rehearsal",
        ready_path=trial_dir / "driver-1.ready",
        report_path=trial_dir / "report.json",
        driver_env={"BUNDLEBEES_TRIAL_REHEARSAL": "1"},
        poll_interval_s=0.005,
        term_grace_s=1.0,
        kill_grace_s=0.5,
        observation_staleness_s=30.0,
    )
    values.update(plan_overrides)
    plan = th.TrialPlan(**values)
    probe = th.ScriptedWorkerProbe(
        clock=active_clock,
        attempt_id=th.REHEARSAL_ATTEMPT,
        completion_polls=completion_polls,
        on_complete=complete_worker,
    )
    return th.RestartTrial(plan, clock=active_clock, worker_probe=probe, **(trial_kwargs or {}))


class DeadlineTrial:
    __test__ = True

    def test_the_deadline_starts_once_before_run_one_and_never_resets(self, tmp_path):
        trial = rehearsal_trial(tmp_path, deadline_s=30.0)
        report = trial.run()

        assert report["verdict"] == "pass"
        assert report["deadline"]["starts"] == 1
        first_start = report["drivers"][0]["started_at_mono"]
        second_start = report["drivers"][1]["started_at_mono"]
        assert report["deadline"]["started_at_mono"] <= first_start < second_start
        assert second_start - report["deadline"]["started_at_mono"] <= 30.0

    def test_a_second_start_is_refused_when_run_one_and_downtime_consume_the_deadline(self, tmp_path):
        clock = th.ManualClock(1000.0, step=2.0)
        trial = rehearsal_trial(
            tmp_path,
            clock=clock,
            completion_polls=2,
            deadline_s=6.0,
            restart_min_remaining_s=4.0,
            poll_interval_s=1.0,
            trial_kwargs={"readiness": lambda: True},
        )
        with pytest.raises(th.TrialIncomplete) as caught:
            trial.run()

        assert caught.value.code == "deadline_exhausted_before_restart"
        report = caught.value.report
        assert report["verdict"] == "incomplete"
        assert len(report["drivers"]) == 1, "no second coordinator may start after the deadline"
        assert "restart_allowed" not in [step["step"] for step in report["timeline"]]
        assert any(limit.startswith("deadline_exhausted_before_restart:") for limit in report["limitations"])

    def test_downtime_that_never_completes_fails_closed_at_the_deadline(self, tmp_path):
        clock = th.ManualClock(1000.0, step=2.0)
        trial = rehearsal_trial(
            tmp_path,
            clock=clock,
            completion_polls=10_000,
            deadline_s=4.0,
            poll_interval_s=1.0,
            trial_kwargs={"readiness": lambda: True},
        )
        with pytest.raises(th.TrialIncomplete) as caught:
            trial.run()

        assert caught.value.code == "deadline_exhausted_during_downtime"
        report = caught.value.report
        assert len(report["drivers"]) == 1
        assert report["worker"] == {}, "no completion proof may be inferred from a timeout"
        assert any("deadline_exhausted_during_downtime" in limit for limit in report["limitations"])

    def test_a_stopped_run_two_is_not_reported_as_a_failed_coordinator(self, tmp_path):
        # Run 2 is resident: it signals completion through the durable campaign
        # record and the harness then stops its own process. That SIGTERM exit
        # is the harness ending its coordinator, not a failure.
        trial = rehearsal_trial(tmp_path, deadline_s=30.0)
        report = trial.run()
        steps = [step["step"] for step in report["timeline"]]
        assert "driver_2_stopped" in steps
        assert report["verdict"] == "pass"


# ---------------------------------------------------------------------------
# The pinned checkout and the three Python contexts
# ---------------------------------------------------------------------------


class CheckoutPin:
    __test__ = True

    def test_a_root_without_the_package_is_refused(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        checkout = th.Checkout(
            root=empty, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
        )
        with pytest.raises(th.TrialRefused) as caught:
            checkout.verify()
        assert caught.value.code == "checkout_invalid"

    def test_a_missing_root_is_refused(self, tmp_path):
        checkout = th.Checkout(
            root=tmp_path / "does-not-exist",
            contexts=th.default_contexts(rehearsal=True, coordinator_python=PY),
        )
        with pytest.raises(th.TrialRefused) as caught:
            checkout.verify()
        assert caught.value.code == "checkout_invalid"

    def test_an_import_from_outside_the_checkout_is_refused(self, tmp_path):
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
        )
        with pytest.raises(th.TrialRefused) as caught:
            checkout.verify(
                probe_result={
                    "file": str(tmp_path / "elsewhere" / "bundlebees_plugin" / "__init__.py"),
                    "executable": PY,
                }
            )
        assert caught.value.code == "checkout_mismatch"
        assert "outside the pinned checkout" in caught.value.message

    def test_a_probe_that_cannot_import_the_package_is_named(self, tmp_path):
        stub = tmp_path / "stub"
        (stub / "bundlebees_plugin").mkdir(parents=True)
        (stub / "bundlebees_plugin" / "__init__.py").write_text(
            "raise RuntimeError('stale copy')\n", encoding="utf-8"
        )
        checkout = th.Checkout(
            root=stub, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
        )
        with pytest.raises(th.TrialRefused) as caught:
            checkout.verify()
        assert caught.value.code == "checkout_probe_failed"

    def test_a_namespace_package_is_not_a_checkout(self, tmp_path):
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
        )
        with pytest.raises(th.TrialRefused) as caught:
            checkout.verify(probe_result={"file": None, "executable": PY})
        assert caught.value.code == "checkout_not_importable"

    def test_the_pinned_root_precedes_a_decoy_checkout_on_pythonpath(self, tmp_path):
        decoy = tmp_path / "decoy"
        (decoy / "bundlebees_plugin").mkdir(parents=True)
        (decoy / "bundlebees_plugin" / "__init__.py").write_text(
            "raise RuntimeError('decoy checkout must never be imported')\n", encoding="utf-8"
        )
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
        )
        env = checkout.child_env(pythonpath_tail=str(decoy))
        assert env["PYTHONPATH"].split(os.pathsep) == [str(ROOT), str(decoy)]

        done = subprocess.run(
            [PY, "-c", th.PROBE_SOURCE], capture_output=True, text=True, env=env, cwd=str(ROOT)
        )
        assert done.returncode == 0, done.stderr
        reported = json.loads(done.stdout)["file"]
        assert Path(reported).resolve().is_relative_to(ROOT.resolve())

    def test_the_child_environment_does_not_inherit_an_ambient_pythonpath(self):
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
        )
        env = checkout.child_env(base={"PATH": "/usr/bin:/bin", "PYTHONPATH": "/polluted/site-packages"})
        assert env["PYTHONPATH"] == str(ROOT)
        assert env["PYTHONNOUSERSITE"] == "1"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_the_three_contexts_are_distinct_and_recorded(self):
        contexts = th.default_contexts(rehearsal=False, coordinator_python=PY)
        contexts.validate()
        recorded = contexts.to_dict()
        assert recorded["coordinator"]["interpreter"] == PY
        assert recorded["sandbox_suite"]["interpreter"] == "/usr/bin/python3"
        assert recorded["worker"]["kind"] == "real"
        assert recorded["worker"]["interpreter"] is None

    def test_the_sandbox_suite_may_not_be_the_project_venv(self):
        contexts = th.PythonContexts(
            coordinator=th.PythonContext("coordinator", PY),
            sandbox_suite=th.PythonContext("sandbox-suite", PY),
            worker=th.WorkerContext("real", host="launcher"),
        )
        with pytest.raises(th.TrialRefused) as caught:
            contexts.validate()
        assert caught.value.code == "interpreter_conflict"

        other_venv = Path(PY).parent / "python3"
        assert other_venv.exists(), other_venv
        contexts = th.PythonContexts(
            coordinator=th.PythonContext("coordinator", PY),
            sandbox_suite=th.PythonContext("sandbox-suite", str(other_venv)),
            worker=th.WorkerContext("fake", interpreter=PY),
        )
        with pytest.raises(th.TrialRefused) as caught:
            contexts.validate()
        assert caught.value.code == "sandbox_suite_is_a_venv"

    def test_a_real_worker_context_has_no_local_interpreter(self):
        contexts = th.PythonContexts(
            coordinator=th.PythonContext("coordinator", PY),
            sandbox_suite=th.PythonContext("sandbox-suite", "/usr/bin/python3"),
            worker=th.WorkerContext("real", interpreter=PY, host="launcher"),
        )
        with pytest.raises(th.TrialRefused) as caught:
            contexts.validate()
        assert caught.value.code == "worker_context_invalid"

    def test_a_fake_worker_needs_an_interpreter(self):
        with pytest.raises(th.TrialRefused) as caught:
            th.WorkerContext("fake").validate()
        assert caught.value.code == "worker_context_invalid"


class SuiteCommand:
    __test__ = True

    def test_the_direct_runner_is_a_system_python_c_program(self):
        command = th.direct_test_runner_command("/usr/bin/python3", "tests.test_smoke", "test_answer_is_42")
        assert command[0] == "/usr/bin/python3"
        assert command[1] == "-c"
        assert "sys.path.insert(0, '.')" in command[2]
        assert "tests.test_smoke" in command[2] and "test_answer_is_42()" in command[2]
        assert "pytest" not in command[1]

    def test_a_suite_command_is_never_defaulted(self):
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=False, coordinator_python=PY)
        )
        argv = (PY, "-m", "bundlebees_plugin.factory_driver", "--once")
        with pytest.raises(th.TrialRefused) as caught:
            checkout.assert_suite_command(argv, ())
        assert caught.value.code == "suite_command_not_configured"

    def test_a_venv_suite_interpreter_is_refused(self):
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=False, coordinator_python=PY)
        )
        suite = (PY, "-c", "print(1)")
        argv = (PY, "-m", "bundlebees_plugin.factory_driver", "--once", "--suite-command", *suite)
        with pytest.raises(th.TrialRefused) as caught:
            checkout.assert_suite_command(argv, suite)
        assert caught.value.code == "suite_interpreter_is_a_venv"

    def test_the_coordinator_must_name_the_configured_suite_exactly(self):
        checkout = th.Checkout(
            root=ROOT, contexts=th.default_contexts(rehearsal=False, coordinator_python=PY)
        )
        suite = th.direct_test_runner_command("/usr/bin/python3", "tests.test_smoke", "test_answer_is_42")
        missing = (PY, "-m", "bundlebees_plugin.factory_driver", "--once")
        with pytest.raises(th.TrialRefused) as caught:
            checkout.assert_suite_command(missing, suite)
        assert caught.value.code == "suite_command_not_pinned"

        pytest_tail = ("/usr/bin/python3", "-m", "pytest", "-q")
        wrong = (PY, "-m", "bundlebees_plugin.factory_driver", "--once", "--suite-command", *pytest_tail)
        with pytest.raises(th.TrialRefused) as caught:
            checkout.assert_suite_command(wrong, suite)
        assert caught.value.code == "suite_command_mismatch"

        right = (PY, "-m", "bundlebees_plugin.factory_driver", "--once", "--suite-command", *suite)
        assert checkout.assert_suite_command(right, suite)["suite_command"] == list(suite)


# ---------------------------------------------------------------------------
# Durable readiness (no foreign ready-file writer)
# ---------------------------------------------------------------------------


class Readiness:
    __test__ = True

    def test_dispatch_readiness_reads_the_job_attempt_and_state(self, tmp_path):
        fixture = make_job(tmp_path / "state")
        state, job_id = fixture["state"], fixture["job_id"]
        assert th.job_readiness(state, job_id, "dispatch") is False
        assert th.job_readiness(state, job_id, "worker-running") is False

        job = jobs.load_job(state, job_id, recover=False)
        jobs.transition(job, "T1", "dispatching", actor="test", reason="dispatch")
        jobs.save_job(state, job)
        assert th.job_readiness(state, job_id, "dispatch") is False, "dispatching is not a recorded dispatch"

        jobs.transition(job, "T1", "dispatched", actor="test", reason="dispatch")
        job["tasks"][0]["attempts"] = ["att-1"]
        jobs.save_job(state, job)
        assert th.job_readiness(state, job_id, "dispatch") is True
        assert th.job_readiness(state, job_id, "worker-running") is False

        jobs.transition(job, "T1", "running", actor="test", reason="worker started")
        jobs.save_job(state, job)
        assert th.job_readiness(state, job_id, "worker-running") is True

    def test_job_complete_readiness_reads_the_campaign_plan(self, tmp_path):
        fixture = make_job(tmp_path / "state")
        state, job_id, campaign_id = fixture["state"], fixture["job_id"], fixture["campaign_id"]
        assert th.campaign_job_complete(state, campaign_id) is False

        complete_job(state, job_id)

        assert th.campaign_job_complete(state, campaign_id) is True

    def test_job_complete_readiness_needs_a_campaign(self, tmp_path):
        with pytest.raises(th.TrialRefused) as caught:
            th.campaign_job_complete(tmp_path, "")
        assert caught.value.code == "campaign_required"

    def test_the_file_readiness_still_works_for_tests(self, tmp_path):
        target = tmp_path / "ready"
        assert th.readiness_callable(tmp_path, "job", mode="file", path=target)() is False
        target.write_text("ok", encoding="utf-8")
        assert th.readiness_callable(tmp_path, "job", mode="file", path=target)() is True
        with pytest.raises(th.TrialRefused) as caught:
            th.readiness_callable(tmp_path, "job", mode="file", path=None)
        assert caught.value.code == "ready_file_missing"

    def test_an_unknown_readiness_mode_is_refused(self, tmp_path):
        with pytest.raises(th.TrialRefused) as caught:
            th.readiness_callable(tmp_path, "job", mode="whenever")
        assert caught.value.code == "readiness_mode_invalid"


# ---------------------------------------------------------------------------
# Exactly one dispatch, verification and accepted wave -- from records
# ---------------------------------------------------------------------------


class ExactlyOnce:
    __test__ = True

    def _state(self, tmp_path) -> tuple[Path, str]:
        fixture = make_job(tmp_path / "state")
        complete_job(fixture["state"], fixture["job_id"])
        return fixture["state"], fixture["job_id"]

    def test_one_of_each_is_accepted_and_counted_from_records(self, tmp_path):
        state, job_id = self._state(tmp_path)
        evidence = th.collect_evidence(state, job_id)
        counts = evidence.assert_exactly_once()

        assert counts["dispatch_records"] == 1
        assert counts["job_attempts"] == 1
        assert counts["verification_records"] == 1
        assert counts["verify_transitions"] == 1
        assert counts["accepted_waves"] == 1
        assert counts["accepted_wave_records"] == 1
        assert evidence.dispatches[0].attempt_id == "att-1"
        assert evidence.verifications[0].sha == "b" * 40

    def test_a_duplicate_dispatch_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        dispatch_record(state, job_id, attempt_no=2, attempt_id="att-2")

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "duplicate_dispatch"

    def test_an_intent_without_an_attempt_is_not_proof_of_a_dispatch(self, tmp_path):
        state, job_id = self._state(tmp_path)
        path = reconcile.intent_path(state, job_id, "T1", 1)
        body = json.loads(path.read_text(encoding="utf-8"))
        body["attempt_id"] = ""
        path.write_text(json.dumps(body), encoding="utf-8")

        with pytest.raises(th.TrialIncomplete) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "dispatch_unproven"

    def test_a_second_attempt_on_the_job_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        job = jobs.load_job(state, job_id, recover=False)
        job["tasks"][0]["attempts"].append("att-2")
        jobs.save_job(state, job)

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "duplicate_attempt"

    def test_a_duplicate_verification_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        job = jobs.load_job(state, job_id, recover=False)
        job["events"].append(
            {"at": "2026-01-01T00:00:01Z", "task_id": "T1", "transition": "verifying->verified",
             "actor": "test", "reason": "again"}
        )
        jobs.save_job(state, job)

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "duplicate_verification"

    def test_a_missing_verification_record_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        verify_local.record_path(state, "att-1").unlink()

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "missing_verification"

    def test_a_failed_gate_in_the_verification_record_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        path = verify_local.record_path(state, "att-1")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["suite"]["exit_code"] = 1
        path.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "verification_gates_failed"

    def test_a_second_accepted_wave_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        job = jobs.load_job(state, job_id, recover=False)
        job["integration"]["accepted_waves"].append(
            {"wave": 2, "commit": "c" * 40, "evidence": "duplicate", "attempts": ["att-1"]}
        )
        jobs.save_job(state, job)
        wave_id = revision.wave_id_for(job_id, 2)
        revision._write_wave(
            state,
            wave_id,
            {"wave_id": wave_id, "accepted": True, "head_sha": "c" * 40, "attempts": ["att-1"]},
        )

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "duplicate_accepted_wave"

    def test_a_missing_accepted_wave_is_refused(self, tmp_path):
        state = tmp_path / "state"
        make_job(state)
        job = jobs.load_job(state, "job_t", recover=False)
        jobs.transition(job, "T1", "dispatching", actor="test", reason="dispatch")
        jobs.transition(job, "T1", "dispatched", actor="test", reason="dispatch")
        job["tasks"][0]["attempts"] = ["att-1"]
        dispatch_record(state, "job_t", attempt_id="att-1")
        jobs.save_job(state, job)
        path = verify_local.record_path(state, "att-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ok": True, "sha": "b" * 40, "base_rev": SHA,
                        "gitleaks": {"exit_code": 0}, "suite": {"exit_code": 0}}),
            encoding="utf-8",
        )
        for step in ("running", "verifying", "verified"):
            jobs.transition(job, "T1", step, actor="test", reason=step)
        jobs.save_job(state, job)

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, "job_t").assert_exactly_once()
        assert caught.value.code == "missing_accepted_wave"

    def test_a_wave_record_that_disagrees_with_the_job_is_refused(self, tmp_path):
        state, job_id = self._state(tmp_path)
        path = state / "waves" / f"{revision.wave_id_for(job_id, 1)}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["head_sha"] = "d" * 40
        path.write_text(json.dumps(record), encoding="utf-8")

        with pytest.raises(th.TrialRefused) as caught:
            th.collect_evidence(state, job_id).assert_exactly_once()
        assert caught.value.code == "accepted_wave_mismatch"


# ---------------------------------------------------------------------------
# Worker proofs use monotonic observations, never inference
# ---------------------------------------------------------------------------


def observation(state: str, at: float, attempt: str = "att-1") -> th.WorkerObservation:
    return th.WorkerObservation(attempt, state, at, source="test")


class WorkerProof:
    __test__ = True

    def test_only_a_running_state_counts_as_running(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.running_attempt([observation("queued", 1.0)])
        assert caught.value.code == "worker_not_running"

        assert th.running_attempt([observation("running", 1.0)]).attempt_id == "att-1"

    def test_two_running_attempts_are_ambiguous(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.running_attempt([observation("running", 1.0, "a"), observation("running", 1.0, "b")])
        assert caught.value.code == "worker_ambiguous"

    def test_a_stale_observation_cannot_prove_the_worker_was_running_at_the_stop(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.prove_running_at_stop(
                pre_stop=observation("running", 1.0),
                stop_at=10.0,
                observations=[observation("running", 1.0)],
                staleness_s=2.0,
            )
        assert caught.value.code == "worker_running_stale"

    def test_a_terminal_attempt_before_the_stop_is_not_running(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.prove_running_at_stop(
                pre_stop=observation("succeeded", 1.0),
                stop_at=2.0,
                observations=[observation("succeeded", 1.0)],
                staleness_s=10.0,
            )
        assert caught.value.code == "worker_running_unproven"

    def test_completion_must_be_observed_after_the_driver_exit(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.prove_downtime_completion(
                attempt_id="att-1",
                driver_exit_at=5.0,
                observations=[observation("succeeded", 4.0)],
                restart_started_at=None,
            )
        assert caught.value.code == "worker_completion_unproven"

    def test_a_failed_attempt_is_not_completion(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.prove_downtime_completion(
                attempt_id="att-1",
                driver_exit_at=5.0,
                observations=[observation("failed", 6.0)],
                restart_started_at=None,
            )
        assert caught.value.code == "worker_completion_failed"

    def test_completion_observed_after_the_restart_is_not_downtime_completion(self):
        with pytest.raises(th.TrialIncomplete) as caught:
            th.prove_downtime_completion(
                attempt_id="att-1",
                driver_exit_at=5.0,
                observations=[observation("succeeded", 9.0)],
                restart_started_at=8.0,
            )
        assert caught.value.code == "worker_completion_after_restart"

    def test_a_downtime_completion_is_proven_from_the_observation(self):
        proof = th.prove_downtime_completion(
            attempt_id="att-1",
            driver_exit_at=5.0,
            observations=[observation("running", 4.0), observation("succeeded", 6.0)],
            restart_started_at=7.0,
        )
        assert proof["state"] == "succeeded"
        assert proof["downtime_s"] == 1.0
        assert proof["observed_at_mono"] > proof["driver_exit_at_mono"]


# ---------------------------------------------------------------------------
# Read-only launcher and timeout semantics
# ---------------------------------------------------------------------------


class LauncherAndTimeouts:
    __test__ = True

    def test_the_launcher_view_refuses_every_write(self):
        class Client:
            def health(self):
                return {"ok": True, "version": "1.2.3"}

            def attempts(self):
                return [{"attempt_id": "att-1", "state": "running"}]

            def create(self, brief):  # pragma: no cover - must never be reached
                raise AssertionError("create must not be reachable")

            def cancel(self, attempt_id):  # pragma: no cover
                raise AssertionError("cancel must not be reachable")

        view = th.ReadOnlyLauncher(Client())
        assert view.health()["version"] == "1.2.3"
        assert view.attempts()[0]["attempt_id"] == "att-1"
        with pytest.raises(th.TrialRefused) as caught:
            view.create({"objective": "x"})
        assert caught.value.code == "launcher_write_refused"
        with pytest.raises(th.TrialRefused):
            view.cancel("att-1")

    def test_the_report_states_exactly_what_each_timeout_stops(self):
        overall = th.TIMEOUT_SEMANTICS["overall_deadline"]
        worker = th.TIMEOUT_SEMANTICS["launcher_worker_timeout"]
        assert overall["stops"] == [
            "the coordinator driver process this harness started",
            "the harness's own waits",
        ]
        assert "the worker attempt" in overall["does_not_stop"]
        assert worker["controlled_by"] == "launcher"
        assert worker["harness_cancellation"] is False
        assert "the worker attempt, and only the launcher can do that" in worker["stops"]

    def test_the_harness_source_has_no_group_shell_or_host_control_calls(self):
        tree = ast.parse(Path(th.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"killpg", "setsid", "pkill", "killall"}
            for keyword in node.keywords:
                if keyword.arg in {"shell", "start_new_session"}:
                    assert not (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                    ), f"{keyword.arg}=True is not allowed in the trial harness"
        strings = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not (strings & {"systemctl", "pkill", "killall"}), "no host-service control belongs in a trial"


# ---------------------------------------------------------------------------
# Approval receipts: bound, verified, consumed once; never a second system
# ---------------------------------------------------------------------------


class FakeApproval:
    def __init__(self, *, verify_result: dict[str, Any] | None = None,
                 consume_result: dict[str, Any] | None = None) -> None:
        self.verify_result = verify_result or {"ok": True, "receipt_id": "receipt-1"}
        self.consume_result = {"consumed": True} if consume_result is None else consume_result
        self.verified: list[dict[str, Any]] = []
        self.consumed: list[dict[str, Any]] = []

    def verify_receipt(self, receipt):
        self.verified.append(dict(receipt))
        return dict(self.verify_result)

    def consume_receipt(self, receipt):
        self.consumed.append(dict(receipt))
        return dict(self.consume_result)


class ApprovalBinding:
    __test__ = True

    def test_a_receipt_is_verified_and_consumed_once_for_the_trial(self, tmp_path):
        approval = FakeApproval()
        report = th.run_rehearsal(
            state_dir=tmp_path / "state",
            keep=True,
            approval=approval,
            receipt={"receipt_id": "receipt-1", "code_rev": th.code_rev_of(ROOT)},
        )

        assert report["verdict"] == "pass"
        assert report["approval"]["verified"] is True
        assert report["approval"]["consumed"] is True
        assert report["approval"]["receipt_id"] == "receipt-1"
        assert len(approval.verified) == 1 and len(approval.consumed) == 1

    def test_a_malformed_consume_result_is_refused(self, tmp_path):
        with pytest.raises(th.TrialRefused) as caught:
            th.run_rehearsal(
                state_dir=tmp_path / "state",
                keep=True,
                approval=FakeApproval(consume_result={}),
                receipt={"receipt_id": "receipt-1", "code_rev": th.code_rev_of(ROOT)},
            )
        assert caught.value.code == "approval_receipt_not_consumed"

    def test_a_reused_rehearsal_report_is_refused(self, tmp_path):
        th.run_rehearsal(state_dir=tmp_path / "state", keep=True)
        with pytest.raises(th.TrialRefused) as caught:
            th.run_rehearsal(state_dir=tmp_path / "state", keep=True)
        assert caught.value.code == "trial_artifacts_already_exist"

    def test_an_already_consumed_receipt_is_refused(self, tmp_path):
        approval = FakeApproval(consume_result={"consumed": False})
        with pytest.raises(th.TrialRefused) as caught:
            th.run_rehearsal(
                state_dir=tmp_path / "state",
                approval=approval,
                receipt={"receipt_id": "receipt-1"},
            )
        assert caught.value.code == "approval_receipt_not_consumed"
        assert len(approval.verified) == 1

    def test_a_rejected_receipt_is_refused_before_consumption(self, tmp_path):
        approval = FakeApproval(verify_result={"ok": False, "reason": "not approved"})
        with pytest.raises(th.TrialRefused) as caught:
            th.run_rehearsal(
                state_dir=tmp_path / "state",
                approval=approval,
                receipt={"receipt_id": "receipt-1"},
            )
        assert caught.value.code == "approval_receipt_rejected"
        assert approval.consumed == []

    def test_a_receipt_for_another_code_rev_is_refused_without_widening(self, tmp_path):
        approval = FakeApproval()
        with pytest.raises(th.TrialRefused) as caught:
            th.run_rehearsal(
                state_dir=tmp_path / "state",
                approval=approval,
                receipt={"receipt_id": "receipt-1", "code_rev": "f" * 40},
            )
        assert caught.value.code == "approval_receipt_code_rev_mismatch"
        assert approval.verified == [] and approval.consumed == []

    def test_a_live_trial_without_the_approval_lane_refuses(self, tmp_path):
        fixture = make_job(tmp_path / "state")
        state = fixture["state"]
        suite = th.direct_test_runner_command("/usr/bin/python3", "tests.test_smoke", "test_answer_is_42")
        plan = th.TrialPlan(
            state_dir=state,
            job_id=fixture["job_id"],
            checkout=th.Checkout(
                root=ROOT, contexts=th.default_contexts(rehearsal=False, coordinator_python=PY)
            ),
            driver_argv=(
                PY, "-m", "bundlebees_plugin.factory_driver",
                "--state", str(state), "--interval", "1",
                "--suite-command", *suite,
            ),
            deadline_s=30.0,
            rehearsal=False,
            live_confirmed=True,
            suite_command=suite,
        )
        trial = th.RestartTrial(plan, worker_probe=None)
        with pytest.raises(th.TrialRefused) as caught:
            trial.run()
        assert caught.value.code == "approval_unavailable"

    def test_a_live_trial_without_a_receipt_refuses_before_starting(self, tmp_path):
        fixture = make_job(tmp_path / "state")
        state = fixture["state"]
        suite = th.direct_test_runner_command("/usr/bin/python3", "tests.test_smoke", "test_answer_is_42")
        plan = th.TrialPlan(
            state_dir=state,
            job_id=fixture["job_id"],
            checkout=th.Checkout(
                root=ROOT, contexts=th.default_contexts(rehearsal=False, coordinator_python=PY)
            ),
            driver_argv=(
                PY, "-m", "bundlebees_plugin.factory_driver",
                "--state", str(state), "--interval", "1",
                "--suite-command", *suite,
            ),
            deadline_s=30.0,
            rehearsal=False,
            live_confirmed=True,
            suite_command=suite,
        )
        trial = th.RestartTrial(plan, worker_probe=None, approval=FakeApproval())
        with pytest.raises(th.TrialRefused) as caught:
            trial.run()
        assert caught.value.code == "approval_receipt_missing"

    def test_a_live_trial_needs_an_explicit_confirmation(self, tmp_path):
        fixture = make_job(tmp_path / "state")
        plan = th.TrialPlan(
            state_dir=fixture["state"],
            job_id=fixture["job_id"],
            checkout=th.Checkout(
                root=ROOT, contexts=th.default_contexts(rehearsal=False, coordinator_python=PY)
            ),
            driver_argv=(PY, "-m", "bundlebees_plugin.factory_driver", "--once"),
            deadline_s=30.0,
            rehearsal=False,
            live_confirmed=False,
        )
        with pytest.raises(th.TrialRefused) as caught:
            th.RestartTrial(plan).run()
        assert caught.value.code == "live_trial_not_confirmed"

    def test_the_harness_does_not_implement_a_second_approval_system(self):
        tree = ast.parse(Path(th.__file__).read_text(encoding="utf-8"))
        top_level = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert not top_level & {
            "create_request",
            "verify_receipt",
            "consume_receipt",
            "approve",
            "issue_receipt",
        }


class Preflight:
    __test__ = True

    def test_a_multi_task_job_is_refused_before_anything_starts(self, tmp_path):
        state = tmp_path / "state"
        make_job(
            state,
            bees=2,
            tasks=[
                {"task_id": "T1", "objective": "a", "lease": ["src/a.py"]},
                {"task_id": "T2", "objective": "b", "lease": ["src/b.py"], "depends_on": ["T1"]},
            ],
        )
        plan = th.TrialPlan(
            state_dir=state,
            job_id="job_t",
            checkout=th.Checkout(
                root=ROOT, contexts=th.default_contexts(rehearsal=True, coordinator_python=PY)
            ),
            driver_argv=(PY, "-m", "bundlebees_plugin.factory_driver", "--once"),
            deadline_s=30.0,
            rehearsal=True,
            live_confirmed=False,
        )
        with pytest.raises(th.TrialRefused) as caught:
            th.RestartTrial(plan).run()
        assert caught.value.code == "trial_requires_single_task"
        assert caught.value.report["drivers"] == []


# ---------------------------------------------------------------------------
# Local rehearsal: process stop, downtime completion, restart adoption, closeout
# ---------------------------------------------------------------------------


class Rehearsal:
    __test__ = True

    def test_the_rehearsal_reaches_job_complete_with_closeout_and_owner_pending(self, tmp_path):
        report = th.run_rehearsal(state_dir=tmp_path / "state", keep=True)

        assert report["verdict"] == "pass", report.get("message")
        assert report["rehearsal"] is True
        assert report["code_rev"]["value"] == th.code_rev_of(ROOT)
        assert report["approval"]["required"] is False

        counts = report["evidence"]["counts"]
        assert counts == {
            "dispatch_records": 1,
            "dispatched_attempts": 1,
            "job_attempts": 1,
            "verification_records": 1,
            "verify_transitions": 1,
            "accepted_waves": 1,
            "accepted_wave_records": 1,
        }
        assert report["campaign"]["phase"] == "job_complete"
        assert report["campaign"]["requires_campaign_closeout"] is True
        assert report["campaign"]["should_continue"] is False
        assert report["campaign"]["next_action"] == "prepare_campaign_closeout"
        assert report["owner_acceptance"]["status"] == "pending"

        running = report["worker"]["running_at_stop"]
        completed = report["worker"]["completed_during_downtime"]
        driver_exit = report["drivers"][0]["started_at_mono"]  # monotonic domain check below
        assert running["age_s"] < running["staleness_limit_s"]
        assert completed["observed_at_mono"] > completed["driver_exit_at_mono"]
        assert completed["observed_at_mono"] < completed["restart_started_at_mono"]
        assert driver_exit <= running["stop_requested_at_mono"]

        stopped = [step for step in report["timeline"] if step["step"] == "driver_1_stopped"][0]["detail"]
        assert stopped["shutdown_confirmed"] is True
        assert stopped["returncode"] is not None
        assert stopped["pid"] == report["drivers"][0]["pid"]

    def test_every_report_states_both_timeout_scopes(self, tmp_path):
        report = th.run_rehearsal(state_dir=tmp_path / "state", keep=True)
        assert report["timeouts"] == th.TIMEOUT_SEMANTICS

    def test_the_simulated_driver_refuses_a_state_without_the_rehearsal_marker(self, tmp_path):
        state = tmp_path / "real-looking-state"
        state.mkdir()
        trial_dir = state / "trials" / "x"
        trial_dir.mkdir(parents=True)
        done = subprocess.run(
            [PY, "-m", "bundlebees_plugin.trial_simulated_driver",
             "--state", str(state), "--trial-dir", str(trial_dir), "--pass", "1"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            timeout=60,
        )
        assert done.returncode == 3
        assert "rehearsal_marker_missing" in done.stderr

    def test_the_rehearsal_cli_returns_zero_and_writes_the_report(self, tmp_path):
        report_path = tmp_path / "report.json"
        code = th.main(
            [
                "rehearse",
                "--source-root", str(ROOT),
                "--state", str(tmp_path / "state"),
                "--deadline-seconds", "30",
                "--keep",
                "--report", str(report_path),
            ]
        )
        assert code == 0
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["verdict"] == "pass"

    def test_a_rehearsal_state_is_not_deleted_when_the_caller_owns_it(self, tmp_path):
        state = tmp_path / "state"
        report = th.run_rehearsal(state_dir=state, keep=True)
        assert Path(report["rehearsal_dir"]) == state
        assert report["rehearsal_dir_removed"] is False
        assert (state / th.REHEARSAL_MARKER).exists()
        assert (state / "trials" / "rehearsal" / "report.json").exists()


# ---------------------------------------------------------------------------
# No candidate for a real trial is a "live" run by accident
# ---------------------------------------------------------------------------


def test_no_test_in_this_module_starts_a_launcher_or_a_worker():
    source = Path(__file__).read_text(encoding="utf-8")
    forbidden = ("Launcher" + "Client(", "job_cli." + "tick(")
    for token in forbidden:
        assert token not in source, f"a local trial test must not contain {token!r}"


def test_scratch_directories_are_cleaned_up(tmp_path):
    # A guard against a rehearsal leaking a background child: every run in this
    # module must leave no process behind that still holds the checkout.
    leftovers = [pid for pid in _children_of(os.getpid()) if _is_rehearsal_child(pid)]
    assert leftovers == [], f"rehearsal children still running: {leftovers}"


def _children_of(pid: int) -> list[int]:
    found: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            parts = stat.rsplit(")", 1)[-1].split()
            if len(parts) > 1 and parts[1] == str(pid):
                found.append(int(entry.name))
    except OSError:  # pragma: no cover - no /proc
        return []
    return found


def _is_rehearsal_child(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"bundlebees_plugin.trial_simulated_driver" in raw


@pytest.fixture(autouse=True)
def _no_stray_rehearsal_children():
    yield
    for pid in _children_of(os.getpid()):
        if not _is_rehearsal_child(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:  # pragma: no cover
            pass


def test_the_live_cli_never_accepts_a_launcher_token_argument():
    parser = th._parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "run", "--state", "s", "--job", "j", "--source-root", "r",
            "--deadline-seconds", "60", "--driver-argv", "[]",
            "--launcher-url", "http://127.0.0.1:1", "--launcher-token", "secret",
        ])
    help_text = parser.format_help()
    assert "--launcher-token" not in help_text
    assert "secret" not in help_text
