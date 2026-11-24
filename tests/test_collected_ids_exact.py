"""RED for wave A slice A1b: GREEN must run exactly the approved test ids.

The frozen collection manifest names the approved test ids. A GREEN run that
skips one, renames one, adds one or reports one twice is not evidence for the
contract, even when every reported case passed.
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import tdd

APPROVED = ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]


def _cases(*ids: str, outcome: str = "passed") -> list[dict]:
    return [{"id": node, "outcome": outcome, "stage": "call"} for node in ids]


def test_exact_match_passes():
    assert tdd.check_collected_ids(APPROVED, _cases(*APPROVED)) is None


def test_order_does_not_matter():
    assert tdd.check_collected_ids(APPROVED, _cases(*reversed(APPROVED))) is None


def test_missing_approved_id_is_refused():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids(APPROVED, _cases(APPROVED[0]))
    assert caught.value.code == "tests_mismatch"
    assert "test_two" in caught.value.error


def test_extra_id_is_refused():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids(APPROVED, _cases(*APPROVED, "tests/test_a.py::test_extra"))
    assert caught.value.code == "tests_mismatch"
    assert "test_extra" in caught.value.error


def test_duplicate_id_is_refused():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids(APPROVED, _cases(*APPROVED, APPROVED[0]))
    assert caught.value.code == "tests_mismatch"


def test_collection_stage_cases_do_not_count_as_run():
    cases = _cases(*APPROVED)
    cases[1]["stage"] = "collection"
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids(APPROVED, cases)
    assert caught.value.code == "tests_mismatch"


def test_empty_approved_list_fails_closed():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([], _cases(*APPROVED))
    assert caught.value.code == "tests_mismatch"


# Wiring: check_green enforces the match when the frozen collection is passed in.
_SHA, _IMPL, _CSHA = "a" * 40, "b" * 40, "c" * 64
_FP = {"tests/test_a.py": "d" * 64}


def _contract() -> dict:
    return {
        "contract_version": "acdd/1",
        "id": "A1b-fixture",
        "title": "fixture",
        "outcome": {"summary": "s", "observable_behavior": ["x"]},
        "interfaces": [{"name": "f", "kind": "function", "change": "add"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "g", "when": "w", "then": "t", "verification": "test", "tests": list(APPROVED)},
        ],
        "scope": {"assigned_files": ["src/a.py"], "test_files": ["tests/test_a.py"]},
        "source_rev": _SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge"]},
    }


def _record(phase: str, cases: list[dict], revision: str) -> dict:
    exit_code = 0 if all(c["outcome"] == "passed" for c in cases) else 1
    result = {
        "exit_code": exit_code, "tests": len(cases),
        "failures": sum(c["outcome"] == "failed" for c in cases), "errors": 0, "skipped": 0,
        "timed_out": False, "cases": cases,
    }
    return tdd.make_record(
        _contract(), _CSHA, phase=phase, revision=revision, test_fingerprints=dict(_FP),
        command=["python", "-m", "pytest"], result=result, produced_by={"role": "verifier", "name": "test"},
        test_first=True, now="2026-11-24T18:00:00Z",
    )


def _red() -> dict:
    cases = _cases(*APPROVED, outcome="failed")
    for case in cases:
        case.update(error_type="AssertionError", message="assert 1 == 2")
    return _record("red", cases, _SHA)


def test_check_green_refuses_a_partial_run_when_approved_ids_are_given():
    green = _record("green", _cases(APPROVED[0]), _IMPL)
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_green(_contract(), _CSHA, _red(), green, approved_ids=APPROVED)
    assert caught.value.code == "tests_mismatch"


def test_check_green_accepts_the_exact_run():
    green = _record("green", _cases(*APPROVED), _IMPL)
    tdd.check_green(_contract(), _CSHA, _red(), green, approved_ids=APPROVED)


def test_failure_code_is_documented():
    assert "tests_mismatch" in tdd.FAILURE_CODES
