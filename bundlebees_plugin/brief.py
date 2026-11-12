"""Strict brief contract for dispatch.

Threat model: a brief is model-generated input, so it is untrusted. A loose
brief can widen a worker's file lease, point it at an arbitrary absolute path,
or start work from a moving branch/tag. This module validates the bundled
JSON Schema subset and then applies code checks that JSON Schema alone cannot
express: repository-relative file leases, lower-case full commit ids, and a
normalised acceptance shape. Validation runs before any client call, returns
a fresh mapping, and never mutates the caller's input. Error text is scrubbed
before it becomes a tool envelope.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .security import scrub

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "brief.schema.json"
_FIX = (
    "Pass an object with objective, a full 40- or 64-character hex source_rev, "
    "non-empty repository-relative assigned_files, and acceptance criteria or "
    "commands."
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_SOURCE_REV_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class BriefError(ValueError):
    """A rejected brief with the tool-envelope code, error, and fix."""

    code = "brief_invalid"

    def __init__(self, error: str, fix: str = _FIX) -> None:
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        super().__init__(self.error)


def load_schema() -> dict[str, Any]:
    """Load the bundled draft 2020-12 brief schema."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_value(value: Any, spec: Mapping[str, Any], path: str) -> str | None:
    expected = spec.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, item) for item in types):
            return f"{path} has the wrong type"
    if "enum" in spec:
        allowed = spec.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            shown = ", ".join(repr(item) for item in allowed)
            return f"{path} must be one of: {shown}"
    if isinstance(value, str):
        minimum_length = spec.get("minLength")
        if isinstance(minimum_length, int) and len(value.strip()) < minimum_length:
            return f"{path} is too short"
        pattern = spec.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return f"{path} does not match the required pattern"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = spec.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return f"{path} must be at least {minimum}"
        maximum = spec.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            return f"{path} must be at most {maximum}"
    if isinstance(value, list):
        minimum_items = spec.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            return f"{path} needs at least {minimum_items} item(s)"
        item_spec = spec.get("items")
        if isinstance(item_spec, Mapping):
            for index, item in enumerate(value):
                problem = _validate_value(item, item_spec, f"{path}[{index}]")
                if problem:
                    return problem
    if isinstance(value, Mapping):
        for required in spec.get("required", []):
            if required not in value:
                return f"{path} is missing required field {required!r}"
        for key, child_spec in (spec.get("properties") or {}).items():
            if key in value and isinstance(child_spec, Mapping):
                problem = _validate_value(value[key], child_spec, f"{path}.{key}")
                if problem:
                    return problem
    alternatives = spec.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if not any(
            _validate_value(value, child, path) is None
            for child in alternatives
            if isinstance(child, Mapping)
        ):
            return f"{path} does not match any allowed shape"
    return None


def _normalise_path(value: Any, index: int) -> str:
    field = f"brief.assigned_files[{index}]"
    if not isinstance(value, str):
        raise BriefError(f"{field} must be a string")
    if "\x00" in value:
        raise BriefError(f"{field} contains a NUL byte")
    text = value.replace("\\", "/").strip()
    if not text:
        raise BriefError(f"{field} must not be empty")
    if text.startswith("/") or _DRIVE_RE.match(text):
        raise BriefError(f"{field} must be repository-relative")
    while text.startswith("./"):
        text = text[2:]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise BriefError(f"{field} must not contain '..'")
    if any(part.lower() == ".git" for part in parts):
        raise BriefError(f"{field} must not be under .git")
    if not parts:
        raise BriefError(f"{field} must not be empty")
    return "/".join(parts)


def _normalise_assigned_files(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise BriefError("brief.assigned_files must be an array")
    files: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        path = _normalise_path(item, index)
        if path not in seen:
            seen.add(path)
            files.append(path)
    return files


def _string_list(value: Any) -> list[str]:
    return list(value) if isinstance(value, list) else []


def _normalise_acceptance(value: Any) -> dict[str, list[str]]:
    if isinstance(value, list):
        return {"criteria": list(value), "commands": [], "tests": []}
    if isinstance(value, Mapping):
        return {
            "criteria": _string_list(value.get("criteria")),
            "commands": _string_list(value.get("commands")),
            "tests": _string_list(value.get("tests")),
        }
    raise BriefError("brief.acceptance must be an array or object")


def validate_brief(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalise *brief*, returning a fresh mapping.

    Raises :class:`BriefError` with a message naming the offending field.
    The input mapping and its nested lists are never mutated.
    """
    if not isinstance(brief, Mapping):
        raise BriefError("brief must be an object")
    normalised = dict(brief)

    source_rev = normalised.get("source_rev")
    if isinstance(source_rev, str):
        normalised["source_rev"] = source_rev.lower()

    try:
        schema = load_schema()
    except (OSError, json.JSONDecodeError) as exc:
        raise BriefError("brief schema is unavailable") from exc

    problem = _validate_value(normalised, schema, "brief")
    if problem:
        raise BriefError(problem)

    if isinstance(normalised.get("source_rev"), str):
        if _SOURCE_REV_RE.match(normalised["source_rev"]) is None:
            raise BriefError(
                "brief.source_rev must be a full 40- or 64-character hex commit id"
            )

    normalised["assigned_files"] = _normalise_assigned_files(
        normalised.get("assigned_files")
    )
    normalised["acceptance"] = _normalise_acceptance(normalised.get("acceptance"))
    return normalised


def brief_sha256(brief: Mapping[str, Any]) -> str:
    """Hash a brief deterministically as compact, key-sorted JSON."""
    body = json.dumps(dict(brief), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
