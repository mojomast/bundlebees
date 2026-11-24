"""Risk tiers for task briefs, derived from the touched paths.

The path-derived floor and the effective tier ARE enforced at dispatch by
``tools._dispatch``: a declared tier below the floor is refused with
``risk_tier_below_floor``, and a high effective tier requires a frozen
``acdd/1`` contract before the brief is accepted. The floor is a lower bound a
task cannot declare away; a human declaration may raise the tier above it.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

__all__ = ["TIERS", "derive_floor", "effective_tier"]

TIERS: tuple[str, str, str] = ("low", "medium", "high")
_RANK = {tier: rank for rank, tier in enumerate(TIERS)}

_POLICY_SCHEMA = "bundlebees.risk-floor/1"
_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy" / "risk-floor.json"
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SELECTORS = ("absolute", "traversal", "basename", "pattern")


def _invalid() -> dict[str, Any]:
    return {"tier": "high", "reasons": ["invalid or empty path list"]}


def _parts(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]


def _is_absolute(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("/") or bool(_DRIVE_RE.match(normalized))


def _policy_failure() -> dict[str, Any]:
    return {
        "tier": "high",
        "reasons": ["risk floor policy is missing, unreadable, or invalid; failing closed to high"],
    }


def _policy_rules(data: dict[str, Any], key: str, *, required: bool) -> list[dict[str, Any]]:
    if key not in data:
        if required:
            raise ValueError(f"policy is missing {key} rules")
        return []
    rules = data[key]
    if not isinstance(rules, list):
        raise ValueError(f"policy {key} rules must be a list")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(f"policy {key} rule must be an object")
        reason = rule.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"policy {key} rule needs a reason")
        selectors = [selector for selector in _SELECTORS if selector in rule]
        if len(selectors) != 1:
            raise ValueError(f"policy {key} rule needs exactly one selector")
        selector = selectors[0]
        value = rule[selector]
        if selector in ("absolute", "traversal"):
            if value is not True:
                raise ValueError(f"policy {selector} selector must be true")
        elif not isinstance(value, str) or not value:
            raise ValueError(f"policy {selector} selector must be a non-empty string")
    return rules


def _load_policy(policy_path: Any) -> dict[str, Any]:
    path = _DEFAULT_POLICY_PATH if policy_path is None else Path(policy_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != _POLICY_SCHEMA:
        raise ValueError("unsupported risk floor policy")
    default = data.get("default", "medium")
    if default not in TIERS:
        raise ValueError("invalid risk floor policy default")
    return {
        "default": default,
        "high": _policy_rules(data, "high", required=True),
        "low": _policy_rules(data, "low", required=False),
    }


def _pattern_matches(normalized: str, name: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/").lower()
    if "/" not in pattern:
        return fnmatch.fnmatchcase(name, pattern)
    if fnmatch.fnmatchcase(normalized, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(normalized, pattern[3:])


def _matching_reason(path: str, rules: list[dict[str, Any]]) -> str | None:
    parts = _parts(path)
    lowered = [part.lower() for part in parts]
    normalized = "/".join(lowered)
    name = lowered[-1] if lowered else ""
    for rule in rules:
        matched = False
        if rule.get("absolute") is True:
            matched = _is_absolute(path)
        elif rule.get("traversal") is True:
            matched = ".." in parts
        elif "basename" in rule:
            matched = name == rule["basename"].lower()
        elif "pattern" in rule:
            matched = _pattern_matches(normalized, name, rule["pattern"])
        if matched:
            return f"{path}: {rule['reason']}"
    return None


def _high_reason(path: str, rules: list[dict[str, Any]]) -> str | None:
    if _is_absolute(path):
        return f"{path}: absolute paths are high risk"
    if ".." in _parts(path):
        return f"{path}: path traversal ('..') is high risk"
    return _matching_reason(path, rules)


def derive_floor(paths: Any, policy_path: Any = None) -> dict[str, Any]:
    """Return the lowest allowed tier for ``paths`` plus the matching reasons."""
    if not isinstance(paths, list) or not paths:
        return _invalid()
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            return _invalid()

    try:
        policy = _load_policy(policy_path)
    except (OSError, TypeError, ValueError):
        return _policy_failure()

    high_reasons: list[str] = []
    low_reasons: list[str] = []
    unmatched_paths: list[str] = []
    for path in paths:
        reason = _high_reason(path, policy["high"])
        if reason is not None:
            high_reasons.append(reason)
            continue
        reason = _matching_reason(path, policy["low"])
        if reason is not None:
            low_reasons.append(reason)
        else:
            unmatched_paths.append(path)

    if high_reasons:
        return {"tier": "high", "reasons": high_reasons}
    if not unmatched_paths:
        return {"tier": "low", "reasons": low_reasons}

    default = policy["default"]
    default_reasons = []
    for path in unmatched_paths:
        if default == "medium":
            reason = f"{path}: source path outside docs/ and tests/"
        else:
            reason = f"{path}: policy default tier {default!r}"
        default_reasons.append(reason)
    return {"tier": default, "reasons": low_reasons + default_reasons}


def effective_tier(declared: Any, paths: Any) -> dict[str, Any]:
    """Combine a human declaration with the path floor, never lowering the floor."""
    floor = derive_floor(paths)
    floor_tier = floor["tier"]
    reasons = list(floor["reasons"])
    result: dict[str, Any] = {
        "tier": floor_tier,
        "floor": floor_tier,
        "declared": declared,
        "raised": False,
        "reasons": reasons,
    }
    if declared is None:
        return result
    if not isinstance(declared, str) or declared not in TIERS:
        result["tier"] = "high"
        reasons.append(f"invalid declared tier {declared!r}; failing closed to high")
        return result

    declared_rank = _RANK[declared]
    if declared_rank < _RANK[floor_tier]:
        result["tier"] = floor_tier
        result["raised"] = True
        reasons.append(f"declared tier {declared!r} raised to path floor {floor_tier!r}")
    elif declared_rank > _RANK[floor_tier]:
        result["tier"] = declared
    return result
