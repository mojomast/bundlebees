"""A verify dry run is a preview and must never replace the official record.

Every case drives the real ``cli._cmd_verify`` with a fake client and a real
temporary lease/worktree; ``sandbox.run_in_sandbox`` is replaced so no worker
code, network, launcher or push is involved.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from bundlebees_plugin import cli
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

OFFICIAL_SHA = "a" * 40
OFFICIAL_BRANCH = "feat/official"
OFFICIAL_KEYS = {
    "attempt_id",
    "ok",
    "sha",
    "branch",
    "files",
    "tests",
    "at",
    "evidence",
}


@pytest.fixture(autouse=True)
def _no_payload_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    monkeypatch.delenv("HYVV_SRC", raising=False)


def _canonical(state: Path) -> Path:
    return state / "verify-results" / f"{ATTEMPT_ID}.json"


def _side(state: Path) -> Path:
    return state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_official(state: Path) -> Path:
    target = _canonical(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "attempt_id": ATTEMPT_ID,
        "ok": True,
        "sha": OFFICIAL_SHA,
        "branch": OFFICIAL_BRANCH,
        "files": ["src/a.py"],
        "tests": ["tests/test_sample.py"],
        "at": "2026-11-01T00:00:00Z",
        "evidence": {
            "source_rev": "b" * 40,
            "backend": "bwrap",
            "exit_code": 0,
        },
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def _green_runner(workdir: Path, argv: list[str], **kwargs):
    return {
        "backend": "fake",
        "exit_code": 0,
        "timed_out": False,
        "output_tail": "",
    }


def _red_runner(workdir: Path, argv: list[str], **kwargs):
    return {
        "backend": "fake",
        "exit_code": 1,
        "timed_out": False,
        "output_tail": "FAILED tests/test_sample.py::test_sample",
    }


def _direct_result_files(state: Path) -> list[str]:
    root = state / "verify-results"
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_file())


def test_dry_run_green_never_replaces_the_official_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    canonical = _seed_official(state)
    before_bytes = canonical.read_bytes()
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    before_mtime = canonical.stat().st_mtime_ns
    before_files = _direct_result_files(state)

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _green_runner)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    after_sha = _sha256(canonical)
    assert after_sha == before_sha, f"canonical sha256 changed: {before_sha} -> {after_sha}"

    # The canonical record is byte-identical and untouched, and nothing new sits
    # beside it where the return report and revision readers look.
    assert canonical.read_bytes() == before_bytes
    assert canonical.stat().st_mtime_ns == before_mtime
    assert _direct_result_files(state) == before_files == [f"{ATTEMPT_ID}.json"]
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["sha"] == OFFICIAL_SHA
    assert payload["branch"] == OFFICIAL_BRANCH

    # The preview lives only at the reserved side path.
    side = _side(state)
    record = json.loads(side.read_text(encoding="utf-8"))
    assert record["dry_run"] is True
    assert record["ok"] is True
    assert record["sha"] is None
    assert sorted(path.name for path in side.parent.iterdir()) == [f"{ATTEMPT_ID}.json"]


def test_dry_run_refusal_never_replaces_the_official_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    canonical = _seed_official(state)
    before_bytes = canonical.read_bytes()
    before_sha = _sha256(canonical)
    before_mtime = canonical.stat().st_mtime_ns

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _red_runner)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert _sha256(canonical) == before_sha
    assert canonical.read_bytes() == before_bytes
    assert canonical.stat().st_mtime_ns == before_mtime
    assert _direct_result_files(state) == [f"{ATTEMPT_ID}.json"]

    record = json.loads(_side(state).read_text(encoding="utf-8"))
    assert record["dry_run"] is True
    assert record["ok"] is False
    assert record["code"] == "tests_failed"
    assert isinstance(record["error"], str) and record["error"]


def test_dry_run_without_an_official_record_leaves_it_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _green_runner)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    assert not _canonical(state).exists()
    assert _side(state).exists()
    assert _direct_result_files(state) == []


def test_dry_run_refusal_without_an_official_record_leaves_it_absent(tmp_path: Path):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert not _canonical(state).exists()
    record = json.loads(_side(state).read_text(encoding="utf-8"))
    assert record["dry_run"] is True
    assert record["ok"] is False
    assert record["code"] == "lease_missing"


def test_dry_run_side_record_keeps_todays_shape_and_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _green_runner)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    assert _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run")) == 0
    first = json.loads(_side(state).read_text(encoding="utf-8"))
    assert set(first) == OFFICIAL_KEYS | {"dry_run"}
    assert first["attempt_id"] == ATTEMPT_ID
    assert first["ok"] is True
    assert first["sha"] is None
    assert first["branch"] == BRANCH
    assert first["files"] == ["src/a.py"]
    assert first["tests"] == ["tests/test_sample.py"]
    assert "code" not in first
    assert "error" not in first

    _git(repo, "reset", "--hard", "-q")
    assert _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run")) == 0
    second = json.loads(_side(state).read_text(encoding="utf-8"))
    assert second["dry_run"] is True
    assert list((state / "verify-results" / "dry-run").iterdir()) == [_side(state)]
    assert not list((state / "verify-results" / "dry-run").glob("*.tmp"))
    assert not _canonical(state).exists()


def test_official_verify_still_writes_the_canonical_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", _green_runner)
    monkeypatch.setattr(cli, "_push_branch", lambda workdir, branch: None)
    monkeypatch.setattr(
        cli._delivery,
        "open_pr_idempotent",
        lambda *args, **kwargs: ("https://example.invalid/pr/1", True),
    )
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc == 0
    canonical = _canonical(state)
    record = json.loads(canonical.read_text(encoding="utf-8"))
    assert set(record) == OFFICIAL_KEYS
    assert "dry_run" not in record
    assert record["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{40}", record["sha"] or "")
    assert record["branch"] == BRANCH
    assert not _side(state).exists()


def test_official_refusal_still_writes_the_canonical_record(tmp_path: Path):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc != 0
    record = json.loads(_canonical(state).read_text(encoding="utf-8"))
    assert set(record) >= OFFICIAL_KEYS | {"code", "error"}
    assert "dry_run" not in record
    assert record["ok"] is False
    assert record["code"] == "lease_missing"
    assert not _side(state).exists()
