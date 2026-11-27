"""Review-bee dispatch and its read-only profile (program: automated outside review, Phase 1.3).

A review bee is an ordinary bundlebees dispatch with three extra properties:

* the model comes from the routed ``roles.reviewer`` role, not the slice tier table;
* the security profile is read-only: it may write ``review/verdict.json`` and
  ``review/scratch/**`` only, holds no push or API credential, and its only network
  destination is the model provider;
* the brief carries the packet's head as ``source_rev`` so the bee reads exactly that tree.

Run: python -m pytest tests/test_review_dispatch.py -q
"""

from __future__ import annotations

import pytest


PACKET = {
    "repo": "mojomast/rizzstack",
    "pr": 41,
    "head_sha": "1a1d032f98691fb4121d5b9673b36562c77a5627",
    "author_model_family": "unknown",
}
ROLE = {
    "model": "k3.1-pro",
    "model_family": "kimi",
    "identity": "rizzstack-reviewer",
    "budget": {"timeout_s": 2700, "max_tool_calls": 120},
    "post_mode": "auto_reject",
}


def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.review_dispatch")


def test_no_push_or_api_credentials():
    m = _load()
    brief = m.build_review_brief(PACKET, ROLE)
    security = brief["security"]
    assert security == m.REVIEW_SECURITY
    assert security["allow_push"] is False
    assert security["allow_api"] is False
    assert list(security["credentials"]) == []
    text = str(brief).lower()
    for forbidden in ("gh_token", "github_token", "api_key", "password"):
        assert forbidden not in text


def test_network_provider_only():
    m = _load()
    brief = m.build_review_brief(PACKET, ROLE)
    assert list(brief["security"]["network"]) == ["model-provider"]


def test_brief_uses_the_routed_model_and_budget():
    m = _load()
    brief = m.build_review_brief(PACKET, ROLE)
    assert brief["kind"] == "review"
    assert brief["model"] == "k3.1-pro"
    assert brief["model_family"] == "kimi"
    assert brief["budget"] == ROLE["budget"]
    assert brief["source_rev"] == PACKET["head_sha"]
    assert brief["assigned_files"] == ["review/verdict.json", "review/scratch/"]
    assert "review/verdict.json" in brief["objective"]


def test_hard_rules_forbid_push_merge_and_posting():
    m = _load()
    rules = " ".join(m.build_review_brief(PACKET, ROLE)["hard_rules"]).lower()
    for verb in ("never push", "never merge", "never post"):
        assert verb in rules


def test_same_family_author_is_refused():
    m = _load()
    packet = dict(PACKET, author_model_family="kimi")
    with pytest.raises(m.ReviewDispatchError) as caught:
        m.build_review_brief(packet, ROLE)
    assert caught.value.code == "review_not_independent"


def test_tampered_profile_is_refused_before_dispatch():
    m = _load()
    brief = m.build_review_brief(PACKET, ROLE)
    brief["security"] = dict(brief["security"], allow_push=True)
    called = []
    with pytest.raises(m.ReviewDispatchError) as caught:
        m.dispatch_review(brief, lambda b: called.append(b) or {"attempt": "att_x"})
    assert caught.value.code == "review_profile_invalid"
    assert called == []


def test_dispatch_calls_the_dispatcher_once():
    m = _load()
    brief = m.build_review_brief(PACKET, ROLE)
    calls = []
    result = m.dispatch_review(brief, lambda b: calls.append(b) or {"attempt": "att_x"})
    assert calls == [brief]
    assert result["attempt"] == "att_x"
