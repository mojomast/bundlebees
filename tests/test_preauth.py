"""Pre-authorization envelopes: checker and ledger (program: automated outside review, Phase 2).

An envelope is ``{id, action_class, mechanical conditions, max_uses, expires_at (<= 7 days),
created_from (the plan hash)}``. Before acting under one, the checker PROVES every condition
from data — scripts, never judgement — and records the use with its evidence. Classes that
touch the gate, merge lane, policy, rulesets, preauth itself, secrets, force-push, data
deletion or an unrolled-back deploy are never pre-authorizable.

Run: python -m pytest tests/test_preauth.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


NOW = "2026-11-27T20:00:00Z"


def _envelope(**overrides):
    value = {
        "id": "p1-refreeze-2026-11",
        "action_class": "contract_refreeze",
        "conditions": [{"name": "semantic diff is source_rev + RED only", "fact": "diff_limited", "value": True}],
        "max_uses": 3,
        "expires_at": "2026-12-04T20:00:00Z",
        "created_from": "0" * 64,
    }
    value.update(overrides)
    return value


def _facts(**overrides):
    value = {"diff_limited": True}
    value.update(overrides)
    return value


def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.preauth")


def test_never_preauthorizable_classes_are_frozen():
    m = _load()
    assert {
        "gate", "merge_lane", "policy", "rulesets", "preauth", "secrets",
        "force_push", "delete_data", "deploy_without_rollback",
    } <= set(m.FORBIDDEN_CLASSES)


def test_envelope_fields_and_expiry_ceiling():
    m = _load()
    m.validate_envelope(_envelope(), NOW)
    with pytest.raises(m.PreauthError) as caught:
        m.validate_envelope(_envelope(expires_at="2026-12-31T00:00:00Z"), NOW)
    assert caught.value.code == "preauth_expiry_too_long"
    with pytest.raises(m.PreauthError) as caught:
        m.validate_envelope(_envelope(max_uses=0), NOW)
    assert caught.value.code == "preauth_envelope_invalid"
    with pytest.raises(m.PreauthError) as caught:
        m.validate_envelope(_envelope(expires_at="2026-11-01T00:00:00Z"), NOW)
    assert caught.value.code == "preauth_expired"


def test_forbidden_action_class_refused():
    m = _load()
    for action_class in sorted(m.FORBIDDEN_CLASSES):
        with pytest.raises(m.PreauthError) as caught:
            m.may_act(_envelope(action_class=action_class), _facts(), Path("/nonexistent"), NOW)
        assert caught.value.code == "preauth_forbidden_class"


def test_conditions_are_proven_from_data_and_fail_closed():
    m = _load()
    envelope = _envelope()
    assert m.conditions_met(envelope, _facts()) is True
    assert m.conditions_met(envelope, _facts(diff_limited=False)) is False
    assert m.conditions_met(envelope, {}) is False
    unknown = _envelope(conditions=[{"name": "vibes", "fact": "unknown_fact", "value": True}])
    assert m.conditions_met(unknown, _facts(unknown_fact=True)) is False


def test_max_uses_counts_the_ledger(tmp_path: Path):
    m = _load()
    ledger = tmp_path / "preauth.jsonl"
    envelope = _envelope(max_uses=2)
    for _ in range(2):
        result = m.may_act(envelope, _facts(), ledger, NOW)
        assert result["allowed"] is True
        m.record_use(ledger, envelope["id"], "refreeze contract", {"command": "contract freeze"})
    with pytest.raises(m.PreauthError) as caught:
        m.may_act(envelope, _facts(), ledger, NOW)
    assert caught.value.code == "preauth_exhausted"


def test_revocation_is_effective_immediately(tmp_path: Path):
    m = _load()
    ledger = tmp_path / "preauth.jsonl"
    envelope = _envelope()
    m.record_use(ledger, envelope["id"], "refreeze", {})
    m.revoke(ledger, envelope["id"], NOW)
    with pytest.raises(m.PreauthError) as caught:
        m.may_act(envelope, _facts(), ledger, NOW)
    assert caught.value.code == "preauth_revoked"


def test_daily_digest_counts_uses_and_revocations(tmp_path: Path):
    m = _load()
    ledger = tmp_path / "preauth.jsonl"
    m.record_use(ledger, "p1", "refreeze", {"sha": "a"})
    m.record_use(ledger, "p1", "refreeze", {"sha": "b"})
    m.record_use(ledger, "p2", "close", {"pr": 41})
    m.revoke(ledger, "p2", NOW)
    digest = m.daily_digest(ledger, "2026-11-27")
    assert digest["uses"] == 3
    assert digest["by_envelope"] == {"p1": 2, "p2": 1}
    assert digest["revoked"] == ["p2"]
    assert json.loads(ledger.read_text().splitlines()[0])["kind"] == "use"
