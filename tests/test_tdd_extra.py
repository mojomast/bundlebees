"""Extra tests for bundlebees_plugin.tdd (not part of the approved set).

These cover edge cases of the same contract: matching boundaries, missing
interfaces, bounded JUnit parsing, safe fingerprints, canonical signing and
append-only storage. No network, no real tokens, no process execution.
"""

from __future__ import annotations

import json
import stat

import pytest

SHA = "a" * 40
IMPL = "b" * 40
CSHA = "c" * 64
KEY = b"e" * 32
RED_TEST = "tests/test_pricing.py::test_apply_tax"


def _contract() -> dict:
    return {
        "contract_version": "acdd/1",
        "id": "W2-extra",
        "title": "extra",
        "outcome": {"summary": "x", "observable_behavior": ["y"]},
        "interfaces": [{"name": "shop.pricing.apply_tax", "kind": "function", "change": "add"}],
        "acceptance_criteria": [
            {"id": "AC-001", "statement": "tax", "verification": "test", "tests": [RED_TEST]}
        ],
        "scope": {"assigned_files": ["shop/pricing.py"], "test_files": ["tests/test_pricing.py"]},
        "source_rev": SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }


def _result(cases, exit_code=1):
    return {
        "exit_code": exit_code,
        "tests": len(cases),
        "failures": sum(c["outcome"] == "failed" for c in cases),
        "errors": sum(c["outcome"] == "error" for c in cases),
        "skipped": sum(c["outcome"] == "skipped" for c in cases),
        "timed_out": False,
        "cases": cases,
    }


def _case(case_id, outcome, error_type=None, message=None, stage="call"):
    case = {"id": case_id, "outcome": outcome, "stage": stage}
    if error_type:
        case["error_type"] = error_type
    if message:
        case["message"] = message
    return case


def test_matches_stops_at_id_boundaries():
    from bundlebees_plugin import tdd

    assert tdd.matches("tests/a.py::test_x[1]", "tests/a.py::test_x")
    assert tdd.matches("tests/a.py", "tests/a.py::test_x")
    assert not tdd.matches("tests/a.py::test_x", "tests/a.py::test_xy")
    assert not tdd.matches("tests/a.py::C", "tests/a.py::C2")


def test_missing_dotted_interface_is_behavioral():
    from bundlebees_plugin import tdd

    result = _result(
        [_case(RED_TEST, "error", "AttributeError", "module has no attribute 'apply_tax'", "setup")]
    )
    assert tdd.classify(result, [RED_TEST], ["shop.pricing.apply_tax"]) == "behavioral_failure"


def test_parse_junit_honours_limit_and_type_attribute(tmp_path):
    from bundlebees_plugin import tdd

    xml = (
        '<testsuite><testcase classname="tests.test_a" name="test_a">'
        '<error type="builtins.KeyError" message="boom">trace</error>'
        "</testcase><testcase classname=\"tests.test_a\" name=\"test_b\"/>"
        "</testsuite>"
    )
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="utf-8")
    cases = tdd.parse_junit_cases(path, limit=1)
    assert len(cases) == 1
    assert cases[0]["id"] == "tests/test_a.py::test_a"
    assert cases[0]["outcome"] == "error"
    assert cases[0]["error_type"] == "KeyError"


def test_fingerprints_refuse_absolute_paths(tmp_path):
    from bundlebees_plugin import tdd

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(tdd.TddError) as exc:
        tdd.fingerprint_files(tmp_path, [str(tmp_path / "tests" / "test_a.py")])
    assert exc.value.code == "tests_tampered"


def test_signature_ignores_private_keys_and_detects_tampering():
    from bundlebees_plugin import tdd

    rec = {"value": "x", "_path": "evidence/1.json"}
    signed = tdd.sign_record(rec, KEY)
    assert signed["_path"] == "evidence/1.json"
    assert tdd.verify_signature(signed, KEY)
    signed["value"] = "y"
    assert not tdd.verify_signature(signed, KEY)
    rec["_path"] = "evidence/2.json"
    assert tdd.sign_record(rec, KEY)["signature"] == tdd.sign_record({"value": "x"}, KEY)["signature"]


def test_store_orders_records_and_keeps_them_private(tmp_path):
    from bundlebees_plugin import tdd

    cases = [_case(RED_TEST, "failed", "AssertionError", "assert 1 == 2")]
    red = tdd.make_record(
        _contract(), CSHA, phase="red", revision=IMPL, test_fingerprints={"tests/test_pricing.py": "d" * 64},
        command=[], result=_result(cases), produced_by={"role": "verifier"}, test_first=True,
    )
    cases = [_case(RED_TEST, "passed")]
    green = tdd.make_record(
        _contract(), CSHA, phase="green", revision=IMPL, test_fingerprints={"tests/test_pricing.py": "d" * 64},
        command=[], result=_result(cases, 0), produced_by={"role": "verifier"}, test_first=True,
    )
    first = tdd.write_record(tmp_path, red, KEY)
    second = tdd.write_record(tmp_path, green, KEY)
    assert [p.name for p in (first, second)] == ["0001-red.json", "0002-green.json"]
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    loaded = tdd.load_records(tmp_path, "W2-extra", KEY)
    assert [r["phase"] for r in loaded] == ["red", "green"]
    # RP1: evidence paths are state-relative, never absolute
    assert loaded[0]["_path"] == "evidence/W2-extra/0001-red.json"
    assert (tmp_path / loaded[0]["_path"]) == first


def test_records_are_canonical_json_without_private_fields_in_signature(tmp_path):
    from bundlebees_plugin import tdd

    rec = tdd.make_record(
        _contract(), CSHA, phase="red", revision=IMPL,
        test_fingerprints={"tests/test_pricing.py": "d" * 64}, command=[],
        result=_result([_case(RED_TEST, "failed", "AssertionError")]),
        produced_by={"role": "verifier"}, test_first=True,
    )
    signed = tdd.sign_record(rec, KEY)
    unsigned = {k: v for k, v in signed.items() if k != "signature"}
    again = tdd.sign_record(unsigned, KEY)
    assert signed["signature"] == again["signature"]
    assert set(json.loads(json.dumps(signed))) >= {"signature", "classification"}
    with pytest.raises(tdd.TddError):
        tdd.write_record(tmp_path, dict(signed, contract_id="../x"), KEY)
