"""One-time owner approval: a signed receipt, not a terminal substitute.

The tests exercise both halves of the boundary:

* the owner decision is refused without the per-request capability, even when
  the approval secret is configured, and a tampered, expired, cross-job or
  replayed receipt never becomes authority;
* the sealed gate records are verified rather than merely present, so a
  forged ``{}`` or a ``refused`` verdict cannot clear a human gate in a
  RETURN packet.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bundlebees_plugin import approval, job as jobs, job_cli, return_report
from bundlebees_plugin.approval import ApprovalError
from tests.fakes import secret_scope

REPO = "mojomast/workflow"
SHA = "817eac479f441396562962e880c1af596638f399"
SCOPE = "tools/acdd-lint/**"
CODE_REV = approval.code_revision() or "a" * 40
SECRET = "owner-approval-test-secret-0123456789abcdef"  # gitleaks:allow (fake value for the test seam)
KEY = SECRET.encode("utf-8")

NOW = "2026-11-15T12:00:00Z"      # request issued; expires at 12:15
LATER = "2026-11-15T12:10:00Z"    # still valid
TOO_LATE = "2026-11-15T12:20:00Z"  # past the 900 s TTL

STATEMENT = "Run the approved acdd-lint trial once, nothing else."


def task(**overrides) -> dict:
    body = {
        "task_id": "T1",
        "objective": "reject malformed source_rev",
        "lease": ["tools/acdd-lint/acdd_lint/checks_source_rev.py"],
        "acceptance": {"criteria": ["reject malformed"], "tests": ["tests/test_source_rev.py"]},
    }
    body.update(overrides)
    return body


def make_request(state: Path, **overrides) -> dict:
    kwargs = dict(
        job_id="job_a",
        statement=STATEMENT,
        repo=REPO,
        base_rev=SHA,
        scope=[SCOPE],
        tasks=[task()],
        bees=2,
        wall_clock_s=3600,
        worker_project="bundlebees",
        worker_profile="default",
        code_rev=CODE_REV,
        coordinator_stop=True,
        restart=True,
        budgets={"attempts": 3},
        ttl_s=900,
        now=NOW,
    )
    kwargs.update(overrides)
    return approval.create_request(state, **kwargs)


def decide(state: Path, request: dict, **overrides) -> dict:
    kwargs = dict(
        decision="approved",
        expected_digest=request["request_digest"],
        owner="owner@example",
        auth_method="dashboard-click",
        owner_capability=approval.owner_capability(request["request_id"], signing_key=KEY),
        signing_key=KEY,
        now=LATER,
    )
    kwargs.update(overrides)
    return approval.record_decision(state, request["request_id"], **kwargs)


def authorize(state: Path, receipt: dict, **overrides) -> dict:
    kwargs = dict(
        job_id="job_a",
        statement=STATEMENT,
        repo=REPO,
        base_rev=SHA,
        scope=[SCOPE],
        bees=2,
        wall_clock_s=3600,
        require_tty=False,
        interactive=False,
        approval_receipt=receipt,
        approval_signing_key=KEY,
        code_rev=CODE_REV,
        tasks=[task()],
        now=LATER,
    )
    kwargs.update(overrides)
    return jobs.record_authorization(state, **kwargs)


def real_now() -> str:
    """A current UTC timestamp in the module's format, for tests that use the real clock."""

    return (
        (datetime.now(timezone.utc) + timedelta(minutes=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_job(state: Path, job_id: str = "job_a") -> dict:
    jobs.record_authorization(
        state,
        job_id=job_id,
        statement=STATEMENT,
        repo=REPO,
        base_rev=SHA,
        scope=[SCOPE],
        bees=2,
        wall_clock_s=3600,
        interactive=True,
        by="reviewer",
    )
    return jobs.create_job(state, job_id=job_id, tasks=[task()])


# ---------------------------------------------------------------------------
# The request is an inert, exact proposal
# ---------------------------------------------------------------------------


def test_a_request_binds_every_approved_fact_and_digests_the_binding(tmp_path):
    request = make_request(tmp_path)

    binding = request["binding"]
    assert binding["job_id"] == "job_a"
    assert binding["repo"] == REPO
    assert binding["base_rev"] == SHA
    assert binding["scope"] == [SCOPE]
    assert binding["tasks_digest"] == approval.tasks_digest([task()])
    assert binding["budgets"] == {"bees": 2, "wall_clock_s": 3600, "attempts": 3}
    assert binding["worker"] == {"project": "bundlebees", "profile": "default"}
    assert binding["code_rev"] == CODE_REV
    assert binding["coordinator"] == {"stop": True, "restart": True}
    assert binding["gates"] == ["merge"]
    assert request["request_digest"] == approval.binding_digest(binding)
    assert request["expires_at"] == "2026-11-15T12:15:00Z"

    path = approval.request_path(tmp_path, request["request_id"])
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert "secret" not in path.read_text().lower()

    shown = approval.preview(request)
    assert shown["request_digest"] == request["request_digest"]
    assert any("job job_a" in line for line in shown["lines"])
    assert any("T1" in line for line in shown["lines"])
    assert any("no merge, release, deploy or acceptance" in line for line in shown["lines"])


def test_one_pending_request_per_job_and_an_identical_retry_is_idempotent(tmp_path):
    first = make_request(tmp_path)
    again = make_request(tmp_path)
    assert again["request_id"] == first["request_id"]

    with pytest.raises(ApprovalError) as caught:
        make_request(tmp_path, statement=STATEMENT + " changed")
    assert caught.value.code == "approval_request_pending"

    pending = approval.list_pending(tmp_path, job_id="job_a", now=LATER)
    assert [item["request_id"] for item in pending] == [first["request_id"]]
    assert approval.pending_for_job(tmp_path, "job_a", now=LATER)["request_id"] == first["request_id"]


def test_a_tampered_request_is_refused_rather_than_read_as_a_preview(tmp_path):
    request = make_request(tmp_path)
    path = approval.request_path(tmp_path, request["request_id"])
    path.chmod(0o600)
    document = json.loads(path.read_text())
    document["binding"]["scope"] = ["tools/**"]
    path.write_text(json.dumps(document))

    with pytest.raises(ApprovalError) as caught:
        approval.load_request(tmp_path, request["request_id"])
    assert caught.value.code == "approval_request_tampered"


# ---------------------------------------------------------------------------
# Only the trusted route can record a decision
# ---------------------------------------------------------------------------


def test_an_unprefixed_agent_call_is_refused_and_cannot_mint_a_receipt(tmp_path):
    request = make_request(tmp_path)

    # The secret is configured, and the call still fails: holding the state
    # directory (or a shell in the same process) is not owner consent.
    with secret_scope({approval.APPROVAL_SECRET_NAME: SECRET}):
        with pytest.raises(ApprovalError) as caught:
            approval.record_decision(
                tmp_path,
                request["request_id"],
                decision="approved",
                expected_digest=request["request_digest"],
                owner="owner@example",
                auth_method="dashboard-click",
            )
    assert caught.value.code == "approval_owner_required"
    assert not approval.receipt_path(tmp_path, request["request_id"]).exists()
    for path in (tmp_path / "job-approvals").rglob("*.json"):
        assert SECRET not in path.read_text()


def test_a_capability_for_another_request_is_refused(tmp_path):
    request = make_request(tmp_path)
    other = make_request(tmp_path, job_id="job_other")
    capability = approval.owner_capability(other["request_id"], signing_key=KEY)

    with pytest.raises(ApprovalError) as caught:
        approval.record_decision(
            tmp_path,
            request["request_id"],
            decision="approved",
            expected_digest=request["request_digest"],
            owner="owner@example",
            auth_method="dashboard-click",
            owner_capability=capability,
            signing_key=KEY,
        )
    assert caught.value.code == "approval_owner_capability_invalid"
    assert not approval.receipt_path(tmp_path, request["request_id"]).exists()


def test_the_trusted_route_uses_the_secret_scope_end_to_end(tmp_path):
    request = make_request(tmp_path)
    with secret_scope({approval.APPROVAL_SECRET_NAME: SECRET}):
        capability = approval.owner_capability(request["request_id"])
        receipt = approval.record_decision(
            tmp_path,
            request["request_id"],
            decision="approved",
            expected_digest=request["request_digest"],
            owner="owner@example",
            auth_method="dashboard-click",
            owner_capability=capability,
            now=LATER,
        )
        verified = approval.verify_receipt(receipt, now=LATER)
    assert verified["decision"] == "approved"
    assert approval.receipt_path(tmp_path, request["request_id"]).exists()
    assert SECRET not in approval.receipt_path(tmp_path, request["request_id"]).read_text()


def test_no_secret_scope_fails_closed_with_a_named_installation_requirement(tmp_path):
    request = make_request(tmp_path)
    capability = approval.owner_capability(request["request_id"], signing_key=KEY)
    with secret_scope({}, raises=True):
        with pytest.raises(ApprovalError) as caught:
            approval.record_decision(
                tmp_path,
                request["request_id"],
                decision="approved",
                expected_digest=request["request_digest"],
                owner="owner@example",
                auth_method="dashboard-click",
                owner_capability=capability,
            )
    assert caught.value.code == "approval_key_unavailable"
    assert approval.APPROVAL_SECRET_NAME in caught.value.fix


def test_a_short_key_is_refused(tmp_path):
    with pytest.raises(ApprovalError) as caught:
        approval.approval_key("short")
    assert caught.value.code == "approval_key_weak"


# ---------------------------------------------------------------------------
# The receipt: signature, expiry, digest, single decision
# ---------------------------------------------------------------------------


def test_a_bad_signature_is_refused(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    tampered = json.loads(json.dumps(receipt))
    tampered["binding"]["scope"] = ["tools/**"]

    with pytest.raises(ApprovalError) as caught:
        approval.verify_receipt(tampered, signing_key=KEY, now=LATER)
    assert caught.value.code == "approval_signature_invalid"


def test_expired_receipts_are_refused_at_decision_and_authorization(tmp_path):
    request = make_request(tmp_path)

    with pytest.raises(ApprovalError) as caught:
        decide(tmp_path, request, now=TOO_LATE)
    assert caught.value.code == "approval_expired"
    assert not approval.receipt_path(tmp_path, request["request_id"]).exists()

    receipt = decide(tmp_path, request)
    with pytest.raises(ApprovalError) as caught:
        approval.verify_receipt(receipt, signing_key=KEY, now=TOO_LATE)
    assert caught.value.code == "approval_expired"

    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt, now=TOO_LATE)
    assert caught.value.code == "approval_expired"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_a_conflicting_decision_after_settlement_is_refused(tmp_path):
    request = make_request(tmp_path)
    decide(tmp_path, request)

    with pytest.raises(ApprovalError) as caught:
        decide(tmp_path, request, decision="declined")
    assert caught.value.code == "approval_conflict"
    assert approval.load_receipt(tmp_path, request["request_id"], signing_key=KEY, now=LATER)["decision"] == "approved"


def test_a_declined_receipt_cannot_authorize(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request, decision="declined")
    assert receipt["decision"] == "declined"

    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt)
    assert caught.value.code == "approval_declined"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_receipt_consumption_is_idempotent_for_one_consumer_and_refuses_another(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    first = approval.consume_receipt(
        tmp_path, receipt, signing_key=KEY, now=LATER, consumer="job-authorization:one"
    )
    assert first["consumed"] is True and first["idempotent"] is False

    again = approval.consume_receipt(
        tmp_path, receipt, signing_key=KEY, now=LATER, consumer="job-authorization:one"
    )
    assert again["idempotent"] is True

    with pytest.raises(ApprovalError) as caught:
        approval.consume_receipt(
            tmp_path, receipt, signing_key=KEY, now=LATER, consumer="job-authorization:two"
        )
    assert caught.value.code == "approval_already_consumed"


# ---------------------------------------------------------------------------
# The authorization path: exact binding, exact tasks, exact revision
# ---------------------------------------------------------------------------


def test_an_owner_receipt_authorizes_without_a_tty_and_is_audited(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    body = authorize(tmp_path, receipt)

    assert body["authority"] == "owner-approval"
    assert body["by"] == "owner:owner@example"
    assert body["statement"] == STATEMENT
    assert body["repo"] == REPO
    assert body["base_rev"] == SHA
    assert body["scope"] == [SCOPE]
    assert body["budget"] == {"bees": 2, "wall_clock_s": 3600}
    assert body["gates"] == ["merge"]
    assert body["recovery_digest"] == jobs.recovery_digest({})

    block = body["approval"]
    assert block["request_id"] == request["request_id"]
    assert block["receipt_id"] == receipt["receipt_id"]
    assert block["receipt_digest"] == approval.receipt_digest(receipt)
    assert block["request_digest"] == request["request_digest"]
    assert block["owner"] == "owner@example"
    assert block["auth_method"] == "dashboard-click"
    assert block["code_rev"] == CODE_REV
    assert block["tasks_digest"] == approval.tasks_digest([task()])
    assert block["worker"] == {"project": "bundlebees", "profile": "default"}
    assert block["coordinator"] == {"stop": True, "restart": True}
    assert block["budgets"] == {"bees": 2, "wall_clock_s": 3600, "attempts": 3}

    sealed = jobs.load_authorization(tmp_path, "job_a")
    assert sealed["authority"] == "owner-approval"
    assert sealed["approval"]["receipt_digest"] == approval.receipt_digest(receipt)
    # The bridge authorizes the trial only: the merge gate is untouched.
    assert jobs.gate_decision(tmp_path, "job_a", "merge") is None


def test_a_non_tty_authorization_without_a_receipt_is_still_refused(tmp_path):
    with pytest.raises(jobs.JobError) as caught:
        jobs.record_authorization(
            tmp_path,
            job_id="job_a",
            statement=STATEMENT,
            repo=REPO,
            base_rev=SHA,
            scope=[SCOPE],
            bees=2,
            wall_clock_s=3600,
            require_tty=True,
            interactive=False,
        )
    assert caught.value.code == "authorization_not_interactive"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_a_receipt_cannot_bypass_require_tty(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt, require_tty=True, interactive=True)
    assert caught.value.code == "authorization_receipt_requires_nontty"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


@pytest.mark.parametrize(
    "changed",
    [
        {"statement": STATEMENT + " (widened)"},
        {"repo": "mojomast/other"},
        {"base_rev": "28e7a4863bc0da34af16062e36a8b55b2f4c6721"},
        {"scope": ["tools/**"]},
        {"bees": 3},
        {"wall_clock_s": 7200},
        {"gates": ["merge", "release"]},
        {"recovery": {"max_attempts_per_task": 2}},
    ],
)
def test_any_changed_authorization_field_is_refused(tmp_path, changed):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt, **changed)
    assert caught.value.code == "approval_scope_changed"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_a_receipt_for_another_job_is_refused(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt, job_id="job_b")
    assert caught.value.code == "approval_scope_changed"
    assert not jobs.authorization_path(tmp_path, "job_b").exists()


def test_the_running_code_revision_must_match_the_approved_one(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt, code_rev="b" * 40)
    assert caught.value.code == "approval_code_rev_mismatch"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_a_task_list_that_differs_from_the_approved_set_is_refused(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    with pytest.raises(jobs.JobError) as caught:
        authorize(tmp_path, receipt, tasks=[task(objective="a different objective")])
    assert caught.value.code == "approval_tasks_mismatch"
    assert not jobs.authorization_path(tmp_path, "job_a").exists()


def test_create_job_refuses_tasks_the_owner_did_not_approve(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    authorize(tmp_path, receipt)

    with pytest.raises(jobs.JobError) as caught:
        jobs.create_job(
            tmp_path,
            job_id="job_a",
            tasks=[task(task_id="T1", objective="a different objective")],
        )
    assert caught.value.code == "approval_scope_changed"

    # Adding a second in-scope task is still a different trial run.
    with pytest.raises(jobs.JobError) as caught:
        jobs.create_job(
            tmp_path,
            job_id="job_a",
            tasks=[task(), task(task_id="T2", lease=["tools/acdd-lint/other.py"])],
        )
    assert caught.value.code == "approval_scope_changed"
    assert not jobs.job_path(tmp_path, "job_a").exists()

    created = jobs.create_job(tmp_path, job_id="job_a", tasks=[task()])
    assert created["authorization"]["authority"] == "owner-approval"
    assert created["authorization"]["approval"]["receipt_id"] == receipt["receipt_id"]


def test_resending_the_same_authorization_is_idempotent(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    first = authorize(tmp_path, receipt)
    again = authorize(tmp_path, receipt)

    assert again["auth_id"] == first["auth_id"]
    written = json.loads(jobs.authorization_path(tmp_path, "job_a").read_text())
    assert written["body"]["auth_id"] == first["auth_id"]
    assert jobs.gate_decision(tmp_path, "job_a", "merge") is None


# ---------------------------------------------------------------------------
# CLI: the receipt path fits the existing parser
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    job_cli.add_parsers(subparsers)
    return parser


def test_the_authorize_parser_accepts_a_receipt_without_the_typed_fields():
    args = _parser().parse_args(["job", "authorize", "--job-id", "job_a", "--approval-receipt", "r.json"])
    assert args.job_handler == "authorize"
    assert args.repo is None and args.statement is None and args.scope is None
    assert args.approval_receipt == "r.json"

    typed = _parser().parse_args([
        "job", "authorize", "--job-id", "job_a", "--repo", REPO, "--base-rev", SHA,
        "--scope", SCOPE, "--statement", STATEMENT,
    ])
    assert typed.bees is None and typed.wall_clock is None and typed.approval_receipt is None


def test_the_cli_turns_a_receipt_into_an_authorization_after_the_owner_clicked(tmp_path):
    # The CLI verifies against the real clock, so this one test uses real time.
    moment = real_now()
    request = make_request(tmp_path, now=moment)
    receipt = decide(tmp_path, request, now=moment)
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(receipt))
    args = _parser().parse_args([
        "job", "authorize", "--job-id", "job_a", "--approval-receipt", str(receipt_file),
        "--state-dir", str(tmp_path),
    ])

    with secret_scope({approval.APPROVAL_SECRET_NAME: SECRET}):
        code = job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=lambda: None)
    assert code == 0
    sealed = jobs.load_authorization(tmp_path, "job_a")
    assert sealed["authority"] == "owner-approval"
    assert sealed["approval"]["receipt_id"] == receipt["receipt_id"]


def test_the_cli_refuses_a_receipt_for_a_different_job(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(receipt))
    args = _parser().parse_args([
        "job", "authorize", "--job-id", "job_b", "--approval-receipt", str(receipt_file),
    ])

    with secret_scope({approval.APPROVAL_SECRET_NAME: SECRET}):
        code = job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=lambda: None)
    assert code == 1
    assert not jobs.authorization_path(tmp_path, "job_b").exists()


# ---------------------------------------------------------------------------
# Gate decisions are verified, not merely present
# ---------------------------------------------------------------------------


def test_a_missing_gate_decision_is_pending(tmp_path):
    make_job(tmp_path)
    assert jobs.gate_decision(tmp_path, "job_a", "merge") is None
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"]) == ["merge"]


def test_a_forged_empty_approval_file_does_not_clear_the_gate(tmp_path):
    make_job(tmp_path)
    path = jobs.approval_path(tmp_path, "job_a", "merge")
    path.write_text("{}")

    with pytest.raises(jobs.JobError) as caught:
        jobs.load_approval(tmp_path, "job_a", "merge")
    assert caught.value.code == "approval_invalid"

    limitations: list[str] = []
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"], limitations) == ["merge"]
    assert any("approval_invalid" in line for line in limitations)


def test_a_refused_decision_does_not_clear_the_gate(tmp_path):
    make_job(tmp_path)
    jobs.record_approval(
        tmp_path,
        job_id="job_a",
        gate="merge",
        decision="refused",
        statement="not merging this",
        interactive=True,
    )
    assert jobs.gate_decision(tmp_path, "job_a", "merge") == "refused"
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"]) == ["merge"]


def test_a_granted_decision_for_another_gate_or_job_does_not_clear_the_gate(tmp_path):
    make_job(tmp_path)
    jobs.record_approval(
        tmp_path, job_id="job_a", gate="release", decision="granted",
        statement="release is not merge", interactive=True,
    )
    other = jobs.approval_path(tmp_path, "job_a", "release").read_bytes()
    jobs.approval_path(tmp_path, "job_a", "merge").write_bytes(other)

    with pytest.raises(jobs.JobError) as caught:
        jobs.gate_decision(tmp_path, "job_a", "merge")
    assert caught.value.code == "approval_mismatch"
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"]) == ["merge"]

    (tmp_path / "approvals" / "job_a.merge.json").unlink()
    jobs.record_approval(
        tmp_path, job_id="job_b", gate="merge", decision="granted",
        statement="merge for another job", interactive=True,
    )
    foreign = jobs.approval_path(tmp_path, "job_b", "merge").read_bytes()
    jobs.approval_path(tmp_path, "job_a", "merge").write_bytes(foreign)

    with pytest.raises(jobs.JobError) as caught:
        jobs.gate_decision(tmp_path, "job_a", "merge")
    assert caught.value.code == "approval_mismatch"
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"]) == ["merge"]


def test_a_valid_granted_decision_clears_the_gate(tmp_path):
    make_job(tmp_path)
    approval_record = jobs.record_approval(
        tmp_path,
        job_id="job_a",
        gate="merge",
        decision="granted",
        statement="Approved: merge PR #31.",
        interactive=True,
    )
    assert approval_record["decision"] == "granted"
    assert jobs.gate_decision(tmp_path, "job_a", "merge") == "granted"
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"]) == []


def test_the_owner_bridge_does_not_clear_the_merge_gate(tmp_path):
    request = make_request(tmp_path)
    receipt = decide(tmp_path, request)
    authorize(tmp_path, receipt)
    jobs.create_job(tmp_path, job_id="job_a", tasks=[task()])

    assert jobs.gate_decision(tmp_path, "job_a", "merge") is None
    assert return_report.pending_gate_approvals(tmp_path, "job_a", ["merge"]) == ["merge"]
