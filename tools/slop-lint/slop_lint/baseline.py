"""Baseline counts, strict loading and the per-file/rule ratchet."""

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path
from typing import Iterable, Mapping

Result = namedtuple("Result", "new fixed")


class BaselineError(Exception):
    """Raised when a baseline cannot be read, parsed, validated or written."""


def counts(findings: Iterable) -> dict[str, dict[str, int]]:
    """Group findings into ``{path: {rule: count}}``."""
    result: dict[str, dict[str, int]] = {}
    for finding in findings:
        per_rule = result.setdefault(finding.path, {})
        per_rule[finding.rule] = per_rule.get(finding.rule, 0) + 1
    return result


def compare(current: Mapping[str, Mapping[str, int]], baseline: Mapping[str, Mapping[str, int]]) -> Result:
    """Compare current counts to a baseline.

    ``new`` holds ``(path, rule, current, allowed)`` for every file and rule
    above its allowance. ``fixed`` holds ``(path, rule)`` pairs below theirs.
    """
    new = []
    for path in sorted(current):
        for rule in sorted(current[path]):
            found = current[path][rule]
            allowed = baseline.get(path, {}).get(rule, 0)
            if found > allowed:
                new.append((path, rule, found, allowed))
    fixed = []
    for path in sorted(baseline):
        for rule in sorted(baseline[path]):
            allowed = baseline[path][rule]
            if current.get(path, {}).get(rule, 0) < allowed:
                fixed.append((path, rule))
    return Result(new, fixed)


def load(path) -> dict[str, dict[str, int]]:
    """Load and strictly validate a baseline file."""
    location = Path(path)
    try:
        text = location.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"cannot read baseline {location}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise BaselineError(f"baseline {location} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BaselineError(f"baseline {location} is not valid JSON: {exc}") from exc
    return _validated(data, location)


def _validated(data, location: Path) -> dict[str, dict[str, int]]:
    if not isinstance(data, dict):
        raise BaselineError(f"baseline {location} must be a JSON object")
    result: dict[str, dict[str, int]] = {}
    for path, per_rule in data.items():
        if not isinstance(path, str) or not path:
            raise BaselineError(f"baseline {location} has a file key that is not a non-empty string")
        if not isinstance(per_rule, dict):
            raise BaselineError(f"baseline {location}: entry for {path} must be a JSON object")
        clean: dict[str, int] = {}
        for rule, count in per_rule.items():
            if not isinstance(rule, str) or not rule:
                raise BaselineError(f"baseline {location}: entry for {path} has an invalid rule key")
            if isinstance(count, bool) or not isinstance(count, int):
                raise BaselineError(f"baseline {location}: count for {path}: {rule} must be an integer")
            if count < 0:
                raise BaselineError(f"baseline {location}: count for {path}: {rule} must not be negative")
            clean[rule] = count
        result[path] = clean
    return result


def dump(values: Mapping[str, Mapping[str, int]], path) -> None:
    """Write *values* as a deterministic, sorted baseline file."""
    location = Path(path)
    try:
        text = json.dumps(values, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise BaselineError(f"cannot serialise baseline: {exc}") from exc
    try:
        location.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"cannot write baseline {location}: {exc.strerror or exc}") from exc
