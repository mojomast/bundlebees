"""Strict brief contract tests (offline, standard library only)."""

from __future__ import annotations

import copy

import pytest

from bundlebees_plugin.brief import (
    BriefError,
    brief_sha256,
    load_schema,
    validate_brief,
)

SHA1 = "a" * 40
SHA256 = "b" * 64


def _brief(**overrides):
    brief = {
        "objective": "Implement the feature",
        "source_rev": SHA1,
        "assigned_files": ["src/a.py"],
        "acceptance": ["tests pass"],
        "project": "proj",
        "branch": "feat/x",
        "commands": ["python3 -m pytest -q"],
        "deliverables": ["patch"],
        "deps": ["BB00"],
        "budget": {"timeout_s": 600, "max_tool_calls": 20},
        "kanban_task_id": "k1",
    }
    brief.update(overrides)
    return brief


def test_brief_error_carries_the_tool_contract():
    error = BriefError("brief.objective is bad", "fix the objective")
    assert isinstance(error, ValueError)
    assert error.code == "brief_invalid"
    assert error.error == "brief.objective is bad"
    assert error.fix == "fix the objective"


def test_load_schema_is_a_strict_draft_2020_12_contract():
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == [
        "objective",
        "source_rev",
        "assigned_files",
        "acceptance",
    ]
    assert schema["additionalProperties"] is True
    assert schema["properties"]["source_rev"]["pattern"] == (
        "^[0-9a-f]{40}([0-9a-f]{24})?$"
    )
    for optional in (
        "project",
        "branch",
        "commands",
        "deliverables",
        "deps",
        "budget",
        "kanban_task_id",
        "run_id",
        "retry_of",
        "wave",
        "tests",
    ):
        assert optional in schema["properties"]


@pytest.mark.parametrize(
    "field", ["objective", "source_rev", "assigned_files", "acceptance"]
)
def test_each_required_field_is_required(field: str):
    brief = _brief()
    brief.pop(field)
    with pytest.raises(BriefError) as raised:
        validate_brief(brief)
    assert field in raised.value.error
    assert raised.value.code == "brief_invalid"


def test_objective_must_be_non_empty():
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(objective="   "))
    assert "objective" in raised.value.error


@pytest.mark.parametrize("source_rev", ["abc123", "feat/hardening", "v0.1.1", "a" * 39])
def test_short_branch_and_tag_source_revs_are_rejected(source_rev: str):
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(source_rev=source_rev))
    assert "source_rev" in raised.value.error


def test_full_sha256_source_rev_is_accepted():
    result = validate_brief(_brief(source_rev=SHA256))
    assert result["source_rev"] == SHA256


def test_source_rev_is_lowercased():
    result = validate_brief(_brief(source_rev=SHA1.upper()))
    assert result["source_rev"] == SHA1


def test_empty_assigned_files_is_rejected():
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(assigned_files=[]))
    assert "assigned_files" in raised.value.error


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "../outside.py",
        "src/../outside.py",
        ".git/config",
        "nested/.git/HEAD",
        "src/\x00evil.py",
    ],
)
def test_non_repository_relative_assigned_files_are_rejected(path: str):
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(assigned_files=["src/ok.py", path]))
    assert "assigned_files" in raised.value.error


def test_assigned_files_are_normalised_and_deduplicated_in_order():
    result = validate_brief(
        _brief(
            assigned_files=[
                "src\\a.py",
                "./src/a.py",
                "src//b.py",
                "src/a.py",
                "./docs/readme.md",
            ]
        )
    )
    assert result["assigned_files"] == ["src/a.py", "src/b.py", "docs/readme.md"]


def test_acceptance_array_becomes_criteria():
    result = validate_brief(_brief(acceptance=["tests pass", "docs updated"]))
    assert result["acceptance"] == {
        "criteria": ["tests pass", "docs updated"],
        "commands": [],
        "tests": [],
    }


def test_acceptance_object_is_normalised_with_defaults():
    result = validate_brief(
        _brief(
            acceptance={
                "criteria": ["tests pass"],
                "commands": ["python3 -m pytest -q"],
                "tests": ["tests/test_a.py"],
                "extra": "dropped",
            }
        )
    )
    assert result["acceptance"] == {
        "criteria": ["tests pass"],
        "commands": ["python3 -m pytest -q"],
        "tests": ["tests/test_a.py"],
    }


def test_acceptance_object_with_only_tests_is_rejected():
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(acceptance={"tests": ["tests/test_a.py"]}))
    assert "acceptance" in raised.value.error


def test_acceptance_object_with_empty_criteria_is_rejected():
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(acceptance={"criteria": []}))
    assert "acceptance" in raised.value.error


def test_acceptance_object_with_commands_only_is_accepted():
    result = validate_brief(_brief(acceptance={"commands": ["python3 -m pytest -q"]}))
    assert result["acceptance"] == {
        "criteria": [],
        "commands": ["python3 -m pytest -q"],
        "tests": [],
    }


def test_optional_run_retry_wave_and_tests_are_validated():
    result = validate_brief(
        _brief(
            run_id="run-01.A_b",
            retry_of="attempt-1",
            wave="wave_2",
            tests=["tests/test_a.py"],
        )
    )
    assert result["run_id"] == "run-01.A_b"
    assert result["retry_of"] == "attempt-1"
    assert result["wave"] == "wave_2"
    assert result["tests"] == ["tests/test_a.py"]
    for bad in ("-leading", ".leading", "has space", "x" * 65):
        with pytest.raises(BriefError) as raised:
            validate_brief(_brief(run_id=bad))
        assert "run_id" in raised.value.error
    with pytest.raises(BriefError) as raised:
        validate_brief(_brief(wave="bad wave"))
    assert "wave" in raised.value.error


def test_validate_returns_a_new_dict_and_does_not_mutate_input():
    brief = _brief(
        source_rev=SHA1.upper(),
        assigned_files=["src\\a.py", "./src/a.py"],
        acceptance=["tests pass"],
    )
    snapshot = copy.deepcopy(brief)
    result = validate_brief(brief)
    assert brief == snapshot
    assert result is not brief
    assert result["source_rev"] == SHA1
    assert result["assigned_files"] == ["src/a.py"]
    assert result["acceptance"]["criteria"] == ["tests pass"]
    assert brief["acceptance"] == ["tests pass"]


def test_brief_sha256_is_stable_under_key_order():
    left = {"objective": "x", "source_rev": SHA1, "assigned_files": ["a"]}
    right = {"assigned_files": ["a"], "source_rev": SHA1, "objective": "x"}
    digest = brief_sha256(left)
    assert digest == brief_sha256(right)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
