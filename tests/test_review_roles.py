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
from bundlebees_plugin.review_roles import (
    DEFAULT_BUDGET,
    DEFAULT_MODEL,
    FALLBACK_MODEL,
    ReviewRoleError,
    author_families,
    model_family,
    reviewer_role,
    route_reviewer,
)


def test_default_reviewer_is_kimi():
    role = reviewer_role({})
    assert role["model"] == DEFAULT_MODEL == "k3.1-pro"
    assert role["model_family"] == "kimi"
    assert role["budget"] == DEFAULT_BUDGET == {"timeout_s": 2700, "max_tool_calls": 120}
    assert role["identity"] == "rizzstack-reviewer"
    assert role["post_mode"] == "auto_reject"


def test_configured_role_overrides_defaults():
    role = reviewer_role({"roles": {"reviewer": {"model": "nous:hy4-flash", "budget": {"timeout_s": 600}}}})
    assert role["model"] == "nous:hy4-flash"
    assert role["budget"] == {"timeout_s": 600, "max_tool_calls": 120}


def test_roles_key_is_delivered_through_settings_defaults():
    """settings_from_ctx only returns keys in DEFAULTS; a role declared only in plugin.yaml
    would reach the code as an empty value and the routing would silently fall back."""

    class Ctx:
        def get_config(self, key, default=None):
            return {"roles": {"reviewer": {"model": "k3.1-pro"}}}.get(key, default)

    out = settings.settings_from_ctx(Ctx())
    assert out["roles"] == {"reviewer": {"model": "k3.1-pro"}}


def test_model_family_table():
    assert model_family("k3.1-pro") == "kimi"
    assert model_family("nous:kimi-k3.1-pro") == "kimi"
    assert model_family("kimi-for-coding") == "kimi"
    assert model_family("nous:deepseek-v4.1-flash-1017") == "deepseek"
    assert model_family("deepseek/deepseek-v4.1-flash-1017") == "deepseek"
    assert model_family("go:deepseek-v4.1-flash") == "deepseek"
    assert model_family("claude-sonnet-4-5") == "unknown"
    assert model_family("nous:hy4-flash") == "unknown"
    assert model_family("") == "unknown"
    assert model_family(None) == "unknown"


def test_kimi_authored_routes_to_deepseek():
    route = route_reviewer({}, ["k3.1-pro", "k3.1-pro"])
    assert route["model"] == FALLBACK_MODEL == "nous:deepseek-v4.1-flash-1017"
    assert route["model_family"] == "deepseek"
    assert route["reason"] == "kimi_authored"


def test_deepseek_authored_keeps_kimi():
    route = route_reviewer({}, ["nous:deepseek-v4.1-flash-1017"])
    assert route["model"] == DEFAULT_MODEL
    assert route["model_family"] == "kimi"
    assert route["reason"] == "default"


def test_both_families_refused():
    with pytest.raises(ReviewRoleError) as caught:
        route_reviewer({}, ["k3.1-pro", "nous:deepseek-v4.1-flash-1017"])
    assert caught.value.code == "review_not_independent"


def test_unknown_author_needs_owner():
    for authors in ([], ["claude-sonnet-4-5"], ["some-agent"]):
        with pytest.raises(ReviewRoleError) as caught:
            route_reviewer({}, authors)
        assert caught.value.code == "review_not_independent"


def test_author_families_from_records():
    assert author_families(["k3.1-pro"]) == {"kimi"}
    assert author_families(["nous:deepseek-v4.1-flash-1017", "k3.1-pro"]) == {"kimi", "deepseek"}
    assert author_families([]) == {"unknown"}
    assert author_families(["unknown"]) == {"unknown"}


def test_routed_family_always_differs_from_every_author_family():
    """The final assertion the dispatcher relies on: whatever route is returned, its family
    is absent from the author set."""

    for authors in (["k3.1-pro"], ["nous:deepseek-v4.1-flash-1017"]):
        route = route_reviewer({}, authors)
        assert route["model_family"] not in author_families(authors)
