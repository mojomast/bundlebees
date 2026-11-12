"""Minimal JSON Schema validation and schema discovery for ACDD.

The validator implements only the Draft 2020-12 keywords used by the shared
``acdd/1`` contract and ``tdd-evidence/1`` evidence schemas, so the tool stays
standard-library only and can be copied into any repository.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

CONTRACT_NAMES = ("acdd-contract.schema.json", "contract.schema.json")
EVIDENCE_NAMES = ("tdd-evidence.schema.json", "evidence.schema.json")


class LintError(Exception):
    """A usage or configuration error that prevents linting."""


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return type(left) is type(right) and left == right


def _shown(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _key_path(path: str, key: str) -> str:
    if key.isidentifier():
        return f"{path}.{key}"
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'{path}["{escaped}"]'


def _expected_names(expected: Sequence[str]) -> str:
    if len(expected) == 1:
        return expected[0]
    if len(expected) == 2:
        return f"{expected[0]} or {expected[1]}"
    return ", ".join(expected[:-1]) + f" or {expected[-1]}"


def validate(instance: Any, schema: Any, path: str = "$") -> list[tuple[str, str]]:
    """Validate *instance* against *schema* and return (path, message) pairs."""
    if not isinstance(schema, Mapping):
        return []

    expected = schema.get("type")
    if expected is not None:
        names = (
            [item for item in expected if isinstance(item, str)]
            if isinstance(expected, list)
            else [expected] if isinstance(expected, str) else []
        )
        if names and not any(_type_matches(instance, name) for name in names):
            return [
                (
                    path,
                    f"expected {_expected_names(names)}, got {_type_name(instance)}",
                )
            ]

    errors: list[tuple[str, str]] = []

    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append((path, f"must equal {_shown(schema['const'])}"))

    allowed = schema.get("enum")
    if isinstance(allowed, list) and not any(
        _json_equal(instance, item) for item in allowed
    ):
        options = ", ".join(_shown(item) for item in allowed)
        errors.append((path, f"must be one of: {options}"))

    if isinstance(instance, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(instance) < minimum_length:
            errors.append((path, f"shorter than {minimum_length} characters"))
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(instance) > maximum_length:
            errors.append((path, f"longer than {maximum_length} characters"))
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            errors.append((path, f"does not match pattern {pattern!r}"))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and instance < minimum:
            errors.append((path, f"must be at least {minimum}"))

    if isinstance(instance, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(instance) < minimum_items:
            errors.append((path, f"needs at least {minimum_items} items"))
        if schema.get("uniqueItems") is True:
            seen: list[Any] = []
            for index, item in enumerate(instance):
                if any(_json_equal(item, earlier) for earlier in seen):
                    errors.append((f"{path}[{index}]", "duplicate item"))
                seen.append(item)
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(instance):
                errors.extend(validate(item, items, f"{path}[{index}]"))

    if isinstance(instance, Mapping):
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in instance:
                    errors.append((path, f"missing required property {name!r}"))

        properties = schema.get("properties")
        known = set(properties) if isinstance(properties, Mapping) else set()
        if isinstance(properties, Mapping):
            for key, child in properties.items():
                if key in instance:
                    errors.extend(validate(instance[key], child, _key_path(path, key)))

        property_names = schema.get("propertyNames")
        if isinstance(property_names, Mapping):
            for key in instance:
                errors.extend(
                    validate(str(key), property_names, _key_path(path, str(key)))
                )

        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            extras = [key for key in instance if key not in known]
            if additional is False:
                for key in extras:
                    errors.append(
                        (_key_path(path, str(key)), f"unexpected property {key!r}")
                    )
            elif isinstance(additional, Mapping):
                for key in extras:
                    errors.extend(
                        validate(
                            instance[key], additional, _key_path(path, str(key))
                        )
                    )

    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        valid = any(
            not validate(instance, child, path)
            for child in alternatives
            if isinstance(child, Mapping)
        )
        if not valid:
            errors.append((path, "does not match any allowed shape"))

    return errors


def _first_existing(directory: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LintError(f"cannot read schema {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise LintError(f"schema {path.name} is not a JSON object")
    return data


def load_schemas(directory: Path) -> tuple[dict, dict]:
    """Load the contract and evidence schemas from *directory*."""
    directory = Path(directory)
    contract_path = _first_existing(directory, CONTRACT_NAMES)
    evidence_path = _first_existing(directory, EVIDENCE_NAMES)
    missing = []
    if contract_path is None:
        missing.append("contract")
    if evidence_path is None:
        missing.append("evidence")
    if missing:
        raise LintError(
            f"missing {' and '.join(missing)} schema in {directory}"
        )
    return _load_json(contract_path), _load_json(evidence_path)


def _has_schema_pair(directory: Path) -> bool:
    return (
        _first_existing(directory, CONTRACT_NAMES) is not None
        and _first_existing(directory, EVIDENCE_NAMES) is not None
    )


def find_schema_dir(start: Path) -> Path | None:
    """Return the nearest directory holding both shared schemas, or None."""
    current = Path(start)
    for parent in [current, *current.parents]:
        for candidate in (parent / "schemas", parent / "bundlebees_plugin" / "schemas"):
            if _has_schema_pair(candidate):
                return candidate
    return None
