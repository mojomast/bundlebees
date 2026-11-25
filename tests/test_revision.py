"""Offline tests for revision provenance and wave integration.

Every test builds a real git repository under ``tmp_path`` (no remotes, no
network, no tokens) and uses ``security.child_env`` for child processes.
"""

from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Mapping

import pytest

from bundlebees_plugin import leases, revision, security


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    env_extra: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
        env=security.child_env(env_extra),
    )


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with a base commit and a later commit on the same line."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Revision Test")
    _git(repo, "config", "user.email", "revision@example.invalid")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "src" / "a.py").write_text("value = 10\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "later")
    later = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base, later


def _orphan_commit(repo: Path) -> str:
    """Create an unrelated root commit and return to main."""
    _git(repo, "checkout", "--quiet", "--orphan", "side")
    _git(repo, "rm", "-rf", "--quiet", ".")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--quiet", "main")
    return side


def _squash_repo(tmp_path: Path, name: str = "repo") -> tuple[Path, str, str]:
    """A verified commit on its own branch, squash-landed on ``main``.

    The verified commit survives in the object store on ``feat`` but is not an
    ancestor of ``main``: exactly what a merge queue produces when it squash-
    lands the verified work as a new commit.
    """
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Revision Test")
    _git(repo, "config", "user.email", "revision@example.invalid")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    _git(repo, "checkout", "--quiet", "-b", "feat")
    (repo / "src" / "a.py").write_text("value = 10\n", encoding="utf-8")
    _git(repo, "commit", "-am", "verified change")
    verified = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "--quiet", "main")
    _git(repo, "merge", "--squash", "feat")
    _git(repo, "commit", "-m", "squash landing")
    landed = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, verified, landed


def _plain_repo(tmp_path: Path, name: str, origin: str) -> Path:
    """A one-commit repository whose origin names the repository it is."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Revision Test")
    _git(repo, "config", "user.email", "revision@example.invalid")
    _git(repo, "remote", "add", "origin", origin)
    (repo / "a.py").write_text(f"repo = {name!r}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def _lease_with_repo(state: Path, attempt_id: str, sha: str, repo: str) -> None:
    leases.record_lease(
        state,
        attempt_id,
        {
            "source_rev": sha,
            "assigned_files": ["src/a.py"],
            "acceptance": {"tests": ["tests/test_sample.py"]},
            "repo": repo,
        },
    )


def _write_verify(
    state: Path,
    attempt_id: str,
    sha: str,
    *,
    ok: bool = True,
) -> None:
    target = state / "verify-results" / f"{attempt_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "ok": ok,
                "sha": sha,
                "branch": "feat/x",
                "files": ["src/a.py"],
                "tests": ["tests/test_sample.py"],
                "at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "verify-results").mkdir(parents=True)
    return state


def _suite(
    *,
    exit_code: int = 0,
    failures: int = 0,
    errors: int = 0,
    timed_out: bool = False,
) -> dict:
    return {
        "backend": "pytest",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "junit": {"tests": 3, "failures": failures, "errors": errors},
        "output_tail": "ok",
        "duration_s": 0.25,
    }


def _runner(evidence: dict, calls: list[Path]):
    def run(workdir: Path) -> dict:
        calls.append(Path(workdir))
        return dict(evidence)

    return run


# ---------------------------------------------------------------------------
# Child environment
# ---------------------------------------------------------------------------


def test_security_allows_git_index_and_object_directory_extras():
    env = security.child_env(
        {"GIT_INDEX_FILE": "index", "GIT_OBJECT_DIRECTORY": "objects"}
    )
    assert env["GIT_INDEX_FILE"] == "index"
    assert env["GIT_OBJECT_DIRECTORY"] == "objects"


# ---------------------------------------------------------------------------
# Declared base
# ---------------------------------------------------------------------------


def test_base_keys_and_declared_base_locations(tmp_path: Path):
    _repo_path, base, later = _repo(tmp_path)

    assert revision.BASE_KEYS == ("base_rev", "source_rev", "base_sha", "base")
    assert revision.declared_base({}) is None
    assert revision.declared_base({"base_rev": base}) == base
    assert revision.declared_base({"source_rev": later}) == later
    assert revision.declared_base({"base": "main", "base_sha": base}) == base
    assert revision.declared_base({"observed": {"base_rev": base}}) == base
    assert revision.declared_base({"observed": base}) == base
    assert revision.declared_base({"result": {"source_rev": later}}) == later
    # Top-level wins over nested evidence.
    assert revision.declared_base(
        {"source_rev": later, "observed": {"base_rev": base}}
    ) == later
    # Non-hex values are skipped rather than returned.
    assert revision.declared_base({"base": "main"}) is None


def test_check_declared_base_undeclared_mismatch_and_case_insensitive_match():
    source = "a" * 40

    with pytest.raises(revision.RevisionError) as missing:
        revision.check_declared_base({}, source)
    assert missing.value.code == "base_undeclared"

    with pytest.raises(revision.RevisionError) as mismatch:
        revision.check_declared_base({"base_rev": "b" * 40}, source)
    assert mismatch.value.code == "base_mismatch"

    declared = revision.check_declared_base({"base_rev": source.upper()}, source)
    assert declared == source.upper()
    assert revision.check_declared_base({"observed": {"base_sha": source}}, source) == source


# ---------------------------------------------------------------------------
# Source revision and patch provenance
# ---------------------------------------------------------------------------


def test_check_source_rev_rejects_missing_or_non_hex(tmp_path: Path):
    repo, base, _later = _repo(tmp_path)

    revision.check_source_rev(repo, base)

    with pytest.raises(revision.RevisionError) as missing:
        revision.check_source_rev(repo, "0" * 40)
    assert missing.value.code == "source_rev_missing"

    with pytest.raises(revision.RevisionError) as malformed:
        revision.check_source_rev(repo, "main")
    assert malformed.value.code == "source_rev_missing"


CORRECT_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

WRONG_BASE_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-value = 10
+value = 20
"""


def test_check_applies_at_source_rev_and_leaves_real_index_untouched(tmp_path: Path):
    repo, base, _later = _repo(tmp_path)

    revision.check_applies_at(repo, base, CORRECT_PATCH)
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 10\n"

    with pytest.raises(revision.RevisionError) as wrong:
        revision.check_applies_at(repo, base, WRONG_BASE_PATCH)
    assert wrong.value.code == "patch_not_at_source_rev"

    # Neither call may move the repository's real index or worktree.
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 10\n"


def test_check_branch_descends_rejects_an_unrelated_root(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)

    revision.check_branch_descends(repo, later)

    side = _orphan_commit(repo)
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == later
    with pytest.raises(revision.RevisionError) as diverged:
        revision.check_branch_descends(repo, side)
    assert diverged.value.code == "branch_diverged"


def test_head_sha_matches_git(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)
    assert revision.head_sha(repo) == later


# ---------------------------------------------------------------------------
# Wave integration
# ---------------------------------------------------------------------------


def test_integrate_wave_missing_and_failed_verify_results_are_collected(
    tmp_path: Path,
):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-failed", later, ok=False)
    calls: list[Path] = []

    record = revision.integrate_wave(
        state,
        "wave-bad",
        repo,
        ["att-missing", "att-failed"],
        _runner(_suite(), calls),
        now="2026-02-03T04:05:06Z",
    )

    assert record["accepted"] is False
    assert set(record["problems"]) == {"att-missing", "att-failed"}
    assert record["at"] == "2026-02-03T04:05:06Z"
    assert calls == []
    assert record["regressions"] == 0
    assert revision.load_wave(state, "wave-bad") == record
    assert revision.attempt_accepted(state, "att-missing") is False


def test_integrate_wave_ancestor_fast_path_never_computes_patch_ids(
    tmp_path: Path, monkeypatch
):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-tip", later)
    calls: list[Path] = []

    def refuse(*_args, **_kwargs):
        raise AssertionError("an ancestor must pass without patch-id evidence")

    monkeypatch.setattr(revision, "_commit_patch_id", refuse)
    monkeypatch.setattr(revision._LandedChanges, "_load", refuse)

    record = revision.integrate_wave(
        state, "wave-fast", repo, ["att-tip"], _runner(_suite(), calls)
    )

    assert record["accepted"] is True
    assert record["problems"] == {}
    assert "replay_evidence" not in record
    assert calls == [repo]


def test_integrate_wave_accepts_a_squash_landed_change_by_patch_id(tmp_path: Path):
    repo, verified, landed = _squash_repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-squash", verified)
    calls: list[Path] = []

    record = revision.integrate_wave(
        state, "wave-squash", repo, ["att-squash"], _runner(_suite(), calls)
    )

    assert record["accepted"] is True
    assert record["problems"] == {}
    assert calls == [repo]
    proof = record["replay_evidence"]["att-squash"]
    assert proof["verified_sha"] == verified
    assert proof["landed_sha"] == landed
    assert re.fullmatch(r"[0-9a-f]{40}", proof["patch_id"])
    assert revision.load_wave(state, "wave-squash") == record


def test_integrate_wave_rejects_a_squash_landing_with_a_different_patch_id(
    tmp_path: Path,
):
    repo, verified, _landed = _squash_repo(tmp_path)
    # The landing keeps the file but changes it differently, so no commit on
    # HEAD introduces the verified change.
    (repo / "src" / "a.py").write_text("value = 11\n", encoding="utf-8")
    _git(repo, "commit", "--amend", "-am", "squash landing, adjusted")
    state = _state(tmp_path)
    _write_verify(state, "att-differs", verified)
    calls: list[Path] = []

    record = revision.integrate_wave(
        state, "wave-differs", repo, ["att-differs"], _runner(_suite(), calls)
    )

    assert record["accepted"] is False
    problem = record["problems"]["att-differs"]
    assert problem.startswith(revision.PROBLEM_CHANGE_NOT_LANDED)
    assert "not an ancestor" not in problem
    assert calls == []
    assert "replay_evidence" not in record


def test_integrate_wave_keeps_the_ancestry_refusal_for_an_absent_sha(tmp_path: Path):
    repo, _base, _later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-gone", "f" * 40)
    calls: list[Path] = []

    record = revision.integrate_wave(
        state, "wave-gone", repo, ["att-gone"], _runner(_suite(), calls)
    )

    assert record["accepted"] is False
    assert record["problems"]["att-gone"] == "verified sha is not an ancestor of HEAD"
    assert calls == []


def test_integrate_wave_names_a_foreign_repository_instead_of_ancestry(
    tmp_path: Path,
):
    alpha = _plain_repo(tmp_path, "alpha", "https://example.invalid/acme/alpha.git")
    beta = _plain_repo(tmp_path, "beta", "https://example.invalid/acme/beta.git")
    state = _state(tmp_path)
    alpha_sha = _git(alpha, "rev-parse", "HEAD").stdout.strip()
    beta_sha = _git(beta, "rev-parse", "HEAD").stdout.strip()
    _write_verify(state, "att-alpha", alpha_sha)
    _write_verify(state, "att-beta", beta_sha)
    _lease_with_repo(state, "att-alpha", alpha_sha, "acme/alpha")
    _lease_with_repo(state, "att-beta", beta_sha, "acme/beta")
    calls: list[Path] = []

    record = revision.integrate_wave(
        state, "wave-mixed", alpha, ["att-alpha", "att-beta"], _runner(_suite(), calls)
    )

    assert record["accepted"] is False
    assert "att-alpha" not in record["problems"]
    problem = record["problems"]["att-beta"]
    assert problem.startswith(revision.PROBLEM_FOREIGN_REPO)
    assert "acme/beta" in problem
    assert "not an ancestor" not in problem
    assert calls == []


def test_integrate_wave_requires_a_clean_worktree(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-a", later)
    (repo / "src" / "a.py").write_text("dirty\n", encoding="utf-8")
    calls: list[Path] = []

    with pytest.raises(revision.RevisionError) as dirty:
        revision.integrate_wave(
            state, "wave-dirty", repo, ["att-a"], _runner(_suite(), calls)
        )

    assert dirty.value.code == "worktree_dirty"
    assert calls == []
    assert revision.load_wave(state, "wave-dirty") is None


def test_integrate_wave_accepts_all_slices_after_a_green_full_suite(tmp_path: Path):
    repo, base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-base", base)
    _write_verify(state, "att-tip", later)
    evidence = _suite()
    calls: list[Path] = []

    record = revision.integrate_wave(
        state,
        "wave-green",
        repo,
        ["att-base", "att-tip"],
        _runner(evidence, calls),
        now="2026-02-03T04:05:06Z",
    )

    assert record["accepted"] is True
    assert record["problems"] == {}
    assert record["head_sha"] == later
    assert record["attempts"] == ["att-base", "att-tip"]
    assert record["suite"] == evidence
    assert record["regressions"] == 0
    assert record["at"] == "2026-02-03T04:05:06Z"
    assert calls == [repo]

    assert revision.attempt_accepted(state, "att-base") is True
    assert revision.attempt_accepted(state, "att-tip") is True
    assert revision.attempt_accepted(state, "att-other") is False

    wave_path = state / "waves" / "wave-green.json"
    assert stat.S_IMODE(wave_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "waves").stat().st_mode) == 0o700


def test_integrate_wave_failing_suite_records_junit_regressions(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-a", later)
    calls: list[Path] = []

    record = revision.integrate_wave(
        state,
        "wave-red",
        repo,
        ["att-a"],
        _runner(_suite(exit_code=1, failures=2), calls),
    )

    assert record["accepted"] is False
    assert record["regressions"] == 2
    assert record["suite"]["exit_code"] == 1
    assert calls == [repo]


def test_integrate_wave_accepted_record_is_final(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-a", later)
    calls: list[Path] = []

    first = revision.integrate_wave(
        state, "wave-final", repo, ["att-a"], _runner(_suite(), calls)
    )
    assert first["accepted"] is True

    with pytest.raises(revision.RevisionError) as overwrite:
        revision.integrate_wave(
            state,
            "wave-final",
            repo,
            ["att-a"],
            _runner(_suite(exit_code=1, failures=1), calls),
        )

    assert overwrite.value.code == "wave_already_accepted"
    assert calls == [repo]
    assert revision.load_wave(state, "wave-final") == first


def test_integrate_wave_may_replace_a_non_accepted_record(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    calls: list[Path] = []

    first = revision.integrate_wave(
        state, "wave-retry", repo, ["att-a"], _runner(_suite(), calls)
    )
    assert first["accepted"] is False

    _write_verify(state, "att-a", later)
    second = revision.integrate_wave(
        state, "wave-retry", repo, ["att-a"], _runner(_suite(), calls)
    )

    assert second["accepted"] is True
    assert second["problems"] == {}
    assert revision.attempt_accepted(state, "att-a") is True


def test_integrate_wave_timed_out_suite_is_not_accepted(tmp_path: Path):
    repo, _base, later = _repo(tmp_path)
    state = _state(tmp_path)
    _write_verify(state, "att-a", later)
    calls: list[Path] = []

    record = revision.integrate_wave(
        state,
        "wave-timeout",
        repo,
        ["att-a"],
        _runner(_suite(exit_code=0, timed_out=True, failures=1), calls),
    )

    assert record["accepted"] is False
    assert record["regressions"] == 1
    assert calls == [repo]


def test_integrate_wave_validates_wave_and_attempt_ids(tmp_path: Path):
    repo, _base, _later = _repo(tmp_path)
    state = _state(tmp_path)
    calls: list[Path] = []

    with pytest.raises(revision.RevisionError) as bad_wave:
        revision.integrate_wave(
            state, "../escape", repo, ["att-a"], _runner(_suite(), calls)
        )
    assert bad_wave.value.code == "invalid_wave_id"

    with pytest.raises(revision.RevisionError) as empty:
        revision.integrate_wave(state, "wave-ok", repo, [], _runner(_suite(), calls))
    assert empty.value.code == "invalid_attempt_id"

    with pytest.raises(revision.RevisionError) as bad_attempt:
        revision.integrate_wave(
            state, "wave-ok", repo, ["../escape"], _runner(_suite(), calls)
        )
    assert bad_attempt.value.code == "invalid_attempt_id"
    assert calls == []
