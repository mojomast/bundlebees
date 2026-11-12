"""Approved acceptance tests for contract BF1-fixture-test-files (found by the combined self-integration, RED first).

A contract's ``scope.test_files`` protects every approved test asset, including non-Python fixtures such as a
node server a Python test starts. Those assets are fingerprinted and protected from worker edits, but only
pytest-collectable targets are passed to pytest. Before the fix, ``pytest tests/fixture.mjs`` exits 4
("not found"), no case is recorded, and RED is reported as ``red_incomplete`` for a correct contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bundlebees_plugin import security
from tests.test_cli_acdd import (APPROVED_TEST, BRANCH, GOOD_PATCH, TEST_ID, VALUE_TEST, FakeClient, _ctx, _frozen,
                                 _git, _invoke, _lease, _records, _result, _verify, _write)

FIXTURE = "tests/fixture_server.mjs"
TAMPER_FIXTURE = """diff --git a/tests/fixture_server.mjs b/tests/fixture_server.mjs
--- a/tests/fixture_server.mjs
+++ b/tests/fixture_server.mjs
@@ -1 +1 @@
-export const port = 0;
+export const port = 1;
"""


def _repo_with_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "work"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / APPROVED_TEST).write_text(VALUE_TEST, encoding="utf-8")
    (repo / FIXTURE).write_text("export const port = 0;\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "approved tests and fixture (RED)")
    base = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, env=security.child_env())
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "branch", BRANCH)
    _git(repo, "push", "-u", "origin", BRANCH)
    return repo, base


def _contract_with_fixture(base: str) -> dict:
    return _frozen(base, scope={"assigned_files": ["src/a.py"], "test_files": [APPROVED_TEST, FIXTURE], "prohibited": []})


def test_red_records_the_failing_case_when_test_files_include_a_non_python_fixture(tmp_path, capsys):
    repo, base = _repo_with_fixture(tmp_path)
    state = tmp_path / "state"
    path = _write(tmp_path / "frozen.json", _contract_with_fixture(base))
    rc = _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 0, out
    red = _records(state)[-1]
    assert red["phase"] == "red"
    assert any(c["id"].endswith("test_value_is_two") and c["outcome"] == "failed" for c in red["result"]["cases"]), red["result"]


def test_green_passes_and_the_fixture_stays_protected(tmp_path, capsys):
    repo, base = _repo_with_fixture(tmp_path)
    state = tmp_path / "state"
    frozen = _contract_with_fixture(base)
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    assert _verify(_ctx(FakeClient(_result(GOOD_PATCH, base))), repo, state) == 0, capsys.readouterr().out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]
    capsys.readouterr()
    # A worker patch that edits the approved fixture is refused.
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard"], check=True)
    subprocess.run(["git", "-C", str(repo), "clean", "-qfd"], check=True)
    rc = _verify(_ctx(FakeClient(_result(GOOD_PATCH + TAMPER_FIXTURE, base))), repo, state)
    assert rc != 0
    assert [r["phase"] for r in _records(state)] == ["red", "green"]
