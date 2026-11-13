"""M2 crash and concurrency tests for the durable reconciler."""

from __future__ import annotations

import threading

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

from .test_reconcile import FakeLauncher, authorize_and_create


class Concurrency:
    __test__ = True

    def test_two_ticks_never_double_dispatch(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher()
        launcher.entered = threading.Event()
        launcher.release = threading.Event()
        results: list[rec.TickResult] = []

        def run_tick() -> None:
            results.append(
                rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
            )

        worker = threading.Thread(target=run_tick)
        worker.start()
        try:
            assert launcher.entered.wait(10), "first tick never reached the launcher"
            second = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
            assert second["status"] == "locked"
            assert second["transitions"] == []
            assert second["posts"] == 0
            assert len(launcher.posts) == 1
        finally:
            launcher.release.set()
            worker.join(10)

        assert not worker.is_alive()
        assert len(results) == 1
        assert results[0]["status"] == "dispatched"
        assert len(launcher.posts) == 1
        assert len(launcher.entries) == 1
        task = jobs.load_job(tmp_path, "job_a")["tasks"][0]
        assert task["state"] == "dispatched"
        dispatched = [
            event
            for event in jobs.load_job(tmp_path, "job_a")["events"]
            if event["transition"] == "dispatching->dispatched"
        ]
        assert len(dispatched) == 1


class Crash:
    __test__ = True

    def test_crash_before_post_is_retried(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(script=["not_sent", "ok"])

        first = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
        assert first["status"] == "not_sent"
        assert first["evidence"] == "not_sent"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatching"

        second = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
        assert second["status"] == "dispatched"
        assert second["evidence"] == "retried"
        task = jobs.load_job(tmp_path, "job_a")["tasks"][0]
        assert task["state"] == "dispatched"
        assert len(launcher.posts) == 2
        assert len(launcher.entries) == 1
        assert len(task["attempts"]) == 1

    def test_prepared_intent_before_post_is_retried_after_restart(self, tmp_path):
        job = authorize_and_create(tmp_path)
        jobs.transition(
            job,
            "T1",
            "dispatching",
            actor="reconciler",
            reason="simulated crash after the durable intent",
        )
        jobs.save_job(tmp_path, job)
        intent = rec._new_intent(job, job["tasks"][0])
        rec.save_intent(tmp_path, intent)
        launcher = FakeLauncher()

        result = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")

        assert result["status"] == "dispatched"
        assert len(launcher.posts) == 1
        assert len(launcher.entries) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatched"

    def test_lost_acknowledgement_is_adopted_not_redispatched(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(
            idempotent=True, script=["uncertain_present"]
        )

        result = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")

        assert result["status"] == "adopted"
        assert result["evidence"] == "proven_present"
        assert result["exactly_once"] is True
        assert result["posts"] == 1
        assert result["journal_calls"] == 1
        assert len(launcher.posts) == 1
        assert len(launcher.entries) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatched"

        again = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
        assert again["status"] == "noop"
        assert len(launcher.posts) == 1

    def test_delayed_journal_visibility_waits_then_adopts(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(
            idempotent=True, script=["uncertain"]
        )

        first = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=10,
        )
        assert first["status"] == "reconcile_wait"
        assert first["posts"] == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatching"

        second = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=10,
        )
        assert second["status"] == "reconcile_wait"
        assert second["posts"] == 0

        key = launcher.posts[0]["idempotency_key"]
        launcher.add_attempt(
            job_id="job_a",
            task_id="T1",
            idempotency_key=key,
            attempt_id="att_delayed",
        )

        third = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=10,
        )
        assert third["status"] == "adopted"
        assert third["posts"] == 0
        assert third["evidence"] == "proven_present"
        assert third["attempt_id"] == "att_delayed"
        assert len(launcher.posts) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatched"

    def test_uncertainty_exhaustion_escalates(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(idempotent=True, script=["uncertain"])

        first = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=2,
        )
        assert first["status"] == "reconcile_wait"
        assert first["posts"] == 1
        posts_after_first = len(launcher.posts)

        result = None
        for _ in range(10):
            result = rec.tick(
                tmp_path,
                "job_a",
                launcher,
                mode="dispatch",
                max_uncertain_ticks=2,
            )
            assert result["posts"] == 0
            if result["status"] == "dispatch_uncertain_exhausted":
                break

        assert result is not None
        assert result["status"] == "dispatch_uncertain_exhausted"
        assert result["exactly_once"] is False
        assert len(launcher.posts) == posts_after_first
        task = jobs.load_job(tmp_path, "job_a")["tasks"][0]
        assert task["state"] == "failed"
        assert task["evidence"] == "dispatch_uncertain_exhausted"

        after = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=2,
        )
        assert after["status"] == "noop"
        assert len(launcher.posts) == posts_after_first

    def test_ambiguous_adoption_escalates(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(idempotent=True, script=["uncertain"])

        first = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=10,
        )
        assert first["status"] == "reconcile_wait"
        key = launcher.posts[0]["idempotency_key"]
        launcher.add_attempt(
            job_id="job_a",
            task_id="T1",
            idempotency_key=key,
            attempt_id="att_one",
        )
        launcher.add_attempt(
            job_id="job_a",
            task_id="T1",
            idempotency_key=key,
            attempt_id="att_two",
        )

        result = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="dispatch",
            max_uncertain_ticks=10,
        )

        assert result["status"] == "dispatch_ambiguous"
        assert result["posts"] == 0
        assert result["exactly_once"] is False
        assert len(launcher.posts) == 1
        task = jobs.load_job(tmp_path, "job_a")["tasks"][0]
        assert task["state"] == "failed"
        assert task["evidence"] == "dispatch_ambiguous"
