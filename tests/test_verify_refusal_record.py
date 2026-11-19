"""The on-disk verify refusal record must say *why* verify refused.

Each case drives the real ``cli._cmd_verify`` failure path with a fake client
and a real temporary lease/worktree, then reads the dry-run record at
``verify-results/dry-run/<attempt>.json`` from disk. No network, launcher, or
remote push is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import cli
from tests.test_cli import (
    ATTEMPT_ID,
    IN_LEASE_PATCH,
    FakeClient,
    _ctx,
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


def _flagged_patch(planted_value: str) -> str:
    flagged_line = "to" + "ken = \"" + planted_value + "\""
    return (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1,2 @@\n"
        " value = 1\n"
        f"+{flagged_line}\n"
    )


def test_lease_missing_refusal_is_auditable(tmp_path: Path):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    record = _record(state)
    assert record["ok"] is False
    assert record["code"] == "lease_missing"
    assert isinstance(record["error"], str) and record["error"]
    assert isinstance(record["files"], list)
    assert isinstance(record["tests"], list)


def test_secret_refusal_reason_is_scrubbed(tmp_path: Path):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    planted_value = "s" + "k-" + "Z" * 36
    client = FakeClient(_result(_flagged_patch(planted_value), base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    record = _record(state)
    assert record["ok"] is False
    assert record["code"] == "secret_detected"
    assert isinstance(record["error"], str) and record["error"]
    assert planted_value not in record["error"]
    assert planted_value not in json.dumps(record)


def test_worktree_refusal_records_its_code_and_reason(tmp_path: Path):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    (repo / "src" / "a.py").write_text("value = 1\ndirty = True\n", encoding="utf-8")
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    record = _record(state)
    assert record["ok"] is False
    assert record["code"] == "worktree_dirty"
    assert isinstance(record["error"], str) and record["error"]


def test_tests_failed_refusal_records_selected_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    seen: list[str] = []

    def fake_run(workdir: Path, argv: list[str], **kwargs):
        seen.append(argv[-1])
        return {
            "backend": "fake",
            "exit_code": 1,
            "timed_out": False,
            "output_tail": "FAILED tests/test_sample.py::test_sample",
        }

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", fake_run)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert seen == ["tests/test_sample.py"]
    record = _record(state)
    assert record["ok"] is False
    assert record["code"] == "tests_failed"
    assert isinstance(record["error"], str) and record["error"]
    assert record["tests"] == ["tests/test_sample.py"]


def test_success_record_keeps_its_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)

    def fake_run(workdir: Path, argv: list[str], **kwargs):
        return {
            "backend": "fake",
            "exit_code": 0,
            "timed_out": False,
            "output_tail": "",
        }

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", fake_run)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    record = _record(state)
    assert record["ok"] is True
    assert {
        "attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence", "dry_run",
    } <= set(record)
    assert record["dry_run"] is True
    assert record["attempt_id"] == ATTEMPT_ID
    assert record["branch"] == "feat/x"
    assert record["sha"] is None
    assert record["files"] == ["src/a.py"]
    assert record["tests"] == ["tests/test_sample.py"]
    assert "code" not in record
    assert "error" not in record
