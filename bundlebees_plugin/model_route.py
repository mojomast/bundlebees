"""Worker model routing for complex bee slices (docs/contracts/model-route.md).

Pure and deterministic: no network, no credentials, no risk-tier changes.
Complex slices get the launcher's allow-listed Kimi route (``k3.1-pro``,
Kimi's own API with the launcher-held key); everything else stays on
``go:deepseek-v4.1-flash`` (OpenCode Go). The coordinator passes the chosen
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

__all__ = ["DEFAULT_MODEL", "COMPLEX_MODEL", "MULTI_FILE_THRESHOLD", "FAILED_ATTEMPTS_THRESHOLD",
           "select_worker_model"]

DEFAULT_MODEL = "go:deepseek-v4.1-flash"
COMPLEX_MODEL = "k3.1-pro"
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


def _complex_reasons(brief: Mapping[str, Any], failed: int) -> list[str]:
    reasons: list[str] = []
    files = _files(brief)
    risk = brief.get("risk")
    declared = risk.get("tier") if isinstance(risk, Mapping) else None
    eff = effective_tier(declared, files)
    if eff["tier"] == "high":
        reasons.append("C1 effective risk tier is high")
    if len(set(files)) >= MULTI_FILE_THRESHOLD:
        reasons.append(f"C2 lease names {len(set(files))} files (>= {MULTI_FILE_THRESHOLD})")
    repos = brief.get("repos")
    distinct = {r for r in repos if isinstance(r, str) and r.strip()} if isinstance(repos, list) else set()
    if brief.get("cross_repo") is True or len(distinct) >= 2:
        reasons.append("C3 slice spans more than one repo")
    if files:
        floor = derive_floor(files)
        if floor["tier"] == "high":
            reasons.append("C4 lease touches a security or approval-gate path")
    text = " ".join(t for t in (brief.get(k) for k in ("project", "track", "label", "branch", "title"))
                    if isinstance(t, str))
    if (_RAILS.search(text) and _HARNESS.search(text)) or any(
            part.lower() == "rails" for f in files for part in f.replace("\\", "/").split("/")):
        reasons.append("C5 Rails harness work")
    if failed >= FAILED_ATTEMPTS_THRESHOLD:
        reasons.append(f"C6 slice already failed {failed} times")
    if brief.get("complexity") == "complex":
        reasons.append("C7 brief asks for complex routing")
    return reasons


def select_worker_model(brief: Any, *, failed_attempts: int | None = None,
                        allowed_models: Iterable[str] | None = None,
                        complex_model: str = COMPLEX_MODEL,
                        default_model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Return ``{model, complex, reasons, fallback, source}`` for one brief."""
    if not isinstance(brief, Mapping):
        raise ValueError("brief must be a JSON object")
    failed = max(_attempts(failed_attempts, "failed_attempts"),
                 _attempts(brief.get("failed_attempts"), "brief.failed_attempts"))
    reasons = _complex_reasons(brief, failed)
    is_complex = bool(reasons)
    if brief.get("model_pinned") is True:
        pinned = brief.get("model")
        if not isinstance(pinned, str) or not pinned.strip():
            raise ValueError("model_pinned needs a non-empty brief.model")
        return {"model": pinned.strip(), "complex": is_complex, "reasons": reasons,
                "fallback": False, "source": "pinned"}
    model, fallback = (complex_model if is_complex else default_model), False
    if is_complex and allowed_models is not None:
        allowed = [m for m in allowed_models if isinstance(m, str)]
        if complex_model not in allowed:
            model, fallback = default_model, True
            reasons = reasons + [f"{complex_model} is not allow-listed on the launcher; "
                                 f"using {default_model}"]
    return {"model": model, "complex": is_complex, "reasons": reasons,
            "fallback": fallback, "source": "rule"}


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
