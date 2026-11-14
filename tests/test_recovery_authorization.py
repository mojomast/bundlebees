"""M4.2: recovery permission must originate in the sealed human authorization.

A job document is mutable and is written by the coordinator. An authorization is
typed by a human, sealed, and pinned into the job when the job is admitted.
These cases exist to prove the direction of authority between the two:

* every recovery permission — attempts, corrections, the exact replacement
  selectors, the file scope and test scope a correction may touch, and the
  human gates that stay in force — is read from the sealed authorization;
* a job or task ``recovery_policy`` may restate one of those to *narrow* it
  (``min`` for attempts and corrections, intersection for selectors and scopes,
  union for gates) and can never add an attempt, a correction, a selector, a
  file, a test file or a gate;
* an authorization with no recovery block grants nothing: one attempt (the one
  that already failed), no correction, no replacement tests. The only reachable
  decision is escalation;
* a recovery block rewritten after admission — even by a writer that can
  re-seal with the state key — no longer matches the digest the job pinned.

Sealing here is an HMAC with a key file in the state directory. That is tamper
*evidence* for a reader of that directory, not OS isolation: any principal that
can read the key can mint a whole new authorization. The interactive approval
path stops the coordinator from minting authority for itself; it does not claim
to separate two processes running as the same user.

Run (recovery refuses to emit anything without the pinned validator):

    WORKFLOW_SRC=/tmp/verify/wf-packet REQUIRE_WORKFLOW=1 \\
        python -m pytest tests/test_recovery_authorization.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bundlebees_plugin import contract as contracts
from bundlebees_plugin import job as jobs
from bundlebees_plugin import recovery

SHA = "817eac479f441396562962e880c1af596638f399"
REPO = "mojomast/workflow"
JOB = "job_auth"
TASK = "T1"
DECIDED_AT = "2026-11-13T10:00:00Z"

MODULE = "tools/t.py"
TEST_FILE = "tests/test_t.py"
OTHER_FILE = "tests/test_other.py"
BAD_TEST = f"{TEST_FILE}::test_missing"
GOOD_TEST = f"{TEST_FILE}::test_real"
OTHER_TEST = f"{OTHER_FILE}::test_other"
#: A test in the approved file that the approver never named as a replacement.
UNAUTHORIZED_SELECTOR = f"{TEST_FILE}::test_unapproved"
GATES = ("merge", "release", "deploy", "publish")

SPEC_EVIDENCE = f"test_unresolved: AC-001 test {BAD_TEST!r} matches no collected test id"
IMPL_EVIDENCE = "tests_failed: named tests failed after the patch: assert 1 == 2"

#: The ceiling the approver signed in these fixtures: three attempts, one
#: correction, one exact replacement selector, the file and test scope that
#: correction may touch, and all four human gates.
AUTHORIZED: dict = {
    "max_attempts_per_task": 3,
    "contract_corrections_allowed": 1,
    "corrected_tests_allowed": [GOOD_TEST],
    "file_scope": [MODULE, TEST_FILE],
    "test_scope": [TEST_FILE],
    "gates": list(GATES),
}

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"


@pytest.fixture(scope="module", autouse=True)
def _pinned_protocol():
    if not WORKFLOW_SRC:
        if REQUIRE_WORKFLOW:
            pytest.fail("REQUIRE_WORKFLOW=1 but WORKFLOW_SRC does not name the pinned workflow checkout")
        pytest.skip("set WORKFLOW_SRC to the pinned workflow checkout; recovery refuses without its validator")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def frozen_contract(*, assigned: str = MODULE, test_file: str = TEST_FILE) -> dict:
    body = {
        "contract_version": "acdd/1",
        "id": "SPEC-AUTH",
        "title": "the tool answers",
        "outcome": {"summary": "the tool answers", "observable_behavior": ["the tool answers 42"]},
        "source_rev": SHA,
        "scope": {"assigned_files": [assigned], "test_files": [test_file]},
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "statement": "the tool answers 42",
                "tests": [f"{test_file}::test_missing"],
                "verification": "test",
            }
        ],
        "tdd": {"applicable": True, "test_first": False, "reason": "the approved test exists at baseline"},
        "verification": {"independent": True},
        "evidence_required": ["green"],
        "authorization": {"human_gates": list(GATES)},
    }
    return contracts.freeze_contract(body, approved_by="reviewer", approved_at="2026-11-13T00:00:00Z")


def make_case(
    tmp_path: Path,
    *,
    recovery_block: dict | None,
    gates: tuple[str, ...] = GATES,
    job_policy: dict | None = None,
    task_policy: dict | None = None,
    contract: dict | None = None,
) -> tuple[Path, dict, dict]:
    """An authorized job whose one task is already rejected with a durable attempt."""

    state = Path(tmp_path)
    jobs.record_authorization(
        state,
        job_id=JOB,
        statement="Add contract source-rev validation to acdd-lint, nothing else.",
        repo=REPO,
        base_rev=SHA,
        scope=[MODULE, TEST_FILE, OTHER_FILE],
        bees=4,
        wall_clock_s=5400,
        gates=gates,
        require_tty=False,
        interactive=False,
        by="reviewer",
        recovery=recovery_block,
    )
    contract = contract if contract is not None else frozen_contract()
    jobs.create_job(
        state,
        job_id=JOB,
        tasks=[
            {
                "task_id": TASK,
                "objective": "make the tool answer 42",
                "lease": [MODULE],
                "acceptance": {"criteria": ["the tool answers 42"], "tests": [TEST_FILE]},
                "contract": contract,
                "contract_sha256": contracts.check_frozen(contract),
            }
        ],
    )
    job = jobs.load_job(state, JOB, recover=False)
    task = job["tasks"][0]
    if job_policy is not None:
        job["recovery_policy"] = dict(job_policy)
    if task_policy is not None:
        task["recovery_policy"] = dict(task_policy)
    for to_state in ("dispatching", "dispatched", "running", "verifying", "rejected"):
        jobs.transition(job, TASK, to_state, actor="reconciler", reason="M4.2 fixture rejection")
    task["attempts"] = ["att_0"]
    task["evidence"] = SPEC_EVIDENCE
    jobs.save_job(state, job)
    return state, job, task


def authorization_of(state: Path) -> dict:
    return jobs.load_authorization(state, JOB)


def build(state: Path, job: dict, task: dict, *, evidence: str = SPEC_EVIDENCE) -> dict:
    return recovery.build_request(
        state,
        job,
        task,
        failure={"ok": False, "evidence": evidence, "attempt_id": (task.get("attempts") or ["att_0"])[-1]},
        authorization=authorization_of(state),
    )


def supersede_body(state: Path, request: dict, tests: list[str]) -> dict:
    job = jobs.load_job(state, JOB, recover=False)
    task = job["tasks"][0]
    derived = recovery.derive_superseding_contract(
        task, tests, adapter="agent", decided_at=DECIDED_AT, contract_id=request["contract"]["id"]
    )
    return {
        "schema": recovery.SCHEMA,
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "adapter": "agent",
        "decision": "supersede",
        "corrected_tests": list(tests),
        "supersedes": {
            "contract_id": request["contract"]["id"],
            "contract_sha256": request["contract"]["sha256"],
            "new_contract_id": derived["id"],
            "new_contract_sha256": contracts.check_frozen(derived),
        },
        "rationale": "The approved fixture method is the replacement the approver named.",
        "decided_at": DECIDED_AT,
    }


def rewrite_request(state: Path, request: dict, **envelope) -> dict:
    """Rewrite a durable request's envelope as an attacker with file access would."""

    path = recovery.request_path(state, JOB, TASK, recovery.request_ordinal(state, request["request_id"]))
    body = json.loads(path.read_text(encoding="utf-8"))
    body["envelope"] = envelope
    body["supersession_permitted"] = bool(envelope.get("contract_corrections_allowed"))
    body["request_digest"] = recovery.request_digest(body)
    path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return body


# ---------------------------------------------------------------------------
# The signed ceiling is the ceiling
# ---------------------------------------------------------------------------


def test_a_signed_ceiling_is_honoured(tmp_path):
    state, job, task = make_case(tmp_path, recovery_block=AUTHORIZED)
    request = build(state, job, task)

    assert request["envelope"] == {
        "max_attempts_per_task": 3,
        "contract_corrections_allowed": 1,
        "corrected_tests_allowed": [GOOD_TEST],
    }
    assert request["supersession_permitted"] is True
    recorded = recovery.record_decision(state, request["request_id"], supersede_body(state, request, [GOOD_TEST]))
    assert recorded["decision"] == "supersede"


def test_an_authorization_without_a_recovery_block_grants_nothing(tmp_path):
    """A mutable job document cannot grant what the approver never signed."""

    wide = {
        "max_attempts_per_task": 5,
        "contract_corrections_allowed": 1,
        "corrected_tests_allowed": [GOOD_TEST],
        "file_scope": [MODULE, TEST_FILE],
        "test_scope": [TEST_FILE],
        "gates": list(GATES),
    }
    state, job, task = make_case(
        tmp_path, recovery_block=None, job_policy=wide, task_policy=dict(wide)
    )
    request = build(state, job, task)

    assert request["envelope"] == {
        "max_attempts_per_task": 1,
        "contract_corrections_allowed": 0,
        "corrected_tests_allowed": [],
    }, "a job policy must not fabricate a recovery ceiling the approver did not sign"
    assert request["supersession_permitted"] is False

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(state, request["request_id"], supersede_body(state, request, [GOOD_TEST]))
    assert caught.value.code in {"supersede_forbidden", "contract_correction_forbidden"}

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(
            state, request["request_id"], {"decision": "retry", "rationale": "retry it anyway"}
        )
    assert caught.value.code in {"retry_budget_exhausted", "decision_class_forbidden"}
    assert recovery.deterministic_adapter(request, task=task)["decision"] == "escalate"

    # The same empty ceiling refuses a retry of an implementation failure on
    # its budget rather than on its class: the one attempt is already spent.
    impl_state, impl_job, impl_task = make_case(tmp_path / "impl", recovery_block=None)
    impl_request = build(impl_state, impl_job, impl_task, evidence=IMPL_EVIDENCE)
    assert impl_request["envelope"]["max_attempts_per_task"] == 1
    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(
            impl_state, impl_request["request_id"], {"decision": "retry", "rationale": "retry it anyway"}
        )
    assert caught.value.code == "retry_budget_exhausted"
    assert recovery.deterministic_adapter(impl_request, task=impl_task)["decision"] == "escalate"

    # Escalation is the only admissible decision, and it dispatches nothing.
    recorded = recovery.record_decision(
        state, request["request_id"], {"decision": "escalate", "rationale": "no authorized retry or correction"}
    )
    assert recorded["decision"] == "escalate"
    assert recovery.list_decisions(state, JOB) and not any(
        state.joinpath("recovery").rglob("*.contract.json")
    )


def test_a_job_document_recovery_block_is_never_read(tmp_path):
    """Only the authorization argument is authority; a job copy is not."""

    state, job, task = make_case(tmp_path, recovery_block=None)
    job["authorization"]["recovery"] = dict(AUTHORIZED)
    job["recovery_policy"] = dict(AUTHORIZED)
    request = build(state, job, task)
    assert request["envelope"]["max_attempts_per_task"] == 1
    assert request["envelope"]["contract_corrections_allowed"] == 0


# ---------------------------------------------------------------------------
# Policies may narrow, never widen
# ---------------------------------------------------------------------------


def test_a_job_policy_cannot_widen_attempts(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block={**AUTHORIZED, "max_attempts_per_task": 2},
        job_policy={"max_attempts_per_task": 5},
        task_policy={"max_attempts_per_task": 4},
    )
    assert build(state, job, task)["envelope"]["max_attempts_per_task"] == 2


def test_a_job_policy_cannot_widen_corrections(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block={"max_attempts_per_task": 3, "contract_corrections_allowed": 0},
        job_policy={"contract_corrections_allowed": 1},
        task_policy={"contract_corrections_allowed": True},
    )
    request = build(state, job, task)
    assert request["envelope"]["contract_corrections_allowed"] == 0
    assert request["envelope"]["corrected_tests_allowed"] == []
    assert request["supersession_permitted"] is False


def test_a_job_policy_cannot_add_a_selector(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block=AUTHORIZED,
        job_policy={"corrected_tests_allowed": [GOOD_TEST, UNAUTHORIZED_SELECTOR]},
        task_policy={"corrected_tests_allowed": [GOOD_TEST, UNAUTHORIZED_SELECTOR]},
    )
    request = build(state, job, task)
    assert request["envelope"]["corrected_tests_allowed"] == [GOOD_TEST]
    # The selector the policy wanted belongs to this job's file scope, but the
    # approver never named it, so it is still not authority.
    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(
            state, request["request_id"], supersede_body(state, request, [UNAUTHORIZED_SELECTOR])
        )
    assert caught.value.code == "corrected_test_unauthorized"


def test_a_job_policy_cannot_add_scope(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block=AUTHORIZED,
        job_policy={"file_scope": [MODULE, TEST_FILE, OTHER_FILE], "test_scope": [TEST_FILE, OTHER_FILE]},
    )
    ceiling = recovery.envelope_for(job, task, authorization_of(state))
    assert ceiling["file_scope"] == sorted([MODULE, TEST_FILE])
    assert ceiling["test_scope"] == [TEST_FILE]


def test_a_contract_outside_the_authorized_file_scope_cannot_be_corrected(tmp_path):
    outside = frozen_contract(assigned=MODULE, test_file=OTHER_FILE)
    state, job, task = make_case(tmp_path, recovery_block=AUTHORIZED, contract=outside)
    request = build(state, job, task)
    assert request["supersession_permitted"] is False, (
        "the approver's file scope names tests/test_t.py, not tests/test_other.py: "
        "a correction may not run under a scope that was never approved"
    )
    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(
            state, request["request_id"], supersede_body(state, request, [f"{OTHER_FILE}::test_real"])
        )
    assert caught.value.code in {"supersede_forbidden", "contract_correction_forbidden"}


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_a_gate_removed_from_the_job_is_refused(tmp_path):
    state, job, task = make_case(tmp_path, recovery_block=AUTHORIZED)
    job["approval_gate"]["gates"] = ["merge", "release"]
    jobs.save_job(state, job)
    with pytest.raises(recovery.RecoveryError) as caught:
        build(state, job, task)
    assert caught.value.code == "gate_removed"
    assert "deploy" in caught.value.message and "publish" in caught.value.message


def test_a_policy_may_add_a_gate_but_never_remove_one(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block=AUTHORIZED,
        task_policy={"gates": ["merge", "counsel"]},
    )
    ceiling = recovery.envelope_for(job, task, authorization_of(state))
    assert ceiling["gates"] == sorted(set(GATES) | {"counsel"}), (
        "a policy gate is an addition; the authorization's four gates must survive"
    )


def test_a_recovery_block_may_not_drop_an_authorization_gate(tmp_path):
    with pytest.raises(jobs.JobError) as caught:
        make_case(tmp_path, recovery_block={**AUTHORIZED, "gates": ["merge"]})
    assert caught.value.code == "authorization_incomplete"
    assert "release" in caught.value.message


# ---------------------------------------------------------------------------
# Sealing: a rewritten block is visible
# ---------------------------------------------------------------------------


def test_an_altered_authorization_recovery_block_is_detected(tmp_path):
    state, job, task = make_case(tmp_path, recovery_block=AUTHORIZED)
    path = jobs.authorization_path(state, JOB)
    os.chmod(path, 0o600)
    sealed = json.loads(path.read_text(encoding="utf-8"))

    # 1) An edit that does not re-seal fails the MAC.
    sealed["body"]["recovery"]["max_attempts_per_task"] = 5
    path.write_text(json.dumps(sealed), encoding="utf-8")
    with pytest.raises(jobs.JobError) as caught:
        jobs.load_authorization(state, JOB)
    assert caught.value.code == "authorization_tampered"

    # 2) A writer with the state key can re-seal. The block is then internally
    #    consistent, but it is no longer the block this job was admitted under.
    sealed["body"]["recovery_digest"] = jobs.recovery_digest(sealed["body"]["recovery"])
    path.write_text(json.dumps(jobs._seal(sealed["body"], jobs._load_key(state))), encoding="utf-8")
    altered = jobs.load_authorization(state, JOB)
    assert altered["recovery"]["max_attempts_per_task"] == 5

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.envelope_for(job, task, altered)
    assert caught.value.code == "authorization_recovery_altered"

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.build_request(
            state, job, task, failure={"ok": False, "evidence": SPEC_EVIDENCE}, authorization=altered
        )
    assert caught.value.code == "authorization_recovery_altered"


def test_an_authorization_recovery_block_cannot_be_added_after_admission(tmp_path):
    state, job, task = make_case(tmp_path, recovery_block=None)
    # Re-seal the same authorization with a ceiling; the job pinned the empty one.
    path = jobs.authorization_path(state, JOB)
    os.chmod(path, 0o600)
    body = json.loads(path.read_text(encoding="utf-8"))["body"]
    body["recovery"] = dict(AUTHORIZED)
    body["recovery_digest"] = jobs.recovery_digest(body["recovery"])
    path.write_text(json.dumps(jobs._seal(body, jobs._load_key(state))), encoding="utf-8")

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.envelope_for(job, task, jobs.load_authorization(state, JOB))
    assert caught.value.code == "authorization_recovery_altered"


# ---------------------------------------------------------------------------
# A forged request is not authority either
# ---------------------------------------------------------------------------


def test_a_forged_request_cannot_widen_the_sealed_ceiling(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block={"max_attempts_per_task": 3, "contract_corrections_allowed": 0},
    )
    request = build(state, job, task)
    assert request["supersession_permitted"] is False

    forged = rewrite_request(
        state,
        request,
        max_attempts_per_task=3,
        contract_corrections_allowed=1,
        corrected_tests_allowed=[GOOD_TEST],
    )
    assert forged["supersession_permitted"] is True  # the forged document claims authority

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(state, forged["request_id"], supersede_body(state, forged, [GOOD_TEST]))
    assert caught.value.code == "recovery_authority_widened"
    assert recovery.list_decisions(state, JOB) == []


# ---------------------------------------------------------------------------
# The dogfood's shape: an authorized ceiling narrowed by job and task policies
# ---------------------------------------------------------------------------


def test_a_dogfood_style_authorized_envelope_narrows_correctly(tmp_path):
    state, job, task = make_case(
        tmp_path,
        recovery_block=AUTHORIZED,
        job_policy={"max_attempts_per_task": 3, "contract_corrections_allowed": True},
        task_policy={"max_attempts_per_task": 2, "contract_corrections_allowed": False},
    )
    first = recovery.envelope_for(job, task, authorization_of(state))
    assert first == {
        "max_attempts_per_task": 2,
        "contract_corrections_allowed": 0,
        "corrected_tests_allowed": [],
        "file_scope": sorted([MODULE, TEST_FILE]),
        "test_scope": [TEST_FILE],
        "gates": sorted(GATES),
    }, "the task narrows the signed three-attempt/one-correction ceiling to two and none"

    request = build(state, job, task)
    assert request["envelope"] == {
        "max_attempts_per_task": 2,
        "contract_corrections_allowed": 0,
        "corrected_tests_allowed": [],
    }
    assert request["supersession_permitted"] is False

    job["tasks"][0]["recovery_policy"] = {
        "max_attempts_per_task": 3,
        "contract_corrections_allowed": True,
        "corrected_tests_allowed": [GOOD_TEST],
    }
    jobs.save_job(state, job)
    second_task = jobs.load_job(state, JOB, recover=False)["tasks"][0]
    second = recovery.envelope_for(job, second_task, authorization_of(state))
    assert second["max_attempts_per_task"] == 3
    assert second["contract_corrections_allowed"] == 1
    assert second["corrected_tests_allowed"] == [GOOD_TEST]


# ---------------------------------------------------------------------------
# The deterministic path still works inside the ceiling
# ---------------------------------------------------------------------------


def test_deterministic_recovery_still_works(tmp_path):
    state, job, task = make_case(tmp_path, recovery_block=AUTHORIZED)
    request = build(state, job, task, evidence=IMPL_EVIDENCE)
    assert request["failure"]["class"] == "implementation"
    assert request["envelope"]["max_attempts_per_task"] == 3

    decision = recovery.deterministic_adapter(request, task=task)
    assert decision["decision"] == "retry"
    recorded = recovery.record_decision(state, request["request_id"], decision)
    assert recorded["decision"] == "retry"
    assert len(recovery.list_decisions(state, JOB)) == 1

    # A specification failure inside the same ceiling reaches the authorized
    # supersession, and only with the selector the approver named.
    spec_state, spec_job, spec_task = make_case(tmp_path / "spec", recovery_block=AUTHORIZED)
    spec_request = build(spec_state, spec_job, spec_task)
    assert recovery.deterministic_adapter(spec_request, task=spec_task)["decision"] == "escalate"
    accepted = recovery.record_decision(
        spec_state, spec_request["request_id"], supersede_body(spec_state, spec_request, [GOOD_TEST])
    )
    assert accepted["decision"] == "supersede"
