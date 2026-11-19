"""A dry run previews a verification; it never replaces the official record.

Every case drives the real ``cli._cmd_verify`` with a temporary lease/worktree,
a fake client and a hermetic sandbox runner. The canonical
``verify-results/<attempt>.json`` belongs to a committing verify; dry-run
evidence goes to ``verify-results/dry-run/<attempt>.json``.
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
    OUT_OF_LEASE_PATCH,
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
OFFICIAL_AT = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _no_payload_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    monkeypatch.delenv("HYVV_SRC", raising=False)


@pytest.fixture
def green_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(workdir: Path, argv: list[str], **kwargs):
        return {
            "backend": "fake",
            "exit_code": 0,
            "timed_out": False,
            "output_tail": "",
        }

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", fake_run)


def _official_record() -> dict:
    return {
        "attempt_id": ATTEMPT_ID,
        "ok": True,
        "sha": OFFICIAL_SHA,
        "branch": OFFICIAL_BRANCH,
        "files": ["src/a.py"],
        "tests": ["tests/test_sample.py"],
        "at": OFFICIAL_AT,
        "evidence": {"source_rev": "b" * 40},
    }


def _seed_official(state: Path) -> Path:
    path = state / "verify-results" / f"{ATTEMPT_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_official_record(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _side_record(state: Path) -> Path:
    return state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(path: Path) -> tuple[str, int]:
    stat = path.stat()
    return _sha256(path), stat.st_mtime_ns


def test_dry_run_green_leaves_the_official_record_byte_identical(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    official = _seed_official(state)
    before_sha, before_mtime = _snapshot(official)
    before_top_level = sorted(p.name for p in (state / "verify-results").glob("*.json"))
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    assert _sha256(official) == before_sha
    assert official.stat().st_mtime_ns == before_mtime
    saved = json.loads(official.read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert saved["sha"] == OFFICIAL_SHA
    assert saved["branch"] == OFFICIAL_BRANCH

    side = _side_record(state)
    payload = json.loads(side.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["ok"] is True
    assert payload["sha"] is None
    assert payload["attempt_id"] == ATTEMPT_ID
    assert payload["evidence"]["exit_code"] == 0
    assert sorted(p.name for p in (state / "verify-results").glob("*.json")) == before_top_level
    assert sorted(p.name for p in side.parent.glob("*")) == [side.name]


def test_dry_run_refusal_leaves_the_official_record_byte_identical(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    official = _seed_official(state)
    before_sha, before_mtime = _snapshot(official)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert _sha256(official) == before_sha
    assert official.stat().st_mtime_ns == before_mtime
    saved = json.loads(official.read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert saved["sha"] == OFFICIAL_SHA

    payload = json.loads(_side_record(state).read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["ok"] is False
    assert payload["code"] == "scope_violation"
    assert isinstance(payload["error"], str) and payload["error"]


def test_dry_run_does_not_create_a_canonical_record(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    assert not (state / "verify-results" / f"{ATTEMPT_ID}.json").exists()
    side = _side_record(state)
    assert side.exists()
    assert json.loads(side.read_text(encoding="utf-8"))["dry_run"] is True


def test_refused_dry_run_does_not_create_a_canonical_record(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert not (state / "verify-results" / f"{ATTEMPT_ID}.json").exists()
    assert json.loads(_side_record(state).read_text(encoding="utf-8"))["ok"] is False


def test_repeated_dry_runs_write_one_side_record_without_temp_files(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))
    argv = _verify_argv(repo, state, "--dry-run")

    assert _invoke(_ctx(client), argv) == 0
    first = json.loads(_side_record(state).read_text(encoding="utf-8"))
    _git(repo, "reset", "--hard", base_sha)
    assert _invoke(_ctx(client), argv) == 0
    second = json.loads(_side_record(state).read_text(encoding="utf-8"))

    side_dir = _side_record(state).parent
    assert sorted(p.name for p in side_dir.iterdir()) == [f"{ATTEMPT_ID}.json"]
    assert set(first) == set(second) == {
        "attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence", "dry_run",
    }
    assert second["dry_run"] is True
    assert second["sha"] is None


def test_official_verify_keeps_the_canonical_shape(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc == 0
    canonical = state / "verify-results" / f"{ATTEMPT_ID}.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert set(payload) == {"attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence"}
    assert "dry_run" not in payload
    assert payload["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{40}", payload["sha"] or "")
    assert payload["branch"] == BRANCH
    assert not _side_record(state).exists()


def test_official_refusal_keeps_the_canonical_shape(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc != 0
    canonical = state / "verify-results" / f"{ATTEMPT_ID}.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert set(payload) == {
        "attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence", "code", "error",
    }
    assert "dry_run" not in payload
    assert payload["ok"] is False
    assert payload["code"] == "scope_violation"
    assert not _side_record(state).exists()
