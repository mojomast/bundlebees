"""Reviewer routing for the outside-review role (program: automated outside review, Phase 1.1).

The role lives in the coordinator's config under ``roles.reviewer``. Routing is decided
per pull request from the AUTHOR model families recorded on its commits:

* default reviewer: Kimi (``k3.1-pro``, family ``kimi``);
* if ANY commit on the PR was authored by a Kimi model -> ``nous:deepseek-v4.1-flash-1017``
  (family ``deepseek``);
* an unknown author, or both families on one PR, has no eligible reviewer: the caller must
  route to the owner (``review_not_independent``).

Run: python -m pytest tests/test_review_roles.py -q
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import settings


def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.review_roles")


def test_default_reviewer_is_kimi():
    m = _load()
    role = m.reviewer_role({})
    assert role["model"] == m.DEFAULT_MODEL == "k3.1-pro"
    assert role["m.model_family"] == "kimi"
    assert role["budget"] == m.DEFAULT_BUDGET == {"timeout_s": 2700, "max_tool_calls": 120}
    assert role["identity"] == "rizzstack-reviewer"
    assert role["post_mode"] == "auto_reject"


def test_configured_role_overrides_defaults():
    m = _load()
    role = m.reviewer_role({"roles": {"reviewer": {"model": "nous:hy4-flash", "budget": {"timeout_s": 600}}}})
    assert role["model"] == "nous:hy4-flash"
    assert role["budget"] == {"timeout_s": 600, "max_tool_calls": 120}


def test_roles_key_is_delivered_through_settings_defaults():
    m = _load()
    """settings_from_ctx only returns keys in DEFAULTS; a role declared only in plugin.yaml
    would reach the code as an empty value and the routing would silently fall back."""

    class Ctx:
        def get_config(self, key, default=None):
            return {"roles": {"reviewer": {"model": "k3.1-pro"}}}.get(key, default)

    out = settings.settings_from_ctx(Ctx())
    assert out["roles"] == {"reviewer": {"model": "k3.1-pro"}}


def test_model_family_table():
    m = _load()
    assert m.model_family("k3.1-pro") == "kimi"
    assert m.model_family("nous:kimi-k3.1-pro") == "kimi"
    assert m.model_family("kimi-for-coding") == "kimi"
    assert m.model_family("nous:deepseek-v4.1-flash-1017") == "deepseek"
    assert m.model_family("deepseek/deepseek-v4.1-flash-1017") == "deepseek"
    assert m.model_family("go:deepseek-v4.1-flash") == "deepseek"
    assert m.model_family("claude-sonnet-4-5") == "unknown"
    assert m.model_family("nous:hy4-flash") == "unknown"
    assert m.model_family("") == "unknown"
    assert m.model_family(None) == "unknown"


def test_kimi_authored_routes_to_deepseek():
    m = _load()
    route = m.route_reviewer({}, ["k3.1-pro", "k3.1-pro"])
    assert route["model"] == m.FALLBACK_MODEL == "nous:deepseek-v4.1-flash-1017"
    assert route["m.model_family"] == "deepseek"
    assert route["reason"] == "kimi_authored"


def test_deepseek_authored_keeps_kimi():
    m = _load()
    route = m.route_reviewer({}, ["nous:deepseek-v4.1-flash-1017"])
    assert route["model"] == m.DEFAULT_MODEL
    assert route["m.model_family"] == "kimi"
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


def test_author_families_from_records():
    m = _load()
    assert m.author_families(["k3.1-pro"]) == {"kimi"}
    assert m.author_families(["nous:deepseek-v4.1-flash-1017", "k3.1-pro"]) == {"kimi", "deepseek"}
    assert m.author_families([]) == {"unknown"}
    assert m.author_families(["unknown"]) == {"unknown"}


def test_routed_family_always_differs_from_every_author_family():
    m = _load()
    """The final assertion the dispatcher relies on: whatever route is returned, its family
    is absent from the author set."""

    for authors in (["k3.1-pro"], ["nous:deepseek-v4.1-flash-1017"]):
        route = m.route_reviewer({}, authors)
        assert route["m.model_family"] not in m.author_families(authors)
