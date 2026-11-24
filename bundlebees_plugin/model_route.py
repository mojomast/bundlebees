"""Worker model routing for complex bee slices (docs/contracts/model-route.md).

Pure and deterministic: no network, no credentials, no risk-tier changes.
Complex slices get the launcher's allow-listed Kimi route (``k3.1-pro``,
Kimi's own API with the launcher-held key); everything else stays on
``go:deepseek-v4.1-flash`` (OpenCode Go), except the Nous low-risk lane:
low-risk slices (docs, tests-only, verify) go to the discounted DeepSeek
flash on Nous Portal (``nous:deepseek-v4.1-flash-1017``) and slightly
harder low-risk slices to ``nous:hy4-flash``. The launcher chains
1017 -> hy4 -> go on a Nous failure or 429 and paces all Nous models
combined under the key's 180 requests/min. The coordinator passes the chosen
id to ``bundlebees_dispatch`` as ``model``; the launcher still decides
whether the id is allowed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Iterable, Mapping

from .risk import derive_floor, effective_tier

__all__ = ["DEFAULT_MODEL", "COMPLEX_MODEL", "LOW_RISK_MODEL", "LOW_RISK_HARDER_MODEL",
           "MULTI_FILE_THRESHOLD", "FAILED_ATTEMPTS_THRESHOLD", "NOUS_REQUESTS_PER_MINUTE",
           "NOUS_MAX_ACTIVE", "select_worker_model"]

DEFAULT_MODEL = "go:deepseek-v4.1-flash"
COMPLEX_MODEL = "k3.1-pro"
LOW_RISK_MODEL = "nous:deepseek-v4.1-flash-1017"
LOW_RISK_HARDER_MODEL = "nous:hy4-flash"
LOW_LANE_MAX_FILES = 3          # low tier, at most this many files, first attempt -> LOW_RISK_MODEL
SMALL_REFACTOR_MAX_FILES = 2    # medium tier + complexity "simple" -> LOW_RISK_HARDER_MODEL
# Pacing lives in the launcher (per key, all nous:* models combined); these mirror it for
# coordinators that budget their own fan-out.
NOUS_REQUESTS_PER_MINUTE = 180
NOUS_MAX_ACTIVE = 8
_CHAINS = {LOW_RISK_MODEL: [LOW_RISK_MODEL, LOW_RISK_HARDER_MODEL, "go:deepseek-v4.1-flash"],
           LOW_RISK_HARDER_MODEL: [LOW_RISK_HARDER_MODEL, "go:deepseek-v4.1-flash"]}
MULTI_FILE_THRESHOLD = 5
FAILED_ATTEMPTS_THRESHOLD = 2
_RAILS = re.compile(r"(?<![a-z0-9])rails(?![a-z0-9])", re.IGNORECASE)
_HARNESS = re.compile(r"(?<![a-z0-9])harness(?![a-z0-9])", re.IGNORECASE)


def _attempts(value: Any, where: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where} must be a non-negative int")
    return value


def _files(brief: Mapping[str, Any]) -> list[str]:
    files = brief.get("assigned_files")
    if not isinstance(files, list):
        return []
    return [f for f in files if isinstance(f, str) and f.strip()]


def _c1_high_risk(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    risk = brief.get("risk")
    declared = risk.get("tier") if isinstance(risk, Mapping) else None
    if effective_tier(declared, files)["tier"] == "high":
        return "C1 effective risk tier is high"
    return None


def _c2_multi_file(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    count = len(set(files))
    if count >= MULTI_FILE_THRESHOLD:
        return f"C2 lease names {count} files (>= {MULTI_FILE_THRESHOLD})"
    return None


def _c3_cross_repo(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    repos = brief.get("repos")
    distinct = {r for r in repos if isinstance(r, str) and r.strip()} if isinstance(repos, list) else set()
    if brief.get("cross_repo") is True or len(distinct) >= 2:
        return "C3 slice spans more than one repo"
    return None


def _c4_security_path(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    if files and derive_floor(files)["tier"] == "high":
        return "C4 lease touches a security or approval-gate path"
    return None


def _c5_rails_harness(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    text = " ".join(t for t in (brief.get(k) for k in ("project", "track", "label", "branch", "title"))
                    if isinstance(t, str))
    in_text = bool(_RAILS.search(text) and _HARNESS.search(text))
    in_path = any(part.lower() == "rails" for f in files for part in f.replace("\\", "/").split("/"))
    return "C5 Rails harness work" if in_text or in_path else None


def _c6_failed_twice(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    if failed >= FAILED_ATTEMPTS_THRESHOLD:
        return f"C6 slice already failed {failed} times"
    return None


def _c7_hint(brief: Mapping[str, Any], files: list[str], failed: int) -> str | None:
    return "C7 brief asks for complex routing" if brief.get("complexity") == "complex" else None


_RULES = (_c1_high_risk, _c2_multi_file, _c3_cross_repo, _c4_security_path, _c5_rails_harness,
          _c6_failed_twice, _c7_hint)


def _complex_reasons(brief: Mapping[str, Any], failed: int) -> list[str]:
    files = _files(brief)
    return [r for r in (rule(brief, files, failed) for rule in _RULES) if r]


def _tier(brief: Mapping[str, Any], files: list[str]) -> str:
    risk = brief.get("risk")
    return effective_tier(risk.get("tier") if isinstance(risk, Mapping) else None, files)["tier"]


def _low_tier_lane(count: int, failed: int, hint: Any) -> str:
    first_try_small = count <= LOW_LANE_MAX_FILES and failed == 0 and hint != "moderate"
    return "low" if first_try_small else "low-harder"


def _low_lane(brief: Mapping[str, Any], failed: int) -> str | None:
    """'low', 'low-harder' or None for a brief that is already known not to be complex."""
    if brief.get("nous") is False:
        return None
    files = _files(brief)
    tier, count, hint = _tier(brief, files), len(set(files)), brief.get("complexity")
    if brief.get("verify_of") and tier in ("low", "medium") and failed == 0:
        return "low"
    if tier == "low":
        return _low_tier_lane(count, failed, hint)
    small_refactor = tier == "medium" and hint == "simple" and 0 < count <= SMALL_REFACTOR_MAX_FILES
    return "low-harder" if small_refactor else None


def _lane_result(lane: str, reasons: list[str], allowed_models: Iterable[str] | None,
                 default_model: str) -> dict[str, Any]:
    chain = list(_CHAINS[LOW_RISK_MODEL if lane == "low" else LOW_RISK_HARDER_MODEL])
    fallback = False
    if allowed_models is not None:
        allowed = {m for m in allowed_models if isinstance(m, str)}
        usable = [m for m in chain if m in allowed] or [default_model]
        if usable[0] != chain[0]:
            reasons = reasons + [f"{chain[0]} is not allow-listed on the launcher; using {usable[0]}"]
            chain, fallback = usable, True
    return {"model": chain[0], "complex": False, "reasons": reasons, "fallback": fallback,
            "source": "rule", "lane": lane, "chain": chain}


def _pinned(brief: Mapping[str, Any], is_complex: bool, reasons: list[str]) -> dict[str, Any]:
    pinned = brief.get("model")
    if not isinstance(pinned, str) or not pinned.strip():
        raise ValueError("model_pinned needs a non-empty brief.model")
    return {"model": pinned.strip(), "complex": is_complex, "reasons": reasons,
            "fallback": False, "source": "pinned", "lane": "pinned", "chain": [pinned.strip()]}


def select_worker_model(brief: Any, *, failed_attempts: int | None = None,
                        allowed_models: Iterable[str] | None = None,
                        complex_model: str = COMPLEX_MODEL,
                        default_model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Return ``{model, complex, reasons, fallback, source, lane, chain}`` for one brief."""
    if not isinstance(brief, Mapping):
        raise ValueError("brief must be a JSON object")
    failed = max(_attempts(failed_attempts, "failed_attempts"),
                 _attempts(brief.get("failed_attempts"), "brief.failed_attempts"))
    reasons = _complex_reasons(brief, failed)
    is_complex = bool(reasons)
    if brief.get("model_pinned") is True:
        return _pinned(brief, is_complex, reasons)
    lane = "complex" if is_complex else (_low_lane(brief, failed) or "default")
    if lane in ("low", "low-harder"):
        return _lane_result(lane, reasons, allowed_models, default_model)
    model, fallback = (complex_model if is_complex else default_model), False
    if is_complex and allowed_models is not None:
        allowed = [m for m in allowed_models if isinstance(m, str)]
        if complex_model not in allowed:
            model, fallback = default_model, True
            reasons = reasons + [f"{complex_model} is not allow-listed on the launcher; "
                                 f"using {default_model}"]
    return {"model": model, "complex": is_complex, "reasons": reasons,
            "fallback": fallback, "source": "rule", "lane": lane, "chain": [model]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bundlebees_plugin.model_route",
                                 description="Pick the worker model for one brief (prints JSON).")
    ap.add_argument("brief")
    ap.add_argument("--failed-attempts", type=int, default=None)
    ap.add_argument("--allowed", default=None, help="comma-separated launcher allow-list")
    ns = ap.parse_args(argv)
    try:
        with open(ns.brief, encoding="utf-8") as fh:
            data = json.load(fh)
        allowed = [m.strip() for m in ns.allowed.split(",")] if ns.allowed else None
        out = select_worker_model(data, failed_attempts=ns.failed_attempts, allowed_models=allowed)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)[:200]}), file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
