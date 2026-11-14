"""M4.1 conformance: bundlebees emits and consumes only ``rizzstack.recovery/1``.

These cases drive the real production module against the *pinned* workflow
validator (``WORKFLOW_SRC/tools/recovery.py``). They prove three things the M4
dogfood cannot prove on its own:

* **emission** — a request and a decision carry exactly the protocol's fields,
  an integer correction budget, structured evidence, canonical failure codes
  and a same-id supersession;
* **admission** — the pinned validator is the gate at request creation, at
  decision admission and again at apply time; a document that edits its way
  around any of them is refused with the protocol's own code;
* **history** — pre-conformance records are labelled ``legacy`` in a sidecar,
  never rewritten, and never used to decide or dispatch anything.

Run (the suite fails, never skips, when the protocol is required):

    WORKFLOW_SRC=/tmp/verify/wf-packet REQUIRE_WORKFLOW=1 \\
        python -m pytest tests/test_recovery_conformance.py -q
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import contract as contracts
from bundlebees_plugin import job as jobs
from bundlebees_plugin import recovery

SHA = "817eac479f441396562962e880c1af596638f399"
BASE = "2026-11-13T00:00:00Z"

TEST_FILE = "tests/test_t.py"
BAD_TEST = f"{TEST_FILE}::test_missing"
GOOD_TEST = f"{TEST_FILE}::test_real"

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"

#: The exact request and decision captured from the pre-conformance runtime.
#: They are embedded verbatim so the labelling test can prove the bytes are
#: preserved, not regenerated.
CAPTURED_REQUEST = (
    '{"attempt_id":"att_m4_2","attempts_used":1,"contract":{"id":"T2-REACHABILITY",'
    '"sha256":"b30fdd670282a097701bae2841177f99ccefcc9243055ac2696766dd6a1e249b"},'
    '"created_at":"2026-11-14T01:49:12Z","envelope":{"contract_corrections_allowed":true,'
    '"corrected_tests_allowed":["tools/acdd-lint/tests/test_check_reachability.py",'
    '"tools/acdd-lint/tests/test_check_reachability.py::Reachability::test_ancestor_ok"],'
    '"max_attempts_per_task":2},"failure":{"class":"specification","code":"test_unresolved",'
    '"evidence":"test_unresolved: no test was collected for tools/acdd-lint/tests/'
    'test_check_reachability.py::Reachability::test_ancestor_ok; tests_failed: named tests '
    'failed after the patch: no tests ran in 0.01s","rationale":"evidence code '
    "'test_unresolved' is recorded as a specification failure\"},"
    '"feature_id":"acdd-source-rev","job_id":"job_m4_dogfood","number":1,'
    '"outstanding_question":"Task T2 was rejected as a specification failure (test_unresolved). '
    'Decide whether to supersede the frozen contract once with an authorised corrected test '
    'selection; retry once within the remaining budget (1 attempt(s) left); escalate to a human. '
    'Record the decision with `bundlebees job recover --job-id ... --decision-file ...`; no '
    'corrective attempt is dispatched until a validated decision exists.",'
    '"request_digest":"cb79fa542688ac8dad6a602c7b42d8a6fb506129d7c0444fc7787d30762f1ff5",'
    '"request_id":"job_m4_dogfood.T2.1","schema":"rizzstack.recovery/1",'
    '"supersession_permitted":true,"task_id":"T2"}'
)

CAPTURED_DECISION = (
    '{"adapter":"deterministic-agent",'
    '"corrected_tests":["tools/acdd-lint/tests/test_check_reachability.py::Reachability::'
    'test_reachable_commit_is_silent"],"decided_at":"2026-11-13T20:00:00Z",'
    '"decision":"supersede","decision_digest":'
    '"c5f8827a4d01f1aa5ac366ad2003c4596fd9093498b69b4365bc436f3f36681e",'
    '"rationale":"The frozen contract names a selector that does not exist at the pinned '
    'revision, so the acceptance selection is unusable rather than the patch. The approved '
    'fixture method selects the same behaviour the criterion states, so one supersession '
    'inside the envelope is enough.","request_digest":'
    '"cb79fa542688ac8dad6a602c7b42d8a6fb506129d7c0444fc7787d30762f1ff5",'
    '"request_id":"job_m4_dogfood.T2.1","schema":"rizzstack.recovery/1","supersedes":'
    '{"contract_id":"T2-REACHABILITY","contract_sha256":'
    '"b30fdd670282a097701bae2841177f99ccefcc9243055ac2696766dd6a1e249b",'
    '"new_contract_id":"T2-REACHABILITY-r1","new_contract_sha256":'
    '"5d2fa3d77465420090ef157250ad0a7f37b8b2d52f61177ff6e44adaef7172d4"}}'
)

REQUEST_FIELDS = frozenset({
    "schema", "request_id", "job_id", "feature_id", "task_id", "attempt_id", "failure",
    "contract", "supersession_permitted", "envelope", "outstanding_question", "request_digest",
})

DECISION_FIELDS = frozenset({
    "schema", "request_id", "request_digest", "adapter", "decision", "corrected_tests",
    "supersedes", "rationale", "decided_at",
})


@pytest.fixture(scope="module", autouse=True)
def _pinned_protocol():
    if not WORKFLOW_SRC:
        if REQUIRE_WORKFLOW:
            pytest.fail("REQUIRE_WORKFLOW=1 but WORKFLOW_SRC does not name the pinned workflow checkout")
        pytest.skip("set WORKFLOW_SRC to the pinned workflow checkout; recovery refuses without its validator")


# ---------------------------------------------------------------------------
# Fixtures: a real authorization, a real frozen contract, a rejected task
# ---------------------------------------------------------------------------


def frozen_contract(*, contract_id: str = "SPEC-1", tests: tuple[str, ...] = (BAD_TEST,)) -> dict:
    body = {
        "contract_version": "acdd/1",
        "id": contract_id,
        "title": "the tool answers",
        "outcome": {"summary": "the tool answers", "observable_behavior": ["the tool answers 42"]},
        "source_rev": SHA,
        "scope": {"assigned_files": ["tools/t.py"], "test_files": [TEST_FILE]},
        "acceptance_criteria": [
            {"id": "AC-001", "statement": "the tool answers 42", "tests": list(tests), "verification": "test"}
        ],
        "tdd": {"applicable": True, "test_first": False, "reason": "the approved test exists at baseline"},
        "verification": {"independent": True},
        "evidence_required": ["green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    return contracts.freeze_contract(body, approved_by="reviewer", approved_at=BASE)


def new_state(
    tmp_path: Path,
    *,
    attempts: tuple[str, ...] = ("att_0",),
    max_attempts: int = 2,
    corrections: bool = True,
    allowed: tuple[str, ...] = (GOOD_TEST,),
) -> tuple[Path, dict]:
    """An authorized, rejected task whose attempt is preserved on disk."""

    authorization = jobs.record_authorization(
        tmp_path,
        job_id="job_a",
        statement="Add contract source-rev validation to acdd-lint, nothing else.",
        repo="mojomast/workflow",
        base_rev=SHA,
        scope=["tools/**"],
        bees=3,
        wall_clock_s=3600,
        interactive=True,
        recovery={
            "max_attempts_per_task": max_attempts,
            "contract_corrections_allowed": corrections,
            "corrected_tests_allowed": list(allowed),
        },
    )
    contract = frozen_contract()
    jobs.create_job(
        tmp_path,
        job_id="job_a",
        tasks=[
            {
                "task_id": "T1",
                "objective": "make the tool answer 42",
                "lease": ["tools/t.py"],
                "contract": contract,
                "contract_sha256": contracts.check_frozen(contract),
                "acceptance": {"criteria": ["the tool answers 42"], "tests": [BAD_TEST]},
            }
        ],
    )
    job = jobs.load_job(tmp_path, "job_a", recover=False)
    task = job["tasks"][0]
    for to_state in ("dispatching", "dispatched", "running", "verifying", "rejected"):
        jobs.transition(job, "T1", to_state, actor="reconciler", reason="conformance fixture rejection")
    task["attempts"] = list(attempts)
    jobs.save_job(tmp_path, job)
    return tmp_path, authorization


def build(state: Path, *, evidence: str | None = None, code: str | None = None) -> dict:
    job = jobs.load_job(state, "job_a", recover=False)
    task = job["tasks"][0]
    record: dict = {
        "ok": False,
        "evidence": evidence or f"test_unresolved: no test was collected for {BAD_TEST}",
        "attempt_id": task["attempts"][-1],
    }
    if code is not None:
        record["code"] = code
    return recovery.build_request(
        state, job, task, failure=record, authorization=jobs.load_authorization(state, "job_a")
    )


def supersede_decision(state: Path, request: dict, *, adapter: str = "agent") -> dict:
    job = jobs.load_job(state, "job_a", recover=False)
    task = job["tasks"][0]
    derived = recovery.derive_superseding_contract(
        task,
        [GOOD_TEST],
        adapter=adapter,
        decided_at="2026-11-13T10:00:00Z",
        contract_id=request["contract"]["id"],
    )
    return {
        "schema": recovery.SCHEMA,
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "adapter": adapter,
        "decision": "supersede",
        "corrected_tests": [GOOD_TEST],
        "supersedes": {
            "contract_id": request["contract"]["id"],
            "contract_sha256": request["contract"]["sha256"],
            "new_contract_id": request["contract"]["id"],
            "new_contract_sha256": contracts.check_frozen(derived),
        },
        "rationale": "the frozen contract names a selector that does not exist at the pinned revision",
        "decided_at": "2026-11-13T10:00:00Z",
    }


def rewrite_request(state: Path, request_id: str, **changes) -> dict:
    """Edit a durable request in place, resealing the digest (a forgery test)."""

    path = next(
        path for path in (state / "recovery" / "job_a").glob("*.request.json")
        if json.loads(path.read_text()).get("request_id") == request_id
    )
    body = json.loads(path.read_text())
    for dotted, value in changes.items():
        walk = body
        keys = dotted.split(".")
        for key in keys[:-1]:
            walk = walk[key]
        walk[keys[-1]] = value
    body["request_digest"] = recovery.request_digest(body)
    path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return body


# ---------------------------------------------------------------------------
# Positives: the emitted documents are exactly canonical
# ---------------------------------------------------------------------------


def test_a_built_request_has_exactly_the_protocol_fields(tmp_path):
    state, _authorization = new_state(tmp_path)

    request = build(state)

    assert set(request) == REQUEST_FIELDS, sorted(set(request) ^ REQUEST_FIELDS)
    assert request["schema"] == "rizzstack.recovery/1"
    assert request["request_digest"] == recovery.request_digest(request)
    # test_unresolved is normalised onto the protocol's code, and the original
    # observation survives verbatim in the evidence artefact.
    assert request["failure"]["class"] == "specification"
    assert request["failure"]["code"] == "test_not_found"
    assert "test_unresolved" in request["failure"]["rationale"]
    evidence = request["failure"]["evidence"]
    assert isinstance(evidence, list) and len(evidence) == 1
    entry = evidence[0]
    assert entry["kind"] == "test"
    assert set(entry) <= {"kind", "reference", "sha256", "paths"}
    payload = (state / entry["reference"]).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
    assert "test_unresolved" in json.loads(payload)["text"]
    # The correction budget is an integer, never the old boolean.
    budget = request["envelope"]["contract_corrections_allowed"]
    assert budget == 1 and not isinstance(budget, bool)
    assert request["envelope"]["corrected_tests_allowed"] == [GOOD_TEST]

    recovery.protocol_validator().validate_request(request)


def test_a_recorded_supersession_is_canonical_and_keeps_the_contract_id(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)

    recorded = recovery.record_decision(state, request["request_id"], supersede_decision(state, request))

    assert set(recorded) == DECISION_FIELDS, sorted(set(recorded) ^ DECISION_FIELDS)
    assert recorded["schema"] == "rizzstack.recovery/1"
    assert "decision_digest" not in recorded
    assert recorded["request_digest"] == request["request_digest"]
    supersedes = recorded["supersedes"]
    assert set(supersedes) == {"contract_id", "contract_sha256", "new_contract_id", "new_contract_sha256"}
    assert supersedes["new_contract_id"] == supersedes["contract_id"] == request["contract"]["id"]
    assert supersedes["new_contract_sha256"] != supersedes["contract_sha256"]
    recovery.protocol_validator().validate_decision(
        recorded, request=request, attempts_used=1, corrections_used=0
    )

    job = jobs.load_job(state, "job_a", recover=False)
    applied = recovery.apply_ready_decisions(state, job)
    assert [item["decision"] for item in applied] == ["supersede"]
    task = job["tasks"][0]
    assert task["contract"]["id"] == request["contract"]["id"]
    assert task["contract_sha256"] != request["contract"]["sha256"]
    assert contracts.check_frozen(task["contract"]) == task["contract_sha256"]
    # The decision re-reads and re-validates from disk; no digest field is
    # needed because it is bound to the request.
    assert recovery.read_decision(state, request) == recorded


def test_the_pinned_validator_gates_build_record_and_apply(tmp_path, monkeypatch):
    state, _authorization = new_state(tmp_path)
    module = recovery.protocol_validator()
    seen: list[str] = []
    for name in ("validate_request", "validate_decision", "validate_pair"):
        original = getattr(module, name)

        def wrapper(*args, __name=name, __original=original, **kwargs):
            seen.append(__name)
            return __original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    request = build(state)
    assert "validate_request" in seen
    recovery.record_decision(state, request["request_id"], supersede_decision(state, request))
    assert "validate_decision" in seen
    job = jobs.load_job(state, "job_a", recover=False)
    recovery.apply_ready_decisions(state, job)
    assert "validate_pair" in seen


def test_every_canonical_code_maps_to_itself_and_the_internal_vocabulary_is_normalised():
    for name, codes in recovery.CANONICAL_CODES.items():
        for code in codes:
            assert recovery.class_for_code(code) == name
            assert recovery.canonical_code_for(code) == code
    assert recovery.canonical_code_for("test_unresolved") == "test_not_found"
    assert recovery.canonical_code_for("tests_failed") == "test_failed"
    assert recovery.SELECTION_CODES == {"acceptance_uncovered", "test_not_found", "missing_test"}


# ---------------------------------------------------------------------------
# Negatives: every way a non-canonical record could sneak in
# ---------------------------------------------------------------------------


def test_an_inconsistent_protocol_version_is_refused(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)
    rewrite_request(state, request["request_id"], schema="rizzstack.recovery/2")

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.load_request(state, request["request_id"])
    assert caught.value.code == "recovery_version_unsupported"

    # A decision cannot smuggle a version of its own either.
    decision = {"schema": "rizzstack.recovery/2", "decision": "escalate",
                "rationale": "a decision that names a protocol that does not exist"}
    with pytest.raises(recovery.RecoveryError) as refused:
        recovery.record_decision(state, request["request_id"], decision)
    assert refused.value.code == "recovery_version_unsupported"


def test_an_unknown_failure_code_is_refused(tmp_path):
    with pytest.raises(recovery.RecoveryError) as forged:
        recovery.classify_failure({"class": "specification", "code": "made_up"}, contract=None)
    assert forged.value.code == "classification_forged"

    state, _authorization = new_state(tmp_path)
    request = build(state)
    rewrite_request(state, request["request_id"], **{"failure.code": "test_unresolved"})

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.load_request(state, request["request_id"])
    assert caught.value.code == "request_forged"

    # A canonical code from the wrong class is just as forged.
    rewrite_request(state, request["request_id"], **{"failure.code": "test_failed"})
    with pytest.raises(recovery.RecoveryError) as wrong:
        recovery.load_request(state, request["request_id"])
    assert wrong.value.code == "request_forged"


def test_a_boolean_envelope_is_labelled_legacy_and_an_incompatible_budget_is_refused(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)

    # The emitter never writes a boolean budget.
    assert isinstance(request["envelope"]["contract_corrections_allowed"], int)
    assert not isinstance(request["envelope"]["contract_corrections_allowed"], bool)

    # A pre-conformance boolean is labelled and refused, not tolerated.
    rewrite_request(state, request["request_id"], **{"envelope.contract_corrections_allowed": True})
    with pytest.raises(recovery.RecoveryError) as legacy:
        recovery.load_request(state, request["request_id"])
    assert legacy.value.code == "recovery_record_legacy"

    # A canonical integer budget that contradicts the request is the protocol's
    # own refusal. An implementation retry may not be granted a correction.
    request = build(state, evidence="tests_failed: named tests failed after the patch")
    assert request["envelope"]["contract_corrections_allowed"] == 0
    rewrite_request(state, request["request_id"], **{"envelope.contract_corrections_allowed": 1})
    with pytest.raises(recovery.RecoveryError) as widened:
        recovery.load_request(state, request["request_id"])
    assert widened.value.code == "envelope_widened"

    rewrite_request(state, request["request_id"], **{"envelope.contract_corrections_allowed": 2})
    with pytest.raises(recovery.RecoveryError) as schema:
        recovery.load_request(state, request["request_id"])
    assert schema.value.code == "recovery_schema"


def test_a_forged_contract_revision_is_refused(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)
    decision = supersede_decision(state, request)
    # The declared revision is not the contract this decision would freeze.
    decision["supersedes"]["new_contract_sha256"] = "0" * 64

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(state, request["request_id"], decision)
    assert caught.value.code == "supersede_mismatch"
    assert recovery.list_decisions(state, "job_a") == []


def test_an_invalid_supersession_identity_is_refused(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)
    decision = supersede_decision(state, request)
    # A correction revises the same contract; a new id is a new feature.
    decision["supersedes"]["new_contract_id"] = f"{request['contract']['id']}-r1"

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(state, request["request_id"], decision)
    assert caught.value.code == "decision_scope_widened"
    assert recovery.list_decisions(state, "job_a") == []


def test_a_supersession_with_an_unchanged_hash_is_refused(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)
    decision = supersede_decision(state, request)
    decision["supersedes"]["new_contract_sha256"] = request["contract"]["sha256"]

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(state, request["request_id"], decision)
    assert caught.value.code == "supersede_mismatch"


def test_an_extra_decision_field_including_a_digest_is_refused(tmp_path):
    state, _authorization = new_state(tmp_path)
    request = build(state)

    for label, extra in (
        ("decision_digest", {"decision_digest": "0" * 64}),
        ("envelope", {"envelope": {"max_attempts_per_task": 9}}),
        ("budget", {"budget": {"bees": 99}}),
    ):
        decision = {
            "decision": "escalate",
            "rationale": "an escalation with a field the protocol does not define",
            **extra,
        }
        with pytest.raises(recovery.RecoveryError) as caught:
            recovery.record_decision(state, request["request_id"], decision)
        assert caught.value.code == "decision_invalid", label
        assert label in caught.value.message
    assert recovery.list_decisions(state, "job_a") == []

    # A decision document carrying a digest is pre-conformance, so a consumer
    # labels and refuses it rather than trusting the extra field.
    recovery.record_decision(
        state, request["request_id"],
        {"decision": "escalate", "rationale": "a real reason for a human to take over"},
    )
    path = next((state / "recovery" / "job_a").glob("*.decision.json"))
    body = json.loads(path.read_text())
    body["decision_digest"] = recovery.decision_digest(body)
    path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(recovery.RecoveryError) as refused:
        recovery.read_decision(state, request)
    assert refused.value.code == "recovery_record_legacy"


def test_a_decision_cannot_be_replayed_onto_another_request(tmp_path):
    state, _authorization = new_state(tmp_path, attempts=("att_0",))
    first = build(state, evidence="tests_failed: named tests failed after the patch")
    recorded = recovery.record_decision(
        state, first["request_id"],
        {"decision": "escalate", "rationale": "a human owns this failure, not another attempt"},
    )

    # A different attempt gets its own request; the first decision does not bind it.
    job = jobs.load_job(state, "job_a", recover=False)
    task = job["tasks"][0]
    task["attempts"] = ["att_0", "att_1"]
    jobs.save_job(state, job)
    second = recovery.build_request(
        state, job, task, failure={"ok": False, "evidence": "tests_failed: still failing"},
        authorization=jobs.load_authorization(state, "job_a"),
    )
    assert second["request_id"] != first["request_id"]

    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.record_decision(state, second["request_id"], recorded)
    assert caught.value.code == "decision_mismatch"


# ---------------------------------------------------------------------------
# Archived pre-conformance records
# ---------------------------------------------------------------------------


def _write_captured(state: Path) -> tuple[Path, Path]:
    base = state / "recovery" / "job_m4_dogfood"
    base.mkdir(parents=True)
    request_path = base / "T2.1.request.json"
    decision_path = base / "T2.1.decision.json"
    request_path.write_text(CAPTURED_REQUEST, encoding="utf-8")
    decision_path.write_text(CAPTURED_DECISION, encoding="utf-8")
    return request_path, decision_path


def test_the_captured_pre_conformance_pair_is_labelled_and_never_rewritten(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    request_path, decision_path = _write_captured(state)
    request_bytes = request_path.read_bytes()
    decision_bytes = decision_path.read_bytes()

    request = json.loads(request_bytes)
    decision = json.loads(decision_bytes)
    assert recovery.is_legacy_request(request)
    assert recovery.is_legacy_decision(decision)
    reasons = " ".join(recovery.legacy_reasons(request, "request"))
    for marker in ("number", "attempts_used", "created_at", "boolean", "string"):
        assert marker in reasons, reasons

    # A reader labels the request and refuses it.
    with pytest.raises(recovery.RecoveryError) as caught:
        recovery.load_request(state, request["request_id"])
    assert caught.value.code == "recovery_record_legacy"

    # A decision reader labels and refuses the decision too.
    with pytest.raises(recovery.RecoveryError) as refused:
        recovery.read_decision(state, request)
    assert refused.value.code == "recovery_record_legacy"

    # Neither record was rewritten; each has a create-only legacy label.
    assert request_path.read_bytes() == request_bytes
    assert decision_path.read_bytes() == decision_bytes
    labels = recovery.legacy_records(state, "job_m4_dogfood")
    assert {label["record"] for label in labels} == {"T2.1.request.json", "T2.1.decision.json"}
    for label in labels:
        assert label["classification"] == "legacy"
        assert label["authoritative"] is False
        assert label["schema"] == recovery.LEGACY_SCHEMA
        assert label["reasons"]
        assert len(label["record_sha256"]) == 64

    # They are not part of the canonical sequence, so nothing can dispatch them.
    assert recovery.list_requests(state, "job_m4_dogfood") == []
    assert recovery.list_decisions(state, "job_m4_dogfood") == []

    job = {
        "job_id": "job_m4_dogfood",
        "tasks": [{"task_id": "T2", "state": "rejected", "attempts": ["att_m4_2"]}],
    }
    with pytest.raises(recovery.RecoveryError) as dispatch:
        recovery.apply_ready_decisions(state, job)
    assert dispatch.value.code == "recovery_record_legacy"
    with pytest.raises(recovery.RecoveryError) as pending:
        recovery.outstanding(state, job)
    assert pending.value.code == "recovery_record_legacy"
    # Nothing was applied to the job document by the refused call.
    assert job["tasks"][0]["state"] == "rejected"
    assert "recovery" not in job

    summary = recovery.summarize(state, "job_m4_dogfood")
    assert summary["requests"] == []
    assert len(summary["legacy"]) == 2


# ---------------------------------------------------------------------------
# Fail closed when the pinned protocol cannot be loaded
# ---------------------------------------------------------------------------


def test_a_missing_validator_fails_closed_and_writes_nothing(tmp_path, monkeypatch):
    state, _authorization = new_state(tmp_path)
    build_state = state

    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    shutil.rmtree(state / "recovery", ignore_errors=True) if (state / "recovery").exists() else None

    with pytest.raises(recovery.RecoveryError) as caught:
        build(build_state)
    assert caught.value.code == "recovery_protocol_unavailable"
    assert not (state / "recovery").exists(), "a request was written without its validator"

    # The same refusal at admission and at apply: nothing is recorded or moved.
    monkeypatch.setenv("WORKFLOW_SRC", WORKFLOW_SRC)
    request = build(build_state)
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    with pytest.raises(recovery.RecoveryError) as refused:
        recovery.record_decision(
            state, request["request_id"],
            {"decision": "escalate", "rationale": "a human must see this failure"},
        )
    assert refused.value.code == "recovery_protocol_unavailable"
    assert recovery.list_decisions(state, "job_a") == []
    job = jobs.load_job(state, "job_a", recover=False)
    with pytest.raises(recovery.RecoveryError) as applied:
        recovery.apply_ready_decisions(state, job)
    assert applied.value.code == "recovery_protocol_unavailable"
    assert job["tasks"][0]["state"] == "rejected"


def test_a_validator_that_is_not_the_recovery_protocol_fails_closed(tmp_path, monkeypatch):
    # A git checkout, but not a recovery protocol: nothing may be written.
    fake = tmp_path / "fake-workflow"
    (fake / "tools").mkdir(parents=True)
    (fake / "schemas").mkdir()
    (fake / "tools" / "recovery.py").write_text(
        "SUPPORTED = ('rizzstack.recovery/9',)\n", encoding="utf-8"
    )
    (fake / "schemas" / "recovery.schema.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(fake), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(fake), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "not a recovery protocol"],
        cwd=str(fake), check=True,
    )
    state, _authorization = new_state(tmp_path / "state")

    monkeypatch.setenv("WORKFLOW_SRC", str(fake))
    with pytest.raises(recovery.RecoveryError) as caught:
        build(state)
    assert caught.value.code == "recovery_protocol_unavailable"
    assert "validate_request" in caught.value.message
    assert recovery.list_requests(state, "job_a") == []


def test_a_protocol_directory_that_is_not_a_git_checkout_fails_closed(tmp_path, monkeypatch):
    # Shape-complete but unpinned: the validator exists, the commit cannot be proved.
    fake = tmp_path / "fake-workflow"
    (fake / "tools").mkdir(parents=True)
    (fake / "schemas").mkdir()
    shutil.copy2(Path(WORKFLOW_SRC) / "tools" / "recovery.py", fake / "tools" / "recovery.py")
    shutil.copy2(Path(WORKFLOW_SRC) / "schemas" / "recovery.schema.json",
                 fake / "schemas" / "recovery.schema.json")
    state, _authorization = new_state(tmp_path / "state")

    monkeypatch.setenv("WORKFLOW_SRC", str(fake))
    with pytest.raises(recovery.RecoveryError) as caught:
        build(state)
    assert caught.value.code == "recovery_protocol_unavailable"
    assert "git checkout" in caught.value.message
    assert recovery.list_requests(state, "job_a") == []
