"""The main ``verify`` command must refuse a patch that can subvert the run.

WA-A1A wired the subversion scan into ``verify_local.verify_and_commit`` only;
``cli._cmd_verify`` (the ``verify`` command a coordinator reaches) applied the
patch and ran the sandbox without scanning it. These approved cases drive the
real ``_cmd_verify`` with a fake client and a real temporary lease/worktree
(no network, no launcher, no remote push) and pin the refusal to the scan:

* a subverting patch is refused with code ``test_subversion``, naming the
  finding, before any worktree, sandbox, commit or push work;
* the same patch clears every other gate (the negative case), so only the scan
  can be responsible for the refusal;
* a clean patch still verifies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bundlebees_plugin import cli, verify_local
from tests.test_cli import (
    ATTEMPT_ID,
    FakeClient,
    _ctx,
    _git,
    _invoke,
    _lease,
    _repo,
    _result,
    _verify_argv,
)

#: An in-lease patch that hands the runner an early exit. ``os._exit(0)`` in an
#: added line is a ``forced_exit`` finding for ``subversion_scan.scan_patch``.
SUBVERSION_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,3 @@
 value = 1
+import os
+os._exit(0)
"""

CLEAN_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 value = 1
+value = 2
"""


def _record(state: Path) -> dict[str, Any]:
    return json.loads(
        (state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json").read_text()
    )


class _Doubles:
    """Hermetic stand-ins that record every step the refusal must prevent."""

    def __init__(self) -> None:
        self.sandbox: list[list[str]] = []
        self.commits: list[str] = []
        self.pushes: list[str] = []

    def run_in_sandbox(self, _workdir: Path, argv: list[str], **_kwargs: Any) -> dict[str, Any]:
        self.sandbox.append(list(argv))
        return {"backend": "fake", "exit_code": 0, "timed_out": False, "output_tail": ""}

    def commit_changes(self, workdir: Path, attempt_id: str, files: list[str]) -> str:
        self.commits.append(attempt_id)
        raise AssertionError("verify must refuse before committing")

    def push_branch(self, workdir: Path, branch: str) -> None:
        self.pushes.append(branch)
        raise AssertionError("verify must refuse before pushing")


def _drive(
    repo: Path, state: Path, patch: str, base_sha: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, cli.CliError | None, _Doubles]:
    """Run ``verify --dry-run``; return ``(rc, propagated error, steps that ran)``."""
    doubles = _Doubles()
    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", doubles.run_in_sandbox)
    monkeypatch.setattr(cli, "_commit_changes", doubles.commit_changes)
    monkeypatch.setattr(cli, "_push_branch", doubles.push_branch)
    client = FakeClient(_result(patch, base_rev=base_sha))
    try:
        rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    except cli.CliError as exc:  # some entry points propagate instead of printing
        return 1, exc, doubles
    return rc, None, doubles


def _code(error: cli.CliError | None, rc: int, state: Path) -> str:
    if error is not None:
        return str(error.code)
    return str(_record(state).get("code") or "")


def _setup(tmp_path: Path) -> tuple[Path, Path, str]:
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    return repo, state, base_sha


def test_cli_verify_refuses_a_subverting_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, state, base_sha = _setup(tmp_path)

    rc, error, doubles = _drive(repo, state, SUBVERSION_PATCH, base_sha, monkeypatch)

    assert rc != 0, "the verify command accepted a subverting patch"
    assert _code(error, rc, state) == "test_subversion"
    record = _record(state)
    assert record["ok"] is False
    assert "forced_exit" in record["error"]
    assert "src/a.py" in record["error"]
    assert doubles.sandbox == []


def test_cli_verify_refusal_precedes_worktree_sandbox_commit_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, state, base_sha = _setup(tmp_path)

    rc, error, doubles = _drive(repo, state, SUBVERSION_PATCH, base_sha, monkeypatch)

    assert rc != 0, "the verify command accepted a subverting patch"
    assert _code(error, rc, state) == "test_subversion"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    assert doubles.sandbox == []
    assert doubles.commits == []
    assert doubles.pushes == []


def test_cli_verify_accepts_a_clean_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, state, base_sha = _setup(tmp_path)

    rc, error, doubles = _drive(repo, state, CLEAN_PATCH, base_sha, monkeypatch)

    assert rc == 0, f"a clean patch was refused: {_code(error, rc, state)}"
    assert len(doubles.sandbox) == 1
    record = _record(state)
    assert record["ok"] is True
    assert "code" not in record


def test_the_scan_alone_refuses_the_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Negative case: without the scan the old path accepts this patch.

    Every other gate (lease scope, revision checks, branch, sandbox) passes for
    ``SUBVERSION_PATCH``; that is what made the CLI gap exploitable, and this
    case fails if a future change starts refusing unrelated patches.
    """
    repo, state, base_sha = _setup(tmp_path)
    monkeypatch.setattr(verify_local, "check_patch_subversion", lambda *a, **k: [])
    monkeypatch.setattr(cli, "check_patch_subversion", lambda *a, **k: [], raising=False)

    rc, error, _doubles = _drive(repo, state, SUBVERSION_PATCH, base_sha, monkeypatch)

    assert rc == 0, f"something other than the scan refused: {_code(error, rc, state)}"
    record = _record(state)
    assert record["ok"] is True
    assert "code" not in record
