"""M4.3 acceptance: the failed attempt wakes a separate decision consumer.

The fixture and the coordinator are the real M4 dogfood.  The harness never
calls the decision adapter and never writes a decision document: it only starts
the production consumer process, exactly as an operator or scheduler would.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from bundlebees_plugin import agent_work, job as jobs, recovery

from tests.test_m4_dogfood_e2e import JOB, M4Dogfood, T2

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
HYVV_SRC = os.environ.get("HYVV_SRC", "")
GITLEAKS = os.environ.get("GITLEAKS", "")


def _consumer_env(*, crash: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env["WORKFLOW_SRC"] = WORKFLOW_SRC
    env["REQUIRE_WORKFLOW"] = "1"
    env["HYVV_SRC"] = HYVV_SRC
    env["REQUIRE_HYVV"] = "1"
    env["GITLEAKS"] = GITLEAKS
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if crash:
        env["BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL"] = "1"
    env["BUNDLEBEES_AUTHORIZED_SELECTOR"] = (
        "tools/acdd-lint/tests/test_check_reachability.py"
        "::Reachability::test_reachable_commit_is_silent")
    return env


class _WakeupFixture(unittest.TestCase):
    adapter_name = "authorized"

    def setUp(self):
        if not WORKFLOW_SRC or not HYVV_SRC or not GITLEAKS:
            self.fail("M4.3 wake-up test requires WORKFLOW_SRC, HYVV_SRC and GITLEAKS")
        self.fx = M4Dogfood(
            methodName="test_one_authorised_objective_recovers_the_injected_failure")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.consumer_runs: list[subprocess.CompletedProcess[str]] = []
        self.fx.recover = self._consumer_step

    def _consumer(self, *, crash: bool = False, adapter: str | None = None):
        argv = [
            sys.executable, "-m", "bundlebees_plugin.agent_consumer",
            "--state", str(self.fx.state),
            "--consumer", "m43-consumer",
            "--adapter", adapter or self.adapter_name,
            "--once",
        ]
        if (adapter or self.adapter_name) == "hermes":
            argv += ["--repo", str(self.fx.work)]
        done = subprocess.run(
            argv, cwd=str(ROOT), env=_consumer_env(crash=crash),
            capture_output=True, text=True, timeout=900,
        )
        self.consumer_runs.append(done)
        return done

    def _consumer_step(self):
        done = self._consumer()
        self.assertEqual(done.returncode, 0,
                         f"consumer failed\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}")
        items = agent_work.list(self.fx.state, job_id=JOB)
        self.assertEqual(len(items), 1, f"exactly one work item, got {items}")
        last = self.consumer_runs[-1] if self.consumer_runs else None
        self.assertEqual(
            items[0]["state"], "completed",
            f"work item not completed: {items[0]}\n"
            f"consumer rc={getattr(last, 'returncode', None)}\n"
            f"stdout:\n{getattr(last, 'stdout', '')}\nstderr:\n{getattr(last, 'stderr', '')}")
        self.assertIsNotNone(recovery.read_decision(
            self.fx.state, recovery.load_request(self.fx.state, items[0]["request_id"])))
        return {"decisions_recorded": True, "work": items[0]}

    def _assert_factory_result(self):
        job = jobs.load_job(self.fx.state, JOB, recover=False)
        attempts = [a for task in job["tasks"] for a in task.get("attempts") or []]
        self.assertEqual(attempts, ["att_m4_1", "att_m4_2", "att_m4_3"])
        items = agent_work.list(self.fx.state, job_id=JOB)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["state"], "completed")
        self.assertTrue(item.get("result", {}).get("decision"))
        self.assertEqual(len(self.fx.worker.posts), 3, "exactly three worker dispatches")
        manifests = json.loads(
            (self.fx.state / "jobs" / JOB / "return.json").read_text())
        self.assertEqual(manifests["receiver_decision"]["status"], "pending")
        self.assertTrue(manifests.get("recovery"), "RETURN must carry the recovery history")
        return job


class M43WakeupDeterministic(_WakeupFixture):
    """Mandatory CI proof: the consumer path is real, the adapter is scripted."""

    def test_failed_attempt_wakes_consumer_and_factory_completes(self):
        M4Dogfood.test_one_authorised_objective_recovers_the_injected_failure(self.fx)
        self._assert_factory_result()


@unittest.skipUnless(os.environ.get("HERMES_M43_REAL") == "1",
                     "supervised real-model wake-up: set HERMES_M43_REAL=1")
class M43WakeupHermes(_WakeupFixture):
    adapter_name = "hermes"

    def test_real_hermes_consumer_recovers_without_harness_decision(self):
        M4Dogfood.test_one_authorised_objective_recovers_the_injected_failure(self.fx)
        job = self._assert_factory_result()
        print("M43_EVIDENCE " + json.dumps({
            "work": agent_work.list(self.fx.state, job_id=JOB)[0],
            "attempts": [a for task in job["tasks"] for a in task.get("attempts") or []],
            "integration": job.get("integration", {}).get("last_commit"),
            "consumer_runs": [
                {"returncode": run.returncode, "stdout": run.stdout[-800:],
                 "stderr": run.stderr[-400:]}
                for run in self.consumer_runs
            ],
        }, default=str), flush=True)

    def test_real_consumer_crash_after_proposal_is_adopted_without_rerun(self):
        # Run the consumer once with the crash hook armed, then a fresh consumer
        # process adopts the persisted proposal and records the decision.
        original = self.fx.recover

        def crash_then_adopt():
            crashed = self._consumer(crash=True)
            self.assertEqual(crashed.returncode, 97,
                             f"crash hook did not fire\n{crashed.stdout}\n{crashed.stderr}")
            items = agent_work.list(self.fx.state, job_id=JOB)
            self.assertEqual(len(items), 1)
            proposal_files = list((self.fx.state / "recovery" / JOB).glob("*.proposal.json"))
            self.assertEqual(len(proposal_files), 1, "the proposal must survive the crash")
            before = proposal_files[0].read_bytes()
            self.assertEqual(recovery.read_decision(
                self.fx.state, recovery.load_request(self.fx.state, items[0]["request_id"])), None,
                "the crashed consumer must not have recorded a decision")
            done = original()  # a fresh consumer process; auto-adopts
            after = proposal_files[0].read_bytes()
            self.assertEqual(before, after, "adoption rewrote the raw proposal")
            print("M43_CRASH_ADOPT " + json.dumps({
                "work": agent_work.list(self.fx.state, job_id=JOB)[0],
                "consumer_runs": [
                    {"returncode": run.returncode, "stdout": run.stdout[-800:]}
                    for run in self.consumer_runs
                ],
            }, default=str), flush=True)
            return done

        self.fx.recover = crash_then_adopt
        M4Dogfood.test_one_authorised_objective_recovers_the_injected_failure(self.fx)
        job = self._assert_factory_result()
        self.assertEqual(len(self.fx.worker.posts), 3)
        self.assertEqual(job.get("integration", {}).get("last_commit") is not None, True)


if __name__ == "__main__":
    unittest.main()
