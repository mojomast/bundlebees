"""Risk-tier to process policy.

The tier -> process table in this module is report-only: it is not enforced
at dispatch or by any gate. Enforcement of the path floor and effective tier
lives in ``tools._dispatch`` (with the tier math in ``risk.py``), which refuses
``risk_tier_below_floor`` and requires a frozen ``acdd/1`` contract for high
risk.

A tier is one of exactly ``"low"``, ``"medium"`` or ``"high"``. Any other
value (``None``, an empty string, an unknown string, a non-string) fails
closed to the high policy.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable

HIGH_TIER = "high"


@dataclass(frozen=True, slots=True)
class _Policy:
    tier: str
    phases: tuple[str, ...]
    reviewer: str
    plan: str
    human_gates_required: bool
    min_verifications: int

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "tier": self.tier,
                "phases": list(self.phases),
                "reviewer": self.reviewer,
                "plan": self.plan,
                "human_gates_required": self.human_gates_required,
                "min_verifications": self.min_verifications,
            }
        )


_LOW = _Policy(
    tier="low",
    phases=("red", "green"),
    reviewer="opencode-go",
    plan="short",
    human_gates_required=False,
    min_verifications=1,
)

_MEDIUM = _Policy(
    tier="medium",
    phases=("red", "green", "refactor"),
    reviewer="opencode-go",
    plan="full",
    human_gates_required=False,
    min_verifications=2,
)

_HIGH = _Policy(
    tier=HIGH_TIER,
    phases=("red", "green", "refactor", "integration", "review", "security"),
    reviewer="deepseek-api",
    plan="full+review",
    human_gates_required=True,
    min_verifications=3,
)

_POLICIES = MappingProxyType(
    {"low": _LOW, "medium": _MEDIUM, "high": _HIGH}
)


def _resolve(tier: Any) -> _Policy:
    """Return the policy for *tier*, failing closed to high."""
    if isinstance(tier, str):
        policy = _POLICIES.get(tier)
        if policy is not None:
            return policy
    return _HIGH


def policy_for(tier: Any) -> dict[str, Any]:
    """Return a fresh deep copy of the policy for *tier*.

    Unknown tiers return the high policy. Callers may mutate the result
    without touching the table.
    """
    return _resolve(tier).as_dict()


def missing_phases(tier: Any, recorded_phases: Iterable[Any]) -> list[str]:
    """Return the policy phases, in policy order, absent from the evidence.

    ``recorded_phases`` may be any iterable; non-string items are ignored.
    An unknown tier uses the high policy.
    """
    try:
        recorded = {item for item in recorded_phases if isinstance(item, str)}
    except TypeError:
        recorded = set()

    return [phase for phase in _resolve(tier).phases if phase not in recorded]
