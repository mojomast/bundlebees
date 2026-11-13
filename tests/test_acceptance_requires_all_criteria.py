"""Approved acceptance tests for X2 and report paths (contracts X2-acceptance, RP1-report-paths; RED first).

X2: ``acceptance_report`` signed ``accepted: true`` while a criterion was still
``review_required``. Orbit's ACDD verifier (fail-closed) shows the same report
as incomplete. Acceptance now requires every criterion to be ``passed``. A
``verification: review`` criterion becomes ``passed`` only through a valid
review record from a human (``tdd review --role human``, Kyle's chat
approval); its evidence is that review record. A ``reviewer``-role record can
still make the review *phase* valid, but it does not satisfy a review
criterion.

RP1: criteria evidence in signed reports named absolute scratch paths
(``/.../evidence/<id>/0004-integration.json``). Evidence paths are now relative
to the state directory: ``evidence/<contract_id>/NNNN-<phase>.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_tdd import CSHA, GREEN_CASES, KEY, RED_CASES, _contract, _record

PHASES_WITH_REVIEW = ["red", "green", "refactor", "integration", "review"]


@pytest.fixture()
def tdd():
    from bundlebees_plugin import tdd

    return tdd


def _base(tdd):
    return [
        _record(tdd, "red", RED_CASES, revision="e" * 40),
        _record(tdd, "green", GREEN_CASES),
        _record(tdd, "refactor", GREEN_CASES, revision="1" * 40),
        _record(tdd, "integration", GREEN_CASES, revision="2" * 40),
    ]


def _review(tdd, role, outcome="passed"):
    rec = tdd.make_record(
        _contract(evidence_required=PHASES_WITH_REVIEW), CSHA, phase="review", revision="2" * 40,
        test_fingerprints={"tests/test_pricing.py": "d" * 64}, command=["review"], result=None,
        produced_by={"role": role, "name": "Kyle" if role == "human" else "reviewer-bee"},
        test_first=True, now="2026-11-12T18:00:00Z", review_outcome=outcome,
    )
    return rec


def _invariant(rep):
    if rep["accepted"]:
        assert all(item["status"] == "passed" for item in rep["criteria"].values()), rep["criteria"]


# X2 AC-001
def test_reviewer_role_does_not_satisfy_a_review_criterion(tdd):
    c = _contract(evidence_required=PHASES_WITH_REVIEW)
    rep = tdd.acceptance_report(c, CSHA, _base(tdd) + [_review(tdd, "reviewer")])
    assert rep["phases"]["review"] == "valid"
    assert rep["criteria"]["AC-004"]["status"] == "review_required"
    assert rep["accepted"] is False
    _invariant(rep)


# X2 AC-002
def test_human_review_satisfies_the_review_criterion_with_its_record_as_evidence(tdd):
    c = _contract(evidence_required=PHASES_WITH_REVIEW)
    records = _base(tdd) + [_review(tdd, "human")]
    for index, r in enumerate(records, 1):
        r["_path"] = f"evidence/W1-pricing/{index:04d}-{r['phase']}.json"
    rep = tdd.acceptance_report(c, CSHA, records)
    assert rep["criteria"]["AC-004"] == {"status": "passed", "evidence": "evidence/W1-pricing/0005-review.json"}
    assert rep["accepted"] is True
    _invariant(rep)


# X2 AC-003
def test_rejected_or_missing_human_review_never_accepts(tdd):
    c = _contract(evidence_required=PHASES_WITH_REVIEW)
    rejected = tdd.acceptance_report(c, CSHA, _base(tdd) + [_review(tdd, "human", "failed")])
    assert rejected["accepted"] is False and rejected["criteria"]["AC-004"]["status"] == "review_required"
    # review phase not required by the contract: a review criterion still blocks acceptance
    no_phase = tdd.acceptance_report(_contract(), CSHA, _base(tdd))
    assert no_phase["accepted"] is False and no_phase["criteria"]["AC-004"]["status"] == "review_required"
    for rep in (rejected, no_phase):
        _invariant(rep)


# X2 AC-004 (regression): contracts with only test/integration criteria accept as before.
def test_contract_without_review_criteria_accepts_as_before(tdd):
    crit = [k for k in _contract()["acceptance_criteria"] if k["verification"] != "review"]
    c = _contract(acceptance_criteria=crit)
    rep = tdd.acceptance_report(c, CSHA, _base(tdd))
    assert rep["accepted"] is True
    _invariant(rep)


# RP1 AC-001
def test_loaded_records_and_report_evidence_are_state_relative(tdd, tmp_path):
    state = tmp_path / "scratch-state-dir"
    for rec in _base(tdd):
        tdd.write_record(state, rec, KEY)
    loaded = tdd.load_records(state, "W1-pricing", KEY)
    rep = tdd.acceptance_report(_contract(), CSHA, loaded)
    evidence = [item["evidence"] for item in rep["criteria"].values() if item["evidence"]]
    assert evidence, rep["criteria"]
    for value in evidence:
        assert not Path(value).is_absolute() and str(tmp_path) not in value
        assert re.fullmatch(r"evidence/W1-pricing/\d{4}-(red|green|refactor|integration|review)\.json", value)
    assert rep["criteria"]["AC-001"]["evidence"] == "evidence/W1-pricing/0004-integration.json"
    assert (state / rep["criteria"]["AC-001"]["evidence"]).is_file()
    assert str(tmp_path) not in json.dumps(rep)


# RP1 AC-002 (regression): relative paths do not weaken tamper detection.
def test_relative_paths_keep_tamper_detection(tdd, tmp_path):
    for rec in _base(tdd):
        tdd.write_record(tmp_path, rec, KEY)
    target = tmp_path / "evidence" / "W1-pricing" / "0002-green.json"
    data = json.loads(target.read_text())
    data["classification"] = "failed"
    target.write_text(json.dumps(data))
    with pytest.raises(tdd.TddError) as exc:
        tdd.load_records(tmp_path, "W1-pricing", KEY)
    assert exc.value.code == "evidence_tampered"
