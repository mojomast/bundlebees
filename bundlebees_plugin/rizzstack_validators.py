"""Vendored JSON Schema (draft 2020-12) validator for the rizzstack worker contracts.

Standard library only, by contract: bundlebees and hyvv copy this module, so it
must start under ``python3 -I -S`` with no installed ``jsonschema``. The keyword
set is deliberately small and closed -- the frozen ``schemas/rizzstack-brief``
and ``schemas/rizzstack-attempt`` documents use exactly these keywords -- and the
verdict is driven by the schema files the caller hands in, never by a
hand-copied checklist.

An empty list means the document is valid; every entry is one problem (a
JSON-pointer-ish path plus a message). ``schema_dir`` defaults to the repository
``schemas/`` directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_array_equal(left: list[Any], right: list[Any]) -> bool:
    if len(left) != len(right):
        return False
    return all(_json_equal(a, b) for a, b in zip(left, right))


def _json_object_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.keys() != right.keys():
        return False
    return all(_json_equal(left[key], right[key]) for key in left)


#: One comparator per JSON class; a mismatched class is never compared.
_JSON_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "boolean": lambda left, right: type(left) is type(right) and left == right,
    "null": lambda left, right: True,
    "number": lambda left, right: left == right,
    "string": lambda left, right: left == right,
    "array": _json_array_equal,
    "object": _json_object_equal,
}

#: Ordered probes; the first match names the JSON class of a value.
_JSON_CLASSES: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    ("boolean", lambda value: isinstance(value, bool)),
    ("null", lambda value: value is None),
    ("number", lambda value: isinstance(value, (int, float))),
    ("string", lambda value: isinstance(value, str)),
    ("array", lambda value: isinstance(value, list)),
    ("object", lambda value: isinstance(value, dict)),
)

#: Type names the frozen schemas may declare, mapped to their predicate.
_TYPE_TESTS: dict[str, Callable[[Any], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _json_class(value: Any) -> str:
    for name, test in _JSON_CLASSES:
        if test(value):
            return name
    return "other"


def _type_matches(value: Any, declared: str) -> bool:
    test = _TYPE_TESTS.get(declared)
    if test is None:
        raise ValueError(f"unsupported JSON Schema type {declared!r}")
    return test(value)


def _json_equal(left: Any, right: Any) -> bool:
    kind = _json_class(left)
    if kind != _json_class(right):
        return False
    comparator = _JSON_COMPARATORS.get(kind)
    if comparator is None:
        return False
    return comparator(left, right)


def _check_type(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    if "type" not in schema:
        return
    declared = schema["type"]
    names = declared if isinstance(declared, list) else [declared]
    if not any(_type_matches(instance, name) for name in names):
        problems.append(f"{path}: expected type {declared!r}")


def _check_literals(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    if "const" in schema and not _json_equal(instance, schema["const"]):
        problems.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and not any(
        _json_equal(instance, option) for option in schema["enum"]
    ):
        problems.append(f"{path}: value {instance!r} is not in the allowed enum")


def _check_string(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    if not isinstance(instance, str):
        return
    if "pattern" in schema and re.search(schema["pattern"], instance) is None:
        problems.append(f"{path}: does not match pattern {schema['pattern']!r}")
    if "minLength" in schema and len(instance) < schema["minLength"]:
        problems.append(f"{path}: length is below minLength {schema['minLength']}")
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        problems.append(f"{path}: length exceeds maxLength {schema['maxLength']}")


def _check_number(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    if not isinstance(instance, (int, float)) or isinstance(instance, bool):
        return
    if "minimum" in schema and instance < schema["minimum"]:
        problems.append(f"{path}: value is below minimum {schema['minimum']}")
    if "maximum" in schema and instance > schema["maximum"]:
        problems.append(f"{path}: value exceeds maximum {schema['maximum']}")


def _check_applicators(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    for branch in schema.get("allOf", []):
        _check(instance, branch, path, problems)
    if "if" not in schema:
        return
    condition_problems: list[str] = []
    _check(instance, schema["if"], path, condition_problems)
    if not condition_problems and "then" in schema:
        _check(instance, schema["then"], path, problems)
    elif condition_problems and "else" in schema:
        _check(instance, schema["else"], path, problems)


def _check_property(
    key: str,
    value: Any,
    properties: dict[str, Any],
    schema: dict[str, Any],
    path: str,
    problems: list[str],
) -> None:
    if key in properties:
        _check(value, properties[key], f"{path}.{key}", problems)
        return
    if "additionalProperties" not in schema:
        return
    additional = schema["additionalProperties"]
    if additional is False:
        problems.append(f"{path}: unexpected property {key!r}")
    elif isinstance(additional, dict):
        _check(value, additional, f"{path}.{key}", problems)


def _check_object(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    if not isinstance(instance, dict):
        return
    for key in schema.get("required", []):
        if key not in instance:
            problems.append(f"{path}: missing required property {key!r}")
    properties = schema.get("properties", {})
    for key, value in instance.items():
        _check_property(key, value, properties, schema, path, problems)


def _check_unique_items(instance: list[Any], path: str, problems: list[str]) -> None:
    for index, item in enumerate(instance):
        if any(_json_equal(item, previous) for previous in instance[:index]):
            problems.append(f"{path}: duplicate item at index {index}")


def _check_array(
    instance: Any, schema: dict[str, Any], path: str, problems: list[str]
) -> None:
    if not isinstance(instance, list):
        return
    if "minItems" in schema and len(instance) < schema["minItems"]:
        problems.append(f"{path}: fewer than minItems {schema['minItems']}")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        problems.append(f"{path}: more than maxItems {schema['maxItems']}")
    if "items" in schema:
        for index, item in enumerate(instance):
            _check(item, schema["items"], f"{path}[{index}]", problems)
    if schema.get("uniqueItems"):
        _check_unique_items(instance, path, problems)


def _check(
    instance: Any,
    schema: dict[str, Any] | bool,
    path: str,
    problems: list[str],
) -> None:
    if schema is True:
        return
    if schema is False:
        problems.append(f"{path}: value is disallowed by the schema")
        return
    _check_type(instance, schema, path, problems)
    _check_literals(instance, schema, path, problems)
    _check_string(instance, schema, path, problems)
    _check_number(instance, schema, path, problems)
    _check_applicators(instance, schema, path, problems)
    _check_object(instance, schema, path, problems)
    _check_array(instance, schema, path, problems)


def _validate(document: Any, schema_name: str, schema_dir: str | Path | None) -> list[str]:
    directory = (
        Path(schema_dir)
        if schema_dir is not None
        else Path(__file__).resolve().parents[2] / "schemas"
    )
    with (directory / schema_name).open(encoding="utf-8") as handle:
        schema = json.load(handle)
    problems: list[str] = []
    _check(document, schema, "$", problems)
    return problems


def validate_brief(
    document: Any, *, schema_dir: str | Path | None = None
) -> list[str]:
    return _validate(document, "rizzstack-brief.schema.json", schema_dir)


def validate_attempt(
    document: Any, *, schema_dir: str | Path | None = None
) -> list[str]:
    return _validate(document, "rizzstack-attempt.schema.json", schema_dir)
