"""Phase 1 (rizzstack cleanup): the heartbeat sweep is wired into the factory driver.

Finding (coordinator, Nov 18 2026): the scout hook (every 2 minutes) already turns terminal
launcher attempts into an immediate factory pass, and the pass ingests their landings. What
nothing ran was beat.beat() / beat_reconciliation.sweep(): the launcher-vs-origin diff that
catches a finished attempt whose branch was never pushed or never got a PR. So the sweep is a
BACKSTOP: the driver runs it classify-only (no processor: it never pushes, opens PRs or
dispatches) at most once per sweep interval (default 1800 s), and reports what it found.

AC-001: a driver given sweep views runs beat.beat on its first pass; the pass result carries
        the beat report (schema rizzstack.beat-report/1) with the reconcile duty, and the gap
        attempts are classified without any processor being called.
AC-002: the sweep is rate-limited by a durable interval: a pass 60 s later skips it, a pass
        after the interval (even from a fresh driver on the same state dir) runs it again.
AC-003: without sweep views the pass result reports the sweep as not configured and the
        beat leaves nothing on disk.
AC-004: a sweep whose launcher view raises never breaks the pass.
AC-005: the default interval is 1800 s (DEFAULT_SWEEP_INTERVAL_S).
"""

from __future__ import annotations

import json
from pathlib import Path

from bundlebees_plugin import beat, factory_driver

T0 = 1_800_000_000.0


class FakeLauncher:
    def __init__(self, entries):
        self.entries = list(entries)
        self.polls = 0

    def list_attempts(self):
        self.polls += 1
        return list(self.entries)


class Boom:
    def list_attempts(self):
        raise RuntimeError("launcher unreachable")


def _gap():
    return FakeLauncher([{
        "attempt_id": "att-gap", "task_id": "att-gap", "state": "succeeded", "branch": "bee/gap",
        "created_at": "2027-01-15T07:00:00Z", "finished_at": "2027-01-15T07:27:30Z",
    }])


def _queue(tmp_path: Path) -> Path:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"items": {}}), encoding="utf-8")
    return path


def _driver(tmp_path, clock, **kw):
    return factory_driver.FactoryDriver(tmp_path / "state", _queue(tmp_path), startup=False,
                                        now=lambda: clock["t"], **kw)


def _views(launcher):
    return {"launcher": launcher, "origin_branches": {}, "open_prs": {}}


class TestDriverSweep:
    def test_first_pass_runs_the_sweep_classify_only(self, tmp_path):
        clock = {"t": T0}
        launcher = _gap()
        result = _driver(tmp_path, clock, sweep_views=_views(launcher)).run_pass()
        report = result["sweep"]
        assert report["schema"] == beat.SCHEMA
        assert beat.DUTY_SWEEP in report["duties"]
        assert launcher.polls >= 1
        assert "att-gap" in json.dumps(report["duties"][beat.DUTY_SWEEP])

    def test_sweep_is_rate_limited_by_a_durable_interval(self, tmp_path):
        clock = {"t": T0}
        launcher = _gap()
        driver = _driver(tmp_path, clock, sweep_views=_views(launcher), sweep_interval_s=1800)
        driver.run_pass()
        polls = launcher.polls
        clock["t"] = T0 + 60
        second = driver.run_pass()
        assert second["sweep"]["status"] == "skipped"
        assert launcher.polls == polls
        clock["t"] = T0 + 1801
        fresh = _driver(tmp_path, clock, sweep_views=_views(launcher), sweep_interval_s=1800)
        third = fresh.run_pass()
        assert third["sweep"].get("schema") == beat.SCHEMA
        assert launcher.polls > polls

    def test_no_views_means_not_configured_and_nothing_written(self, tmp_path):
        clock = {"t": T0}
        result = _driver(tmp_path, clock).run_pass()
        assert result["sweep"] == {"status": "not_configured"}
        assert not (tmp_path / "state" / "beat").exists()

    def test_a_failing_launcher_view_never_breaks_the_pass(self, tmp_path):
        clock = {"t": T0}
        result = _driver(tmp_path, clock, sweep_views=_views(Boom())).run_pass()
        assert "sweep" in result
        assert result["sweep"].get("status") in {"error", "partial"}

    def test_default_interval_is_thirty_minutes(self):
        assert factory_driver.DEFAULT_SWEEP_INTERVAL_S == 1800
