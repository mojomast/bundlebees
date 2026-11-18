"""Integration wiring: two sibling verification commits, one history, no rewrites."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job, leases, sandbox, verify_local

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]


def sandboxed(workdir, argv):
    """Explicit test runner for hosts without a strong isolation backend.

    The runner uses the disposable copy and the sandbox's exact, scrubbed
    environment without silently inheriting the coordinator environment.
    """

    return sandbox.run_in_sandbox(workdir, list(argv), allow_unsafe_local=True, timeout=120)


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
        runner=sandboxed,
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

        result = integrate_job.integrate(state, job_for(base), repo, suite_command=SUITE, runner=sandboxed)

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
            verify_local.verify_and_commit(state, repo, attempt, patch=patch, declared_files=lease["assigned_files"], base_rev=base, tests=["tests/test_ok.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        with pytest.raises(integrate_job.IntegrateError) as exc:
            integrate_job.integrate(state, job_for(base), repo, suite_command=SUITE, runner=sandboxed)

        assert exc.value.code == "integration_conflict"


def job_with_one_attempt(base: str) -> dict:
    return {
        "job_id": "job_a",
        "goal": {"base_rev": base},
        "tasks": [{"task_id": "T1", "state": "verified", "attempts": ["att_1"], "depends_on": []}],
        "integration": {"wave": 1},
    }


def _text_slice(repo, state, base, job_id, attempt_id, label):
    """A sibling commit and a persisted verify-result fixture; no worker runs."""
    git(repo, "checkout", "-q", "-b", label, base)
    (repo / "slice.txt").write_text(label)
    git(repo, "add", "slice.txt")
    git(repo, "commit", "-qm", label)
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    verify_local._write_record(state, attempt_id, {"attempt_id": attempt_id, "ok": True, "sha": sha})
    return {"job_id": job_id, "goal": {"base_rev": base}, "integration": {"wave": 1},
            "tasks": [{"task_id": "T1", "state": "verified", "attempts": [attempt_id]}]}


@pytest.mark.parametrize("separate_state", [False, True], ids=["distinct-jobs", "independent-lock-stores"])
def test_overlapping_integrations_test_their_own_checkouts(tmp_path, separate_state):
    repo, base = repo_with_base(tmp_path)
    state = tmp_path / "state"
    outer = _text_slice(repo, state, base, "job_a", "att_a", "outer")
    # Independent state stores can issue identical request IDs. Distinct jobs
    # in a single state store are the ordinary parallel case.
    other_state = tmp_path / "other" if separate_state else state
    inner = _text_slice(repo, other_state, base, "job_a" if separate_state else "job_b",
                        "att_a" if separate_state else "att_b", "inner")
    options = {"cacheable": False, "worktree_root": tmp_path / "custom"} if separate_state else {"cacheable": False}
    paths = []

    def inner_suite(worktree, _argv):
        paths.append(worktree)
        assert (worktree / "slice.txt").read_text() == "inner"
        # Exercise cleanup on rejection, too. The independent stores have
        # the same job id and must not both publish a head to its Git ref.
        return {"exit_code": 1 if separate_state else 0}

    def outer_suite(worktree, _argv):
        paths.append(worktree)
        assert (worktree / "slice.txt").read_text() == "outer"
        # Nest the calls at the suite boundary to force a deterministic
        # overlap without threads, sleeps, or running a real suite.
        result = integrate_job.integrate(other_state, inner, repo, runner=inner_suite, **options)
        assert result["ok"] is (not separate_state)
        assert git(repo, "show", result["record"]["head_sha"] + ":slice.txt").stdout == "inner"
        assert (worktree / "slice.txt").is_file(), "the inner job removed the outer suite's checkout"
        assert (worktree / "slice.txt").read_text() == "outer"
        return {"exit_code": 0}

    accepted = integrate_job.integrate(state, outer, repo, runner=outer_suite, **options)
    assert accepted["ok"]
    assert git(repo, "show", accepted["record"]["head_sha"] + ":slice.txt").stdout == "outer"
    assert len(set(paths)) == 2
    assert all(not path.exists() for path in paths)
    assert git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1


def test_retry_reclaims_abandoned_checkout_for_its_own_request(tmp_path):
    repo, base = repo_with_base(tmp_path)
    state = tmp_path / "state"
    job = _text_slice(repo, state, base, "job_a", "att_a", "outer")
    paths = []

    def suite(worktree, _argv):
        paths.append(worktree)
        assert not (worktree / "stale.txt").exists()
        return {"exit_code": 0 if len(paths) > 1 else 1}

    assert not integrate_job.integrate(state, job, repo, runner=suite, cacheable=False)["ok"]
    rejected = {p.parent.name: p.read_bytes() for p in state.glob("*/job_a.w1.json")}
    # Restore the linked worktree that an interrupted attempt would leave.
    git(repo, "worktree", "add", "--detach", str(paths[0]), base)
    (paths[0] / "stale.txt").write_text("left over")
    assert integrate_job.integrate(state, job, repo, runner=suite, cacheable=False)["ok"]
    assert {p.stem: p.read_bytes() for p in (state / "wave-history").rglob("*.json")} == rejected
    assert not paths[0].exists()
    assert git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1


class SandboxRequirements:
    __test__ = True

    @pytest.mark.parametrize("raw", [{"exit_code": False}, {"exit_code": "0"}, {"exit_code": 0.5},
                                     {"exit_code": 0, "blocking": ["contract"]}])
    def test_live_and_cached_suite_results_cannot_convert_a_refusal_to_a_pass(self, tmp_path, raw):
        observed = integrate_job._suite_evidence(
            tmp_path, ["fixture"], runner=lambda *_: raw, backend="auto",
            allow_unsafe_local=False, allow_weak=False, timeout=5,
        )
        assert not integrate_job.revision.suite_passed(observed)
        if type(raw["exit_code"]) is not int:
            with pytest.raises(integrate_job.IntegrateError, match="exit code"):
                integrate_job._receipt_suite_evidence({"result": raw})
        else:
            cached = integrate_job._receipt_suite_evidence({"result": observed})
            assert not integrate_job.revision.suite_passed(cached)

    def test_the_integration_suite_does_not_inherit_coordinator_environment(self, tmp_path, monkeypatch):
        for name in ("BUNDLEBEES_LAUNCHER_TOKEN", "BUNDLEBEES_SIGNING_KEY", "HERMES_API_KEY", "HERMES_API_URL"):
            monkeypatch.setenv(name, "set-by-the-coordinator")
        script = (
            "import os\n"
            "print('coordinator=%d' % sum(n.startswith(('BUNDLEBEES_', 'HERMES_')) for n in os.environ))\n"
        )

        evidence = integrate_job._suite_evidence(
            tmp_path,
            [sys.executable, "-c", script],
            runner=None,
            backend="auto",
            allow_unsafe_local=True,
            allow_weak=False,
            timeout=60,
        )

        assert evidence["exit_code"] == 0
        assert "coordinator=0" in evidence["tail"]

    def test_a_timed_out_integration_suite_is_not_accepted(self, tmp_path):
        repo, base = repo_with_base(tmp_path)
        state = tmp_path / "state"
        verified(state, repo, base, "att_1", A_FILES)

        def timed_out(workdir, argv):
            return {"exit_code": 124, "timed_out": True, "output_tail": "sandbox timed out"}

        result = integrate_job.integrate(state, job_with_one_attempt(base), repo, suite_command=SUITE, runner=timed_out)

        assert result["ok"] is False
        assert result["record"]["accepted"] is False
        assert result["record"]["suite"]["timed_out"] is True

    def test_an_unavailable_sandbox_fails_integration_closed(self, tmp_path, monkeypatch):
        repo, base = repo_with_base(tmp_path)
        state = tmp_path / "state"
        verified(state, repo, base, "att_1", A_FILES)

        def unavailable(*args, **kwargs):
            raise sandbox.SandboxUnavailable("no strong sandbox backend is usable")

        monkeypatch.setattr(integrate_job.sandbox, "run_in_sandbox", unavailable)

        with pytest.raises(integrate_job.IntegrateError) as exc:
            integrate_job.integrate(state, job_with_one_attempt(base), repo, suite_command=SUITE)

        assert exc.value.code == "sandbox_unavailable"
