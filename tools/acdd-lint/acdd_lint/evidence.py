"""Protocol checks for ``tdd-evidence/1`` histories.

A history is the ordered set of records for one contract id.  The checks here
cover RED before GREEN, test-fingerprint equality, contract-hash staleness,
orphans and (when a key is supplied) HMAC signatures.  Inputs are plain
dictionaries and the functions never import bundlebees.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

Problem = dict[str, str]

_AUTHORITY_ROLES = {"verifier", "human"}


def canonical(record: Mapping[str, Any]) -> bytes:
    """Return the canonical signed bytes for *record*."""
    body = {
        key: value
        for key, value in record.items()
        if key not in ("signature", "_path")
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("utf-8")


def verify_signature(record: Mapping[str, Any], key: bytes) -> bool:
    """True only when *record* carries the HMAC-SHA256 made with *key*."""
    if not isinstance(record, Mapping):
        return False
    signature = record.get("signature")
    if not isinstance(signature, str) or not signature:
        return False
    expected = hmac.new(bytes(key), canonical(record), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _problem(code: str, where: str, message: str) -> Problem:
    return {"code": code, "where": where, "message": message}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _role(record: Mapping[str, Any]) -> str:
    return str(_mapping(record.get("produced_by")).get("role") or "")


def _phase(record: Mapping[str, Any]) -> str:
    return str(record.get("phase") or "")


def _sort_key(item: tuple[str, Mapping[str, Any]]) -> tuple[str, str]:
    filename, record = item
    return (str(record.get("at") or ""), filename)


def _expected_contract_sha(contract: Mapping[str, Any]) -> str:
    return str(_mapping(contract.get("review")).get("contract_sha256") or "")


def _test_first(contract: Mapping[str, Any]) -> bool:
    return _mapping(contract.get("tdd")).get("test_first") is not False


def _is_valid_red(record: Mapping[str, Any]) -> bool:
    return (
        _phase(record) == "red"
        and _role(record) in _AUTHORITY_ROLES
        and record.get("classification") == "behavioral_failure"
    )


def check_history(
    contract: Mapping[str, Any] | None,
    records: list[tuple[str, Mapping[str, Any]]],
    key: bytes | None = None,
) -> list[Problem]:
    """Check one contract's evidence history and return its protocol problems."""
    items = [(filename, record) for filename, record in records]
    items.sort(key=_sort_key)

    if contract is None:
        return [
            _problem(
                "evidence_orphan",
                filename,
                "evidence record names no known contract; add the contract or move the record to the right evidence directory",
            )
            for filename, _ in items
        ]

    expected_sha = _expected_contract_sha(contract)
    require_test_first = _test_first(contract)
    problems: list[Problem] = []
    last_valid_red: Mapping[str, Any] | None = None

    for filename, record in items:
        record = record if isinstance(record, Mapping) else {}
        if str(record.get("contract_sha256") or "") != expected_sha:
            problems.append(
                _problem(
                    "evidence_stale",
                    filename,
                    "record was produced against a different contract hash; re-run the check against the frozen contract",
                )
            )
        if key is not None and not verify_signature(record, key):
            problems.append(
                _problem(
                    "evidence_tampered",
                    filename,
                    "record signature is missing or invalid; re-record it with the coordinator key",
                )
            )

        role = _role(record)
        if _phase(record) == "red" and role in _AUTHORITY_ROLES:
            if record.get("classification") != "behavioral_failure":
                classification = record.get("classification")
                problems.append(
                    _problem(
                        "red_invalid",
                        f"{filename}: classification",
                        f"RED classified as {classification!r}, expected behavioral_failure; fix the test environment so only the missing behavior fails",
                    )
                )
            else:
                last_valid_red = record
            continue

        if _phase(record) == "green" and role in _AUTHORITY_ROLES:
            if record.get("classification") != "passed":
                classification = record.get("classification")
                problems.append(
                    _problem(
                        "green_failed",
                        f"{filename}: classification",
                        f"GREEN classified as {classification!r}, expected passed; make the implementation pass the approved tests without editing them",
                    )
                )
            if require_test_first and last_valid_red is None:
                problems.append(
                    _problem(
                        "red_missing",
                        filename,
                        "GREEN has no earlier verifier behavioral_failure RED; record RED on the baseline or set tdd.test_first: false honestly",
                    )
                )
            if last_valid_red is not None and record.get("test_fingerprints") != last_valid_red.get("test_fingerprints"):
                problems.append(
                    _problem(
                        "tests_tampered",
                        f"{filename}: test_fingerprints",
                        "GREEN test fingerprints differ from RED; rerun the same approved test files unchanged",
                    )
                )

    return problems
