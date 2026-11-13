"""Focused extra tests for REVIEW evidence and review-aware ``make_record``.

These complement the approved acceptance tests: ``review_outcome`` is confined
to the review phase, only accepts a real verdict, flips only review/manual
criteria, and a worker-produced review never turns the report into acceptance.
They reuse the temporary-repository helpers from the approved suites.
"""

from __future__ import annotations

import json

import pytest

from bundlebees_plugin import contract as acdd
from bundlebees_plugin import leases, tdd
from tests.test_cli_acdd import _frozen, _records
from tests.test_cli_acdd_review import REVIEW_AC, TEST_AC, _report, _verified

BASE = "a" * 40
SHA = "c" * 64
IMPL = "b" * 40
FP = {"tests/test_value.py": "d" * 64}


def _review_record(contract, **overrides):
    values = {
        "phase": "review",
        "revision": IMPL,
        "test_fingerprints": dict(FP),
        "command": ["review"],
        "result": None,
        "produced_by": {"role": "reviewer", "name": "kyle"},
        "test_first": True,
        "notes": "reviewed the diff",
        "review_outcome": "passed",
    }
    values.update(overrides)
    return tdd.make_record(contract, SHA, **values)


def test_review_outcome_is_refused_on_other_phases():
    contract = _frozen(BASE)
    with pytest.raises(tdd.TddError) as exc:
        _review_record(contract, phase="green")
    assert exc.value.code == "evidence_invalid"
    assert exc.value.fix


@pytest.mark.parametrize("outcome", ["maybe", "", "PASSED"])
def test_review_outcome_must_be_passed_or_failed(outcome):
    contract = _frozen(BASE)
    with pytest.raises(tdd.TddError) as exc:
        _review_record(contract, review_outcome=outcome)
    assert exc.value.code == "evidence_invalid"


def test_review_outcome_flips_only_review_and_manual_criteria():
    criteria = [
        TEST_AC,
        {"id": "AC-003", "statement": "combined suite", "verification": "integration"},
        REVIEW_AC,
        {"id": "AC-004", "statement": "manual spot check", "verification": "manual"},
    ]
    contract = _frozen(BASE, acceptance_criteria=criteria)
    approved = _review_record(contract, review_outcome="passed")
    assert approved["classification"] == "passed"
    assert approved["result"]["exit_code"] is None
    assert approved["result"]["tests"] == 0
    assert approved["criteria"] == {
        "AC-001": "not_run",
        "AC-002": "passed",
        "AC-003": "not_run",
        "AC-004": "passed",
    }
    rejected = _review_record(contract, review_outcome="failed")
    assert rejected["classification"] == "failed"
    assert rejected["criteria"]["AC-002"] == "failed"
    assert rejected["criteria"]["AC-004"] == "failed"
    assert rejected["criteria"]["AC-001"] == "not_run"
    assert rejected["criteria"]["AC-003"] == "not_run"


def test_worker_produced_review_is_never_accepted(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"])
    frozen = json.loads(path.read_text(encoding="utf-8"))
    sha = acdd.check_frozen(frozen)
    green = _records(state)[-1]
    claim = tdd.make_record(
        frozen, sha, phase="review", revision=green["revision"],
        test_fingerprints=green["test_fingerprints"], command=["review"],
        result=None, produced_by={"role": "worker", "name": "bee"},
        test_first=green["test_first"], notes="worker claims this is fine",
        review_outcome="passed",
    )
    tdd.write_record(state, claim, leases.evidence_key(state))
    report = _report(state, path, capsys)
    assert report["phases"]["review"] == "review_failed"
    assert report["accepted"] is False


def test_a_review_record_needs_a_reviewer_name_and_a_stored_note(tmp_path):
    contract = _frozen(BASE)
    values = dict(
        phase="review", revision=IMPL, test_fingerprints=dict(FP), command=["review"],
        result=None, produced_by={"role": "reviewer", "name": "kyle"},
        test_first=True, review_outcome="passed",
    )
    with pytest.raises(tdd.TddError):
        tdd.make_record(contract, SHA, notes="reviewed", **dict(values, produced_by={"role": "reviewer"}))
    note_less = tdd.make_record(contract, SHA, **values)
    with pytest.raises(tdd.TddError):
        tdd.write_record(tmp_path, note_less, leases.evidence_key(tmp_path))
