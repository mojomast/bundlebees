"""Approved acceptance tests for contract-aware CLI wiring (ACDD + TDD).

Written before the implementation (RED). They pin the user-run
``hermes bundlebees`` surface: ``contract validate|freeze``, ``tdd red|report``
and a contract-aware ``verify`` that refuses edits to approved tests, requires
a valid RED and records a signed GREEN. Legacy briefs keep working unchanged.
Everything is offline: a temporary git repo, a local bare remote, a fake
launcher result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import contract as acdd
from bundlebees_plugin import leases, security, tdd
from bundlebees_plugin.brief import validate_brief
from tests.fakes import FakeCtx, load_plugin

ATTEMPT_ID = "att-acdd-1"
BRANCH = "feat/acdd"
CID = "BB-value"
APPROVED_TEST = "tests/test_value.py"
TEST_ID = "tests/test_value.py::test_value_is_two"

VALUE_TEST = '''from pathlib import Path


def _value():
    ns = {}
    exec((Path(__file__).resolve().parents[1] / "src" / "a.py").read_text(), ns)
    return ns["value"]


def test_value_is_two():
    assert _value() == 2
'''

GOOD_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

TAMPER_PATCH = """diff --git a/tests/test_value.py b/tests/test_value.py
--- a/tests/test_value.py
+++ b/tests/test_value.py
@@ -10,2 +10,2 @@ def _value():
 def test_value_is_two():
-    assert _value() == 2
+    assert True
"""


class FakeClient:
    def __init__(self, result: dict) -> None:
        self.result_data = result

    def result(self, attempt_id: str) -> dict:
        return dict(self.result_data)

    def get_raw(self, attempt_id: str) -> dict:
        return {"result": dict(self.result_data)}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env=security.child_env(),
    ).stdout.strip()


def _repo(tmp_path: Path, test_source: str = VALUE_TEST) -> tuple[Path, str]:
    """A repo whose base commit already holds the approved (failing) test."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / APPROVED_TEST).write_text(test_source, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "approved tests (RED)")
    base = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True,
                   env=security.child_env())
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "branch", BRANCH)
    _git(repo, "push", "-u", "origin", BRANCH)
    return repo, base


def _contract(source_rev: str, **overrides) -> dict:
    data = {
        "contract_version": "acdd/1",
        "id": CID,
        "title": "value becomes two",
        "outcome": {"summary": "value is 2", "observable_behavior": ["src.a.value == 2"]},
        "interfaces": [{"name": "value", "kind": "other", "change": "modify"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "the module", "when": "value is read", "then": "it is 2",
             "verification": "test", "tests": [TEST_ID]},
        ],
        "scope": {"assigned_files": ["src/a.py"], "test_files": [APPROVED_TEST], "prohibited": []},
        "source_rev": source_rev,
        "tdd": {"applicable": True, "test_first": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "tag", "release", "deploy", "publish"]},
    }
    data.update(overrides)
    return data


def _frozen(source_rev: str, **overrides) -> dict:
    return acdd.freeze_contract(_contract(source_rev, **overrides), approved_by="reviewer",
                                approved_at="2026-11-12T18:00:00Z")


def _ctx(client: FakeClient | None = None) -> FakeCtx:
    plugin = load_plugin()
    ctx = FakeCtx(settings={"verify_allow_unsafe_local": True})
    ctx.bundlebees_client = client or FakeClient({})
    plugin.register(ctx)
    return ctx


def _invoke(ctx: FakeCtx, argv: list[str]) -> int:
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    return record["handler_fn"](parser.parse_args(argv))


def _lease(state: Path, base: str, contract: dict | None) -> dict:
    brief = {
        "objective": "make value two",
        "source_rev": base,
        "branch": BRANCH,
        "assigned_files": ["src/a.py"],
        "acceptance": {"criteria": ["AC-001"], "tests": [APPROVED_TEST]},
    }
    if contract is not None:
        brief["contract"] = contract
    return leases.record_lease(state, ATTEMPT_ID, validate_brief(brief))


def _result(patch: str, base: str) -> dict:
    return {"attempt_id": ATTEMPT_ID, "branch": BRANCH, "patch": patch, "base_rev": base,
            "brief": {"branch": BRANCH, "assigned_files": ["src/a.py"], "tests": [APPROVED_TEST]}}


def _verify(ctx: FakeCtx, repo: Path, state: Path) -> int:
    return _invoke(ctx, ["verify", ATTEMPT_ID, "--workdir", str(repo), "--branch", BRANCH,
                         "--state-dir", str(state), "--dry-run"])


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _records(state: Path) -> list[dict]:
    return tdd.load_records(state, CID, leases.evidence_key(state))


# ------------------------------------------------------------ contract commands


def test_contract_validate_prints_hash_and_rejects_invalid(tmp_path, capsys):
    ctx = _ctx()
    good = _write(tmp_path / "c.json", _contract("a" * 40))
    assert _invoke(ctx, ["contract", "validate", str(good)]) == 0
    out = capsys.readouterr().out
    assert acdd.contract_sha256(_contract("a" * 40)) in out
    assert "valid" in out

    bad = _contract("a" * 40)
    bad["scope"]["assigned_files"].append(APPROVED_TEST)
    path = _write(tmp_path / "bad.json", bad)
    assert _invoke(ctx, ["contract", "validate", str(path)]) != 0
    assert "contract_invalid" in capsys.readouterr().out


def test_contract_freeze_writes_an_approved_contract(tmp_path, capsys):
    ctx = _ctx()
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    src = _write(tmp_path / "c.json", _contract(base))
    out_path = tmp_path / "frozen.json"
    rc = _invoke(ctx, ["contract", "freeze", str(src), "--approved-by", "Kyle", "--out", str(out_path),
                       "--workdir", str(repo), "--state-dir", str(state)])
    assert rc == 0, capsys.readouterr().out
    frozen = json.loads(out_path.read_text(encoding="utf-8"))
    assert frozen["review"]["status"] == "approved"
    assert frozen["review"]["approved_by"] == "Kyle"
    assert acdd.check_frozen(frozen) == acdd.contract_sha256(_contract(base))
    assert frozen["review"]["test_collection"]["source_rev"] == base
    assert frozen["review"]["test_collection"]["collected"] >= 1

    bad = _contract(base, evidence_required=["green"])  # test-first but no red
    bad_src = _write(tmp_path / "bad.json", bad)
    bad_out = tmp_path / "bad-frozen.json"
    assert _invoke(ctx, ["contract", "freeze", str(bad_src), "--approved-by", "Kyle", "--out", str(bad_out),
                         "--workdir", str(repo), "--state-dir", str(state)]) != 0
    assert not bad_out.exists()


# ------------------------------------------------------------ RED


def test_tdd_red_records_a_signed_verifier_red(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    path = _write(tmp_path / "frozen.json", _frozen(base))
    rc = _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "red: valid" in out
    (record,) = _records(state)
    assert record["phase"] == "red"
    assert record["classification"] == "behavioral_failure"
    assert record["revision"] == base
    assert record["produced_by"]["role"] == "verifier"
    assert set(record["test_fingerprints"]) == {APPROVED_TEST}
    assert any(case["id"] == TEST_ID and case["outcome"] == "failed" for case in record["result"]["cases"])


def test_tdd_red_refuses_tests_that_already_pass(tmp_path, capsys):
    repo, _ = _repo(tmp_path)
    (repo / "src" / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "commit", "-am", "already implemented")
    base = _git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    path = _write(tmp_path / "frozen.json", _frozen(base))
    rc = _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)])
    assert rc != 0
    assert "red_incomplete" in capsys.readouterr().out


def test_tdd_red_refuses_an_unfrozen_contract(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    path = _write(tmp_path / "draft.json", _contract(base))
    rc = _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(tmp_path / "s")])
    assert rc != 0
    assert "contract_not_frozen" in capsys.readouterr().out


# ------------------------------------------------------------ contract-aware verify


def test_verify_with_contract_requires_red(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base, _frozen(base))
    rc = _verify(_ctx(FakeClient(_result(GOOD_PATCH, base))), repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    assert "red_missing" in out
    assert _git(repo, "rev-parse", f"origin/{BRANCH}") == base


def test_verify_with_contract_refuses_edits_to_approved_tests(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base)
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    rc = _verify(_ctx(FakeClient(_result(TAMPER_PATCH, base))), repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    assert "tests_tampered" in out
    assert [r["phase"] for r in _records(state)] == ["red"]


def test_verify_with_contract_records_green_after_valid_red(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base)
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    rc = _verify(_ctx(FakeClient(_result(GOOD_PATCH, base))), repo, state)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "green: valid" in out
    red, green = _records(state)
    assert green["phase"] == "green"
    assert green["classification"] == "passed"
    assert green["produced_by"]["role"] == "verifier"
    assert green["test_fingerprints"] == red["test_fingerprints"]
    assert green["contract_sha256"] == acdd.check_frozen(frozen)


def test_legacy_verify_is_labelled_and_unchanged(tmp_path, capsys):
    repo, base = _repo(tmp_path, "def test_ok():\n    assert True\n")
    state = tmp_path / "state"
    _lease(state, base, None)
    rc = _verify(_ctx(FakeClient(_result(GOOD_PATCH, base))), repo, state)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "no ACDD contract; TDD evidence not required" in out
    assert not (state / "evidence").exists()


# ------------------------------------------------------------ report


def test_tdd_report_never_shows_unrun_as_passed(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    path = _write(tmp_path / "frozen.json", _frozen(base))
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    rc = _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc != 0  # not accepted yet
    assert report["phases"]["red"] == "valid"
    assert report["phases"]["green"] == "unrun"
    assert report["criteria"]["AC-001"]["status"] != "passed"
    assert report["accepted"] is False


def test_evidence_key_is_private_and_stable(tmp_path):
    state = tmp_path / "state"
    first = leases.evidence_key(state)
    assert isinstance(first, bytes) and len(first) == 32
    assert leases.evidence_key(state) == first
