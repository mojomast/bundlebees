"""Offline tests for landed-commit verification and its integrate acceptance.

Every test builds a real git repository under ``tmp_path`` (no remotes, no
network, no tokens) and injects a stub test runner, so no host sandbox and no
real test suite runs. The stub records the worktree it was handed, which is how
these tests prove the run happened at the landed commit and nowhere else.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from bundlebees_plugin import revision, security, verify_local

ATTEMPT = "att_2baaa852e7d645df"
TESTS = ["tests/test_sample.py"]


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env_extra: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        env=security.child_env(env_extra),
    )


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _repo_with_drifted_landing(tmp_path: Path, name: str = "repo") -> tuple[Path, str, str]:
    """A verified commit on ``feat`` and a *different* squash landing on main.

    The landing commit's message names the attempt (the bundlebees landing
    convention) while its content differs from the verified commit's, so
    neither ancestry nor per-file blob equality can accept the attempt.
    """
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Landed Verify Test")
    _git(repo, "config", "user.email", "landed@example.invalid")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    _commit(repo, "base")

    _git(repo, "checkout", "--quiet", "-b", "feat")
    (repo / "src" / "a.py").write_text("value = 10\n", encoding="utf-8")
    verified = _commit(repo, "verified change")

    _git(repo, "checkout", "--quiet", "main")
    (repo / "src" / "a.py").write_text("value = 11  # refactored in landing\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("extra = True\n", encoding="utf-8")
    landed = _commit(repo, f"bundlebees: apply verified attempt {ATTEMPT}")
    return repo, verified, landed


def _worktree_paths(repo: Path) -> list[str]:
    """Every path git has registered as a worktree of *repo*."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "verify-results").mkdir(parents=True)
    return state


def _write_verify(state: Path, attempt_id: str, sha: str, *, ok: bool = True) -> None:
    target = state / "verify-results" / f"{attempt_id}.json"
    target.write_text(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "ok": ok,
                "sha": sha,
                "branch": "feat/x",
                "files": ["src/a.py"],
                "tests": TESTS,
                "at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _runner(
    *,
    calls: list[tuple[Path, list[str], str]] | None = None,
    evidence: Mapping[str, Any] | None = None,
):
    """A stub runner: records the worktree/argv/revision, returns fixed evidence."""

    def run(workdir: Path, argv: Sequence[str]) -> dict[str, Any]:
        head = _git(Path(workdir), "rev-parse", "HEAD").stdout.strip()
        if calls is not None:
            calls.append((Path(workdir), list(argv), head))
        result: dict[str, Any] = {
            "backend": "stub",
            "exit_code": 0,
            "timed_out": False,
            "duration_s": 0.5,
            "junit": {"tests": 4, "failures": 0, "errors": 0, "skipped": 0},
            "output_tail": "4 passed",
        }
        result.update(dict(evidence or {}))
        return result

    return run


def _suite_runner(calls: list[Path]):
    def run(workdir: Path) -> dict:
        calls.append(Path(workdir))
        return {
            "backend": "stub",
            "exit_code": 0,
            "timed_out": False,
            "junit": {"tests": 9, "failures": 0, "errors": 0},
            "output_tail": "ok",
        }

    return run


# ---------------------------------------------------------------------------
# verify-landed refusals
# ---------------------------------------------------------------------------


def test_verify_landed_refuses_a_sha_not_in_head(tmp_path: Path):
    repo, verified, _landed = _repo_with_drifted_landing(tmp_path)
    state = _state(tmp_path)

    with pytest.raises(verify_local.VerifyError) as refused:
        verify_local.verify_landed(state, repo, verified, TESTS, runner=_runner())

    assert refused.value.code == "landed_sha_not_in_head"
    assert refused.value.message == f"commit {verified} is not an ancestor of HEAD"
    assert not (state / "verify-results" / f"landed-{verified}.json").exists()
    # No worktree was created and the checkout was not touched.
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert _worktree_paths(repo) == [str(repo)]


def test_verify_landed_refuses_a_dirty_worktree(tmp_path: Path):
    repo, _verified, landed = _repo_with_drifted_landing(tmp_path)
    state = _state(tmp_path)
    (repo / "src" / "a.py").write_text("value = 99\n", encoding="utf-8")

    with pytest.raises(verify_local.VerifyError) as refused:
        verify_local.verify_landed(state, repo, landed, TESTS, runner=_runner())

    assert refused.value.code == "workdir_dirty"
    assert refused.value.message == "the checkout has uncommitted changes"
    assert not list((state / "verify-results").glob("landed-*.json"))
    assert _worktree_paths(repo) == [str(repo)]


# ---------------------------------------------------------------------------
# verify-landed record
# ---------------------------------------------------------------------------


def _assert_landed_record(record, path, state, landed, tests) -> None:
    """The record must be exactly the schema, on disk, and readable back."""
    assert set(record) == {"schema", "sha", "tests", "ok", "counts", "sandbox", "created_at"}
    assert record == {
        "schema": "rizzstack.verify-landed/1",
        "sha": landed,
        "tests": tests,
        "ok": True,
        "counts": {
            "targets": 2,
            "tests": 4,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "passed": 4,
        },
        "sandbox": {
            "backend": "stub",
            "exit_code": 0,
            "timed_out": False,
            "duration_s": 0.5,
            "timeout_s": verify_local.DEFAULT_SANDBOX_TIMEOUT_S,
            "output_tail": "4 passed",
        },
        "created_at": "2026-11-24T12:00:00Z",
    }
    assert json.loads(path.read_text(encoding="utf-8")) == record
    assert verify_local.read_landed_record(state, landed.upper()) == record
    assert path.stat().st_mode & 0o777 == 0o600


def _assert_landed_worktree(calls, repo, landed, tests) -> None:
    """The run happened once, at the landed sha, in a now-removed worktree."""
    assert len(calls) == 1
    worktree, argv, revision_seen = calls[0]
    assert revision_seen == landed
    assert worktree != repo
    assert all(target in argv for target in tests)
    assert argv[1:5] == ["-B", "-m", "pytest", "-p"]
    assert str(worktree) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_verify_landed_writes_the_record_for_the_landed_tree(tmp_path: Path):
    repo, _verified, landed = _repo_with_drifted_landing(tmp_path)
    state = _state(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    calls: list[tuple[Path, list[str], str]] = []
    tests = ["tests/test_sample.py", "tests/test_other.py"]

    record = verify_local.verify_landed(
        state,
        repo,
        landed.upper(),
        tests,
        runner=_runner(calls=calls),
        now="2026-11-24T12:00:00Z",
    )

    path = state / "verify-results" / f"landed-{landed}.json"
    _assert_landed_record(record, path, state, landed, tests)
    _assert_landed_worktree(calls, repo, landed, tests)

    # The checkout's revision and contents were never modified.
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _git(repo, "status", "--porcelain").stdout == ""

    # The schema the writer emits is the one integration consumes.
    assert revision.LANDED_VERIFY_SCHEMA == verify_local.LANDED_RECORD_SCHEMA


# ---------------------------------------------------------------------------
# integrate acceptance
# ---------------------------------------------------------------------------


def test_integrate_accepts_an_attempt_with_a_matching_landed_record(tmp_path: Path):
    repo, verified, landed = _repo_with_drifted_landing(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, ATTEMPT, verified)
    landed_record = verify_local.verify_landed(
        state, repo, landed, TESTS, runner=_runner(), now="2026-11-24T12:00:00Z"
    )
    assert landed_record["ok"] is True
    suites: list[Path] = []

    record = revision.integrate_wave(
        state,
        "wave-a",
        repo,
        [ATTEMPT],
        _suite_runner(suites),
        now="2026-11-24T12:00:01Z",
    )

    assert record["accepted"] is True
    assert record["problems"] == {}
    assert "replay_evidence" not in record
    proof = record["landed_evidence"][ATTEMPT]
    assert proof == {
        "landed_sha": landed,
        "record": f"verify-results/landed-{landed}.json",
        "tests": TESTS,
        "method": "landed-tests",
    }
    assert suites == [repo]
    assert revision.load_wave(state, "wave-a") == record


def test_integrate_refuses_when_the_landed_record_sha_is_elsewhere(tmp_path: Path):
    repo, verified, _landed = _repo_with_drifted_landing(tmp_path)
    # A landing on a side branch names the same attempt but is not in HEAD.
    _git(repo, "checkout", "--quiet", "-b", "side")
    (repo / "src" / "a.py").write_text("value = 12\n", encoding="utf-8")
    elsewhere = _commit(repo, f"bundlebees: apply verified attempt {ATTEMPT}")

    state = _state(tmp_path)
    record = verify_local.verify_landed(state, repo, elsewhere, TESTS, runner=_runner())
    assert record["ok"] is True

    _git(repo, "checkout", "--quiet", "main")
    _write_verify(state, ATTEMPT, verified)
    suites: list[Path] = []

    wave = revision.integrate_wave(
        state, "wave-elsewhere", repo, [ATTEMPT], _suite_runner(suites)
    )

    assert wave["accepted"] is False
    problem = wave["problems"][ATTEMPT]
    assert problem.startswith(revision.PROBLEM_LANDED_NOT_IN_HEAD)
    assert "is not in HEAD's history" in problem
    assert "not an ancestor" not in problem
    assert "landed_evidence" not in wave
    assert suites == []
