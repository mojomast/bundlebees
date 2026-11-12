"""Approved acceptance tests for REVIEW evidence and REFACTOR-aware ``integrate`` (written first, RED).

``hermes bundlebees tdd review`` records a signed ``review`` record made by a
named reviewer or human (never a worker). It requires a valid GREEN, and a
valid REFACTOR when the contract requires one; it reviews the latest of those
revisions. ``--approve`` records ``passed``; ``--reject`` records ``failed``
and exits non-zero with ``review_failed``. A note is required.

``integrate`` keeps requiring GREEN, and when the contract lists ``refactor``
in ``evidence_required`` it also requires a valid REFACTOR record (a real one
or an explicit ``--none`` skip) before it runs the wave.
"""

from __future__ import annotations

import json
import pytest

from tests.test_cli_acdd import (
    APPROVED_TEST, ATTEMPT_ID, BRANCH, CID, GOOD_PATCH, FakeClient, TEST_ID, _ctx, _frozen, _git,
    _invoke, _lease, _records, _repo, _result, _write,
)

REVIEW_AC = {"id": "AC-002", "statement": "the change is readable and keeps the module tiny",
             "verification": "review"}
TEST_AC = {"id": "AC-001", "given": "the module", "when": "value is read", "then": "it is 2",
           "verification": "test", "tests": [TEST_ID]}


def _verified(tmp_path, capsys, evidence_required, **overrides):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base, evidence_required=evidence_required, **overrides)
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    _git(repo, "checkout", BRANCH)
    rc = _invoke(_ctx(FakeClient(_result(GOOD_PATCH, base))),
                 ["verify", ATTEMPT_ID, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])
    assert rc == 0, capsys.readouterr().out
    capsys.readouterr()
    return repo, state, path


def _review(state, path, *extra):
    return _invoke(_ctx(), ["tdd", "review", str(path), "--state-dir", str(state), *extra])


def _report(state, path, capsys):
    _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    return json.loads(capsys.readouterr().out)


def _integrate(repo, state):
    return _invoke(_ctx(), ["integrate", "--wave", "w1", "--attempts", ATTEMPT_ID,
                            "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])


# ------------------------------------------------------------------ tdd review


def test_approved_review_is_recorded_and_accepts_review_criteria(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"],
                                  acceptance_criteria=[TEST_AC, REVIEW_AC])
    green = _records(state)[-1]
    rc = _review(state, path, "--reviewer", "kyle", "--approve", "--note", "read the diff; fine")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "review: valid" in out
    review = _records(state)[-1]
    assert review["phase"] == "review"
    assert review["classification"] == "passed"
    assert review["produced_by"] == {"role": "reviewer", "name": "bundlebees tdd review (coordinator-recorded)"}
    assert review["attestation"]["reviewer_label"] == "kyle"
    assert review["attestation"]["identity_verified"] is False
    assert review["revision"] == green["revision"]
    assert review["test_fingerprints"] == green["test_fingerprints"]
    assert "read the diff; fine" in review.get("notes", "")
    assert review["criteria"]["AC-002"] == "passed"
    report = _report(state, path, capsys)
    assert report["phases"] == {"red": "valid", "green": "valid", "review": "valid"}
    assert report["accepted"] is True


def test_review_criteria_are_not_accepted_without_a_review(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"],
                                  acceptance_criteria=[TEST_AC, REVIEW_AC])
    report = _report(state, path, capsys)
    assert report["phases"]["review"] == "unrun"
    assert report["accepted"] is False


def test_rejected_review_is_recorded_and_fails(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"],
                                  acceptance_criteria=[TEST_AC, REVIEW_AC])
    rc = _review(state, path, "--reviewer", "kyle", "--reject", "--note", "too clever")
    out = capsys.readouterr().out
    assert rc != 0
    assert "review_failed" in out
    review = _records(state)[-1]
    assert review["phase"] == "review"
    assert review["classification"] == "failed"
    assert review["criteria"]["AC-002"] == "failed"
    report = _report(state, path, capsys)
    assert report["phases"]["review"] == "review_failed"
    assert report["accepted"] is False


def test_human_role_is_only_an_unverified_label(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"])
    rc = _review(state, path, "--reviewer", "kyle", "--role", "human", "--approve", "--note", "ok")
    assert rc == 0, capsys.readouterr().out
    # Review C6: a coordinator-recorded review never claims authenticated human identity.
    last = _records(state)[-1]
    assert last["produced_by"]["role"] == "reviewer"
    assert last["attestation"] == {
        "kind": "coordinator_recorded_review", "recorded_by": "coordinator", "reviewer_label": "kyle",
        "claimed_reviewer_kind": "human", "identity_verified": False,
    }


def test_review_requires_a_note_and_a_reviewer_name(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"])
    assert _review(state, path, "--reviewer", "kyle", "--approve", "--note", "   ") != 0
    assert "invalid_argument" in capsys.readouterr().out
    assert _review(state, path, "--reviewer", " ", "--approve", "--note", "ok") != 0
    assert "invalid_argument" in capsys.readouterr().out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_review_requires_green(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    path = _write(tmp_path / "frozen.json", _frozen(base, evidence_required=["red", "green", "review"]))
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    rc = _review(state, path, "--reviewer", "kyle", "--approve", "--note", "ok")
    out = capsys.readouterr().out
    assert rc != 0
    assert "green_failed" in out
    assert [r["phase"] for r in _records(state)] == ["red"]


def test_review_requires_refactor_when_the_contract_does(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "refactor", "review"])
    rc = _review(state, path, "--reviewer", "kyle", "--approve", "--note", "ok")
    out = capsys.readouterr().out
    assert rc != 0
    assert "refactor_failed" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_review_targets_the_refactor_revision(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "refactor", "review"])
    (repo / "src" / "a.py").write_text("value = 1 + 1\n", encoding="utf-8")
    _git(repo, "add", "src/a.py")
    _git(repo, "commit", "-m", "refactor")
    rev = _git(repo, "rev-parse", "HEAD")
    assert _invoke(_ctx(), ["tdd", "refactor", str(path), "--workdir", str(repo), "--state-dir", str(state),
                            "--rev", rev]) == 0, capsys.readouterr().out
    capsys.readouterr()
    assert _review(state, path, "--reviewer", "kyle", "--approve", "--note", "ok") == 0, capsys.readouterr().out
    capsys.readouterr()
    assert _records(state)[-1]["revision"] == rev
    report = _report(state, path, capsys)
    assert report["phases"] == {"red": "valid", "green": "valid", "refactor": "valid", "review": "valid"}
    assert report["accepted"] is True


# --------------------------------------------------- integrate needs REFACTOR


def test_integrate_refuses_without_required_refactor(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "refactor", "integration"],
                                  verification={"independent": True, "integration": True})
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    assert "refactor_failed" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_integrate_accepts_an_explicit_refactor_skip(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "refactor", "integration"],
                                  verification={"independent": True, "integration": True})
    assert _invoke(_ctx(), ["tdd", "refactor", str(path), "--workdir", str(repo), "--state-dir", str(state),
                            "--none", "--note", "already minimal"]) == 0, capsys.readouterr().out
    capsys.readouterr()
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "accepted: yes" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green", "refactor", "integration"]
    report = _report(state, path, capsys)
    assert report["accepted"] is True


# Review C6: the report separates a coordinator-recorded review from human authorization.
def test_report_says_review_identity_is_not_authenticated(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"])
    assert _review(state, path, "--reviewer", "kyle", "--role", "human", "--approve", "--note", "ok") == 0
    capsys.readouterr()
    report = _report(state, path, capsys)
    assert report["review_identity"] == "coordinator_recorded"
    assert report["human_authorization"]["status"] == "not_recorded"


def test_a_review_record_cannot_claim_role_human_without_a_receipt():
    from bundlebees_plugin import tdd

    contract = {"id": "C-1", "source_rev": "a" * 40, "acceptance_criteria": []}
    with pytest.raises(tdd.TddError):
        tdd.make_record(contract, "b" * 64, phase="review", revision="a" * 40, test_fingerprints={},
                        command=["review"], result=None, produced_by={"role": "human", "name": "kyle"},
                        test_first=True, review_outcome="passed")
