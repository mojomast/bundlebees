"""Approved acceptance tests for self-integration (contract BI1-self-integration, RED first).

bundlebees must be able to verify and integrate its own repository inside the sandbox, whose disposable
copy is mounted at ``/work``. Integration runs three categories of tests on the combined branch:
the configured regression suite, every approved contract test file (even outside ``testpaths``) and the
configured security/negative tests. It records each category as passed, failed or not_run in the signed
integration record, and it fails closed when a required category fails or does not run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import sandbox
from tests.test_cli_acdd import (ATTEMPT_ID, BRANCH, GOOD_PATCH, FakeClient, _frozen, _git, _invoke, _lease,
                                 _records, _repo, _result, _write)
from tests.fakes import FakeCtx, load_plugin

ROOT = Path(__file__).resolve().parents[1]
_SKIP = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def _copy_checkout(dest: Path) -> Path:
    shutil.copytree(ROOT, dest, ignore=lambda _d, names: {n for n in names if n in _SKIP})
    return dest


def test_the_plugin_loads_from_a_checkout_not_named_bundlebees(tmp_path):
    work = _copy_checkout(tmp_path / "work")
    code = "from tests.fakes import load_plugin; p = load_plugin(); print(p.__name__, callable(p.register))"
    proc = subprocess.run([sys.executable, "-B", "-c", code], cwd=work, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.split() == ["bundlebees", "True"]


@pytest.mark.skipif(sandbox.detect_backend() != "bwrap", reason="needs a working bubblewrap sandbox")
def test_bundlebees_tests_pass_inside_the_real_bwrap_sandbox(tmp_path):
    work = _copy_checkout(tmp_path / "checkout")
    # Offline targets only: the sandbox has no network (tiktoken downloads are environmental failures).
    targets = ["tests/test_plugin.py", "tests/test_cli_acdd.py", "tests/test_cli.py"]
    evidence = sandbox.run_in_sandbox(work, sandbox.pytest_argv(targets), backend="bwrap", timeout=600)
    assert evidence["backend"] == "bwrap"
    failed = [c["id"] for c in evidence["cases"] if c["outcome"] in ("failed", "error")]
    assert evidence["exit_code"] == 0 and not failed, (failed[:10], evidence["output_tail"][-1500:])
    assert evidence["junit"]["tests"] >= 40


# --------------------------------------------------------------------------- integrate categories

EXTRA = "checks/test_contract_extra.py"
SECURITY = "security/test_negative.py"


def _ctx(client=None, **settings) -> FakeCtx:
    ctx = FakeCtx(settings={"verify_allow_unsafe_local": True, **settings})
    ctx.bundlebees_client = client or FakeClient({})
    load_plugin().register(ctx)
    return ctx


def _verified(tmp_path, capsys, *, extra_source="def test_extra():\n    assert True\n"):
    repo, base = _repo(tmp_path)
    (repo / "checks").mkdir()
    (repo / EXTRA).write_text(extra_source)
    (repo / "security").mkdir()
    (repo / SECURITY).write_text("def test_refuses_traversal():\n    assert '..' not in 'a/b'\n")
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "contract and security tests")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "branch", "-f", BRANCH, "main")
    _git(repo, "push", "-q", "-f", "origin", BRANCH)
    state = tmp_path / "state"
    test_files = ["tests/test_value.py", EXTRA]
    frozen = _frozen(base, evidence_required=["red", "green", "integration"],
                     verification={"independent": True, "integration": True},
                     scope={"assigned_files": ["src/a.py"], "test_files": test_files, "prohibited": []})
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    _git(repo, "checkout", BRANCH)
    rc = _invoke(_ctx(FakeClient(_result(GOOD_PATCH, base))),
                 ["verify", ATTEMPT_ID, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 0, out
    return repo, state, path


def _integrate(repo, state, **settings) -> int:
    return _invoke(_ctx(**settings), ["integrate", "--wave", "w1", "--attempts", ATTEMPT_ID,
                                      "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])


def test_integrate_runs_regression_contract_and_security_tests_and_records_each(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    rc = _integrate(repo, state, integrate_security_tests=SECURITY)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "suite regression: passed" in out
    assert "suite contract: passed" in out
    assert "suite security: passed" in out
    record = _records(state)[-1]
    assert record["phase"] == "integration" and record["classification"] == "passed"
    suites = record["suites"]
    assert suites["regression"]["status"] == "passed" and suites["regression"]["targets"] == ["tests"]
    assert suites["contract"]["targets"] == ["checks/test_contract_extra.py", "tests/test_value.py"]
    assert suites["security"]["targets"] == [SECURITY]
    assert all(suites[k]["tests"] >= 1 for k in suites)
    ids = {c["id"] for c in record["result"]["cases"]}
    assert any(i.startswith(EXTRA) for i in ids) and any(i.startswith(SECURITY) for i in ids)
    # Every approved contract test file is fingerprinted in the integration evidence.
    assert set(record["test_fingerprints"]) >= {"tests/test_value.py", EXTRA}
    assert "checks/test_contract_extra.py" in " ".join(record["command"])


def test_a_failing_contract_test_outside_testpaths_blocks_integration(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    (repo / EXTRA).write_text("def test_extra():\n    assert False\n")
    _git(repo, "commit", "-qam", "break the contract test outside testpaths")
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0 and "accepted: no" in out
    assert "suite regression: passed" in out and "suite contract: failed" in out
    record = _records(state)[-1]
    assert record["suites"]["contract"]["status"] == "failed"
    assert record["suites"]["regression"]["status"] == "passed"
    assert record["classification"] != "passed"


def test_unconfigured_security_tests_are_recorded_as_not_run(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "suite security: not_run (not configured)" in out
    assert _records(state)[-1]["suites"]["security"] == {"status": "not_run", "reason": "not configured",
                                                        "targets": [], "tests": 0}


def test_a_configured_category_that_collects_nothing_fails_closed(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    rc = _integrate(repo, state, integrate_security_tests="security/test_missing.py")
    out = capsys.readouterr().out
    assert rc != 0 and "accepted: no" in out
    assert "suite security: failed" in out
    assert _records(state)[-1]["suites"]["security"]["status"] == "failed"
