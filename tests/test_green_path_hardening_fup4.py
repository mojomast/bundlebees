"""RED for wave A follow-up A1b-FUP4: GREEN-adjacent residuals from PR #290.

The independent review of WA-A1B-FUP3 (bundlebees PR #290, APPROVE) left two
``minor`` findings on the same class of defect the FUP3 slice had just narrowed:
a default stands in for evidence that is present but malformed.

1. ``tdd._collection_pin`` still maps a *present but non-mapping* ``review``
   block to ``{}``, so it returns ``None`` - the legacy unpinned path - instead
   of refusing. The review block is excluded from the contract hash and
   ``check_frozen`` refuses a non-mapping review, so the shipped CLI cannot
   reach the shape, but a direct caller that skips validation gets the
   permissive branch. Only a genuinely absent ``review`` key (or an absent
   ``test_collection`` key inside a mapping review) may mean "no pin".
2. ``acdd_cli._result`` and ``verify_local._check_frozen_green`` still use
   ``junit.get(name) or default``, so a *falsy* malformed counter (``""``,
   ``[]``, ``{}``) is treated as absent and silently becomes the case count or
   zero. Truthy non-numeric counters are already refused with
   ``evidence_invalid``; the fix is to treat only ``None`` as absent and pass
   every other present value through ``verify_local._junit_counter``.

``verify_local._check_frozen_green`` carries the identical fallback, so finding
2 is fixed in both consumers rather than leaving the verify lane permissive.

Every test is offline and hermetic: no git repo is required for the pin shape or
for the counter refusals, and the fallback regression on ``_check_frozen_green``
stubs the record builder so only the counter mapping is under test. The
regression tests keep the legacy no-pin path and the ``None`` fallbacks honest.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from bundlebees_plugin import acdd_cli, cli, leases, tdd, verify_local
from bundlebees_plugin import contract as acdd

CID = "A1B-fup4-fixture"
TEST_FILE = "tests/test_pin.py"
T1 = "tests/test_pin.py::test_first_value"
T2 = "tests/test_pin.py::test_second_value"
SHA = "a" * 64
REV = "b" * 40
COUNTERS = ("tests", "failures", "errors", "skipped")
FALSY = ("", [], {})


# --------------------------------------------------------------------------- fixtures
def _contract(**overrides) -> dict:
    data = {
        "contract_version": "acdd/1",
        "id": CID,
        "title": "malformed review and counter shapes are refused, not defaulted",
        "outcome": {
            "summary": "a present-but-malformed review block or JUnit counter is refused",
            "observable_behavior": ["refusals are named evidence_tampered/evidence_invalid"],
        },
        "interfaces": [{"name": "tdd._collection_pin", "kind": "function", "change": "modify"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "a frozen contract", "when": "the pin loader runs",
             "then": "a malformed review is refused", "verification": "test", "tests": [T1]},
            {"id": "AC-002", "given": "a contract", "when": "no pin exists",
             "then": "the loader returns None", "verification": "test", "regression": True,
             "tests": [T2]},
        ],
        "scope": {"assigned_files": ["src/a.py"], "test_files": [TEST_FILE], "prohibited": []},
        "source_rev": REV,
        "tdd": {"applicable": True, "test_first": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "tag", "release", "deploy", "publish"]},
    }
    data.update(overrides)
    return data


def _frozen(**overrides) -> dict:
    return acdd.freeze_contract(
        _contract(**overrides),
        approved_by="Bundlebees Test",
        approved_at="2026-11-24T22:00:00Z",
    )


def _junit(name: str, value: object) -> dict:
    junit: dict[str, object] = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    junit[name] = value
    return junit


# ------------------------------------------------------- the present-but-malformed review
def test_non_mapping_review_block_is_refused_not_treated_as_absent(tmp_path: Path):
    state = tmp_path / "state"
    key = leases.evidence_key(state)
    frozen = _frozen()
    sha = acdd.contract_sha256(frozen)

    # The review block is excluded from the hash, so a malformed shape keeps the
    # same contract id; the shipped CLI still refuses it in check_frozen, which
    # is why only a direct caller can reach the permissive branch.
    for malformed in ("not-a-mapping", ["approved"], 7, None):
        broken = copy.deepcopy(frozen)
        broken["review"] = malformed
        with pytest.raises(acdd.ContractError):
            acdd.check_frozen(broken)
        with pytest.raises(tdd.TddError) as caught:
            tdd.frozen_approved_ids(state, {"contract": broken, "sha": sha, "key": key})
        assert caught.value.code == "evidence_tampered"


def test_absent_pin_shapes_still_return_none(tmp_path: Path):
    state = tmp_path / "state"
    key = leases.evidence_key(state)

    # A contract body with no review block at all keeps the legacy no-pin path.
    body = _contract()
    assert "review" not in body
    assert tdd.frozen_approved_ids(state, {"contract": body, "sha": SHA, "key": key}) is None

    # A mapping review block without the test_collection key is a genuine absence.
    frozen = _frozen()
    assert "test_collection" not in frozen["review"]
    assert tdd.frozen_approved_ids(
        state, {"contract": frozen, "sha": acdd.contract_sha256(frozen), "key": key}
    ) is None


# --------------------------------------------------------------- acdd_cli._result
def test_result_refuses_falsy_malformed_junit_counters():
    for name in COUNTERS:
        for bad in FALSY:
            with pytest.raises(cli.CliError) as caught:
                acdd_cli._result({"junit": _junit(name, bad), "cases": []})
            assert caught.value.code == "evidence_invalid"
            assert name in caught.value.error


def test_result_treats_only_none_as_absent():
    out = acdd_cli._result(
        {
            "exit_code": 0,
            "junit": {"tests": None, "failures": None, "errors": None, "skipped": None},
            "cases": [{"id": T1}, {"id": T2}],
        }
    )
    assert (out["tests"], out["failures"], out["errors"], out["skipped"]) == (2, 0, 0, 0)
    assert all(isinstance(out[name], int) for name in COUNTERS)

    # No junit block at all is the same absence: the case count is the fallback.
    plain = acdd_cli._result({"cases": [{"id": T1}]})
    assert (plain["tests"], plain["failures"], plain["errors"], plain["skipped"]) == (1, 0, 0, 0)


# ---------------------------------------------------- verify_local._check_frozen_green
def test_check_frozen_green_refuses_falsy_malformed_junit_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def _never(*_args, **_kwargs):
        raise AssertionError("make_record must not run for a malformed counter")

    monkeypatch.setattr(verify_local.tdd, "fingerprint_files", lambda worktree, tests: {})
    monkeypatch.setattr(verify_local.tdd, "make_record", _never)
    preflight = {"contract": _frozen(), "tests": [TEST_FILE], "sha": SHA}
    for name in COUNTERS:
        for bad in FALSY:
            with pytest.raises(verify_local.VerifyError) as caught:
                verify_local._check_frozen_green(
                    preflight, [], tmp_path, {"junit": _junit(name, bad), "cases": []}, REV, None
                )
            assert caught.value.code == "evidence_invalid"
            assert name in caught.value.message


def test_check_frozen_green_treats_only_none_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict = {}

    def _record(contract, sha, **kwargs):
        captured.update(kwargs)
        return {"phase": "green"}

    monkeypatch.setattr(verify_local.tdd, "fingerprint_files", lambda worktree, tests: {})
    monkeypatch.setattr(verify_local.tdd, "make_record", _record)
    monkeypatch.setattr(verify_local.tdd, "check_green", lambda *_args, **_kwargs: None)

    preflight = {"contract": _frozen(), "tests": [TEST_FILE], "sha": SHA, "red": {"phase": "red"}}
    evidence = {
        "exit_code": 0,
        "junit": {"tests": None, "failures": None, "errors": None, "skipped": None},
        "cases": [{"id": T1}, {"id": T2}],
    }
    verify_local._check_frozen_green(preflight, [], tmp_path, evidence, REV, None)
    result = captured["result"]
    assert (result["tests"], result["failures"], result["errors"], result["skipped"]) == (2, 0, 0, 0)
    assert all(isinstance(result[name], int) for name in COUNTERS)
