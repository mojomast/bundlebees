"""Reviewer routing for the outside-review role (program: automated outside review, Phase 1.1).

The role lives in the coordinator's config under ``roles.reviewer`` and is a LIST of models,
each with an explicit family (the policy's ``models`` shape). Routing is decided per pull
request from the AUTHOR model families recorded on its commits: a Kimi-authored PR gets the
deepseek entry, everything else gets the kimi entry, and a PR with an unknown author, both
families, or a configured reviewer whose family cannot be identified refuses with
``review_not_independent`` (the owner/outside reviewer decides).

Run: python -m pytest tests/test_review_roles.py -q
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import settings


def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.review_roles")


def test_default_reviewer_models():
    m = _load()
    role = m.reviewer_role({})
    assert role["models"] == [
        {"model": "k3.1-pro", "model_family": "kimi"},
        {"model": "nous:deepseek-v4.1-flash-1017", "model_family": "deepseek"},
    ]
    assert role["budget"] == m.DEFAULT_BUDGET == {"timeout_s": 2700, "max_tool_calls": 120}
    assert role["identity"] == "rizzstack-reviewer"
    assert role["post_mode"] == "auto_reject"


def test_configured_models_are_honoured():
    m = _load()
    config = {"roles": {"reviewer": {
        "models": [{"model": "nous:hy4-flash", "model_family": "kimi"}],
        "budget": {"timeout_s": 600},
    }}}
    role = m.reviewer_role(config)
    assert role["models"] == [{"model": "nous:hy4-flash", "model_family": "kimi"}]
    assert role["budget"] == {"timeout_s": 600, "max_tool_calls": 120}
    route = m.route_reviewer(config, ["nous:deepseek-v4.1-flash-1017"])
    assert route["model"] == "nous:hy4-flash"
    assert route["model_family"] == "kimi"


def test_roles_key_is_delivered_through_settings_defaults():
    """settings_from_ctx only returns keys in DEFAULTS; a role declared only in plugin.yaml
    would reach the code as an empty value and the routing would silently fall back."""

    class Ctx:
        def get_config(self, key, default=None):
            return {"roles": {"reviewer": {"models": [{"model": "k3.1-pro", "model_family": "kimi"}]}}}.get(key, default)

    out = settings.settings_from_ctx(Ctx())
    assert out["roles"] == {"reviewer": {"models": [{"model": "k3.1-pro", "model_family": "kimi"}]}}


def test_model_family_table():
    m = _load()
    cases = {
        "k3.1-pro": "kimi",
        "nous:kimi-k3.1-pro": "kimi",
        "kimi-for-coding": "kimi",
        "kimi-k2-0905": "kimi",
        "k2": "unknown",
        "nous:k2-preview": "unknown",
        "llama-k2-instruct": "unknown",
        "nous:deepseek-v4.1-flash-1017": "deepseek",
        "deepseek/deepseek-v4.1-flash-1017": "deepseek",
        "go:deepseek-v4.1-flash": "deepseek",
        "claude-sonnet-4-5": "unknown",
        "nous:hy4-flash": "unknown",
        "": "unknown",
        None: "unknown",
    }
    assert {model: m.model_family(model) for model in cases} == cases


def test_kimi_authored_routes_to_deepseek():
    m = _load()
    route = m.route_reviewer({}, ["k3.1-pro", "k3.1-pro"])
    assert route["model"] == "nous:deepseek-v4.1-flash-1017"
    assert route["model_family"] == "deepseek"
    assert route["reason"] == "kimi_authored"


def test_deepseek_authored_keeps_kimi():
    m = _load()
    route = m.route_reviewer({}, ["nous:deepseek-v4.1-flash-1017"])
    assert route["model"] == "k3.1-pro"
    assert route["model_family"] == "kimi"
    assert route["reason"] == "default"


def test_both_families_refused():
    m = _load()
    with pytest.raises(m.ReviewRoleError) as caught:
        m.route_reviewer({}, ["k3.1-pro", "nous:deepseek-v4.1-flash-1017"])
    assert caught.value.code == "review_not_independent"


def test_unknown_author_needs_owner():
    m = _load()
    for authors in ([], ["claude-sonnet-4-5"], ["some-agent"]):
        with pytest.raises(m.ReviewRoleError) as caught:
            m.route_reviewer({}, authors)
        assert caught.value.code == "review_not_independent"


def test_configured_unknown_family_refuses():
    """A reviewer the config cannot identify has no eligible family; the route refuses even
    though the PR itself is reviewable."""
    m = _load()
    config = {"roles": {"reviewer": {"models": [{"model": "mystery-model", "model_family": "unknown"}]}}}
    for authors in (["k3.1-pro"], ["nous:deepseek-v4.1-flash-1017"]):
        with pytest.raises(m.ReviewRoleError) as caught:
            m.route_reviewer(config, authors)
        assert caught.value.code == "review_not_independent"


def test_author_families_from_records():
    m = _load()
    assert m.author_families(["k3.1-pro"]) == {"kimi"}
    assert m.author_families(["nous:deepseek-v4.1-flash-1017", "k3.1-pro"]) == {"kimi", "deepseek"}
    assert m.author_families([]) == {"unknown"}
    assert m.author_families(["unknown"]) == {"unknown"}


def test_routed_family_always_differs_from_every_author_family():
    """The final assertion the dispatcher relies on: whatever route is returned, its family
    is absent from the author set."""

    m = _load()
    for authors in (["k3.1-pro"], ["nous:deepseek-v4.1-flash-1017"]):
        route = m.route_reviewer({}, authors)
        assert route["model_family"] not in m.author_families(authors)
