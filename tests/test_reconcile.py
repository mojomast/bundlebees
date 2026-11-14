"""M2 acceptance tests (coordinator-corrected interface).

The harness drives the *contract* surface: ``list_attempts``, ``get_attempt``,
``post_attempt`` and a bool ``idempotent``. Nothing here reaches into aliases or
keyword buckets, so a module that guesses at a launcher's methods fails these
tests instead of quietly working.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

SHA = "817eac479f441396562962e880c1af596638f399"
T0 = 1_800_000_000.0


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class FakeLauncher:
    """A launcher implementing exactly the contract's four members."""

    def __init__(self, *, idempotent: bool = True) -> None:
        self.idempotent = idempotent
        self.entries: list[dict] = []
        self.posts: list[dict] = []
        self.script: list[str] = []
        self.hide_next: int = 0

    def _add(self, brief: dict, attempt_id: str, *, state: str = "succeeded", at: float = T0) -> dict:
        entry = {
            "attempt_id": attempt_id,
            "state": state,
            "created_at": iso(at),
            "brief": dict(brief),
        }
        self.entries.append(entry)
        return entry

    def post_attempt(self, brief: dict) -> dict:
        self.posts.append(dict(brief))
        action = self.script.pop(0) if self.script else "ok"
        if action == "not_sent":
            raise rec.DispatchNotSent("request never left the process")
        if action == "uncertain":
            raise rec.DispatchUncertain("connection dropped after send")
        if action == "uncertain_present":
            self._add(brief, "att_late")
            raise rec.DispatchUncertain("response was lost")
        if action == "ambiguous":
            self._add(brief, "att_one")
            self._add(brief, "att_two")
            raise rec.DispatchUncertain("two attempts may exist")
        return self._add(brief, f"att_{len(self.entries)}")

    def list_attempts(self) -> list[dict]:
        if self.hide_next > 0:
            self.hide_next -= 1
            return []
        return list(self.entries)

    def get_attempt(self, attempt_id: str) -> dict | None:
        return next((entry for entry in self.entries if entry["attempt_id"] == attempt_id), None)


def make_job(tmp_path: Path, tasks=None, *, bees: int = 3) -> dict:
    jobs.record_authorization(
        tmp_path,
        job_id="job_a",
        statement="Add contract source-rev validation to acdd-lint, nothing else.",
        repo="mojomast/workflow",
        base_rev=SHA,
        scope=["tools/**"],
        bees=bees,
        wall_clock_s=3600,
        interactive=True,
    )
    return jobs.create_job(
        tmp_path,
        job_id="job_a",
        tasks=tasks
        or [
            {
                "task_id": "T1",
                "objective": "reject malformed source_rev",
                "lease": ["tools/x.py"],
                "acceptance": {"criteria": ["malformed source_rev is refused"], "tests": ["tests/x.py"]},
            }
        ],
    )


# --- granularity -----------------------------------------------------------


class Granularity:
    __test__ = True
    def test_dispatch_tick_does_not_verify(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        calls: list[dict] = []

        result = rec.tick(tmp_path, "job_a", launcher=launcher, verifier=calls.append, now=T0)

        assert result.action == "dispatch"
        assert launcher.posts[0]["objective"] == "reject malformed source_rev"
        assert calls == []

    def test_verify_tick_is_one_heavy_operation(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        calls: list[dict] = []
        verifier = lambda ctx: calls.append(ctx) or {"ok": True, "evidence": "green"}
        result = rec.tick(tmp_path, "job_a", launcher=launcher, verifier=verifier, now=T0 + 1)

        assert len(calls) == 1
        assert result.action == "verify"
        assert result.transitions == ("T1->running", "T1->verifying", "T1->verified")
        assert len(launcher.posts) == 1

        again = rec.tick(tmp_path, "job_a", launcher=launcher, verifier=verifier, now=T0 + 2)
        assert again.status == "idle"
        assert len(calls) == 1


# --- idempotency / honesty -------------------------------------------------


class Idempotency:
    __test__ = True
    def test_unchanged_world_is_a_noop(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        # The fake journal says the worker already succeeded, so the first tick
        # reaps it; after that the world is genuinely unchanged.
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)
        before = json.loads((tmp_path / "jobs" / "job_a.json").read_text())["body"]["tasks"]
        results = [rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 2 + i) for i in range(1, 11)]
        after = json.loads((tmp_path / "jobs" / "job_a.json").read_text())["body"]["tasks"]

        assert all(result.status == "idle" and not result.transitions for result in results)
        assert before == after
        assert len(launcher.posts) == 1


class Honesty:
    __test__ = True
    def test_exactly_once_is_never_claimed_without_server_idempotency(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher(idempotent=False)

        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        assert result.exactly_once is False

    def test_exactly_once_is_claimed_only_when_the_launcher_enforces_it(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher(idempotent=True)
        launcher.script = ["uncertain_present"]

        assert rec.tick(tmp_path, "job_a", launcher=launcher, now=T0).status == "reconcile_wait"
        adopted = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 1)

        assert adopted.evidence == "proven_present"
        assert adopted.exactly_once is True


# --- attribution -----------------------------------------------------------


class Attribution:
    __test__ = True
    def test_transitions_are_recorded_and_persisted(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        reloaded = jobs.load_job(tmp_path, "job_a")
        task = reloaded["tasks"][0]

        assert task["state"] == "dispatched"
        assert task["attempts"] == ["att_0"]
        moves = [event["transition"] for event in reloaded["events"]]
        assert moves == ["pending->dispatching", "dispatching->dispatched"]
        assert {event["actor"] for event in reloaded["events"]} == {"reconciler"}
        assert all(event["at"].endswith("Z") for event in reloaded["events"])


# --- contract negatives (RED against the original bee patch) ---------------


class Contract:
    __test__ = True
    def test_grace_is_measured_in_elapsed_seconds(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain"]

        first = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0, grace_s=10, max_uncertain_ticks=100)
        assert first.status == "reconcile_wait"

        # Same tick count, only 5 s later: still inside the grace window.
        inside = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 5, grace_s=10, max_uncertain_ticks=100)
        assert inside.status == "reconcile_wait"

        # 11 s after the uncertainty began the grace is over: escalate, never retry.
        escalated = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 11, grace_s=10, max_uncertain_ticks=100)
        assert escalated.status == "escalated"
        assert escalated.evidence == "dispatch_uncertain_exhausted"
        assert len(launcher.posts) == 1

    def test_tickresult_is_the_frozen_dataclass_from_the_contract(self):
        assert dataclasses.is_dataclass(rec.TickResult)
        names = [field.name for field in dataclasses.fields(rec.TickResult)]
        assert names == ["status", "transitions", "action", "detail", "exactly_once", "evidence",
                         "work_state", "work_id"]

        result = rec.TickResult(status="idle")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = "ok"  # type: ignore[misc]

    def test_missing_contract_method_fails_closed(self, tmp_path):
        make_job(tmp_path)

        class OldStyle:
            idempotent = True

            def post(self, brief):  # not the contract
                raise AssertionError("must not be called")

            def journal(self):
                return []

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=OldStyle(), now=T0)

        assert exc.value.code == "launcher_incompatible"
        assert jobs.load_job(tmp_path, "job_a")["tasks"][0]["state"] == "pending"

    def test_non_bool_idempotent_fails_closed(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.idempotent = "yes"

        with pytest.raises(rec.ReconcileError) as exc:
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0)

        assert exc.value.code == "launcher_incompatible"
        assert launcher.posts == []

    def test_uncertainty_survives_a_restart(self, tmp_path):
        make_job(tmp_path)
        launcher = FakeLauncher()
        launcher.script = ["uncertain"]
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0, grace_s=30)

        # A fresh process, same state dir: the elapsed clock still decides.
        restarted = FakeLauncher()
        waited = rec.tick(tmp_path, "job_a", launcher=restarted, now=T0 + 10, grace_s=30)
        assert waited.status == "reconcile_wait"

        escalated = rec.tick(tmp_path, "job_a", launcher=restarted, now=T0 + 31, grace_s=30)
        assert escalated.status == "escalated"
        assert restarted.posts == []
