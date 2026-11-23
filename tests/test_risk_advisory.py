"""Approved acceptance tests: advisory-only risk tier wiring for brief generation. Coordinator-written; do not edit."""
from __future__ import annotations

import copy

import pytest

from bundlebees_plugin.brief import advise_risk, brief_sha256, validate_brief

SHA = "a" * 40


def brief(files, **over):
    b = {"objective": "Implement", "source_rev": SHA, "assigned_files": files, "acceptance": ["tests pass"]}
    b.update(over)
    return b


def test_ac001_advise_risk_reports_the_path_derived_tier():
    adv = advise_risk(brief(["bundlebees_plugin/security.py"]))
    assert adv["advisory"] is True
    assert adv["tier"] == "high"
    assert adv["reasons"]


def test_ac002_plain_source_is_medium_and_docs_are_low():
    assert advise_risk(brief(["bundlebees_plugin/foo.py"]))["tier"] == "medium"
    assert advise_risk(brief(["docs/a.md"]))["tier"] == "low"


def test_ac003_declared_tier_can_only_raise():
    assert advise_risk(brief(["docs/a.md"], risk={"tier": "high"}))["tier"] == "high"
    adv = advise_risk(brief(["bundlebees_plugin/security.py"], risk={"tier": "low"}))
    assert adv["tier"] == "high"
    assert adv["raised"] is True


def test_ac004_does_not_mutate_the_brief_and_validate_brief_is_unchanged():
    b = brief(["docs/a.md"])
    before = copy.deepcopy(b)
    advise_risk(b)
    assert b == before
    v = validate_brief(b)
    assert "risk" not in v and "advisory" not in v
    assert brief_sha256(v) == brief_sha256(validate_brief(before))


def test_ac005_never_raises_and_fails_closed_to_high_on_garbage():
    for bad in (None, 5, "x", {}, {"assigned_files": None}, {"assigned_files": []}, {"assigned_files": ["../x"]},
                {"assigned_files": ["a.py"], "risk": "nope"}, {"assigned_files": ["a.py"], "risk": {"tier": "weird"}}):
        adv = advise_risk(bad)
        assert adv["tier"] == "high", bad
        assert adv["advisory"] is True


def test_ac006_advice_is_json_serialisable_and_has_no_enforcement_fields():
    import json
    adv = advise_risk(brief(["bundlebees_plugin/foo.py"]))
    json.dumps(adv)
    assert not {"blocked", "enforce", "required", "deny"} & set(adv)
