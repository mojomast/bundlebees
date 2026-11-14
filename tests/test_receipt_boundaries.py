"""Receipts wired into the real verifier and integrator execution seams."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job, leases, test_receipts, verify_local

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]


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
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text(
        "def test_base():\n    assert True\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD").stdout.strip()


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
    "tests/test_newmod.py": (
        "from pkg.newmod import double\n\n"
        "def test_double():\n    assert double(2) == 4\n"
    ),
}


def record_lease(state: Path, attempt: str, base: str, files=None) -> None:
    leases.record_lease(
        state,
        attempt,
        {
            "objective": f"work for {attempt}",
            "source_rev": base,
            "assigned_files": list(files or NEW_FILES),
            "acceptance": {"criteria": ["double"], "tests": ["tests/test_newmod.py"]},
        },
    )


class CountingRunner:
    """A deterministic test runner: RED without the patch, GREEN with it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[list[str]] = []

    def __call__(self, workdir, argv):
        with self.lock:
            self.calls.append(list(argv))
        installed = (Path(workdir) / "pkg" / "newmod.py").is_file()
        return {
            "exit_code": 0 if installed else 1,
            "timed_out": False,
            "output_tail": "green" if installed else "red",
        }


def verify_args(state: Path, repo: Path, base: str, patch: str, attempt: str, **extra):
    args = dict(
        state_dir=state,
        repo=repo,
        attempt_id=attempt,
        patch=patch,
        declared_files=list(NEW_FILES),
        base_rev=base,
        tests=["tests/test_newmod.py"],
        suite_command=SUITE,
        require_gitleaks=False,
    )
    args.update(extra)
    return args


def counts(record):
    cache = record["test_cache"]
    return cache["executions"], cache["hits"], cache["misses"]


def test_verifier_executes_once_then_reuses_the_exact_receipts(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    patch = make_patch(repo, base, NEW_FILES)
    record_lease(state, "att_1", base)
    runner = CountingRunner()

    first = verify_local.verify_and_commit(
        **verify_args(state, repo, base, patch, "att_1", runner=runner)
    )
    assert first["ok"] is True
    assert counts(first) == (3, 0, 3)  # RED, GREEN and suite each executed once
    assert len(runner.calls) == 3

    # Drop only the aggregation record: the durable execution receipts remain.
    verify_local.record_path(state, "att_1").unlink()
    second = verify_local.verify_and_commit(
        **verify_args(state, repo, base, patch, "att_1", runner=runner)
    )

    assert second["ok"] is True
    assert counts(second) == (0, 3, 0)
    assert len(runner.calls) == 3  # no test command ran a second time


def test_changed_command_test_or_source_invalidates_reuse(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    patch = make_patch(repo, base, NEW_FILES)
    record_lease(state, "att_1", base)
    runner = CountingRunner()

    verify_local.verify_and_commit(
        **verify_args(state, repo, base, patch, "att_1", runner=runner)
    )
    assert len(runner.calls) == 3

    # A different exact command is a different execution identity.
    verify_local.record_path(state, "att_1").unlink()
    changed = verify_local.verify_and_commit(
        **verify_args(
            state, repo, base, patch, "att_1",
            runner=runner,
            suite_command=SUITE + ["-x"],
        )
    )
    assert counts(changed) == (3, 0, 3)
    assert len(runner.calls) == 6

    # A different test/source patch is also a different identity.
    other_repo, other_base = make_repo(tmp_path / "other")
    other_patch = make_patch(
        other_repo,
        other_base,
        {
            "pkg/newmod.py": "def double(x):\n    return x + x\n",
            "tests/test_newmod.py": (
                "from pkg.newmod import double\n\n"
                "def test_double():\n    assert double(3) == 6\n"
            ),
        },
    )
    record_lease(state, "att_2", other_base)
    other = verify_local.verify_and_commit(
        **verify_args(
            state, other_repo, other_base, other_patch, "att_2", runner=runner
        )
    )
    assert counts(other) == (3, 0, 3)
    assert len(runner.calls) == 9


def test_key_identity_covers_dependencies_runtime_and_sandbox(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    (worktree / "pyproject.toml").write_text("[project]\nname='sample'\n")

    def key_for(**changes):
        options = dict(
            command=["python", "-m", "pytest", "-q", "tests/test_sample.py"],
            stage="verify.green",
            source_tree_sha256="a" * 64,
            patch_sha256="b" * 64,
            tests=["tests/test_sample.py"],
            runner=None,
            backend="auto",
            allow_unsafe_local=True,
            allow_weak=False,
            timeout=600,
        )
        options.update(changes)
        return test_receipts.canonical_key(
            verify_local._receipt_key_inputs(worktree, **options)
        )

    baseline = key_for()
    assert key_for() == baseline

    (worktree / "pyproject.toml").write_text("[project]\nname='changed'\n")
    assert key_for() != baseline
    (worktree / "pyproject.toml").write_text("[project]\nname='sample'\n")

    monkeypatch.setattr(
        verify_local, "_stack_identity", lambda: {"python": "other-runtime"}
    )
    assert key_for() != baseline
    monkeypatch.undo()

    assert key_for(timeout=601) != baseline
    assert key_for(backend="bwrap") != baseline
    assert key_for(allow_unsafe_local=False) != baseline
    assert key_for(stage="verify.red") != baseline


def test_concurrent_identical_requests_share_one_execution(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    patch = make_patch(repo, base, NEW_FILES)
    record_lease(state, "att_a", base)
    record_lease(state, "att_b", base)
    runner = CountingRunner()
    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(attempt: str) -> None:
        try:
            barrier.wait(timeout=10)
            result = verify_local.verify_and_commit(
                **verify_args(state, repo, base, patch, attempt, runner=runner)
            )
            with lock:
                results[attempt] = result
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("att_a", "att_b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, errors
    assert set(results) == {"att_a", "att_b"}
    assert all(result["ok"] is True for result in results.values())
    # Exactly one RED, one GREEN and one suite execution happened in total.
    assert len(runner.calls) == 3
    assert sum(counts(result)[0] for result in results.values()) == 3
    assert sum(counts(result)[1] for result in results.values()) == 3


CRASH_HOLDER = """
import sys, time
from bundlebees_plugin import test_receipts
claim = test_receipts.claim_key(sys.argv[1], sys.argv[2])
print("ready", flush=True)
time.sleep(120)
"""


def test_abandoned_single_flight_owner_is_recovered_after_a_crash(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    patch = make_patch(repo, base, NEW_FILES)
    record_lease(state, "att_1", base)
    runner = CountingRunner()

    request_key = verify_local._verify_request_lock(
        "att_1",
        patch=patch,
        base_rev=base,
        tests=["tests/test_newmod.py"],
        suite_command=SUITE,
    )
    child = subprocess.Popen(
        [PY, "-c", CRASH_HOLDER, str(state), request_key],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
    finally:
        child.kill()
        child.wait(timeout=10)

    result = verify_local.verify_and_commit(
        **verify_args(state, repo, base, patch, "att_1", runner=runner)
    )

    assert result["ok"] is True
    assert len(runner.calls) == 3
    assert counts(result) == (3, 0, 3)


def test_corrupt_receipt_is_refused_not_treated_as_a_pass(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    patch = make_patch(repo, base, NEW_FILES)
    record_lease(state, "att_1", base)
    runner = CountingRunner()

    verify_local.verify_and_commit(
        **verify_args(state, repo, base, patch, "att_1", runner=runner)
    )
    verify_local.record_path(state, "att_1").unlink()

    receipts_dir = state / "receipts"
    red_path = None
    for path in receipts_dir.glob("*.json"):
        body = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(body.get("result"), dict) and body["result"].get("exit_code") != 0:
            red_path = path
            break
    assert red_path is not None
    red_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local.verify_and_commit(
            **verify_args(state, repo, base, patch, "att_1", runner=runner)
        )
    assert caught.value.code == "receipt_corrupt"
    assert len(runner.calls) == 3  # no new execution, and no false pass


def test_integration_suite_reuses_the_combined_receipt(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    patch = make_patch(repo, base, NEW_FILES)
    record_lease(state, "att_1", base)
    verify_runner = CountingRunner()
    verify_local.verify_and_commit(
        **verify_args(state, repo, base, patch, "att_1", runner=verify_runner)
    )

    job = {
        "job_id": "job_a",
        "goal": {"base_rev": base},
        "tasks": [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}],
        "integration": {"wave": 1},
    }
    suite_runner = CountingRunner()

    first = integrate_job.integrate(
        state, job, repo, suite_command=SUITE, runner=suite_runner
    )
    assert first["ok"] is True
    assert first["test_cache"] == {"executions": 1, "hits": 0, "misses": 1}
    assert len(suite_runner.calls) == 1

    # Forget the wave aggregation (but not the execution receipts) and the
    # durable integration ref, then request the identical integration again.
    wave = integrate_job.job_wave_id(job)
    (state / "waves" / f"{wave}.json").unlink()
    git(repo, "update-ref", "-d", integrate_job.integration_ref("job_a"), check=False)

    second = integrate_job.integrate(
        state, job, repo, suite_command=SUITE, runner=suite_runner
    )
    assert second["ok"] is True
    assert second["test_cache"] == {"executions": 0, "hits": 1, "misses": 0}
    assert len(suite_runner.calls) == 1
