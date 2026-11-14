"""Local-only verification tests: real git repos, real commits, no pushes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import leases, sandbox, verify_local

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]


def sandboxed(workdir, argv):
    """Explicit test runner for hosts without a strong isolation backend.

    This repo's test hosts may only have ``unshare``, so the tests opt into the
    documented ``allow_unsafe_local`` escape hatch explicitly. The runner still
    uses the disposable copy and the sandbox's exact, scrubbed environment; it
    never silently inherits the coordinator environment.
    """

    return sandbox.run_in_sandbox(workdir, list(argv), allow_unsafe_local=True, timeout=120)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "existing.py").write_text("VALUE = 1\n")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text("from pkg.existing import VALUE\n\ndef test_value():\n    assert VALUE == 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base


def make_patch(repo: Path, base: str, files: dict[str, str]) -> str:
    git(repo, "checkout", "-q", "-b", "work", base)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    patch = git(repo, "diff", "--cached", base).stdout
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "work")
    return patch


NEW_FILES = {
    "pkg/newmod.py": "def double(x):\n    return x * 2\n",
    "tests/test_newmod.py": "from pkg.newmod import double\n\ndef test_double():\n    assert double(2) == 4\n",
}
BRIEF = {
    "objective": "add double()",
    "source_rev": "PLACEHOLDER",
    "assigned_files": ["pkg/newmod.py", "tests/test_newmod.py"],
    "acceptance": {"criteria": ["double"], "tests": ["tests/test_newmod.py"]},
}


def lease(state: Path, base: str, files=None) -> None:
    brief = dict(BRIEF, source_rev=base, assigned_files=files or BRIEF["assigned_files"])
    leases.record_lease(state, "att_1", brief)


class HappyPath:
    __test__ = True

    def test_verification_creates_a_local_commit_and_touches_nothing_else(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base)
        patch = make_patch(repo, base, NEW_FILES)

        before_head = git(repo, "rev-parse", "HEAD").stdout.strip()
        before_status = git(repo, "status", "--porcelain").stdout

        record = verify_local.verify_and_commit(
            state,
            repo,
            "att_1",
            patch=patch,
            declared_files=list(NEW_FILES),
            base_rev=base,
            tests=["tests/test_newmod.py"],
            suite_command=SUITE,
            require_gitleaks=False,
            runner=sandboxed,
        )

        assert record["ok"] is True
        sha = record["sha"]
        assert git(repo, "rev-parse", f"{sha}^").stdout.strip() == base
        assert sorted(git(repo, "diff", "--name-only", base, sha).stdout.split()) == sorted(NEW_FILES)
        assert git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
        assert git(repo, "status", "--porcelain").stdout == before_status
        assert git(repo, "branch", "--list", "bb-verify/att_1").stdout.strip()
        # No remote exists, so nothing could have been pushed.
        assert git(repo, "remote").stdout.strip() == ""

    def test_a_second_run_reuses_the_record(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base)
        patch = make_patch(repo, base, NEW_FILES)

        first = verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(NEW_FILES), base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)
        second = verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(NEW_FILES), base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        assert second["sha"] == first["sha"]
        assert second.get("resumed") is False  # served from the record


class Refusals:
    __test__ = True

    def _setup(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base)
        return repo, base, state, make_patch(repo, base, NEW_FILES)

    def test_a_patch_touching_files_outside_the_lease_is_refused(self, tmp_path):
        repo, base, state, patch = self._setup(tmp_path)
        extra = dict(NEW_FILES, **{"pkg/secret.py": "TOKEN = 'x'\n"})
        patch = make_patch(repo, base, extra)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=[], base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        assert exc.value.code in {"lease_scope_mismatch", "patch_scope_mismatch"}
        assert git(repo, "branch", "--list", "bb-verify/att_1").stdout.strip() == ""

    def test_tests_that_pass_before_the_patch_are_not_red(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base, files=["pkg/unchanged.py", "tests/test_existing.py"])
        patch = make_patch(repo, base, {"pkg/unchanged.py": "X = 1\n", "tests/test_existing.py": "from pkg.existing import VALUE\n\ndef test_value():\n    assert VALUE == 1\n"})

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=["pkg/unchanged.py", "tests/test_existing.py"], base_rev=base, tests=["tests/test_existing.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        assert exc.value.code == "red_missing"

    def test_failing_tests_after_the_patch_are_refused(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        bad = {
            "pkg/newmod.py": "def double(x):\n    return x * 3\n",
            "tests/test_newmod.py": "from pkg.newmod import double\n\ndef test_double():\n    assert double(2) == 4\n",
        }
        patch = make_patch(repo, base, bad)
        lease(state, base, files=list(bad))

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(bad), base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        assert exc.value.code == "tests_failed"
        assert git(repo, "branch", "--list", "bb-verify/att_1").stdout.strip() == ""

    def test_a_missing_lease_is_refused(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        patch = make_patch(repo, base, NEW_FILES)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(NEW_FILES), base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        assert exc.value.code == "lease_missing"


class Recovery:
    __test__ = True

    def test_a_commit_without_a_record_is_reverified_not_adopted(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base)
        patch = make_patch(repo, base, NEW_FILES)

        first = verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(NEW_FILES), base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)
        verify_local.record_path(state, "att_1").unlink()

        second = verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(NEW_FILES), base_rev=base, tests=["tests/test_newmod.py"], suite_command=SUITE, require_gitleaks=False, runner=sandboxed)

        # A leftover branch is not evidence: every check ran again.
        assert second["ok"] is True
        assert second["resumed"] is False
        assert second["red"] is not None and second["red"]["exit_code"] != 0
        assert second["suite"] is not None and second["suite"]["exit_code"] == 0
        assert second["patch_sha256"]
        assert git(repo, "rev-parse", f"{second['sha']}^").stdout.strip() == base


class SandboxRequirements:
    __test__ = True

    def test_the_sandbox_child_does_not_inherit_coordinator_environment(self, tmp_path, monkeypatch):
        for name in ("BUNDLEBEES_LAUNCHER_TOKEN", "BUNDLEBEES_SIGNING_KEY", "HERMES_API_KEY", "HERMES_API_URL"):
            monkeypatch.setenv(name, "set-by-the-coordinator")
        script = (
            "import os\n"
            "print('coordinator=%d' % sum(n.startswith(('BUNDLEBEES_', 'HERMES_')) for n in os.environ))\n"
        )

        evidence = verify_local.sandbox_runner(
            tmp_path,
            [sys.executable, "-c", script],
            allow_unsafe_local=True,
            timeout=60,
        )

        assert evidence["exit_code"] == 0
        assert "coordinator=0" in evidence["output_tail"]

    def test_the_sandbox_enforces_its_timeout(self, tmp_path):
        evidence = verify_local.sandbox_runner(
            tmp_path,
            [sys.executable, "-c", "import time; time.sleep(5)"],
            allow_unsafe_local=True,
            timeout=1,
        )

        assert evidence["timed_out"] is True
        assert evidence["exit_code"] == 124

    def test_a_timed_out_test_run_is_not_red_or_green_evidence(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base)
        patch = make_patch(repo, base, NEW_FILES)

        def timed_out(workdir, argv):
            return {"exit_code": 124, "timed_out": True, "output_tail": "sandbox timed out"}

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(
                state,
                repo,
                "att_1",
                patch=patch,
                declared_files=list(NEW_FILES),
                base_rev=base,
                tests=["tests/test_newmod.py"],
                suite_command=SUITE,
                require_gitleaks=False,
                runner=timed_out,
            )

        assert exc.value.code == "tests_timed_out"

    def test_an_unavailable_sandbox_fails_verify_closed(self, tmp_path, monkeypatch):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        lease(state, base)
        patch = make_patch(repo, base, NEW_FILES)

        def unavailable(*args, **kwargs):
            raise sandbox.SandboxUnavailable("no strong sandbox backend is usable")

        monkeypatch.setattr(verify_local.sandbox, "run_in_sandbox", unavailable)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(
                state,
                repo,
                "att_1",
                patch=patch,
                declared_files=list(NEW_FILES),
                base_rev=base,
                tests=["tests/test_newmod.py"],
                suite_command=SUITE,
                require_gitleaks=False,
            )

        assert exc.value.code == "sandbox_unavailable"
        assert git(repo, "branch", "--list", "bb-verify/att_1").stdout.strip() == ""
