"""ACDD ``acdd/1`` contracts: validate, freeze by hash, and compare with a brief.

Threat model
============

A contract is model- or coordinator-authored JSON that decides what a worker may
edit and which tests judge it. Treat it as untrusted until frozen: it can widen
a lease, remove a required human gate, name a test file as assigned, point at
an absolute path, or claim tests that are not in ``scope.test_files``. The
validator therefore checks both the bundled JSON Schema and the code checks in
``docs/ACDD.md`` section 2, returns a fresh normalised copy, and never mutates
the caller's mapping. Freezing stores a SHA-256 of the canonical JSON without
the ``review`` block, so any later edit (a wider lease, an easier test list)
changes the hash. All error text is passed through
``bundlebees_plugin.security.scrub`` before it can reach a tool envelope.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .security import scrub

__all__ = [
    "CONTRACT_VERSION",
    "REQUIRED_HUMAN_GATES",
    "SUPPORTED_KEYWORDS",
    "ContractError",
    "load_schema",
    "validate_contract",
    "contract_sha256",
    "freeze_contract",
    "check_frozen",
    "red_set",
    "criteria_tests",
    "added_interfaces",
    "is_prohibited",
    "contract_from_brief",
    "check_brief_against_contract",
]

CONTRACT_VERSION = "acdd/1"
REQUIRED_HUMAN_GATES = ("merge", "release", "deploy", "publish")

#: JSON Schema keywords this small validator implements. Every keyword used by
#: ``schemas/contract.schema.json`` is present; unknown keywords are ignored.
SUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "type",
        "enum",
        "const",
        "pattern",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "items",
        "required",
        "properties",
        "additionalProperties",
        "propertyNames",
        "anyOf",
        "minimum",
        "maximum",
        "default",
    }
)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "contract.schema.json"
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_FIX_SCHEMA = "Fix the named contract field to match the bundled acdd/1 schema."
_FIX_CODE = (
    "Change the contract so it satisfies the ACDD code checks in "
    "docs/ACDD.md section 2."
)
_FIX_FREEZE = (
    "Have the coordinator review the contract and freeze it with "
    "freeze_contract(contract, approved_by=...) before dispatch."
)
_FIX_CHANGED = (
    "Revert the edit, or re-review and re-freeze the contract after the "
    "intended change; a frozen contract is immutable."
)
_FIX_STALE = (
    "Re-freeze the contract for the brief's source_rev, or dispatch the brief "
    "at the contract's source_rev."
)
_FIX_SCOPE = (
    "Dispatch only files listed in contract.scope.assigned_files that are not "
    "matched by contract.scope.prohibited."
)
_FIX_TESTS = (
    "The implementer must not change approved test files; keep "
    "brief.assigned_files disjoint from contract.scope.test_files."
)


class ContractError(Exception):
    """A rejected contract, with the envelope code, error, fix and field.

    ``code`` is one of ``contract_invalid``, ``contract_not_frozen``,
    ``contract_changed``, ``contract_missing``, ``stale_revision``,
    ``scope_violation`` or ``tests_tampered``. ``error`` and ``fix`` are
    scrubbed before exposure; ``field`` is the dotted path of the first
    schema or code-check violation.
    """

    def __init__(self, code: str, error: str, fix: str, field: str | None = None) -> None:
        self.code = str(code)
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        self.field = str(scrub(str(field))) if field is not None else None
        super().__init__(f"{self.code}: {self.error}")


def _invalid(error: str, field: str | None = None, fix: str = _FIX_CODE) -> ContractError:
    return ContractError("contract_invalid", error, fix, field)


def load_schema() -> dict[str, Any]:
    """Load the bundled draft 2020-12 ``acdd/1`` contract schema."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Small JSON Schema validator
# ---------------------------------------------------------------------------


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


def _json_equal(left: Any, right: Any) -> bool:
    """JSON equality where booleans never equal the integers 0/1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    return left == right


def _shown(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _schema_problem(value: Any, spec: Any, path: str) -> tuple[str, str] | None:
    """Return ``(field, message)`` for the first violation under *spec*."""
    if not isinstance(spec, Mapping):
        return None

    expected = spec.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, item) for item in types if isinstance(item, str)):
            return path, f"{path} has the wrong type"

    if "const" in spec and not _json_equal(value, spec.get("const")):
        return path, f"{path} must equal {_shown(spec.get('const'))}"

    if "enum" in spec:
        allowed = spec.get("enum")
        if isinstance(allowed, list) and not any(
            _json_equal(value, item) for item in allowed
        ):
            shown = ", ".join(_shown(item) for item in allowed)
            return path, f"{path} must be one of: {shown}"

    if isinstance(value, str):
        minimum_length = spec.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            return path, f"{path} is shorter than {minimum_length} character(s)"
        maximum_length = spec.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            return path, f"{path} is longer than {maximum_length} character(s)"
        pattern = spec.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            return path, f"{path} does not match the required pattern"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = spec.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            return path, f"{path} must be at least {minimum}"
        maximum = spec.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            return path, f"{path} must be at most {maximum}"

    if isinstance(value, list):
        minimum_items = spec.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            return path, f"{path} needs at least {minimum_items} item(s)"
        maximum_items = spec.get("maxItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            return path, f"{path} allows at most {maximum_items} item(s)"
        if spec.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(_json_equal(item, earlier) for earlier in value[:index]):
                    return path, f"{path}[{index}] duplicates an earlier item"
        item_spec = spec.get("items")
        if isinstance(item_spec, Mapping):
            for index, item in enumerate(value):
                problem = _schema_problem(item, item_spec, f"{path}[{index}]")
                if problem:
                    return problem
        elif isinstance(item_spec, list):
            for index, item in enumerate(value):
                if index >= len(item_spec):
                    break
                problem = _schema_problem(item, item_spec[index], f"{path}[{index}]")
                if problem:
                    return problem

    if isinstance(value, Mapping):
        for required in spec.get("required", []):
            if required not in value:
                return path, f"{path} is missing required field {required!r}"
        properties = spec.get("properties")
        if isinstance(properties, Mapping):
            for key, child_spec in properties.items():
                if key in value:
                    problem = _schema_problem(value[key], child_spec, f"{path}.{key}")
                    if problem:
                        return problem
        property_names = spec.get("propertyNames")
        if isinstance(property_names, Mapping):
            for key in value:
                problem = _schema_problem(str(key), property_names, f"{path}.{key}")
                if problem:
                    return problem
        if "additionalProperties" in spec:
            additional = spec["additionalProperties"]
            known = set(properties) if isinstance(properties, Mapping) else set()
            extras = [key for key in value if key not in known]
            if additional is False and extras:
                return path, f"{path} has unexpected field {extras[0]!r}"
            if isinstance(additional, Mapping):
                for key in extras:
                    problem = _schema_problem(
                        value[key], additional, f"{path}.{key}"
                    )
                    if problem:
                        return problem

    alternatives = spec.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if not any(
            _schema_problem(value, child, path) is None
            for child in alternatives
            if isinstance(child, Mapping)
        ):
            return path, f"{path} does not match any allowed shape"

    return None


# ---------------------------------------------------------------------------
# Paths and code checks
# ---------------------------------------------------------------------------


def _clean_path(text: str) -> str:
    """Repository-relative normal form used for matching (never raises)."""
    text = text.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return "/".join(parts)


def _normalise_scope_path(value: str, field: str) -> str:
    text = value.replace("\\", "/").strip()
    if not text or "\x00" in text:
        raise _invalid(f"{field} must be a non-empty repository-relative path", field)
    if text.startswith("/") or _DRIVE_RE.match(text):
        raise _invalid(f"{field} must be repository-relative (got {value!r})", field)
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise _invalid(f"{field} must not contain '..' (got {value!r})", field)
    if any(part.lower() == ".git" for part in parts):
        raise _invalid(f"{field} must not be under .git (got {value!r})", field)
    if not parts:
        raise _invalid(f"{field} must not be empty", field)
    return "/".join(parts)


def _normalise_scope(candidate: dict[str, Any]) -> None:
    scope = candidate.get("scope")
    if not isinstance(scope, Mapping):
        return
    for key in ("assigned_files", "test_files", "read_only", "prohibited"):
        values = scope.get(key)
        if not isinstance(values, list):
            continue
        normalised: list[str] = []
        for index, value in enumerate(values):
            if not isinstance(value, str):
                continue
            normalised.append(
                _normalise_scope_path(value, f"contract.scope.{key}[{index}]")
            )
        scope[key] = normalised


def _list_of(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _test_file(test_id: str) -> str:
    return _clean_path(test_id.split("::", 1)[0])


def _criteria(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in _list_of(contract.get("acceptance_criteria"))
        if isinstance(item, Mapping)
    ]


def _scope(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    scope = contract.get("scope")
    return scope if isinstance(scope, Mapping) else {}


def _tdd(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    tdd = contract.get("tdd")
    return tdd if isinstance(tdd, Mapping) else {}


def _check_unique_ids(criteria: list[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for index, criterion in enumerate(criteria):
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str):
            continue
        if criterion_id in seen:
            raise _invalid(
                f"acceptance criterion id {criterion_id!r} is not unique",
                f"contract.acceptance_criteria[{index}].id",
            )
        seen.add(criterion_id)


def _check_tdd(
    criteria: list[Mapping[str, Any]],
    scope: Mapping[str, Any],
    tdd: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    test_files = {
        _clean_path(item)
        for item in _list_of(scope.get("test_files"))
        if isinstance(item, str) and item.strip()
    }
    if tdd.get("applicable") is not True:
        reason = tdd.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _invalid(
                "tdd.reason is required when tdd.applicable is false",
                "contract.tdd.reason",
            )
        return

    for index, criterion in enumerate(criteria):
        if criterion.get("verification") != "test":
            continue
        criterion_id = str(criterion.get("id") or f"#{index}")
        tests = [
            item for item in _list_of(criterion.get("tests")) if isinstance(item, str)
        ]
        if not tests:
            raise _invalid(
                f"test criterion {criterion_id} lists no tests",
                f"contract.acceptance_criteria[{index}].tests",
            )
        for test_id in tests:
            if _test_file(test_id) not in test_files:
                raise _invalid(
                    f"test {test_id!r} for {criterion_id} is not in scope.test_files",
                    f"contract.acceptance_criteria[{index}].tests",
                )

    assigned = {
        _clean_path(item)
        for item in _list_of(scope.get("assigned_files"))
        if isinstance(item, str) and item.strip()
    }
    overlap = sorted(assigned & test_files)
    if overlap:
        raise _invalid(
            f"assigned file {overlap[0]!r} is also in scope.test_files; "
            "the implementer must not edit approved tests",
            "contract.scope.assigned_files",
        )

    evidence = [
        item
        for item in _list_of(candidate.get("evidence_required"))
        if isinstance(item, str)
    ]
    if "green" not in evidence:
        raise _invalid(
            "evidence_required must include 'green' when tdd.applicable is true",
            "contract.evidence_required",
        )
    if "red" not in evidence and tdd.get("test_first") is not False:
        raise _invalid(
            "evidence_required must include 'red' when tdd.applicable is true "
            "and tdd.test_first is not false",
            "contract.evidence_required",
        )


def _check_scope_paths(candidate: dict[str, Any]) -> None:
    _normalise_scope(candidate)
    for index, assigned in enumerate(_list_of(_scope(candidate).get("assigned_files"))):
        if isinstance(assigned, str) and is_prohibited(assigned, candidate):
            raise _invalid(
                f"assigned file {assigned!r} matches scope.prohibited",
                f"contract.scope.assigned_files[{index}]",
            )


def _check_human_gates(candidate: Mapping[str, Any]) -> None:
    authorization = candidate.get("authorization")
    human_gates = (
        _list_of(authorization.get("human_gates"))
        if isinstance(authorization, Mapping)
        else []
    )
    missing = [gate for gate in REQUIRED_HUMAN_GATES if gate not in human_gates]
    if missing:
        raise _invalid(
            "authorization.human_gates is missing required gate(s): "
            + ", ".join(missing),
            "contract.authorization.human_gates",
        )


def _check_dependencies(candidate: Mapping[str, Any]) -> None:
    dependencies = [
        item for item in _list_of(candidate.get("dependencies")) if isinstance(item, str)
    ]
    contract_id = candidate.get("id")
    if isinstance(contract_id, str) and contract_id in dependencies:
        raise _invalid(
            f"dependencies must not include the contract's own id {contract_id!r}",
            "contract.dependencies",
        )


def _code_checks(candidate: dict[str, Any]) -> None:
    criteria = _criteria(candidate)
    _check_unique_ids(criteria)
    _check_tdd(criteria, _scope(candidate), _tdd(candidate), candidate)
    _check_scope_paths(candidate)
    _check_human_gates(candidate)
    _check_dependencies(candidate)


# ---------------------------------------------------------------------------
# Public validation and freezing API
# ---------------------------------------------------------------------------


def _schema_checked(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy after only the bundled JSON Schema check."""
    candidate = (
        copy.deepcopy(dict(contract))
        if isinstance(contract, Mapping)
        else copy.deepcopy(contract)
    )
    problem = _schema_problem(candidate, load_schema(), "contract")
    if problem:
        field, message = problem
        raise _invalid(message, field, _FIX_SCHEMA)
    if not isinstance(candidate, dict):
        raise _invalid("contract must be a JSON object", "contract", _FIX_SCHEMA)
    return candidate


def validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate *contract* and return a fresh, normalised deep copy.

    The JSON Schema runs first (the first violation's dotted path is reported
    in :attr:`ContractError.field`), then the numbered code checks from
    ``docs/ACDD.md`` section 2. The caller's mapping is never modified.
    """
    candidate = _schema_checked(contract)
    _code_checks(candidate)
    return candidate


def contract_sha256(contract: Mapping[str, Any]) -> str:
    """SHA-256 of compact, key-sorted JSON with the ``review`` block removed."""
    body = {key: value for key, value in contract.items() if key != "review"}
    text = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def freeze_contract(
    contract: Mapping[str, Any],
    *,
    approved_by: str,
    approved_at: str | None = None,
    test_collection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate *contract* and return a copy approved and bound to its hash.

    *test_collection* (from :mod:`collection`) binds the frozen contract to the
    signed collected-test manifest by digest. The review block is excluded
    from the contract hash, so the manifest and the contract can name each
    other.
    """
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise _invalid(
            "approved_by must be a non-empty string",
            "review.approved_by",
            _FIX_FREEZE,
        )
    timestamp = approved_at if approved_at is not None else _utc_now()
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise _invalid(
            "approved_at must be a non-empty string",
            "review.approved_at",
            _FIX_FREEZE,
        )
    frozen = validate_contract(contract)
    frozen["review"] = {
        "status": "approved",
        "approved_by": approved_by,
        "approved_at": timestamp,
        "contract_sha256": contract_sha256(frozen),
    }
    if test_collection is not None:
        frozen["review"]["test_collection"] = dict(test_collection)
    return frozen


def check_frozen(contract: Mapping[str, Any]) -> str:
    """Return the frozen hash, or raise if the contract is not approved/unchanged."""
    _schema_checked(contract)
    review = contract.get("review")
    if not isinstance(review, Mapping) or review.get("status") != "approved":
        raise ContractError(
            "contract_not_frozen",
            "contract has no approved review block",
            _FIX_FREEZE,
            "contract.review",
        )
    stored = review.get("contract_sha256")
    actual = contract_sha256(contract)
    if not isinstance(stored, str) or stored != actual:
        raise ContractError(
            "contract_changed",
            "contract content no longer matches review.contract_sha256",
            _FIX_CHANGED,
            "contract.review.contract_sha256",
        )
    return actual


# ---------------------------------------------------------------------------
# Verifier helpers
# ---------------------------------------------------------------------------


def red_set(contract: Mapping[str, Any]) -> list[str]:
    """Tests of non-regression ``test`` criteria, in order, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for criterion in _criteria(contract):
        if criterion.get("verification") != "test":
            continue
        if criterion.get("regression") is True:
            continue
        for test_id in _list_of(criterion.get("tests")):
            if isinstance(test_id, str) and test_id not in seen:
                seen.add(test_id)
                out.append(test_id)
    return out


def criteria_tests(contract: Mapping[str, Any]) -> dict[str, list[str]]:
    """Map every criterion id to its declared tests (possibly an empty list)."""
    out: dict[str, list[str]] = {}
    for criterion in _criteria(contract):
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, str):
            continue
        bucket = out.setdefault(criterion_id, [])
        for test_id in _list_of(criterion.get("tests")):
            if isinstance(test_id, str):
                bucket.append(test_id)
    return out


def added_interfaces(contract: Mapping[str, Any]) -> list[str]:
    """Names of interfaces whose ``change`` is ``add``."""
    out: list[str] = []
    for item in _list_of(contract.get("interfaces")):
        if isinstance(item, Mapping) and item.get("change") == "add":
            name = item.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def is_prohibited(path: str, contract: Mapping[str, Any]) -> bool:
    """True when *path* matches one of ``scope.prohibited``'s glob patterns."""
    if not isinstance(path, str):
        return False
    candidate = _clean_path(path)
    for pattern in _list_of(_scope(contract).get("prohibited")):
        if isinstance(pattern, str) and fnmatch.fnmatchcase(
            candidate, _clean_path(pattern)
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Brief comparison
# ---------------------------------------------------------------------------


def contract_from_brief(brief: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return ``brief['contract']`` when it is a mapping, else ``None``."""
    if not isinstance(brief, Mapping):
        return None
    contract = brief.get("contract")
    return contract if isinstance(contract, Mapping) else None


def check_brief_against_contract(
    brief: Mapping[str, Any], contract: Mapping[str, Any]
) -> None:
    """Raise when the brief's revision or file scope disagrees with *contract*."""
    brief_rev = brief.get("source_rev")
    contract_rev = contract.get("source_rev")
    if (
        not isinstance(brief_rev, str)
        or not isinstance(contract_rev, str)
        or brief_rev.lower() != contract_rev.lower()
    ):
        raise ContractError(
            "stale_revision",
            "brief.source_rev does not match contract.source_rev",
            _FIX_STALE,
            "brief.source_rev",
        )

    scope = _scope(contract)
    test_files = {
        _clean_path(item)
        for item in _list_of(scope.get("test_files"))
        if isinstance(item, str) and item.strip()
    }
    assigned_in_contract = {
        _clean_path(item)
        for item in _list_of(scope.get("assigned_files"))
        if isinstance(item, str) and item.strip()
    }
    brief_files = [
        _clean_path(item)
        for item in _list_of(brief.get("assigned_files"))
        if isinstance(item, str) and item.strip()
    ]
    if not brief_files:
        raise ContractError(
            "scope_violation",
            "brief has no assigned files",
            _FIX_SCOPE,
            "brief.assigned_files",
        )

    for path in brief_files:
        if path in test_files:
            raise ContractError(
                "tests_tampered",
                f"brief assigned file {path!r} is an approved test file",
                _FIX_TESTS,
                "brief.assigned_files",
            )

    for path in brief_files:
        if path not in assigned_in_contract:
            raise ContractError(
                "scope_violation",
                f"brief assigned file {path!r} is not in contract.scope.assigned_files",
                _FIX_SCOPE,
                "brief.assigned_files",
            )
        if is_prohibited(path, contract):
            raise ContractError(
                "scope_violation",
                f"brief assigned file {path!r} matches contract.scope.prohibited",
                _FIX_SCOPE,
                "brief.assigned_files",
            )
