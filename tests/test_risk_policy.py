"""Approved acceptance tests for contrib.unwired.risk_policy. Coordinator-written; do not edit."""
from __future__ import annotations

import pytest

from contrib.unwired import risk_policy

PHASES = {"red", "green", "refactor", "integration", "review", "security"}


def test_ac001_each_tier_has_a_complete_policy():
    for tier in ("low", "medium", "high"):
        p = risk_policy.policy_for(tier)
        assert set(p) >= {"tier", "phases", "reviewer", "plan", "human_gates_required", "min_verifications"}
        assert p["tier"] == tier
        assert set(p["phases"]) <= PHASES and "red" in p["phases"] and "green" in p["phases"]


def test_ac002_policy_grows_monotonically_with_tier():
    low, med, high = (risk_policy.policy_for(t) for t in ("low", "medium", "high"))
    assert set(low["phases"]) < set(med["phases"]) < set(high["phases"])
    assert low["min_verifications"] <= med["min_verifications"] <= high["min_verifications"]


def test_ac003_high_requires_security_phase_api_reviewer_and_human_gates():
    p = risk_policy.policy_for("high")
    assert "security" in p["phases"] and "review" in p["phases"]
    assert p["reviewer"] == "deepseek-api"
    assert p["human_gates_required"] is True


def test_ac004_low_is_light_and_uses_the_go_reviewer():
    p = risk_policy.policy_for("low")
    assert p["reviewer"] == "opencode-go" and p["plan"] == "short"
    assert p["human_gates_required"] is False


@pytest.mark.parametrize("bad", [None, "", "extreme", 3, ["high"], {}])
def test_ac005_unknown_tier_fails_closed_to_the_high_policy(bad):
    assert risk_policy.policy_for(bad)["tier"] == "high"


def test_ac006_policy_is_a_copy_callers_cannot_mutate_the_table():
    p = risk_policy.policy_for("low")
    p["phases"].append("security")
    assert "security" not in risk_policy.policy_for("low")["phases"]


def test_ac007_required_phases_missing_from_evidence_are_reported():
    gaps = risk_policy.missing_phases("high", ["red", "green"])
    assert set(gaps) == {"refactor", "integration", "review", "security"} & set(risk_policy.policy_for("high")["phases"])
    assert risk_policy.missing_phases("low", ["red", "green"]) == []
    assert "security" in risk_policy.missing_phases("banana", ["red", "green"])
