"""Supplementary tests for the ACDD contract module (not coordinator-approved)."""

from __future__ import annotations

import copy
import re

import pytest

from bundlebees_plugin import contract

SHA = "a" * 40


def _contract(**over):
    value = {
        "contract_version": "acdd/1",
        "id": "W1-extra",
        "title": "Extra checks",
        "outcome": {"summary": "work", "observable_behavior": ["it works"]},
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "given": "a",
                "when": "b",
                "then": "c",
                "verification": "test",
                "tests": ["tests/test_extra.py::test_one"],
            }
        ],
        "scope": {
            "assigned_files": ["src/a.py"],
            "test_files": ["tests/test_extra.py"],
            "prohibited": ["secret/*.pem"],
        },
        "source_rev": SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    value.update(over)
    return value


def test_all_scope_lists_are_normalised_without_mutating_input():
    value = _contract()
    value["scope"]["assigned_files"] = ["src\\a.py", "./src/b.py"]
    value["scope"]["test_files"] = ["./tests//test_extra.py"]
    value["scope"]["read_only"] = ["docs\\guide.md"]
    value["scope"]["prohibited"] = ["secret\\*.pem"]
    before = copy.deepcopy(value)
    out = contract.validate_contract(value)
    assert value == before
    assert out["scope"] == {
        "assigned_files": ["src/a.py", "src/b.py"],
        "test_files": ["tests/test_extra.py"],
        "read_only": ["docs/guide.md"],
        "prohibited": ["secret/*.pem"],
    }


def test_every_scope_list_rejects_unsafe_paths():
    for key in ("test_files", "read_only", "prohibited"):
        value = _contract(
            tdd={"applicable": False, "reason": "docs-only change"},
            evidence_required=["review"],
        )
        value["acceptance_criteria"] = [
            {"id": "AC-001", "statement": "docs", "verification": "review"}
        ]
        value["scope"][key] = ["../escape"]
        with pytest.raises(contract.ContractError) as exc:
            contract.validate_contract(value)
        assert exc.value.code == "contract_invalid"
        assert key in (exc.value.field or "")


def test_red_set_deduplicates_and_keeps_order():
    value = _contract()
    value["acceptance_criteria"] = [
        {
            "id": "AC-001",
            "statement": "one",
            "verification": "test",
            "tests": ["tests/test_extra.py::test_one", "tests/test_extra.py::test_two"],
        },
        {
            "id": "AC-002",
            "statement": "regression",
            "verification": "test",
            "tests": ["tests/test_extra.py::test_old"],
            "regression": True,
        },
        {
            "id": "AC-003",
            "statement": "two",
            "verification": "test",
            "tests": ["tests/test_extra.py::test_two"],
        },
    ]
    assert contract.red_set(value) == [
        "tests/test_extra.py::test_one",
        "tests/test_extra.py::test_two",
    ]


def test_freeze_defaults_to_utc_iso_z():
    frozen = contract.freeze_contract(_contract(), approved_by="kyle")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", frozen["review"]["approved_at"])


def test_hash_ignores_review_and_tracks_content():
    value = _contract()
    digest = contract.contract_sha256(value)
    assert contract.contract_sha256(dict(value, review={"status": "draft"})) == digest
    assert contract.contract_sha256(dict(value, title="different")) != digest


def test_unknown_contract_version_is_rejected():
    with pytest.raises(contract.ContractError) as exc:
        contract.validate_contract(_contract(contract_version="acdd/2"))
    assert exc.value.code == "contract_invalid"
    assert "contract_version" in (exc.value.field or "")


def test_brief_paths_are_normalised_before_scope_comparison():
    frozen = contract.freeze_contract(_contract(), approved_by="kyle")
    brief = {
        "source_rev": SHA.upper(),
        "assigned_files": ["src\\a.py"],
        "contract": frozen,
    }
    contract.check_brief_against_contract(brief, frozen)


def test_validator_enforces_max_length_and_property_names():
    spec = {
        "type": "object",
        "properties": {},
        "propertyNames": {"pattern": "^[a-z]+$"},
        "additionalProperties": {"type": "string", "maxLength": 3},
    }
    assert contract._schema_problem({"ok": "toolong"}, spec, "x") == (
        "x.ok",
        "x.ok is longer than 3 character(s)",
    )
    assert contract._schema_problem({"OK": "x"}, spec, "x") == (
        "x.OK",
        "x.OK does not match the required pattern",
    )


def test_require_contract_setting_uses_get_config():
    from tests.fakes import FakeCtx
    from bundlebees_plugin.tools import _require_contract

    assert _require_contract(FakeCtx()) is False
    assert _require_contract(FakeCtx(settings={"require_contract": "true"})) is True
    assert _require_contract(FakeCtx(settings={"require_contract": "false"})) is False
