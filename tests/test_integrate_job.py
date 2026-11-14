"""Integration wiring: two sibling verification commits, one history, no rewrites."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job, leases, verify_local

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc


def repo_with_base(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD").stdout.strip()


def patch_for(repo: Path, base: str, files: dict[str, str]) -> str:
    git(repo, "checkout", "-q", "-b", "w", base)
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", base).stdout
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "w")
    return diff


def verified(state: Path, repo: Path, base: str, attempt: str, files: dict[str, str]) -> str:
    patch = patch_for(repo, base, files)
    leases.record_lease(
        state,
        attempt,
        {
            "objective": f"work for {attempt}",
            "source_rev": base,
            "assigned_files": list(files),
            "acceptance": {"criteria": ["x"], "tests": ["tests/test_ok.py"]},
        },
    )
    test_file = next(name for name in files if name.startswith("tests/"))
    record = verify_local.verify_and_commit(
        state,
        repo,
        attempt,
        patch=patch,
        declared_files=list(files),
        base_rev=base,
        tests=[test_file],
        suite_command=SUITE,
        require_gitleaks=False,
    )
    return record["sha"]


A_FILES = {
    "pkg/a.py": "def a():\n    return 'a'\n",
    "tests/test_a.py": "from pkg.a import a\n\ndef test_a():\n    assert a() == 'a'\n",
}
B_FILES = {
    "pkg/b.py": "def b():\n    return 'b'\n",
    "tests/test_b.py": "from pkg.b import b\n\ndef test_b():\n    assert b() == 'b'\n",
}


def job_for(base: str) -> dict:
    return {
        "job_id": "job_a",
        "goal": {"base_rev": base},
        "tasks": [
            {"task_id": "T1", "state": "verified", "attempts": ["att_1"], "depends_on": []},
            {"task_id": "T2", "state": "verified", "attempts": ["att_2"], "depends_on": []},
        ],
        "integration": {"wave": 1},
    }


class Siblings:
    __test__ = True

    def test_two_verified_commits_become_ancestors_of_one_history(self, tmp_path):
        repo, base = repo_with_base(tmp_path)
        state = tmp_path / "state"

        sha1 = verified(state, repo, base, "att_1", A_FILES)
        sha2 = verified(state, repo, base, "att_2", B_FILES)
        assert sha1 != sha2

        result = integrate_job.integrate(state, job_for(base), repo, suite_command=SUITE)

        assert result["ok"] is True, result.get("evidence")
        head = result["record"]["head_sha"]
        assert head not in {sha1, sha2}

        # Both verified commits are ancestors, unmodified.
        for sha in (sha1, sha2):
            assert git(repo, "merge-base", "--is-ancestor", sha, head, check=False).returncode == 0
            assert git(repo, "cat-file", "-t", sha).stdout.strip() == "commit"

        # The combined history contains both patches.
        assert (repo / "pkg" / "a.py").exists() is False  # worktree removed; history only
        tree = git(repo, "ls-tree", "-r", "--name-only", head).stdout.split()
        assert "pkg/a.py" in tree and "pkg/b.py" in tree

    def test_a_conflict_is_reported_not_forced(self, tmp_path):
        repo, base = repo_with_base(tmp_path)
        state = tmp_path / "state"

        same_a = {"pkg/same.py": "VALUE = 1\n", "tests/test_ok.py": "def test_ok():\n    assert True\n"}
        same_b = {"pkg/same.py": "VALUE = 2\n", "tests/test_ok.py": "def test_ok():\n    assert True\n"}

        lease = {"assigned_files": ["pkg/same.py", "tests/test_ok.py"]}
        for attempt, files in (("att_1", same_a), ("att_2", same_b)):
            patch = patch_for(repo, base, files)
            leases.record_lease(
                state,
                attempt,
                {
                    "objective": attempt,
                    "source_rev": base,
                    "assigned_files": lease["assigned_files"],
                    "acceptance": {"criteria": ["x"], "tests": ["tests/test_ok.py"]},
                },
            )
            verify_local.verify_and_commit(state, repo, attempt, patch=patch, declared_files=lease["assigned_files"], base_rev=base, tests=["tests/test_ok.py"], suite_command=SUITE, require_gitleaks=False)

        with pytest.raises(integrate_job.IntegrateError) as exc:
            integrate_job.integrate(state, job_for(base), repo, suite_command=SUITE)

        assert exc.value.code == "integration_conflict"
