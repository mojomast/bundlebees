"""Approved acceptance tests for bundlebees_plugin.contract (ACDD acdd/1).

Written by the coordinator before implementation (contract BC1). The
implementer must not edit this file; see docs/ACDD.md.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def _contract(**over):
    c = {
        "contract_version": "acdd/1",
        "id": "W1-pricing",
        "title": "Add apply_tax",
        "outcome": {"summary": "Callers can add tax", "observable_behavior": ["apply_tax(1000, 10) == 1100"]},
        "interfaces": [{"name": "apply_tax", "kind": "function", "change": "add"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "a price of 1000", "when": "apply_tax(1000, 10)", "then": "1100",
             "verification": "test", "tests": ["tests/test_pricing.py::test_apply_tax"]},
            {"id": "AC-002", "statement": "existing discounts still pass", "verification": "test",
             "tests": ["tests/test_pricing.py::test_ten_percent_off"], "regression": True},
            {"id": "AC-003", "statement": "reviewed for naming", "verification": "review"},
        ],
        "dependencies": [],
        "assumptions": ["prices are integer cents"],
        "scope": {"assigned_files": ["shop/pricing.py"], "test_files": ["tests/test_pricing.py"],
                  "prohibited": ["shop/labels.py", "secrets/*"]},
        "source_rev": SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True, "commands": ["python -m pytest tests/test_pricing.py"]},
        "evidence_required": ["red", "green", "integration"],
        "authorization": {"human_gates": ["merge", "tag", "release", "deploy", "publish"]},
    }
    c.update(over)
    return c


@pytest.fixture()
def contract_mod():
    from bundlebees_plugin import contract

    return contract


def _code(exc):
    return getattr(exc.value, "code", None)


# AC-001: the bundled schema is the shared acdd/1 schema.
def test_schema_is_the_shared_acdd1_schema(contract_mod):
    schema = contract_mod.load_schema()
    assert schema["$id"].endswith("/schemas/acdd-contract.schema.json")
    assert schema["properties"]["contract_version"] == {"const": "acdd/1"}
    assert contract_mod.CONTRACT_VERSION == "acdd/1"
    assert tuple(contract_mod.REQUIRED_HUMAN_GATES) == ("merge", "release", "deploy", "publish")
    evidence = json.loads((ROOT / "bundlebees_plugin/schemas/evidence.schema.json").read_text())
    assert evidence["properties"]["evidence_version"] == {"const": "tdd-evidence/1"}


# AC-001: the validator supports every keyword the schema uses (no silent skips).
def test_validator_supports_every_schema_keyword(contract_mod):
    used = set()

    def walk(node):
        if isinstance(node, dict):
            used.update(k for k in node if not k.startswith("$"))
            for key, value in node.items():
                if key in ("properties",):
                    for child in value.values():
                        walk(child)
                elif key not in ("enum", "const", "required", "description", "title"):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(contract_mod.load_schema())
    meta = {"description", "title"}
    assert used - meta <= set(contract_mod.SUPPORTED_KEYWORDS), used - meta - set(contract_mod.SUPPORTED_KEYWORDS)


# AC-001: a valid contract passes and comes back as a fresh, normalised copy.
def test_valid_contract_is_returned_as_a_fresh_copy(contract_mod):
    c = _contract()
    c["scope"]["assigned_files"] = ["./shop/pricing.py"]
    before = copy.deepcopy(c)
    out = contract_mod.validate_contract(c)
    assert c == before
    assert out is not c
    assert out["scope"]["assigned_files"] == ["shop/pricing.py"]


@pytest.mark.parametrize(
    "mutate, field_part",
    [
        (lambda c: c.pop("outcome"), "outcome"),
        (lambda c: c.update(contract_version="acdd/2"), "contract_version"),
        (lambda c: c.update(source_rev="abc123"), "source_rev"),
        (lambda c: c["acceptance_criteria"][0].update(id="AC-1"), "acceptance_criteria"),
        (lambda c: c["acceptance_criteria"][0].pop("then"), "acceptance_criteria"),
        (lambda c: c["scope"].update(allow_all=True), "scope"),
        (lambda c: c["verification"].update(independent=False), "verification"),
        (lambda c: c.update(evidence_required=["red", "red"]), "evidence_required"),
    ],
)
def test_schema_violations_are_rejected_with_the_field(contract_mod, mutate, field_part):
    c = _contract()
    mutate(c)
    with pytest.raises(contract_mod.ContractError) as exc:
        contract_mod.validate_contract(c)
    assert _code(exc) == "contract_invalid"
    assert field_part in (exc.value.field or "") or field_part in exc.value.error
    assert exc.value.fix


# AC-002: the code checks beyond JSON Schema (docs/ACDD.md §2, checks 1-8).
@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda c: c["acceptance_criteria"][1].update(id="AC-001"), "AC-001"),
        (lambda c: c["acceptance_criteria"][0].update(tests=[]), "AC-001"),
        (lambda c: c["acceptance_criteria"][0].update(tests=["tests/test_other.py::test_x"]), "test_files"),
        (lambda c: c["scope"]["assigned_files"].append("tests/test_pricing.py"), "test_files"),
        (lambda c: c.update(evidence_required=["green"]), "red"),
        (lambda c: c.update(tdd={"applicable": False}), "reason"),
        (lambda c: c["scope"]["assigned_files"].append("/etc/passwd"), "assigned_files"),
        (lambda c: c["scope"]["assigned_files"].append("shop/../x.py"), "assigned_files"),
        (lambda c: c["scope"]["assigned_files"].append(".git/config"), "assigned_files"),
        (lambda c: c["scope"]["assigned_files"].append("shop/labels.py"), "prohibited"),
        (lambda c: c["authorization"].update(human_gates=["merge", "release", "deploy"]), "publish"),
        (lambda c: c.update(dependencies=["W1-pricing"]), "dependencies"),
    ],
)
def test_code_checks_reject_with_actionable_errors(contract_mod, mutate, needle):
    c = _contract()
    mutate(c)
    with pytest.raises(contract_mod.ContractError) as exc:
        contract_mod.validate_contract(c)
    assert _code(exc) == "contract_invalid"
    assert needle in exc.value.error or needle in (exc.value.field or "")
    assert exc.value.fix


def test_tdd_not_applicable_with_reason_is_valid(contract_mod):
    c = _contract(tdd={"applicable": False, "reason": "docs-only change"}, evidence_required=["review"])
    c["acceptance_criteria"] = [{"id": "AC-001", "statement": "docs read well", "verification": "review"}]
    c["scope"] = {"assigned_files": ["docs/x.md"]}
    contract_mod.validate_contract(c)


# AC-003: freezing hashes the canonical contract without the review block.
def test_contract_sha256_is_canonical_and_ignores_review(contract_mod):
    c = _contract()
    body = {k: v for k, v in c.items() if k != "review"}
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    assert contract_mod.contract_sha256(c) == expected
    c2 = dict(c, review={"status": "draft"})
    assert contract_mod.contract_sha256(c2) == expected


def test_freeze_then_check_frozen(contract_mod):
    frozen = contract_mod.freeze_contract(_contract(), approved_by="kyle", approved_at="2026-11-12T18:00:00Z")
    assert frozen["review"]["status"] == "approved"
    assert frozen["review"]["approved_by"] == "kyle"
    assert frozen["review"]["approved_at"] == "2026-11-12T18:00:00Z"
    assert contract_mod.check_frozen(frozen) == frozen["review"]["contract_sha256"]


def test_freeze_refuses_an_invalid_contract(contract_mod):
    with pytest.raises(contract_mod.ContractError) as exc:
        contract_mod.freeze_contract(_contract(source_rev="nope"), approved_by="kyle")
    assert _code(exc) == "contract_invalid"


# AC-004: a frozen contract cannot be widened, relaxed or given easier tests silently.
@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["scope"]["assigned_files"].append("shop/extra.py"),
        lambda c: c["acceptance_criteria"][0].update(then="anything"),
        lambda c: c["acceptance_criteria"][0].update(tests=["tests/test_pricing.py::test_trivial"]),
        lambda c: c.update(evidence_required=["red", "green"]),
    ],
)
def test_editing_a_frozen_contract_is_detected(contract_mod, mutate):
    frozen = contract_mod.freeze_contract(_contract(), approved_by="kyle")
    mutate(frozen)
    with pytest.raises(contract_mod.ContractError) as exc:
        contract_mod.check_frozen(frozen)
    assert _code(exc) == "contract_changed"


def test_unapproved_contract_is_not_frozen(contract_mod):
    for review in (None, {"status": "draft"}):
        c = _contract()
        if review:
            c["review"] = review
        with pytest.raises(contract_mod.ContractError) as exc:
            contract_mod.check_frozen(c)
        assert _code(exc) == "contract_not_frozen"


# AC-005: helpers used by the verifier.
def test_red_set_excludes_regression_and_non_test_criteria(contract_mod):
    c = _contract()
    assert contract_mod.red_set(c) == ["tests/test_pricing.py::test_apply_tax"]
    assert contract_mod.criteria_tests(c) == {
        "AC-001": ["tests/test_pricing.py::test_apply_tax"],
        "AC-002": ["tests/test_pricing.py::test_ten_percent_off"],
        "AC-003": [],
    }
    assert contract_mod.added_interfaces(c) == ["apply_tax"]


def test_is_prohibited_uses_glob_patterns(contract_mod):
    c = _contract()
    assert contract_mod.is_prohibited("secrets/key.pem", c)
    assert contract_mod.is_prohibited("shop/labels.py", c)
    assert not contract_mod.is_prohibited("shop/pricing.py", c)


# AC-006: a brief must match its contract.
def test_brief_must_match_contract(contract_mod):
    frozen = contract_mod.freeze_contract(_contract(), approved_by="kyle")
    good = {"objective": "x", "source_rev": SHA, "assigned_files": ["shop/pricing.py"], "acceptance": ["AC-001"], "contract": frozen}
    assert contract_mod.contract_from_brief(good) == frozen
    assert contract_mod.contract_from_brief({"objective": "x"}) is None
    contract_mod.check_brief_against_contract(good, frozen)
    cases = [
        (dict(good, source_rev="b" * 40), "stale_revision"),
        (dict(good, assigned_files=["shop/pricing.py", "shop/other.py"]), "scope_violation"),
        (dict(good, assigned_files=["shop/pricing.py", "tests/test_pricing.py"]), "tests_tampered"),
    ]
    for brief, code in cases:
        with pytest.raises(contract_mod.ContractError) as exc:
            contract_mod.check_brief_against_contract(brief, frozen)
        assert _code(exc) == code, brief


def test_errors_are_scrubbed(contract_mod):
    c = _contract(title="x")
    c["scope"]["assigned_files"].append("/home/user/token=abcdef0123456789abcdef")  # gitleaks:allow (fake value for the scrub test)
    with pytest.raises(contract_mod.ContractError) as exc:
        contract_mod.validate_contract(c)
    assert "abcdef0123456789abcdef" not in exc.value.error
