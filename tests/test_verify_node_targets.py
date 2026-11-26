"""Verify selects a Node test runner for .mjs/.cjs/.js targets.

Every case drives the real ``cli._cmd_verify`` test step with a fake client and a
real temporary lease/worktree. ``sandbox.run_in_sandbox`` is replaced so the
tests record the selected argv without executing anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import cli, oracle_lock, sandbox
from tests.test_cli import (
    ATTEMPT_ID,
    BRANCH,
    IN_LEASE_PATCH,
    FakeClient,
    _ctx,
    _git,
    _invoke,
    _lease,
    _repo,
    _result,
    _verify_argv,
)


@pytest.fixture(autouse=True)
def _no_payload_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    monkeypatch.delenv("HYVV_SRC", raising=False)


def _record(state: Path) -> dict:
    return json.loads(
        (state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json").read_text()
    )


def _commit_targets(repo: Path, files: dict[str, str]) -> None:
    """Put target files on the feature branch so verify sees them in the worktree."""
    _git(repo, "checkout", "-q", BRANCH)
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add test targets")
    _git(repo, "checkout", "-q", "main")


def _fake_runner(seen: list[list[str]], *, exit_code: int = 0):
    def fake_run(workdir: Path, argv: list[str], **kwargs):
        seen.append(list(argv))
        return {
            "backend": "fake",
            "exit_code": exit_code,
            "timed_out": False,
            "output_tail": "ok",
        }

    return fake_run


MJS_FILES = {
    "tests/a.test.mjs": "export const a = true;\n",
    "tests/b.test.mjs": "export const b = true;\n",
}


def test_two_mjs_targets_run_through_node_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    _commit_targets(repo, MJS_FILES)
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _fake_runner(seen))
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(
        _ctx(client),
        _verify_argv(
            repo, state, "--dry-run", "--tests", "tests/a.test.mjs", "tests/b.test.mjs"
        ),
    )

    assert rc == 0
    assert seen == [["node", "--test", "tests/a.test.mjs", "tests/b.test.mjs"]]
    record = _record(state)
    assert record["evidence"]["runner"] == "node-test"
    assert record["evidence"]["test_argv"] == "node --test tests/a.test.mjs tests/b.test.mjs"


def test_a_py_target_still_selects_pytest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _fake_runner(seen))
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(
        _ctx(client), _verify_argv(repo, state, "--dry-run", "--tests", "tests/test_sample.py")
    )

    assert rc == 0
    # Verify's oracle runs with main's pytest configuration, never the PR's.
    lock = oracle_lock.from_git(repo, base_sha)
    try:
        expected = oracle_lock.locked_argv(
            lock, sandbox.pytest_argv(["tests/test_sample.py"]), rootdir=repo
        )
    finally:
        lock.cleanup()
    assert seen == [expected]
    record = _record(state)
    assert record["evidence"]["runner"] == "pytest"


def test_mixed_python_and_node_targets_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    _commit_targets(repo, MJS_FILES)
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _fake_runner(seen))
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(
        _ctx(client),
        _verify_argv(
            repo, state, "--dry-run", "--tests", "tests/test_sample.py", "tests/a.test.mjs"
        ),
    )

    assert rc != 0
    assert seen == []
    record = _record(state)
    assert record["code"] == "mixed_test_targets"
    assert "tests/a.test.mjs" in record["error"]


def test_missing_node_refuses_with_a_named_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    _commit_targets(repo, MJS_FILES)

    def missing_node(workdir: Path, argv: list[str], **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "node")

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", missing_node)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(
        _ctx(client), _verify_argv(repo, state, "--dry-run", "--tests", "tests/a.test.mjs")
    )

    assert rc != 0
    record = _record(state)
    assert record["code"] == "node_runner_unavailable"
    assert record["sha"] is None


def test_refusal_record_keeps_code_and_error_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    _commit_targets(repo, MJS_FILES)
    seen: list[list[str]] = []
    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _fake_runner(seen))
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(
        _ctx(client),
        _verify_argv(
            repo, state, "--dry-run", "--tests", "tests/test_sample.py", "tests/b.test.mjs"
        ),
    )

    assert rc != 0
    assert seen == []
    record = _record(state)
    assert record["ok"] is False
    assert record["code"] == "mixed_test_targets"
    assert isinstance(record["error"], str) and record["error"]
