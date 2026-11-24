"""Side-effect-free dispatch preflight for dispatchers outside Hermes.

Bees dispatched straight to the launcher bypass the risk-tier, contract and
research gates in :func:`bundlebees_plugin.tools._dispatch`. This module runs
the exact same gate sequence and reports the verdict, without building a
client, opening a socket or writing into the state directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from . import tools

_FIX_INVALID = (
    "Pass a JSON object with objective, a full 40- or 64-character hex "
    "source_rev, non-empty repository-relative assigned_files, and acceptance "
    "criteria or commands."
)
_FIX_TIER = (
    "Add a risk block declaring an explicit tier: {\"risk\": {\"tier\": "
    "\"low\" | \"medium\" | \"high\"}}. A box-level model tier is not a risk tier."
)
_FIX_UNREADABLE = (
    "Pass the path to a readable JSON brief file; nothing was dispatched."
)


def _refusal(code: str, error: str, fix: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, "fix": fix}


def _parse(brief: Any) -> dict[str, Any] | None:
    if isinstance(brief, str):
        try:
            parsed = json.loads(brief)
        except json.JSONDecodeError:
            return None
    else:
        parsed = brief
    return parsed if isinstance(parsed, dict) else None


def _declares_tier(brief: Mapping[str, Any]) -> bool:
    risk = brief.get("risk")
    if not isinstance(risk, Mapping):
        return False
    tier = risk.get("tier")
    return isinstance(tier, str) and bool(tier.strip())


def preflight(brief: Any, state_dir: Any = None) -> dict[str, Any]:
    """Run the dispatch gates for *brief* and report the verdict.

    *brief* may be a dict or a JSON object string. A box-shaped brief keeps its
    extra keys and is gated exactly like the bare brief. Refusals return
    ``{ok: false, code, error, fix}``; success returns
    ``{ok: true, tier, floor, reasons, acdd_mode}``.
    """
    parsed = _parse(brief)
    if parsed is None:
        return _refusal(
            "brief_invalid",
            "brief must be a JSON object or a JSON object string",
            _FIX_INVALID,
        )

    if not _declares_tier(parsed):
        return _refusal(
            "risk_tier_missing",
            "brief declares no explicit risk.tier; preflight requires one",
            _FIX_TIER,
        )

    gated = tools._gate({"brief": parsed}, state_dir)
    if isinstance(gated, str):
        envelope = json.loads(gated)
        return _refusal(
            str(envelope.get("code") or "brief_invalid"),
            str(envelope.get("error") or "the dispatch gates refused this brief"),
            str(envelope.get("fix") or _FIX_INVALID),
        )

    _brief, acdd, assessed = gated
    reasons = assessed.get("reasons")
    return {
        "ok": True,
        "tier": assessed.get("tier"),
        "floor": assessed.get("floor"),
        "reasons": list(reasons) if isinstance(reasons, list) else [],
        "acdd_mode": acdd.get("mode"),
    }


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bundlebees_plugin.preflight",
        description="Run the bundlebees dispatch gates without dispatching.",
    )
    parser.add_argument("brief", help="Path to a JSON brief file.")
    parser.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    args = parser.parse_args(argv)

    try:
        with open(args.brief, encoding="utf-8") as handle:
            raw = handle.read()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps(_refusal(
            "brief_unreadable",
            f"brief file could not be read as JSON: {exc}",
            _FIX_UNREADABLE,
        )))
        return 2

    result = preflight(payload, args.state_dir)
    print(json.dumps(result))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
