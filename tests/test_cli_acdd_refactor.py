"""Approved acceptance tests for ``hermes bundlebees tdd refactor`` (written first, RED).

REFACTOR is a later revision that must keep the approved tests green with the
same fingerprints. ``tdd refactor`` requires a valid RED and GREEN, refuses a
revision that does not descend from GREEN or that edits approved tests or
paths outside the contract's assigned files, reruns the approved tests in the
sandbox and records a signed ``refactor`` record. ``--none --note`` records an
explicit "no refactor needed" on the GREEN revision instead of skipping it.
"""

from __future__ import annotations

import json

from tests.test_cli_acdd import (
    APPROVED_TEST, ATTEMPT_ID, BRANCH, GOOD_PATCH, FakeClient, _ctx, _frozen, _git, _invoke,
    _lease, _records, _repo, _result, _write,
)


def _verified(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base, evidence_required=["red", "green", "refactor"])
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    _git(repo, "checkout", BRANCH)
    rc = _invoke(_ctx(FakeClient(_result(GOOD_PATCH, base))),
                 ["verify", ATTEMPT_ID, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])
    assert rc == 0, capsys.readouterr().out
    capsys.readouterr()
    return repo, state, path


def _commit(repo, rel, text, message):
    (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _refactor(repo, state, path, *extra):
    return _invoke(_ctx(), ["tdd", "refactor", str(path), "--workdir", str(repo), "--state-dir", str(state), *extra])


def _report(state, path, capsys):
    _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    return json.loads(capsys.readouterr().out)


def test_refactor_that_stays_green_is_recorded(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    rev = _commit(repo, "src/a.py", "value = 1 + 1\n", "refactor")
    rc = _refactor(repo, state, path, "--rev", rev)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "refactor: valid" in out
    records = _records(state)
    assert [r["phase"] for r in records] == ["red", "green", "refactor"]
    refactor = records[-1]
    assert refactor["revision"] == rev
    assert refactor["classification"] == "passed"
    assert refactor["produced_by"]["role"] == "verifier"
    assert refactor["test_fingerprints"] == records[1]["test_fingerprints"]
    report = _report(state, path, capsys)
    assert report["phases"]["refactor"] == "valid"
    assert report["accepted"] is True


def test_refactor_that_breaks_the_tests_is_refused_and_recorded(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    rev = _commit(repo, "src/a.py", "value = 3\n", "bad refactor")
    rc = _refactor(repo, state, path, "--rev", rev)
    out = capsys.readouterr().out
    assert rc != 0
    assert "refactor_failed" in out
    refactor = _records(state)[-1]
    assert refactor["phase"] == "refactor"
    assert refactor["classification"] != "passed"
    assert _report(state, path, capsys)["accepted"] is False


def test_refactor_that_edits_approved_tests_is_refused(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    rev = _commit(repo, APPROVED_TEST, "def test_value_is_two():\n    assert True\n", "weaken")
    rc = _refactor(repo, state, path, "--rev", rev)
    out = capsys.readouterr().out
    assert rc != 0
    assert "tests_tampered" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_refactor_outside_assigned_files_is_refused(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    rev = _commit(repo, "src/other.py", "x = 1\n", "outside scope")
    rc = _refactor(repo, state, path, "--rev", rev)
    out = capsys.readouterr().out
    assert rc != 0
    assert "scope_violation" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_no_refactor_needed_is_recorded_explicitly(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    green_rev = _records(state)[-1]["revision"]
    rc = _refactor(repo, state, path, "--none", "--note", "already minimal")
    out = capsys.readouterr().out
    assert rc == 0, out
    refactor = _records(state)[-1]
    assert refactor["phase"] == "refactor"
    assert refactor["revision"] == green_rev
    assert "already minimal" in refactor.get("notes", "")
    assert _report(state, path, capsys)["phases"]["refactor"] == "valid"


def test_refactor_requires_green(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    path = _write(tmp_path / "frozen.json", _frozen(base, evidence_required=["red", "green", "refactor"]))
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    rc = _refactor(repo, state, path, "--rev", base)
    out = capsys.readouterr().out
    assert rc != 0
    assert "green_failed" in out
