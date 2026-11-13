"""Job CLI and wake-up handoff: mechanical ticks, durable prompts, acknowledgement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import job as jobs
from bundlebees_plugin import job_cli, leases
from bundlebees_plugin.reconcile import DispatchUncertain

SHA = "817eac479f441396562962e880c1af596638f399"
BRIEF_OK = {"criteria": ["x"], "tests": ["tests/x.py"]}


class FakeClient:
    """Launcher client double: one create, then a configurable journal."""

    def __init__(self, *, fail: bool = False, journal: list[dict] | None = None) -> None:
        self.fail = fail
        self.created: list[dict] = []
        self.journal: list[dict] = journal or []

    def create(self, brief):
        self.created.append(dict(brief))
        if self.fail:
            raise DispatchUncertain("connection dropped")
        return {"attempt_id": f"att_{len(self.created)}"}

    def attempts_raw(self):
        return list(self.journal)

    def get_raw(self, attempt_id):
        return next((entry for entry in self.journal if entry["attempt_id"] == attempt_id), None)

    def result_envelope(self, attempt_id):
        return {"attempt_id": attempt_id, "patch": {"diff": "", "files": []}}


def make_job(tmp_path: Path, tasks=None) -> dict:
    jobs.record_authorization(
        tmp_path,
        job_id="job_a",
        statement="Add contract source-rev validation to acdd-lint, nothing else.",
        repo="mojomast/workflow",
        base_rev=SHA,
        scope=["tools/**"],
        bees=3,
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
                "acceptance": BRIEF_OK,
            }
        ],
    )


class Mechanical:
    __test__ = True

    def test_a_tick_dispatches_without_a_prompt_and_binds_the_lease(self, tmp_path):
        make_job(tmp_path)
        client = FakeClient()

        result = job_cli.tick(tmp_path, "job_a", client=client, repo=tmp_path)

        assert result["status"] == "ok"
        assert result["action"] == "dispatch"
        assert result["handoff"] is None
        assert client.created and client.created[0]["job_id"] == "job_a"
        # The coordinator wrote the lease from its own brief, not worker claims.
        assert leases.lease_files(leases.load_lease(tmp_path, "att_1")) == ["tools/x.py"]

    def test_a_second_tick_is_a_noop(self, tmp_path):
        make_job(tmp_path)
        client = FakeClient()
        job_cli.tick(tmp_path, "job_a", client=client, repo=tmp_path)

        second = job_cli.tick(tmp_path, "job_a", client=client, repo=tmp_path)

        assert second["status"] == "idle"
        assert len(client.created) == 1


class Handoff:
    __test__ = True

    def _escalated(self, tmp_path):
        make_job(tmp_path)
        client = FakeClient(fail=True)
        job_cli.tick(tmp_path, "job_a", client=client, repo=tmp_path)  # uncertain

        # Two plausible journal entries make adoption ambiguous on the next tick.
        brief = {
            "objective": "reject malformed source_rev",
            "source_rev": SHA,
            "assigned_files": ["tools/x.py"],
            "job_id": "job_a",
            "task_id": "T1",
        }
        from bundlebees_plugin.reconcile import _iso

        client.journal = [
            {"attempt_id": "att_a", "state": "succeeded", "created_at": _iso(1_800_000_000.0), "brief": brief},
            {"attempt_id": "att_b", "state": "succeeded", "created_at": _iso(1_800_000_000.0), "brief": brief},
        ]
        result = job_cli.tick(tmp_path, "job_a", client=client, repo=tmp_path, now=1_800_000_001.0)
        assert result["status"] == "escalated"
        return result

    def test_an_escalation_writes_a_durable_handoff(self, tmp_path):
        result = self._escalated(tmp_path)

        assert result["handoff"]["status"] == "pending"
        on_disk = job_cli.read_handoff(tmp_path, "job_a")
        assert on_disk["kind"] == "dispatch_ambiguous"
        assert "bundlebees job note" in on_disk["prompt"]

    def test_claiming_the_prompt_does_not_delete_it(self, tmp_path):
        self._escalated(tmp_path)

        prompt = job_cli.pending_prompt(tmp_path, "job_a")

        assert prompt and "dispatch_ambiguous" in prompt
        payload = job_cli.read_handoff(tmp_path, "job_a")
        assert payload["status"] == "claimed"
        assert payload["claimed_at"]
        # A crashed agent turn can still find it: claiming is not acknowledgement.
        assert job_cli.pending_prompt(tmp_path, "job_a") is None  # not pending twice
        assert job_cli.read_handoff(tmp_path, "job_a")["prompt"] == payload["prompt"]

    def test_acknowledgement_records_the_decision(self, tmp_path):
        self._escalated(tmp_path)

        payload = job_cli.acknowledge(tmp_path, "job_a", "Re-dispatch with a narrower contract.")

        assert payload["status"] == "done"
        assert payload["decision"].startswith("Re-dispatch")
        assert job_cli.pending_prompt(tmp_path, "job_a") is None
        events = jobs.load_job(tmp_path, "job_a")["events"]
        assert events[-1]["transition"] == "agent_ack"

    def test_a_second_escalation_does_not_overwrite_a_pending_handoff(self, tmp_path):
        self._escalated(tmp_path)
        before = job_cli.read_handoff(tmp_path, "job_a")

        job_cli.record_handoff(tmp_path, "job_a", job_cli.reconcile.TickResult(status="escalated", evidence="dispatch_ambiguous"))

        after = job_cli.read_handoff(tmp_path, "job_a")
        assert after["created_at"] == before["created_at"]

    def test_acknowledgement_requires_a_note(self, tmp_path):
        self._escalated(tmp_path)

        with pytest.raises(job_cli.JobCliError):
            job_cli.acknowledge(tmp_path, "job_a", "   ")
