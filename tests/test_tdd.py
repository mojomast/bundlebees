"""Approved acceptance tests for bundlebees_plugin.tdd (RED/GREEN/REFACTOR evidence).

Written by the coordinator before implementation (contract BC2). The
implementer must not edit this file; see docs/ACDD.md sections 3-7.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

SHA = "a" * 40
IMPL = "b" * 40
CSHA = "c" * 64
KEY = b"k" * 32
RED_TEST = "tests/test_pricing.py::test_apply_tax"
REG_TEST = "tests/test_pricing.py::test_ten_percent_off"


def _contract(**over):
    c = {
        "contract_version": "acdd/1",
        "id": "W1-pricing",
        "title": "Add apply_tax",
        "outcome": {"summary": "tax", "observable_behavior": ["x"]},
        "interfaces": [{"name": "apply_tax", "kind": "function", "change": "add"},
                       {"name": "shop.pricing", "kind": "module", "change": "modify"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "g", "when": "w", "then": "t", "verification": "test", "tests": [RED_TEST]},
            {"id": "AC-002", "statement": "old behaviour", "verification": "test", "tests": [REG_TEST], "regression": True},
            {"id": "AC-003", "statement": "combined suite", "verification": "integration"},
            {"id": "AC-004", "statement": "naming", "verification": "review"},
        ],
        "scope": {"assigned_files": ["shop/pricing.py"], "test_files": ["tests/test_pricing.py"]},
        "source_rev": SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green", "refactor", "integration"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    c.update(over)
    return c


def _result(cases, exit_code=1, timed_out=False):
    return {
        "exit_code": exit_code,
        "tests": len(cases),
        "failures": sum(c["outcome"] == "failed" for c in cases),
        "errors": sum(c["outcome"] == "error" for c in cases),
        "skipped": sum(c["outcome"] == "skipped" for c in cases),
        "timed_out": timed_out,
        "cases": cases,
    }


def _case(test_id, outcome, error_type=None, message=None, stage="call"):
    c = {"id": test_id, "outcome": outcome, "stage": stage}
    if error_type:
        c["error_type"] = error_type
    if message:
        c["message"] = message
    return c


RED_CASES = [_case(RED_TEST, "failed", "AssertionError", "assert 1000 == 1100"), _case(REG_TEST, "passed")]
GREEN_CASES = [_case(RED_TEST, "passed"), _case(REG_TEST, "passed")]
FP = {"tests/test_pricing.py": "d" * 64}


@pytest.fixture()
def tdd():
    from bundlebees_plugin import tdd

    return tdd


def _record(tdd, phase, cases, *, revision=IMPL, exit_code=None, fp=None, role="verifier", sha=CSHA, contract=None, test_first=True):
    if exit_code is None:
        exit_code = 0 if all(c["outcome"] in ("passed", "skipped") for c in cases) else 1
    return tdd.make_record(
        contract or _contract(), sha, phase=phase, revision=revision,
        test_fingerprints=dict(FP if fp is None else fp), command=["python", "-m", "pytest"],
        result=_result(cases, exit_code), produced_by={"role": role, "name": "test"},
        test_first=test_first, now="2026-11-12T18:00:00Z",
    )


# AC-001: constants and the shared evidence schema.
def test_constants(tdd):
    assert tdd.EVIDENCE_VERSION == "tdd-evidence/1"
    assert set(tdd.CLASSIFICATIONS) == {
        "behavioral_failure", "passed", "syntax_error", "collection_error",
        "environment_error", "unrelated_failure", "timeout", "not_run"}
    for code in ("red_invalid", "red_incomplete", "red_missing", "stale_revision", "tests_tampered",
                 "green_failed", "refactor_failed", "evidence_tampered"):
        assert code in tdd.FAILURE_CODES


def test_records_match_the_evidence_schema(tdd):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[1] / "bundlebees_plugin/schemas/evidence.schema.json").read_text())
    rec = _record(tdd, "red", RED_CASES)
    jsonschema.Draft202012Validator(schema).validate(rec)
    signed = tdd.sign_record(rec, KEY)
    jsonschema.Draft202012Validator(schema).validate(signed)


# AC-002: classification distinguishes real behavioural RED from broken runs (docs/ACDD.md §4).
@pytest.mark.parametrize(
    "cases, exit_code, timed_out, expected",
    [
        (RED_CASES, 1, False, "behavioral_failure"),
        ([_case(RED_TEST, "error", "ImportError", "cannot import name 'apply_tax' from 'shop.pricing'", "setup")], 1, False, "behavioral_failure"),
        ([_case("tests/test_pricing.py", "error", "ImportError", "cannot import name 'apply_tax' from 'shop.pricing'", "collection")], 2, False, "behavioral_failure"),
        ([_case(RED_TEST, "error", "NotImplementedError", "", "call")], 1, False, "behavioral_failure"),
        ([_case(RED_TEST, "error", "SyntaxError", "invalid syntax", "collection")], 2, False, "syntax_error"),
        ([_case("tests/test_pricing.py", "error", "ModuleNotFoundError", "No module named 'requests'", "collection")], 2, False, "collection_error"),
        ([_case(RED_TEST, "error", "PermissionError", "denied", "setup")], 1, False, "environment_error"),
        ([_case(RED_TEST, "failed", "AssertionError"), _case(REG_TEST, "failed", "AssertionError")], 1, False, "unrelated_failure"),
        ([_case(RED_TEST, "failed", "AssertionError"), _case("tests/test_other.py::test_z", "error", "KeyError", "", "setup")], 1, False, "unrelated_failure"),
        (GREEN_CASES, 0, False, "passed"),
        ([], 5, False, "environment_error"),
        ([], 4, False, "environment_error"),
        (RED_CASES, None, True, "timeout"),
    ],
)
def test_classify(tdd, cases, exit_code, timed_out, expected):
    result = _result(cases, exit_code, timed_out)
    assert tdd.classify(result, [RED_TEST], ["apply_tax"]) == expected


def test_classify_none_is_not_run(tdd):
    assert tdd.classify(None, [RED_TEST], []) == "not_run"


def test_matches(tdd):
    assert tdd.matches("tests/a.py::test_x", "tests/a.py")
    assert tdd.matches("tests/a.py::test_x[1]", "tests/a.py::test_x")
    assert tdd.matches("tests/a.py::C::test_x", "tests/a.py::C")
    assert not tdd.matches("tests/a.py::test_xy", "tests/a.py::test_x")
    assert not tdd.matches("tests/ab.py::test_x", "tests/a.py")


# AC-003: junit parsing for pytest output (ids, outcomes, error types), safely.
JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4">
<testcase classname="tests.test_pricing" name="test_ten_percent_off" time="0.001"/>
<testcase classname="tests.test_pricing" name="test_apply_tax" time="0.001"><failure message="assert 1000 == 1100">def test_apply_tax():
&gt;       assert apply_tax(1000, 10) == 1100
E       AssertionError: assert 1000 == 1100</failure></testcase>
<testcase classname="tests.test_pricing.TestRates" name="test_rate[neg]" time="0.001"><skipped message="later"/></testcase>
<testcase classname="" name="tests.test_labels" time="0"><error message="collection failure">tests/test_labels.py:3: in &lt;module&gt;
    from shop.labels import fmt
E   ImportError: cannot import name 'fmt' from 'shop.labels' (token=abcdef0123456789abcdef)</error></testcase>
</testsuite></testsuites>"""


def test_parse_junit_cases(tdd, tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text(JUNIT, encoding="utf-8")
    cases = {c["id"]: c for c in tdd.parse_junit_cases(p)}
    assert cases["tests/test_pricing.py::test_ten_percent_off"]["outcome"] == "passed"
    failed = cases["tests/test_pricing.py::test_apply_tax"]
    assert failed["outcome"] == "failed" and failed["error_type"] == "AssertionError"
    assert cases["tests/test_pricing.py::TestRates::test_rate[neg]"]["outcome"] == "skipped"
    coll = cases["tests/test_labels.py"]
    assert coll["outcome"] == "error" and coll["stage"] == "collection"
    assert coll["error_type"] == "ImportError"
    assert "cannot import name 'fmt'" in coll["message"]
    assert "abcdef0123456789abcdef" not in coll["message"]
    assert all(len(c.get("message", "")) <= 500 for c in cases.values())


def test_parse_junit_rejects_entities_and_huge_files(tdd, tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><testsuite/>', encoding="utf-8")
    with pytest.raises(tdd.TddError) as exc:
        tdd.parse_junit_cases(p)
    assert exc.value.code == "evidence_invalid"
    big = tmp_path / "big.xml"
    big.write_bytes(b"<testsuite>" + b" " * (3 * 1024 * 1024) + b"</testsuite>")
    with pytest.raises(tdd.TddError):
        tdd.parse_junit_cases(big)


# AC-004: test fingerprints.
def test_fingerprint_files(tdd, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_pricing.py").write_text("x = 1\n")
    fp = tdd.fingerprint_files(tmp_path, ["tests/test_pricing.py"])
    import hashlib

    assert fp == {"tests/test_pricing.py": hashlib.sha256(b"x = 1\n").hexdigest()}
    with pytest.raises(tdd.TddError) as exc:
        tdd.fingerprint_files(tmp_path, ["tests/missing.py"])
    assert exc.value.code == "tests_tampered"
    with pytest.raises(tdd.TddError):
        tdd.fingerprint_files(tmp_path, ["../outside.py"])
    os.symlink("/etc/hostname", tmp_path / "tests" / "link.py")
    with pytest.raises(tdd.TddError):
        tdd.fingerprint_files(tmp_path, ["tests/link.py"])


# AC-005: records carry per-criterion status, honestly marking unrun and review items.
def test_make_record_fields_and_criteria(tdd):
    rec = _record(tdd, "red", RED_CASES, revision="e" * 40)
    assert rec["evidence_version"] == "tdd-evidence/1"
    assert rec["contract_id"] == "W1-pricing" and rec["contract_sha256"] == CSHA
    assert rec["base_rev"] == SHA and rec["revision"] == "e" * 40
    assert rec["classification"] == "behavioral_failure"
    assert rec["criteria"] == {"AC-001": "failed", "AC-002": "passed", "AC-003": "not_run", "AC-004": "review_required"}
    assert rec["test_first"] is True and rec["at"] == "2026-11-12T18:00:00Z"
    unrun = tdd.make_record(_contract(), CSHA, phase="green", revision=IMPL, test_fingerprints=FP,
                            command=[], result=None, produced_by={"role": "verifier"}, test_first=True)
    assert unrun["classification"] == "not_run"
    assert unrun["criteria"]["AC-001"] == "not_run"


def test_integration_phase_marks_integration_criteria(tdd):
    rec = _record(tdd, "integration", GREEN_CASES)
    assert rec["criteria"]["AC-003"] == "passed"
    bad = _record(tdd, "integration", [_case(RED_TEST, "passed"), _case("tests/test_x.py::t", "failed", "AssertionError")])
    assert bad["criteria"]["AC-003"] == "failed"


# AC-006: RED validity.
def test_valid_red(tdd):
    tdd.check_red(_contract(), CSHA, _record(tdd, "red", RED_CASES, revision="e" * 40))


@pytest.mark.parametrize(
    "kwargs, code",
    [
        (dict(cases=GREEN_CASES), "red_incomplete"),
        (dict(cases=[_case(RED_TEST, "error", "SyntaxError", "x", "collection")]), "red_invalid"),
        (dict(cases=RED_CASES, sha="f" * 64), "stale_revision"),
        (dict(cases=RED_CASES, role="worker"), "red_invalid"),
        (dict(cases=RED_CASES, fp={}), "tests_tampered"),
    ],
)
def test_invalid_red(tdd, kwargs, code):
    cases = kwargs.pop("cases")
    rec = _record(tdd, "red", cases, revision="e" * 40, **kwargs)
    with pytest.raises(tdd.TddError) as exc:
        tdd.check_red(_contract(), CSHA, rec)
    assert exc.value.code == code
    assert exc.value.fix


def test_red_with_other_base_is_stale(tdd):
    rec = _record(tdd, "red", RED_CASES, revision="e" * 40)
    with pytest.raises(tdd.TddError) as exc:
        tdd.check_red(_contract(source_rev="9" * 40), CSHA, rec)
    assert exc.value.code == "stale_revision"


# AC-007: GREEN validity, including tampered tests.
def test_valid_green(tdd):
    red = _record(tdd, "red", RED_CASES, revision="e" * 40)
    tdd.check_green(_contract(), CSHA, red, _record(tdd, "green", GREEN_CASES))


@pytest.mark.parametrize(
    "red_none, green_kwargs, code",
    [
        (True, dict(cases=GREEN_CASES), "red_missing"),
        (False, dict(cases=RED_CASES), "green_failed"),
        (False, dict(cases=GREEN_CASES, fp={"tests/test_pricing.py": "0" * 64}), "tests_tampered"),
        (False, dict(cases=GREEN_CASES, sha="f" * 64), "stale_revision"),
        (False, dict(cases=GREEN_CASES, role="worker"), "green_failed"),
    ],
)
def test_invalid_green(tdd, red_none, green_kwargs, code):
    red = None if red_none else _record(tdd, "red", RED_CASES, revision="e" * 40)
    cases = green_kwargs.pop("cases")
    green = _record(tdd, "green", cases, **green_kwargs)
    with pytest.raises(tdd.TddError) as exc:
        tdd.check_green(_contract(), CSHA, red, green)
    assert exc.value.code == code


# AC-008: REFACTOR keeps the same tests green.
def test_refactor(tdd):
    red = _record(tdd, "red", RED_CASES, revision="e" * 40)
    green = _record(tdd, "green", GREEN_CASES)
    tdd.check_refactor(_contract(), CSHA, red, green, _record(tdd, "refactor", GREEN_CASES, revision="1" * 40))
    with pytest.raises(tdd.TddError) as exc:
        tdd.check_refactor(_contract(), CSHA, red, green, _record(tdd, "refactor", RED_CASES, revision="1" * 40))
    assert exc.value.code == "refactor_failed"
    with pytest.raises(tdd.TddError) as exc:
        tdd.check_refactor(_contract(), CSHA, red, green,
                           _record(tdd, "refactor", GREEN_CASES, revision="1" * 40, fp={"tests/test_pricing.py": "0" * 64}))
    assert exc.value.code == "tests_tampered"


# AC-009: signed, append-only evidence store.
def test_store_is_signed_append_only_and_private(tdd, tmp_path):
    red = _record(tdd, "red", RED_CASES, revision="e" * 40)
    green = _record(tdd, "green", GREEN_CASES)
    p1 = tdd.write_record(tmp_path, red, KEY)
    p2 = tdd.write_record(tmp_path, green, KEY)
    assert p1 != p2 and p1.parent == p2.parent
    assert stat.S_IMODE(p1.stat().st_mode) == 0o600
    assert stat.S_IMODE(p1.parent.stat().st_mode) == 0o700
    loaded = tdd.load_records(tmp_path, "W1-pricing", KEY)
    assert [r["phase"] for r in loaded] == ["red", "green"]
    assert all(tdd.verify_signature(r, KEY) for r in loaded)
    data = json.loads(p1.read_text())
    data["classification"] = "passed"
    os.chmod(p1, 0o600)
    p1.write_text(json.dumps(data))
    with pytest.raises(tdd.TddError) as exc:
        tdd.load_records(tmp_path, "W1-pricing", KEY)
    assert exc.value.code == "evidence_tampered"
    assert tdd.load_records(tmp_path, "nobody", KEY) == []


def test_write_record_rejects_bad_ids(tdd, tmp_path):
    rec = _record(tdd, "red", RED_CASES, revision="e" * 40)
    rec["contract_id"] = "../escape"
    with pytest.raises(tdd.TddError):
        tdd.write_record(tmp_path, rec, KEY)


# AC-010: acceptance report links evidence and never reports unrun as passed.
def test_acceptance_report(tdd, tmp_path):
    c = _contract()
    records = [
        _record(tdd, "red", RED_CASES, revision="e" * 40),
        _record(tdd, "green", GREEN_CASES),
    ]
    rep = tdd.acceptance_report(c, CSHA, records)
    assert rep["contract_id"] == "W1-pricing" and rep["contract_sha256"] == CSHA
    assert rep["phases"]["red"] == "valid" and rep["phases"]["green"] == "valid"
    assert rep["phases"]["refactor"] == "unrun" and rep["phases"]["integration"] == "unrun"
    assert rep["accepted"] is False
    assert rep["test_first"] is True
    assert rep["criteria"]["AC-003"]["status"] == "not_run"
    records += [_record(tdd, "refactor", GREEN_CASES, revision="1" * 40), _record(tdd, "integration", GREEN_CASES, revision="2" * 40)]
    for r in records:
        r["_path"] = f"evidence/{r['phase']}.json"
    rep = tdd.acceptance_report(c, CSHA, records)
    assert rep["phases"] == {"red": "valid", "green": "valid", "refactor": "valid", "integration": "valid"}
    assert rep["criteria"]["AC-001"]["status"] == "passed"
    assert rep["criteria"]["AC-001"]["evidence"] == "evidence/integration.json"
    assert rep["criteria"]["AC-004"]["status"] == "review_required"
    # review_required criteria block acceptance until a review is required and valid
    assert rep["accepted"] is False
    c2 = _contract(evidence_required=["red", "green", "refactor", "integration", "review"])
    review = _record(tdd, "review", GREEN_CASES, role="reviewer")
    rep2 = tdd.acceptance_report(c2, CSHA, records + [review])
    assert rep2["phases"]["review"] == "valid"
    assert rep2["accepted"] is True


def test_tests_after_code_is_reported_honestly(tdd):
    c = _contract(tdd={"applicable": True, "test_first": False}, evidence_required=["green"])
    rep = tdd.acceptance_report(c, CSHA, [_record(tdd, "green", GREEN_CASES, test_first=False)])
    assert rep["test_first"] is False
    assert rep["phases"]["green"] == "valid"


def test_report_flags_green_without_red(tdd):
    rep = tdd.acceptance_report(_contract(), CSHA, [_record(tdd, "green", GREEN_CASES)])
    assert rep["phases"]["red"] == "unrun"
    assert rep["phases"]["green"] == "red_missing"
    assert rep["accepted"] is False


# AC-011: the sandbox returns per-case outcomes from the run it already performs.
def test_sandbox_returns_cases(tmp_path):
    from bundlebees_plugin import sandbox

    if sandbox.detect_backend() is None:
        pytest.skip("no bwrap/firejail on this host")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n")
    out = sandbox.run_in_sandbox(tmp_path, sandbox.pytest_argv(["tests"]), timeout=300)
    ids = {c["id"]: c["outcome"] for c in out["cases"]}
    assert ids == {"tests/test_a.py::test_ok": "passed", "tests/test_a.py::test_bad": "failed"}
