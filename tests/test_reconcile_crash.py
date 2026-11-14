"""M2 crash and uncertainty tests.

Every case here is about a request whose fate is not knowable from the caller's
side. The rule under test is always the same: an uncertain dispatch is
reconciled against the journal or escalated — never retried on a guess.
"""

from __future__ import annotations

import threading

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

from tests.test_reconcile import FakeLauncher, make_job, T0, iso


class Concurrency:
    __test__ = True
    def test_two_ticks_never_double_dispatch(self, tmp_path):
        make_job(tmp_path)
        entered = threading.Event()
        release = threading.Event()

        class Blocking(FakeLauncher):
            def post_attempt(self, brief):
                entered.set()
                assert release.wait(10), "test never released the blocked POST"
                return super().post_attempt(brief)

        launcher = Blocking()
        results: list[rec.TickResult] = []

        worker = threading.Thread(
            target=lambda: results.append(rec.tick(tmp_path, "job_a", launcher=launcher, now=T0))
        )
        worker.start()
        assert entered.wait(10), "the first tick never reached post_attempt"

        second = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        release.set()
        worker.join(10)

        assert second.status == "locked"
        assert second.transitions == ()
        assert len(launcher.posts) == 1
        assert results and results[0].status == "ok"


class Crash:
    __test__ = True
    def test_crash_before_post_is_retried(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["not_sent"]

        first = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        assert first.status == "ok"
        assert "not sent" in first.detail
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "dispatching"

        second = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert second.status == "ok"
        task = jobs.load_job(tmp_path, "job_a")["tasks"][0]
        assert task["state"] == "dispatched"
        assert len(task["attempts"]) == 1
        assert len(launcher.posts) == 2  # one that never landed, one that did

    def test_lost_acknowledgement_is_adopted_not_redispatched(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher(idempotent=False)
        launcher.script = ["uncertain_present"]

        first = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)
        assert first.status == "reconcile_wait"

        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert adopted.status == "ok"
        assert adopted.action == "adopt"
        assert adopted.evidence == "adopted"  # no server idempotency, so no claim
        assert adopted.exactly_once is False
        assert len(launcher.posts) == 1
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["attempts"] == ["att_late"]

    def test_delayed_journal_visibility_waits_then_adopts(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain_present"]
        launcher.hide_next = 4  # the journal lags the POST by four reads

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0).status == "reconcile_wait"
        for step in (1, 2, 3):
            waiting = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + step, grace_s=600)
            assert waiting.status == "reconcile_wait"

        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4, grace_s=600)

        assert adopted.action == "adopt"
        assert len(launcher.posts) == 1

    def test_uncertainty_exhaustion_escalates(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain"]

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0, grace_s=600, max_uncertain_ticks=3)
        for step in (1, 2):
            assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + step, grace_s=600, max_uncertain_ticks=3).status == "reconcile_wait"

        escalated = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4, grace_s=600, max_uncertain_ticks=3)

        assert escalated.status == "escalated"
        assert escalated.evidence == "dispatch_uncertain_exhausted"
        assert len(launcher.posts) == 1

    def test_ambiguous_adoption_escalates(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["ambiguous"]

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0).status == "reconcile_wait"
        escalated = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert escalated.status == "escalated"
        assert escalated.evidence == "dispatch_ambiguous"
        assert len(launcher.posts) == 1

    def test_a_second_tick_while_dispatching_does_not_post_again(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain"]
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        # Even with a journal that never shows the attempt, ticks stay reads.
        for step in range(1, 4):
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + step, grace_s=600, max_uncertain_ticks=50)

        assert len(launcher.posts) == 1
