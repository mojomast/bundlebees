"""RED for wave A follow-up A1b-FUP3: harden the remaining GREEN-adjacent paths.

The independent review of WA-A1B-FUP1 (bundlebees PR #282, APPROVE) left three
``minor`` findings on the same class of defect - a GREEN gate that is absent
where the evidence is consumed. This file pins the fix for each one:

1. ``tdd._collection_pin`` treated a *present but null*
   ``review.test_collection`` exactly like an absent pin, so a frozen contract
   whose review block was rewritten with ``"test_collection": null`` silently
   disabled the frozen collected-test gate. The review block is not part of the
   contract hash, so such a contract still passes ``check_frozen``. Absence
   must mean the key is missing; any other shape is ``evidence_tampered``.
2. ``acdd_cli._cmd_refactor`` and ``acdd_cli._cmd_review`` still called
   ``tdd.check_green`` without ``approved_ids``, so a partial GREEN stored
   before the pin was enforced could still be used to record REFACTOR and
   REVIEW evidence. Both handlers must load the frozen pin and pass it.
3. ``acdd_cli._result`` coerced JUnit counters with bare ``int(...)``, so a
   non-numeric counter on the ``hermes bundlebees verify`` lane surfaced as
   ``internal_error`` instead of a named ``evidence_invalid`` refusal.

Every test is offline: temporary git repos, a signed collection manifest bound
by the real freeze API, and the plugin's own CLI handler. The regression tests
keep the legacy no-pin path and the valid-counter path honest.
"""

from __future__ import annotations

import argparse
import copy
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import acdd_cli, cli, leases, security, tdd
from bundlebees_plugin import contract as acdd
from tests.fakes import FakeCtx, load_plugin

CID = "A1B-fup3-fixture"
SRC = "src/a.py"
TEST_FILE = "tests/test_pin.py"
T1 = "tests/test_pin.py::test_first_value"
T2 = "tests/test_pin.py::test_second_value"
BRANCH = "feat/fup3"
ATTEMPT = "att-fup3"
IMPL_SHA = "b" * 40

TEST_SOURCE = """from pathlib import Path


def _value():
    ns = {}
    exec((Path(__file__).resolve().parents[1] / "src" / "a.py").read_text(), ns)
    return ns["value"]


def test_first_value():
    assert _value() == 2


def test_second_value():
    assert _value() == 2
"""


# --------------------------------------------------------------------------- fixtures

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env=security.child_env(),
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose base commit already holds the two approved (failing) tests."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")
    (repo / "src").mkdir()
    (repo / SRC).write_text("value = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "approved tests (RED)")
    return repo, _git(repo, "rev-parse", "HEAD")


def _contract(source_rev: str, **overrides) -> dict:
    data = {
        "contract_version": "acdd/1",
        "id": CID,
        "title": "value stays two under the pinned tests",
        "outcome": {"summary": "value is 2", "observable_behavior": ["src.a.value == 2"]},
        "interfaces": [{"name": "value", "kind": "other", "change": "modify"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "the module", "when": "the first test runs",
             "then": "it passes", "verification": "test", "tests": [T1]},
            {"id": "AC-002", "given": "the module", "when": "the second test runs",
             "then": "it passes", "verification": "test", "tests": [T2]},
        ],
        "scope": {"assigned_files": [SRC], "test_files": [TEST_FILE], "prohibited": []},
        "source_rev": source_rev,
        "tdd": {"applicable": True, "test_first": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "tag", "release", "deploy", "publish"]},
    }
    data.update(overrides)
    return data


def _manifest(contract: dict, sha: str, repo: Path, ids: list[str]) -> dict:
    return {
        "collection_version": tdd.COLLECTION_VERSION,
        "contract_id": str(contract["id"]),
        "contract_sha256": sha,
        "source_rev": str(contract["source_rev"]),
        "test_fingerprints": tdd.fingerprint_files(repo, [TEST_FILE]),
        "methods": {TEST_FILE: "pytest"},
        "collected_ids": list(ids),
        "resolved": {"AC-001": {T1: [T1]}, "AC-002": {T2: [T2]}},
        "environment": {"isolation": "fake", "runner": "pytest --collect-only (fixture)"},
        "at": "2026-11-24T22:00:00Z",
    }


def _pin_state(state: Path, repo: Path, base: str) -> tuple[dict, str, bytes]:
    """Freeze one contract bound to a signed collection manifest (the real pin shape)."""
    ids = [T1, T2]
    contract = _contract(base)
    sha = acdd.contract_sha256(contract)
    key = leases.evidence_key(state)
    manifest = _manifest(contract, sha, repo, ids)
    _path, digest = tdd.write_collection(state, manifest, key)
    frozen = acdd.freeze_contract(
        contract,
        approved_by="Kyle Durepos (fixture)",
        approved_at="2026-11-24T22:00:00Z",
        test_collection={"manifest_sha256": digest, "source_rev": base, "collected": len(ids)},
    )
    assert acdd.check_frozen(frozen) == sha
    return frozen, sha, key


def _cases(*ids: str, outcome: str = "passed") -> list[dict]:
    return [{"id": node, "outcome": outcome, "stage": "call"} for node in ids]


def _result(*, exit_code: int, cases: list[dict]) -> dict:
    failing = sum(case["outcome"] in ("failed", "error") for case in cases)
    skipped = sum(case["outcome"] == "skipped" for case in cases)
    return {
        "exit_code": exit_code,
        "tests": len(cases),
        "failures": failing,
        "errors": 0,
        "skipped": skipped,
        "timed_out": False,
        "cases": cases,
    }


def _record(state: Path, phase: str, revision: str, *, frozen: dict, sha: str,
            repo: Path, cases: list[dict], exit_code: int) -> dict:
    record = tdd.make_record(
        frozen, sha, phase=phase, revision=revision,
        test_fingerprints=tdd.fingerprint_files(repo, [TEST_FILE]),
        command=["python", "-m", "pytest", TEST_FILE],
        result=_result(exit_code=exit_code, cases=cases),
        produced_by={"role": "verifier", "name": "bundlebees verify"},
        test_first=True, now="2026-11-24T22:00:00Z",
    )
    tdd.write_record(state, record, leases.evidence_key(state))
    return record


def _seed_red(state: Path, repo: Path, base: str, *, frozen: dict, sha: str) -> dict:
    cases = _cases(T1, T2, outcome="failed")
    for case in cases:
        case.update(error_type="AssertionError", message="assert 1 == 2")
    return _record(state, "red", base, frozen=frozen, sha=sha, repo=repo,
                   cases=cases, exit_code=1)


def _records(state: Path) -> list[dict]:
    return tdd.load_records(state, CID, leases.evidence_key(state))


def _phases(state: Path) -> list[str]:
    return [record["phase"] for record in _records(state)]


def _contract_file(tmp_path: Path, frozen: dict) -> Path:
    """Write the frozen contract as the CLI reads it (regular file, no symlink)."""
    import json

    path = tmp_path / "contract.json"
    path.write_text(json.dumps(frozen), encoding="utf-8")
    return path


def _cli_ctx() -> FakeCtx:
    plugin = load_plugin()
    ctx = FakeCtx(settings={"verify_allow_unsafe_local": True})
    plugin.register(ctx)
    return ctx


def _invoke(ctx: FakeCtx, argv: list[str]) -> int:
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    return record["handler_fn"](parser.parse_args(argv))


# --------------------------------------------------------------- the frozen pin shape

def test_null_pin_is_refused_not_treated_as_absent(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)

    # The review block is excluded from the contract hash, so the malformed
    # shape still passes check_frozen - it must not pass the pin loader.
    nulled = copy.deepcopy(frozen)
    nulled["review"]["test_collection"] = None
    assert acdd.check_frozen(nulled) == sha

    preflight = {"contract": nulled, "sha": sha, "key": key}
    with pytest.raises(tdd.TddError) as caught:
        tdd.frozen_approved_ids(state, preflight)
    assert caught.value.code == "evidence_tampered"

    stringly = copy.deepcopy(frozen)
    stringly["review"]["test_collection"] = str(frozen["review"]["test_collection"]["manifest_sha256"])
    assert acdd.check_frozen(stringly) == sha
    with pytest.raises(tdd.TddError) as caught:
        tdd.frozen_approved_ids(state, {"contract": stringly, "sha": sha, "key": key})
    assert caught.value.code == "evidence_tampered"


def test_absent_pin_still_returns_none(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    contract = _contract(base)
    sha = acdd.contract_sha256(contract)
    frozen = acdd.freeze_contract(
        contract, approved_by="Kyle Durepos (fixture)", approved_at="2026-11-24T22:00:00Z"
    )
    assert "test_collection" not in frozen["review"]
    preflight = {"contract": frozen, "sha": sha, "key": leases.evidence_key(state)}
    assert tdd.frozen_approved_ids(state, preflight) is None


# --------------------------------------------------------------- acdd_cli tdd refactor

def test_refactor_refuses_a_pinned_partial_green(tmp_path: Path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(T1), exit_code=0)
    path = _contract_file(tmp_path, frozen)

    rc = _invoke(_cli_ctx(), ["tdd", "refactor", str(path), "--none", "--note",
                              "no refactor needed", "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "tests_mismatch" in out
    assert T2 in out
    assert _phases(state) == ["red", "green"]


def test_refactor_records_the_exact_pinned_green(tmp_path: Path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(T1, T2), exit_code=0)
    path = _contract_file(tmp_path, frozen)

    rc = _invoke(_cli_ctx(), ["tdd", "refactor", str(path), "--none", "--note",
                              "no refactor needed", "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "refactor: valid" in out
    assert _phases(state) == ["red", "green", "refactor"]


# --------------------------------------------------------------- acdd_cli tdd review

def test_review_refuses_a_pinned_partial_green(tmp_path: Path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(T1), exit_code=0)
    path = _contract_file(tmp_path, frozen)

    rc = _invoke(_cli_ctx(), ["tdd", "review", str(path), "--reviewer", "kyle",
                              "--approve", "--note", "looks right",
                              "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "tests_mismatch" in out
    assert T2 in out
    assert _phases(state) == ["red", "green"]


def test_review_records_the_exact_pinned_green(tmp_path: Path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(T1, T2), exit_code=0)
    path = _contract_file(tmp_path, frozen)

    rc = _invoke(_cli_ctx(), ["tdd", "review", str(path), "--reviewer", "kyle",
                              "--approve", "--note", "looks right",
                              "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "review: valid" in out
    assert _phases(state) == ["red", "green", "review"]


# --------------------------------------------------------------- acdd_cli._result

def test_result_reports_a_non_numeric_junit_counter():
    for name in ("tests", "failures", "errors", "skipped"):
        junit = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
        junit[name] = "many"
        with pytest.raises(cli.CliError) as caught:
            acdd_cli._result({"junit": junit, "cases": []})
        assert caught.value.code == "evidence_invalid"
        assert name in caught.value.error


def test_result_keeps_integer_counters():
    out = acdd_cli._result(
        {"junit": {"tests": 3, "failures": 1, "errors": 0, "skipped": 2}, "cases": []}
    )
    assert (out["tests"], out["failures"], out["errors"], out["skipped"]) == (3, 1, 0, 2)
    assert all(isinstance(out[key], int) for key in ("tests", "failures", "errors", "skipped"))
    counted = acdd_cli._result({"cases": _cases(T1, T2)})
    assert counted["tests"] == 2


def test_record_green_reports_a_non_numeric_junit_counter(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    acdd_state = {"contract": frozen, "sha": sha, "red": red, "key": key, "tests": [TEST_FILE]}
    evidence = {
        "backend": "fake",
        "exit_code": 0,
        "junit": {"tests": "many", "failures": 0, "errors": 0, "skipped": 0},
        "cases": _cases(T1, T2),
    }
    with pytest.raises(cli.CliError) as caught:
        acdd_cli.record_green(state, acdd_state, repo, evidence, IMPL_SHA)
    assert caught.value.code == "evidence_invalid"
    assert _phases(state) == ["red"]
