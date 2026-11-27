"""Independent reviewer selection based on recorded author model families."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

DEFAULT_MODEL = "k3.1-pro"
FALLBACK_MODEL = "nous:deepseek-v4.1-flash-1017"
DEFAULT_BUDGET = {"timeout_s": 2700, "max_tool_calls": 120}

__all__ = [
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "DEFAULT_BUDGET",
    "ReviewRoleError",
    "model_family",
    "author_families",
    "reviewer_role",
    "route_reviewer",
]


class ReviewRoleError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


def model_family(model: Any) -> str:
    if not isinstance(model, str):
        return "unknown"
    value = model.strip().lower()
    if not value:
        return "unknown"
    if re.search(r"(?<![a-z0-9])deepseek(?![a-z0-9])", value):
        return "deepseek"
    if re.search(r"(?<![a-z0-9])kimi(?![a-z0-9])", value) or re.search(
        r"(?:^|[^a-z0-9])k\d+(?:\.\d+)*(?:[^a-z0-9]|$)", value
    ):
        return "kimi"
    return "unknown"


def author_families(models: Iterable[Any] | None) -> set[str]:
    families = {model_family(model) for model in models or ()}
    return families or {"unknown"}


def reviewer_role(config: Mapping[str, Any]) -> dict[str, Any]:
    roles = config.get("roles") if isinstance(config, Mapping) else None
    configured = roles.get("reviewer") if isinstance(roles, Mapping) else None
    if not isinstance(configured, Mapping):
        configured = {}
    budget = dict(DEFAULT_BUDGET)
    if isinstance(configured.get("budget"), Mapping):
        budget.update(configured["budget"])
    model = configured.get("model") or DEFAULT_MODEL
    return {
        "model": model,
        "model_family": model_family(model),
        "budget": budget,
        "identity": "rizzstack-reviewer",
        "post_mode": "auto_reject",
    }


def route_reviewer(config: Mapping[str, Any], author_models: Iterable[Any] | None) -> dict[str, Any]:
    families = author_families(author_models)
    if "unknown" in families or len(families) != 1:
        raise ReviewRoleError(
            "review_not_independent",
            "no reviewer is independent of every recorded author model",
            "Route the pull request to the owner for review.",
        )
    role = reviewer_role(config)
    if families == {"kimi"}:
        role["model"] = FALLBACK_MODEL
        role["model_family"] = "deepseek"
        role["reason"] = "kimi_authored"
    else:
        role["reason"] = "default"
    if role["model_family"] in families:
        raise ReviewRoleError(
            "review_not_independent",
            "the configured reviewer shares a model family with an author",
            "Configure an independent reviewer or route the pull request to the owner.",
        )
    return role
