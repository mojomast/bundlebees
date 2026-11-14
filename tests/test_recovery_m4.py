"""M4: bounded, evidence-based recovery for rejected tasks.

Every case here drives the *real* reconciler against a real job document, real
frozen ``acdd/1`` contracts and the durable files under ``recovery/``. Nothing
is asserted from an in-memory opinion: classification comes from evidence codes,
a retry needs a recorded decision, a supersession needs an authorised corrected
test selection, and an exhausted budget ends in ``awaiting_approval`` — never in
an accepted job.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bundlebees_plugin import contract as contracts
from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec
from bundlebees_plugin import job_cli, recovery, return_report

SHA = "817eac479f441396562962e880c1af596638f399"
T0 = 1_800_000_000.0

TEST_FILE = "tests/test_t.py"
WRONG_TEST = f"{TEST_FILE}::test_missing"
RIGHT_TEST = f"{TEST_FILE}::test_real"

#: Recovery is refused while the pinned protocol validator cannot be loaded, so
#: these tests run against the real checkout rather than a local stub.
WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"


@pytest.fixture(scope="module", autouse=True)
def _pinned_protocol():
    if not WORKFLOW_SRC:
        if REQUIRE_WORKFLOW:
            pytest.fail("REQUIRE_WORKFLOW=1 but WORKFLOW_SRC does not name the pinned workflow checkout")
        pytest.skip("set WORKFLOW_SRC to the pinned workflow checkout; recovery refuses without its validator")



# ---------------------------------------------------------------------------
# Fixtures: a real launcher double, a real frozen contract, a real job
# ---------------------------------------------------------------------------


class FakeLauncher:
    """The four-member launcher contract: list/get/post and a bool idempotent."""

    def __init__(self, *, idempotent: bool = True) -> None:
        self.idempotent = idempotent
        self.entries: list[dict] = []
        self.posts: list[dict] = []

    def post_attempt(self, brief: dict) -> dict:
        self.posts.append(json.loads(json.dumps(brief)))
        entry = {
            "attempt_id": f"att_{len(self.entries)}",
            "state": "succeeded",
            # Far enough in the future that no intent is ever "too old" for it.
            "created_at": rec._iso(T0 + 10_000),
            "brief": json.loads(json.dumps(brief)),
        }
        self.entries.append(entry)
        return entry

    def list_attempts(self) -> list[dict]:
        return list(self.entries)

    def get_attempt(self, attempt_id: str) -> dict | None:
        return next((entry for entry in self.entries if entry["attempt_id"] == attempt_id), None)


def frozen_contract(
    *,
    contract_id: str = "SPEC-1",
    tests: tuple[str, ...] = (WRONG_TEST,),
    test_file: str = TEST_FILE,
    assigned: str = "tools/t.py",
    source_rev: str = SHA,
) -> dict:
    body = {
        "contract_version": "acdd/1",
        "id": contract_id,
        "title": "the tool answers",
        "outcome": {"summary": "the tool answers", "observable_behavior": ["the tool answers 42"]},
        "source_rev": source_rev,
        "scope": {"assigned_files": [assigned], "test_files": [test_file]},
        "acceptance_criteria": [
            {"id": "AC-001", "statement": "the tool answers 42", "tests": list(tests), "verification": "test"}
        ],
        "tdd": {"applicable": True, "test_first": False, "reason": "the approved test exists at baseline"},
        "verification": {"independent": True},
        "evidence_required": ["green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    return contracts.freeze_contract(body, approved_by="reviewer", approved_at="2026-11-13T00:00:00Z")


def make_job(tmp_path: Path, *, max_attempts: int = 2, frozen: dict | None = None) -> tuple[dict, dict, str]:
    """An authorized job whose one task pins a frozen contract."""

    envelope = {
        "max_attempts_per_task": max_attempts,
        "contract_corrections_allowed": True,
        # The approver pre-authorizes the exact replacement selection; the
        # protocol refuses a corrected test that is not named here.
        "corrected_tests_allowed": [RIGHT_TEST],
    }
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
        recovery=envelope,
    )
    contract = frozen if frozen is not None else frozen_contract()
    sha = contracts.check_frozen(contract)
    jobs.create_job(
        tmp_path,
        job_id="job_a",
        tasks=[
            {
                "task_id": "T1",
                "objective": "make the tool answer 42",
                "lease": ["tools/t.py"],
                "contract": contract,
                "contract_sha256": sha,
                "acceptance": {"criteria": ["the tool answers 42"], "tests": [TEST_FILE]},
            }
        ],
    )
    return jobs.load_job(tmp_path, "job_a"), jobs.load_authorization(tmp_path, "job_a"), sha


def drive_to_verification(state: Path, launcher: FakeLauncher, *, now: float = T0) -> None:
    """Dispatch once and reap the worker, leaving the task in ``verifying``."""

    first = rec.tick(state, "job_a", launcher=launcher, now=now)
    assert first.action == "dispatch"
    second = rec.tick(state, "job_a", launcher=launcher, now=now + 1)
    assert second.status == "ok"
    assert jobs.load_job(state, "job_a")["tasks"][0]["state"] == "verifying"


def reject(state: Path, launcher: FakeLauncher, evidence: str, *, now: float = T0 + 2):
    return rec.tick(
        state,
        "job_a",
        launcher=launcher,
        verifier=lambda ctx: {"ok": False, "evidence": evidence},
        now=now,
    )


def requests_of(state: Path, job_id: str = "job_a") -> list[Path]:
    return sorted((state / "recovery" / job_id).glob("*.request.json"))


def decisions_of(state: Path, job_id: str = "job_a") -> list[Path]:
    return sorted((state / "recovery" / job_id).glob("*.decision.json"))


def task_of(state: Path, task_id: str = "T1") -> dict:
    return next(task for task in jobs.load_job(state, "job_a")["tasks"] if task["task_id"] == task_id)


def gate_of(state: Path) -> dict:
    return jobs.load_job(state, "job_a")["approval_gate"]


# ---------------------------------------------------------------------------
# The five classes
# ---------------------------------------------------------------------------


class Classification:
    __test__ = True

    @pytest.mark.parametrize(
        "evidence,name,code",
        [
            (f"test_unresolved: AC-001 test {WRONG_TEST!r} matches no collected test id",
             "specification", "test_not_found"),
            ("tests_failed: named tests failed after the patch", "implementation", "test_failed"),
            ("suite_failed: the regression suite exited 1", "implementation", "suite_failed"),
            ("security_tool_missing: gitleaks is not on PATH", "environment", "toolchain_missing"),
            ("sandbox_unavailable: no usable isolation backend", "environment", "dependency_unavailable"),
            ("launcher_unavailable: the launcher refused the connection", "infrastructure", "launcher_unavailable"),
            ("worker_failed: worker ended failed", "infrastructure", "launcher_unavailable"),
            ("integration_failed: the wave merge conflicted", "integration", "merge_conflict"),
        ],
    )
    def test_every_class_is_derived_from_its_evidence_code(self, evidence, name, code):
        failure = recovery.classify_failure({"ok": False, "evidence": evidence}, contract=None)

        assert failure["class"] == name, failure
        assert failure["code"] == code, failure
        assert failure["evidence"] == evidence
        # The protocol code is canonical; the observation that produced it is
        # still named, so normalisation hides nothing.
        assert evidence.split(":", 1)[0] in failure["rationale"], failure

    def test_the_five_classes_are_the_documented_ones(self):
        assert recovery.CLASSES == (
            "specification",
            "environment",
            "infrastructure",
            "integration",
            "implementation",
        )
        covered = {recovery.classify_failure({"ok": False, "evidence": e}, contract=None)["class"]
                   for e in (
                       "test_unresolved: x",
                       "tests_failed: x",
                       "security_tool_missing: x",
                       "launcher_unavailable: x",
                       "integration_failed: x",
                   )}
        assert covered == set(recovery.CLASSES)

    def test_an_unattributable_record_fails_closed_to_a_non_dispatching_class(self):
        failure = recovery.classify_failure({"ok": False, "evidence": "something odd happened"}, contract=None)

        # The protocol has no "unclassified" code. A stalled scheduler is the
        # honest description of "no next step could be scheduled", and the class
        # keeps the record non-dispatching.
        assert failure["class"] == "infrastructure"
        assert failure["code"] == "scheduler_stalled"

        request = {"failure": failure, "envelope": {"max_attempts_per_task": 2}}
        assert recovery.attributed(request) is True
        assert recovery.exhausted(request) is True
        assert recovery.deterministic_adapter(request)["decision"] == "escalate"

    def test_a_declared_class_must_agree_with_its_code(self):
        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.classify_failure({"class": "implementation", "code": "security_tool_missing"}, contract=None)

        assert caught.value.code == "classification_forged"


# ---------------------------------------------------------------------------
# Bounded retry and exhaustion
# ---------------------------------------------------------------------------


class RetryBudget:
    __test__ = True

    def test_no_corrective_attempt_without_a_validated_decision(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)

        result = reject(tmp_path, launcher, "tests_failed: named tests failed")
        assert "T1->rejected" in result.transitions
        assert task_of(tmp_path)["state"] == "rejected"

        waiting = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert waiting.status == "awaiting_decision"
        assert waiting.action == "recovery"
        assert len(launcher.posts) == 1, "a rejected task must not be re-dispatched on an opinion"

        again = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4)
        assert again.status == "awaiting_decision"
        assert len(launcher.posts) == 1
        assert len(requests_of(tmp_path)) == 1, "a repeated tick must not add a request"

    def test_a_retry_is_bounded_and_then_escalates_to_approval(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        assert request["failure"]["class"] == "implementation"
        assert request["envelope"]["max_attempts_per_task"] == 2
        # A canonical request carries no attempts_used field: the ledger is the
        # task's attempt list, which is what every budget check reads.
        assert "attempts_used" not in request
        assert recovery.remaining_attempts(request, task_of(tmp_path)) == 1

        decision = recovery.deterministic_adapter(request)
        assert decision["decision"] == "retry"
        recovery.record_decision(tmp_path, request["request_id"], decision)

        dispatched = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert dispatched.action == "dispatch"
        assert len(launcher.posts) == 2
        assert task_of(tmp_path)["attempts"] == ["att_0", "att_1"]

        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4)  # reap
        reject(tmp_path, launcher, "tests_failed: still failing", now=T0 + 5)

        second = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        assert second["request_id"] != request["request_id"]
        assert recovery.remaining_attempts(second, task_of(tmp_path)) == 0

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(tmp_path, second["request_id"],
                                      {"decision": "retry", "rationale": "one more attempt inside the budget"})
        assert caught.value.code == "retry_budget_exhausted"

        exhausted = recovery.deterministic_adapter(second, task=task_of(tmp_path))
        assert exhausted["decision"] == "escalate"
        recovery.record_decision(tmp_path, second["request_id"], exhausted)

        escalated = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 6)
        assert escalated.status == "escalated"
        assert escalated.action == "needs_approval"
        assert escalated.evidence == "recovery_escalated"

        job = jobs.load_job(tmp_path, "job_a")
        assert job["approval_gate"]["state"] == "awaiting_approval"
        assert job["tasks"][0]["state"] == "failed"
        assert job["recovery"]["state"] == "needs_approval"
        assert any(item.get("code") == "recovery_escalated" for item in job["escalations"])
        assert len(launcher.posts) == 2, "an exhausted budget dispatches nothing"

        idle = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 7)
        assert idle.status == "idle"
        assert len(launcher.posts) == 2

    def test_a_single_attempt_budget_escalates_the_first_rejection(self, tmp_path):
        make_job(tmp_path, max_attempts=1)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        assert recovery.remaining_attempts(request, task_of(tmp_path)) == 0
        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(tmp_path, request["request_id"],
                                      {"decision": "retry", "rationale": "one more attempt inside the budget"})
        assert caught.value.code == "retry_budget_exhausted"

        assert recovery.deterministic_adapter(request)["decision"] == "escalate"

        # With no decision at all, an attributed and exhausted request escalates.
        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert result.status == "escalated"
        assert gate_of(tmp_path)["state"] == "awaiting_approval"
        assert len(launcher.posts) == 1


# ---------------------------------------------------------------------------
# Environment and infrastructure never dispatch
# ---------------------------------------------------------------------------


class NoDispatch:
    __test__ = True

    @pytest.mark.parametrize(
        "evidence,name",
        [
            ("security_tool_missing: gitleaks is not on PATH", "environment"),
            ("launcher_unavailable: connection refused", "infrastructure"),
        ],
    )
    def test_a_non_artifact_failure_can_only_escalate(self, tmp_path, evidence, name):
        make_job(tmp_path, max_attempts=3)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, evidence)

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        assert request["failure"]["class"] == name

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(tmp_path, request["request_id"],
                                      {"decision": "retry", "rationale": "retry the same contract anyway"})
        assert caught.value.code == "decision_class_forbidden"

        assert recovery.deterministic_adapter(request)["decision"] == "escalate"

        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert result.status == "escalated"
        assert result.action == "needs_approval"
        assert len(launcher.posts) == 1
        assert gate_of(tmp_path)["state"] == "awaiting_approval"

    def test_an_explicit_escalation_decision_is_recorded_and_applied(self, tmp_path):
        make_job(tmp_path, max_attempts=3)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "security_tool_missing: gitleaks is not on PATH")

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        decision = recovery.deterministic_adapter(request)
        recovery.record_decision(tmp_path, request["request_id"], decision)

        job = jobs.load_job(tmp_path, "job_a")
        applied = recovery.apply_ready_decisions(tmp_path, job, now=T0 + 3)
        assert [item["decision"] for item in applied] == ["escalate"]
        jobs.save_job(tmp_path, job)  # the caller's atomic commit: "applied"

        job = jobs.load_job(tmp_path, "job_a")
        assert job["tasks"][0]["state"] == "failed"
        assert job["approval_gate"]["state"] == "awaiting_approval"
        # Applying twice cannot escalate twice.
        assert recovery.apply_ready_decisions(tmp_path, job, now=T0 + 4) == []


# ---------------------------------------------------------------------------
# One authorised supersession
# ---------------------------------------------------------------------------


class Supersession:
    __test__ = True

    def _specification_rejection(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, f"test_unresolved: AC-001 test {WRONG_TEST!r} matches no collected test id")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        return launcher, request

    def _supersede(self, tmp_path, request, *, adapter="agent", decided_at="2026-11-13T10:00:00Z"):
        """The canonical supersede decision: same contract id, changed hash."""

        derived = recovery.derive_superseding_contract(
            task_of(tmp_path), [RIGHT_TEST], adapter=adapter, decided_at=decided_at,
            contract_id=request["contract"]["id"],
        )
        return {
            "adapter": adapter,
            "decision": "supersede",
            "corrected_tests": [RIGHT_TEST],
            "supersedes": {
                "contract_id": request["contract"]["id"],
                "contract_sha256": request["contract"]["sha256"],
                "new_contract_id": request["contract"]["id"],
                "new_contract_sha256": contracts.check_frozen(derived),
            },
            "rationale": "the contract named a test that does not exist at source_rev",
            "decided_at": decided_at,
        }

    def test_supersede_once_repoints_the_task_and_touches_no_packet(self, tmp_path):
        launcher, request = self._specification_rejection(tmp_path)
        old_sha = request["contract"]["sha256"]
        assert request["failure"]["class"] == "specification"
        assert request["failure"]["code"] == "test_not_found"
        assert request["supersession_permitted"] is True

        recovery.record_decision(tmp_path, request["request_id"], self._supersede(tmp_path, request))

        result = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert result.action == "dispatch"
        assert len(launcher.posts) == 2

        brief_contract = launcher.posts[-1]["contract"]
        # A correction revises the same contract; only the selection (and
        # therefore the reviewed hash) moves.
        assert brief_contract["id"] == "SPEC-1"
        assert brief_contract["supersedes"] == "SPEC-1"
        assert brief_contract["supersedes_sha256"] == old_sha
        assert brief_contract["acceptance_criteria"][0]["tests"] == [RIGHT_TEST]

        task = task_of(tmp_path)
        assert task["contract_sha256"] != old_sha
        assert task["contract"]["id"] == "SPEC-1"
        assert task["contract_supersession"]["supersedes"] == "SPEC-1"
        assert task["contract_supersession"]["supersedes_sha256"] == old_sha
        stored = tmp_path / "recovery" / "job_a" / "T1.1.contract.json"
        assert stored.exists()
        assert contracts.check_frozen(json.loads(stored.read_text())) == task["contract_sha256"]
        assert not (tmp_path / "packets").exists(), "the original packet must stay untouched"

        # Accepted after the correction: the wave is not blocked by the old id.
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4)
        accepted = rec.tick(
            tmp_path,
            "job_a",
            launcher=launcher,
            verifier=lambda ctx: {"ok": True, "evidence": f"commit {SHA[:12]} verified locally"},
            now=T0 + 5,
        )
        assert accepted.action == "verify"
        assert task_of(tmp_path)["state"] == "verified"

    def test_only_one_supersession_is_ever_permitted(self, tmp_path):
        launcher, request = self._specification_rejection(tmp_path)
        recovery.record_decision(tmp_path, request["request_id"], self._supersede(tmp_path, request))
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)  # dispatch the correction
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4)  # reap
        reject(tmp_path, launcher, f"test_unresolved: AC-001 test {WRONG_TEST!r} matches no collected test id",
               now=T0 + 5)

        second = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        assert second["request_id"] != request["request_id"]
        assert second["supersession_permitted"] is False
        assert second["envelope"]["contract_corrections_allowed"] == 0

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path,
                second["request_id"],
                {
                    "adapter": "agent",
                    "decision": "supersede",
                    "corrected_tests": [RIGHT_TEST],
                    "supersedes": {
                        "contract_id": second["contract"]["id"],
                        "contract_sha256": second["contract"]["sha256"],
                        "new_contract_id": second["contract"]["id"],
                        "new_contract_sha256": "0" * 64,
                    },
                    "rationale": "second correction",
                },
            )
        assert caught.value.code == "supersede_forbidden"

    def test_a_specification_failure_may_not_simply_retry(self, tmp_path):
        _launcher, request = self._specification_rejection(tmp_path)

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(tmp_path, request["request_id"],
                                      {"decision": "retry", "rationale": "retry the same contract anyway"})
        assert caught.value.code == "decision_class_forbidden"


# ---------------------------------------------------------------------------
# Forged, altered and unauthorised material
# ---------------------------------------------------------------------------


class Forgeries:
    __test__ = True

    def test_a_forged_request_on_disk_is_refused_even_with_a_matching_digest(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "security_tool_missing: gitleaks is not on PATH")

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        path = requests_of(tmp_path)[0]
        body = json.loads(path.read_text())
        body["failure"]["class"] = "implementation"  # the lie: "this was the worker's fault"
        body["request_digest"] = recovery.request_digest(body)
        path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.load_request(tmp_path, request["request_id"])
        assert caught.value.code == "request_forged"

    def test_build_request_refuses_a_forged_classification(self, tmp_path):
        job, authorization, _sha = make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "security_tool_missing: gitleaks is not on PATH")
        job = jobs.load_job(tmp_path, "job_a")
        task = job["tasks"][0]

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.build_request(
                tmp_path,
                job,
                task,
                failure={"class": "implementation", "code": "security_tool_missing"},
                authorization=authorization,
            )
        assert caught.value.code == "classification_forged"
        assert len(requests_of(tmp_path)) == 1

    def test_an_altered_request_contract_is_not_a_supersession_target(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, f"test_unresolved: AC-001 test {WRONG_TEST!r} matches no collected test id")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        path = requests_of(tmp_path)[0]
        body = json.loads(path.read_text())
        body["contract"]["sha256"] = "0" * 64  # the contract the decision would replace
        path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path,
                request["request_id"],
                {
                    "adapter": "agent",
                    "decision": "supersede",
                    "corrected_tests": [RIGHT_TEST],
                    "supersedes": {"contract_id": "SPEC-1", "contract_sha256": "0" * 64},
                    "rationale": "supersede whatever is on disk",
                },
            )
        assert caught.value.code == "request_tampered"

    def test_a_decision_cannot_supersede_a_different_contract(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, f"test_unresolved: AC-001 test {WRONG_TEST!r} matches no collected test id")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path,
                request["request_id"],
                {
                    "adapter": "agent",
                    "decision": "supersede",
                    "corrected_tests": [RIGHT_TEST],
                    "supersedes": {
                        "contract_id": "SPEC-1",
                        "contract_sha256": "f" * 64,
                        "new_contract_id": "SPEC-1-r1",
                        "new_contract_sha256": "",
                    },
                    "rationale": "supersede a contract of my choosing",
                },
            )
        assert caught.value.code == "supersede_mismatch"

    def test_an_unauthorised_corrected_test_is_refused(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, f"test_unresolved: AC-001 test {WRONG_TEST!r} matches no collected test id")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path,
                request["request_id"],
                {
                    "adapter": "agent",
                    "decision": "supersede",
                    "corrected_tests": ["tests/test_other.py::test_invented"],
                    "supersedes": {
                        "contract_id": "SPEC-1",
                        "contract_sha256": request["contract"]["sha256"],
                    },
                    "rationale": "widen the acceptance selection",
                },
            )
        assert caught.value.code == "corrected_test_unauthorized"
        assert decisions_of(tmp_path) == []

    def test_a_retry_cannot_smuggle_a_new_test_into_the_lease(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path,
                request["request_id"],
                {"decision": "retry", "rationale": "retry with an extra test",
                 "corrected_tests": ["tests/test_other.py::test_extra"]},
            )
        assert caught.value.code == "corrected_test_unauthorized"


# ---------------------------------------------------------------------------
# Exactly-once decisions and restart idempotency
# ---------------------------------------------------------------------------


class ExactlyOnce:
    __test__ = True

    def test_a_request_is_decided_once(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        recovery.record_decision(
            tmp_path, request["request_id"],
            {"decision": "retry", "rationale": "one corrective attempt", "decided_at": "2026-11-13T10:00:00Z"},
        )

        with pytest.raises(recovery.RecoveryError) as duplicate:
            recovery.record_decision(
                tmp_path, request["request_id"],
                {"decision": "retry", "rationale": "one corrective attempt", "decided_at": "2026-11-13T10:00:00Z"},
            )
        assert duplicate.value.code == "decision_exists"

        with pytest.raises(recovery.RecoveryError) as conflicting:
            recovery.record_decision(tmp_path, request["request_id"],
                                      {"decision": "escalate", "rationale": "a conflicting second decision"})
        assert conflicting.value.code == "decision_exists"
        assert len(decisions_of(tmp_path)) == 1

    def test_a_decision_must_name_its_request(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path, request["request_id"],
                {"request_id": "job_a.T1.99", "decision": "retry"},
            )
        assert caught.value.code == "decision_mismatch"

        with pytest.raises(recovery.RecoveryError) as stale:
            recovery.record_decision(
                tmp_path, request["request_id"],
                {"request_digest": "0" * 64, "decision": "retry"},
            )
        assert stale.value.code == "decision_mismatch"

    def test_repeated_ticks_and_a_restart_never_duplicate_an_attempt(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        # Ten ticks with no decision: one request, one POST.
        for index in range(10):
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3 + index)
        assert len(launcher.posts) == 1
        assert len(requests_of(tmp_path)) == 1

        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        recovery.record_decision(tmp_path, request["request_id"], recovery.deterministic_adapter(request))

        first = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 20)
        assert first.action == "dispatch"
        assert len(launcher.posts) == 2

        # A restart re-reads the job; the applied decision must not re-dispatch.
        for index in range(6):
            rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 21 + index)
        assert len(launcher.posts) == 2
        reloaded = jobs.load_job(tmp_path, "job_a")
        assert recovery.apply_ready_decisions(tmp_path, reloaded, now=T0 + 30) == []
        assert len(launcher.posts) == 2

    def test_build_request_is_idempotent_for_one_attempt(self, tmp_path):
        job, authorization, _sha = make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        job = jobs.load_job(tmp_path, "job_a")
        task = job["tasks"][0]
        first = recovery.build_request(tmp_path, job, task, failure={"code": "tests_failed", "evidence": "tests_failed: x"},
                                       authorization=authorization)
        second = recovery.build_request(tmp_path, job, task, failure={"code": "tests_failed", "evidence": "tests_failed: x"},
                                        authorization=authorization)

        assert first["request_id"] == second["request_id"]
        assert first["request_digest"] == second["request_digest"]
        assert len(requests_of(tmp_path)) == 1


# ---------------------------------------------------------------------------
# RETURN output
# ---------------------------------------------------------------------------


class ReturnEvidence:
    __test__ = True

    def test_the_manifest_lists_every_attempt_and_the_recovery_decision(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)

        # Attempt 1 fails, is retried under a decision, attempt 2 fails and escalates.
        reject(tmp_path, launcher, "tests_failed: named tests failed")
        first = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        recovery.record_decision(tmp_path, first["request_id"], recovery.deterministic_adapter(first))
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 4)
        reject(tmp_path, launcher, "tests_failed: still failing", now=T0 + 5)
        second = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))
        recovery.record_decision(tmp_path, second["request_id"],
                                 recovery.deterministic_adapter(second, task=task_of(tmp_path)))
        rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 6)

        manifest = return_report.build(tmp_path, "job_a")

        assert [item["attempt_id"] for item in manifest["attempts"]] == ["att_0", "att_1"]
        recovery_rows = manifest["recovery"]
        assert [row["request_id"] for row in recovery_rows] == [
            first["request_id"],
            second["request_id"],
        ]
        assert recovery_rows[0]["failure"]["class"] == "implementation"
        assert recovery_rows[0]["decision"]["decision"] == "retry"
        assert recovery_rows[0]["applied"] is True
        assert recovery_rows[1]["decision"]["decision"] == "escalate"
        assert recovery_rows[1]["escalated"] is True
        assert manifest["receiver_decision"] == {
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
            "note": None,
        }

        rendered = (tmp_path / "jobs" / "job_a" / "RETURN.md").read_text()
        assert "## Recovery" in rendered
        assert first["request_id"] in rendered

        # The report verifies against the same durable state.
        assert return_report.verify(tmp_path, "job_a")["ok"] is True

    def test_a_job_without_recovery_keeps_the_legacy_manifest_shape(self, tmp_path):
        make_job(tmp_path, max_attempts=2)

        manifest = return_report.build(tmp_path, "job_a")

        assert "recovery" not in manifest
        assert manifest["receiver_decision"]["status"] == "pending"


# ---------------------------------------------------------------------------
# `job recover`: print the outstanding request, or record a decision
# ---------------------------------------------------------------------------


class JobRecoverCommand:
    __test__ = True

    def test_no_decision_file_prints_the_outstanding_request(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        payload = job_cli.recover(tmp_path, "job_a")

        assert payload["request"] is not None
        assert payload["request"]["failure"]["class"] == "implementation"
        assert payload["decisions_recorded"] == []
        assert decisions_of(tmp_path) == []

    def test_a_decision_file_is_validated_and_recorded_but_not_applied(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        decision_file = tmp_path / "decision.json"
        decision_file.write_text(json.dumps({
            "request_id": request["request_id"],
            "adapter": "human",
            "decision": "escalate",
            "rationale": "the failure needs a human decision, not another attempt",
        }))

        payload = job_cli.recover(tmp_path, "job_a", decision_file=decision_file)

        assert [item["decision"] for item in payload["decisions_recorded"]] == ["escalate"]
        assert len(decisions_of(tmp_path)) == 1
        # Recording never applies: the task stays rejected until the next tick.
        assert task_of(tmp_path)["state"] == "rejected"
        assert len(launcher.posts) == 1

        applied = rec.tick(tmp_path, "job_a", launcher=launcher, now=T0 + 3)
        assert applied.status == "escalated"
        assert gate_of(tmp_path)["state"] == "awaiting_approval"
        assert len(launcher.posts) == 1

    def test_a_bad_decision_file_is_refused_and_records_nothing(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")
        request = recovery.outstanding(tmp_path, jobs.load_job(tmp_path, "job_a"))

        decision_file = tmp_path / "decision.json"
        decision_file.write_text(json.dumps({
            "request_id": request["request_id"],
            "decision": "retry",
            "rationale": "",  # a placeholder is not evidence
        }))

        with pytest.raises(recovery.RecoveryError) as caught:
            job_cli.recover(tmp_path, "job_a", decision_file=decision_file)
        assert caught.value.code == "decision_invalid"
        assert decisions_of(tmp_path) == []

    def test_the_deterministic_adapter_is_reachable_from_the_command(self, tmp_path):
        make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        reject(tmp_path, launcher, "tests_failed: named tests failed")

        payload = job_cli.recover(tmp_path, "job_a", adapter="deterministic")

        assert [item["decision"] for item in payload["decisions_recorded"]] == ["retry"]
        assert payload["request"] is None
        # A second pass does not overwrite or duplicate the recorded decision.
        again = job_cli.recover(tmp_path, "job_a", adapter="deterministic")
        assert again["decisions_recorded"] == []
        assert len(decisions_of(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Integration repair and specification eligibility
# ---------------------------------------------------------------------------


class IntegrationAndSelection:
    __test__ = True

    def _request(self, tmp_path, failure, *, corrections: bool = True):
        job, authorization, _sha = make_job(tmp_path, max_attempts=2)
        launcher = FakeLauncher()
        drive_to_verification(tmp_path, launcher)
        job = jobs.load_job(tmp_path, "job_a")
        task = job["tasks"][0]
        task["attempts"] = ["att_0"]
        task["recovery_policy"] = {
            "max_attempts_per_task": 2,
            "contract_corrections_allowed": corrections,
        }
        job["tasks"][0] = task
        return recovery.build_request(tmp_path, job, task, failure=failure, authorization=authorization)

    def test_an_integration_failure_gets_bounded_repair(self, tmp_path):
        request = self._request(
            tmp_path,
            {
                "code": "integration_failed",
                "evidence": "integration_failed: wave conflicted",
                # Distinct repair evidence: a conflict record must name the paths.
                "paths": ["tools/t.py"],
            },
        )
        assert request["failure"]["class"] == "integration"
        assert request["failure"]["evidence"][0]["kind"] == "conflict"
        assert request["failure"]["evidence"][0]["paths"] == ["tools/t.py"]
        assert recovery.deterministic_adapter(request)["decision"] == "retry"

        recovery.record_decision(tmp_path, request["request_id"],
                                 {"decision": "retry", "rationale": "one bounded repair of the combined wave"})
        assert len(decisions_of(tmp_path)) == 1

        # The ceiling still applies: a second repair needs budget that is gone.
        job = jobs.load_job(tmp_path, "job_a")
        task = job["tasks"][0]
        task["attempts"] = ["att_0", "att_1"]
        jobs.save_job(tmp_path, job)
        second = recovery.build_request(tmp_path, job, task,
                                        failure={"code": "integration_failed",
                                                 "evidence": "integration_failed: again",
                                                 "paths": ["tools/t.py"]},
                                        authorization=jobs.load_authorization(tmp_path, "job_a"))
        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(tmp_path, second["request_id"],
                                     {"decision": "retry", "rationale": "repair it once more anyway"})
        assert caught.value.code == "retry_budget_exhausted"
        assert recovery.deterministic_adapter(second, task=task)["decision"] == "escalate"

    def test_an_integration_failure_without_conflicted_paths_is_refused(self, tmp_path):
        with pytest.raises(recovery.RecoveryError) as caught:
            self._request(
                tmp_path,
                {"code": "integration_failed", "evidence": "integration_failed: wave conflicted"},
            )
        assert caught.value.code == "recovery_evidence_incomplete"
        assert requests_of(tmp_path) == []

    def test_a_specification_failure_without_a_correction_budget_cannot_supersede(self, tmp_path):
        request = self._request(
            tmp_path,
            {"code": "missing_test", "evidence": "missing_test: the approved file is missing at source_rev"},
            corrections=False,
        )
        assert request["failure"]["class"] == "specification"
        assert request["failure"]["code"] in recovery.SELECTION_CODES
        assert request["envelope"]["contract_corrections_allowed"] == 0
        assert request["supersession_permitted"] is False

        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(
                tmp_path,
                request["request_id"],
                {
                    "adapter": "agent",
                    "decision": "supersede",
                    "corrected_tests": [RIGHT_TEST],
                    "supersedes": {
                        "contract_id": request["contract"]["id"],
                        "contract_sha256": request["contract"]["sha256"],
                        "new_contract_id": request["contract"]["id"],
                        "new_contract_sha256": "0" * 64,
                    },
                    "rationale": "supersede a contract without a correction budget",
                },
            )
        assert caught.value.code == "supersede_forbidden"

