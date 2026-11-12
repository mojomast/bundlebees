"""Focused extra tests for ``hermes bundlebees tdd refactor``.

These complement the approved acceptance tests: argument exclusivity, revision
validation, ancestry against GREEN, and the explicit "no refactor needed"
record. They reuse the temporary-repository helpers from the approved suites.
"""

from __future__ import annotations

import pytest

from tests.test_cli_acdd import _ctx, _git, _invoke, _records
from tests.test_cli_acdd_refactor import _commit, _refactor, _verified


def test_refactor_rev_and_none_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        _invoke(_ctx(), ["tdd", "refactor", "contract.json", "--rev", "a" * 40, "--none"])
    assert exc.value.code == 2


def test_refactor_non_hex_rev_is_refused(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    rc = _refactor(repo, state, path, "--rev", "not-a-sha")
    out = capsys.readouterr().out
    assert rc != 0
    assert "invalid_argument" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_refactor_revision_not_descending_from_green_is_refused(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    _git(repo, "checkout", "-b", "side", "HEAD~1")
    rev = _commit(repo, "src/a.py", "value = 9\n", "side refactor")
    rc = _refactor(repo, state, path, "--rev", rev)
    out = capsys.readouterr().out
    assert rc != 0
    assert "stale_revision" in out
    assert [r["phase"] for r in _records(state)] == ["red", "green"]


def test_no_refactor_needed_notes_and_reuses_green_revision(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    green = _records(state)[-1]
    assert _refactor(repo, state, path, "--none", "--note", "still minimal") == 0
    capsys.readouterr()
    record = _records(state)[-1]
    assert record["revision"] == green["revision"]
    assert record["test_fingerprints"] == green["test_fingerprints"]
    assert record["notes"] == "no refactor needed: still minimal"
