"""Approved acceptance tests for bundlebees_plugin.risk and the optional `risk` schema field.
Coordinator-written (Wave 1, risk tiers); the implementer must not edit this file."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

from bundlebees_plugin import risk

SCHEMAS = Path(__file__).resolve().parents[1] / "bundlebees_plugin" / "schemas"
SHA = "a" * 40


def validator(name):
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / name).read_text()))


def brief(**over):
    b = {"objective": "do it", "source_rev": SHA, "assigned_files": ["a.py"], "acceptance": ["AC-001: x - t.py::T::a"]}
    b.update(over)
    return b


# ---- derive_floor -------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "bundlebees_plugin/security.py", "bundlebees_plugin/sandbox.py", "bundlebees_plugin/contract_gate.py",
    "bundlebees_plugin/verify_local.py", "bundlebees_plugin/schemas/evidence.schema.json",
    ".github/workflows/ci.yml", "server/security.mjs", "src/approvals.mjs", "src/auth.mjs",
])
def test_ac001_sensitive_paths_derive_high(path):
    r = risk.derive_floor([path])
    assert r["tier"] == "high" and r["reasons"]


def test_ac002_plain_source_is_medium_and_docs_tests_are_low():
    assert risk.derive_floor(["bundlebees_plugin/cli.py"])["tier"] == "medium"
    assert risk.derive_floor(["docs/x.md", "tests/test_a.py", "README.md"])["tier"] == "low"


def test_ac003_the_highest_path_wins():
    r = risk.derive_floor(["docs/x.md", "bundlebees_plugin/cli.py", "bundlebees_plugin/sandbox.py"])
    assert r["tier"] == "high"


def test_ac004_empty_or_invalid_paths_fail_closed_to_high():
    assert risk.derive_floor([])["tier"] == "high"
    assert risk.derive_floor(None)["tier"] == "high"
    assert risk.derive_floor(["", 3])["tier"] == "high"


def test_ac005_path_traversal_and_absolute_paths_are_high():
    assert risk.derive_floor(["../outside.py"])["tier"] == "high"
    assert risk.derive_floor(["/etc/passwd"])["tier"] == "high"


# ---- effective_tier -----------------------------------------------------------------

def test_ac006_declared_may_raise_above_the_floor():
    r = risk.effective_tier("high", ["docs/x.md"])
    assert r["tier"] == "high" and r["floor"] == "low" and r["raised"] is False


def test_ac007_declared_below_the_floor_is_raised_and_flagged():
    r = risk.effective_tier("low", ["bundlebees_plugin/sandbox.py"])
    assert r["tier"] == "high" and r["raised"] is True and r["declared"] == "low"


def test_ac008_missing_or_unknown_declared_uses_floor_or_high():
    assert risk.effective_tier(None, ["docs/x.md"])["tier"] == "low"
    assert risk.effective_tier("banana", ["docs/x.md"])["tier"] == "high"
    assert risk.effective_tier(5, ["docs/x.md"])["tier"] == "high"


# ---- schema -------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["brief.schema.json"])
def test_ac009_brief_without_risk_still_validates(name):
    assert not list(validator(name).iter_errors(brief()))


def test_ac009_brief_with_valid_risk_validates():
    b = brief(risk={"tier": "high", "reasons": ["touches sandbox.py"], "blast_radius": "launcher"})
    assert not list(validator("brief.schema.json").iter_errors(b))


@pytest.mark.parametrize("bad", [{"tier": "extreme"}, {"tier": 3}, {}, {"tier": "low", "reasons": "x"},
                                 {"tier": "low", "reasons": [1]}, {"tier": "low", "unknown": 1}, "high"])
def test_ac010_brief_rejects_malformed_risk(bad):
    assert list(validator("brief.schema.json").iter_errors(brief(risk=bad)))


def _contract_example():
    path = SCHEMAS.parents[1] / "tests" / "test_contract.py"
    # reuse the minimal valid contract the existing approved tests build
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._contract()


def test_ac011_contract_accepts_optional_risk_and_rejects_bad_tier():
    c = _contract_example()
    v = validator("contract.schema.json")
    assert not list(v.iter_errors(c))
    ok = copy.deepcopy(c); ok["risk"] = {"tier": "medium", "reasons": ["r"]}
    assert not list(v.iter_errors(ok))
    bad = copy.deepcopy(c); bad["risk"] = {"tier": "nope"}
    assert list(v.iter_errors(bad))
