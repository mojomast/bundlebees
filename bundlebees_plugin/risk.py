"""Risk tiers for task briefs, derived from the touched paths.

The path-derived floor and the effective tier ARE enforced at dispatch by
``tools._dispatch``: a declared tier below the floor is refused with
``risk_tier_below_floor``, and a high effective tier requires a frozen
``acdd/1`` contract before the brief is accepted. The floor is a lower bound a
task cannot declare away; a human declaration may raise the tier above it.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["TIERS", "derive_floor", "effective_tier"]

TIERS: tuple[str, str, str] = ("low", "medium", "high")
_RANK = {tier: rank for rank, tier in enumerate(TIERS)}

_SENSITIVE_MODULES = frozenset(
    {"security.py", "sandbox.py", "contract_gate.py", "verify_local.py"}
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _invalid() -> dict[str, Any]:
    return {"tier": "high", "reasons": ["invalid or empty path list"]}


def _parts(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]


def _is_absolute(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith("/") or bool(_DRIVE_RE.match(normalized))


def _high_reason(path: str) -> str | None:
    if _is_absolute(path):
        return f"{path}: absolute paths are high risk"
    parts = _parts(path)
    lowered = [part.lower() for part in parts]
    if ".." in parts:
        return f"{path}: path traversal ('..') is high risk"
    if not lowered:
        return None
    name = lowered[-1]
    if name in _SENSITIVE_MODULES:
        return f"{path}: sensitive module {parts[-1]}"
    if name.startswith("auth") or name.startswith("approvals"):
        return f"{path}: authentication or approval code {parts[-1]}"
    if name.endswith("security.mjs"):
        return f"{path}: security module {parts[-1]}"
    if name.endswith(".json") and "schemas" in lowered[:-1]:
        return f"{path}: schema contract under a schemas/ directory"
    for index in range(len(lowered) - 1):
        if lowered[index] == ".github" and lowered[index + 1] == "workflows":
            return f"{path}: CI workflow under .github/workflows/"
    return None


def _is_low(path: str) -> bool:
    parts = _parts(path)
    if not parts:
        return False
    if parts[-1].lower().endswith(".md"):
        return True
    return len(parts) > 1 and parts[0].lower() in ("docs", "tests")


def derive_floor(paths: Any) -> dict[str, Any]:
    """Return the lowest allowed tier for ``paths`` plus the matching reasons."""
    if not isinstance(paths, list) or not paths:
        return _invalid()
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            return _invalid()

    high_reasons: list[str] = []
    medium_paths: list[str] = []
    for path in paths:
        reason = _high_reason(path)
        if reason is not None:
            high_reasons.append(reason)
        elif not _is_low(path):
            medium_paths.append(path)

    if high_reasons:
        return {"tier": "high", "reasons": high_reasons}
    if medium_paths:
        return {
            "tier": "medium",
            "reasons": [
                f"{path}: source path outside docs/ and tests/" for path in medium_paths
            ],
        }
    return {
        "tier": "low",
        "reasons": [f"{path}: documentation or test path" for path in paths],
    }


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
