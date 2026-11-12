"""Protocol checks for ``acdd/1`` contracts beyond the shared JSON schema.

The helpers here are deliberately independent of bundlebees: they work on plain
dictionaries and never import the plugin, so the linter can be copied into any
repository.  Missing keys and wrong value types must not raise; the CLI reports
schema errors separately.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

Problem = dict[str, str]

AC_ID = re.compile(r"^AC-[0-9]{3,}$")


def contract_sha256(contract: Mapping[str, Any]) -> str:
    """Return the canonical protocol hash of *contract* without its review block."""
    body = {key: value for key, value in contract.items() if key != "review"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _problem(code: str, where: str, message: str) -> Problem:
    return {"code": code, "where": where, "message": message}


def _list_of(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_path(text: str) -> str:
    """Normalise a repository path the same way the protocol does (never raises)."""
    text = str(text).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return "/".join(parts)


def _test_file(test_id: Any) -> str:
    return _clean_path(str(test_id).split("::", 1)[0])


def _check_criteria(contract: Mapping[str, Any]) -> list[Problem]:
    problems: list[Problem] = []
    criteria = _list_of(contract.get("acceptance_criteria"))
    scope = _mapping(contract.get("scope"))
    test_files = {_clean_path(str(path)) for path in _list_of(scope.get("test_files")) if isinstance(path, str)}
    seen: set[str] = set()

    for index, criterion in enumerate(criteria):
        where = f"acceptance_criteria[{index}]"
        if not isinstance(criterion, Mapping):
            problems.append(
                _problem(
                    "ac_id_invalid",
                    where,
                    "acceptance criterion is not a JSON object; make it an object with an id and verification",
                )
            )
            continue
        ac_id = criterion.get("id")
        if not isinstance(ac_id, str) or not AC_ID.match(ac_id):
            problems.append(
                _problem(
                    "ac_id_invalid",
                    f"{where}.id",
                    f"criterion id {ac_id!r} does not match AC-NNN; use AC- followed by at least three digits",
                )
            )
        elif ac_id in seen:
            problems.append(
                _problem(
                    "ac_id_duplicate",
                    f"{where}.id",
                    f"criterion id {ac_id!r} is used more than once; give every criterion a unique AC-NNN id",
                )
            )
        else:
            seen.add(ac_id)

        if criterion.get("verification") != "test":
            continue

        tests = _list_of(criterion.get("tests"))
        named = [test for test in tests if isinstance(test, str) and test.strip()]
        if not named:
            problems.append(
                _problem(
                    "ac_untested",
                    f"{where}.tests",
                    "test criterion names no tests; list at least one path or path::name in tests",
                )
            )
        for test_id in named:
            if _test_file(test_id) not in test_files:
                problems.append(
                    _problem(
                        "test_not_in_scope",
                        f"{where}.tests",
                        f"test {test_id!r} is not in scope.test_files; add its file to scope.test_files or use an approved test",
                    )
                )
    return problems


def _check_frozen(contract: Mapping[str, Any]) -> list[Problem]:
    review = contract.get("review")
    if not isinstance(review, Mapping):
        return [
            _problem(
                "contract_not_frozen",
                "review",
                "contract has no approved review block; freeze it with an approved review and contract_sha256",
            )
        ]
    problems: list[Problem] = []
    stored = review.get("contract_sha256")
    if review.get("status") != "approved":
        problems.append(
            _problem(
                "contract_not_frozen",
                "review.status",
                "review.status is not 'approved'; approve the contract before dispatch",
            )
        )
    if not isinstance(stored, str) or not stored:
        problems.append(
            _problem(
                "contract_not_frozen",
                "review.contract_sha256",
                "review has no contract_sha256; recompute it and store the canonical hash when approving",
            )
        )
    elif stored != contract_sha256(contract):
        problems.append(
            _problem(
                "contract_changed",
                "review.contract_sha256",
                "contract content no longer matches review.contract_sha256; revert the edit or re-freeze with a new approved hash",
            )
        )
    return problems


def check_contract(contract: Mapping[str, Any]) -> list[Problem]:
    """Return the protocol problems of one ``acdd/1`` contract (never raises)."""
    if not isinstance(contract, Mapping):
        return [
            _problem(
                "contract_not_frozen",
                "review",
                "contract is not a JSON object; load an acdd/1 object before checking it",
            )
        ]
    return _check_criteria(contract) + _check_frozen(contract)
