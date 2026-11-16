"""Regression checks for the session's disconnected execution paths."""
import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bundlebees_plugin import cli, execution, factory_driver, job_cli, sandbox, verify_local
from bundlebees_plugin import job as jobs
from bundlebees_plugin import recovery
from bundlebees_plugin import provisioning as P, provision_bind as B


def test_bare_python_resolves_on_path_and_only_matches_the_coordinator(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: sys.executable)
    assert sandbox._is_coordinator_interpreter("python3")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: str(tmp_path / "other-python"))
    assert not sandbox._is_coordinator_interpreter("python3")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert not sandbox._is_coordinator_interpreter("python3")
    assert sandbox._is_coordinator_interpreter(sys.executable)


def test_repeated_test_flags_keep_every_target():
    parser = cli._build_parser(argparse.ArgumentParser())
    args = parser.parse_args(["verify", "att_test", "--tests", "a.py", "b.py", "--tests", "c.py"])
    assert args.tests == ["a.py", "b.py", "c.py"]


def test_scanner_resolution_precedence(monkeypatch):
    monkeypatch.setenv("GITLEAKS", "environment-scanner")
    monkeypatch.setattr(execution.shutil, "which", lambda _: "path-scanner")
    assert execution.scanner({}, "flag-scanner") == "flag-scanner"
    assert execution.scanner({"gitleaks_path": "setting-scanner"}) == "setting-scanner"
    assert execution.scanner({}) == "environment-scanner"
    monkeypatch.delenv("GITLEAKS")
    assert execution.scanner({}) == "path-scanner"


def test_configured_runner_forwards_the_real_sandbox_policy(monkeypatch, tmp_path):
    captured = {}
    def run(workdir, argv, **options):
        captured.update(options)
        return {"exit_code": 0}
    monkeypatch.setattr(sandbox, "run_in_sandbox", run)
    runner = execution.SandboxRunner({"verify_python": sys.executable,
        "sandbox_tiktoken_cache": str(tmp_path), "verify_isolation": "firejail",
        "verify_timeout_s": 123})
    runner(tmp_path, [sys.executable, "-m", "pytest"])
    assert captured["python"] == sys.executable
    assert captured["tiktoken_cache"] == str(tmp_path)
    assert captured["backend"] == "firejail"
    assert captured["timeout"] == 123
    assert captured["allow_unsafe_local"] is False
    assert captured["allow_weak"] is False


def test_changed_cache_bytes_invalidate_runner_receipt_identity(tmp_path):
    cache = tmp_path / "encoding"
    cache.write_bytes(b"first")
    runner = execution.SandboxRunner({"sandbox_tiktoken_cache": str(tmp_path), "verify_isolation": "bwrap"})
    before = verify_local._environment_policy(runner)
    cache.write_bytes(b"other")
    after = verify_local._environment_policy(runner)
    assert before != after


def test_invalid_cache_is_refused_before_fingerprinting_the_filesystem():
    runner = execution.SandboxRunner({"sandbox_tiktoken_cache": "/"})
    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local._environment_policy(runner)
    assert caught.value.code == "unsafe_cache_dir"


def test_cached_results_still_check_the_configured_sandbox(monkeypatch):
    runner = execution.SandboxRunner({})
    monkeypatch.setattr(sandbox, "detect_backend", lambda *args, **kwargs: None)
    with pytest.raises(verify_local.VerifyError, match="strong sandbox"):
        verify_local._assert_sandbox_usable(runner, backend="auto", allow_unsafe_local=False, allow_weak=False)


def test_job_cli_passes_declared_settings_and_scanner(monkeypatch, tmp_path):
    captured = {}
    class Ctx:
        def get_config(self, key, default=None):
            return {"verify_python": sys.executable, "verify_timeout_s": 123,
                    "sandbox_tiktoken_cache": str(tmp_path), "gitleaks_path": "configured-scanner"}.get(key, default)
    monkeypatch.setattr(job_cli, "tick", lambda *args, **kwargs: captured.update(kwargs) or {})
    args = SimpleNamespace(job_handler="tick", job_id="j", repo=str(tmp_path), gitleaks=None)
    assert job_cli.cmd_job(Ctx(), args, state_dir=tmp_path, client_factory=object) == 0
    assert captured["gitleaks"] == "configured-scanner"
    assert captured["runner"].options["python"] == sys.executable
    assert captured["runner"].options["tiktoken_cache"] == str(tmp_path)


def test_factory_never_turns_off_required_scanning(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(job_cli, "tick", lambda *args, **kwargs: captured.update(kwargs) or {})
    factory_driver.make_job_tick(tmp_path, client=object(), repo=tmp_path)("j")
    assert captured["require_gitleaks"] is True


def test_missing_scanner_stops_before_constructing_a_client(monkeypatch, tmp_path):
    monkeypatch.setattr(execution, "scanner", lambda *args: None)
    def forbidden():
        pytest.fail("a missing scanner must stop before launcher access")
    args = SimpleNamespace(job_handler="tick", job_id="j", repo=str(tmp_path), gitleaks=None)
    assert job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=forbidden) == 1


@pytest.mark.parametrize("error", [P.ProvisionDenied("profile_not_enrolled", "not enrolled"),
                                    B.ProvisionBindError("provision_conflict", "conflict")])
def test_cli_provision_refusal_is_structured_instead_of_a_traceback(monkeypatch, tmp_path, capsys, error):
    def tick(*args, **kwargs):
        raise error
    monkeypatch.setattr(job_cli, "tick", tick)
    args = SimpleNamespace(job_handler="tick", job_id="j", repo=str(tmp_path), gitleaks=sys.executable)
    assert job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=object) == 1
    import json
    assert json.loads(capsys.readouterr().out)["error"] == error.code


def test_tick_uses_the_same_runner_for_verification_and_integration(monkeypatch, tmp_path):
    runner = object()
    job = {"job_id": "j", "tasks": []}
    monkeypatch.setattr(jobs, "load_job", lambda *args, **kwargs: job)
    monkeypatch.setattr(jobs, "recover_integration", lambda *args: False)
    monkeypatch.setattr(jobs, "release_repo_grant_for_job", lambda *args: None)
    monkeypatch.setattr(job_cli, "record_handoff", lambda *args: None)
    monkeypatch.setattr(job_cli, "_after_dispatch", lambda *args: None)
    monkeypatch.setattr(job_cli.leases, "load_lease", lambda *args: {})
    captured = []
    def verified(*args, **kwargs):
        captured.append(kwargs["runner"])
        return {"sha": "a" * 40}
    def integrated(*args, **kwargs):
        captured.append(kwargs["runner"])
        return {"ok": True}
    monkeypatch.setattr(verify_local, "verify_and_commit", verified)
    monkeypatch.setattr(job_cli.integrate_job, "integrate", integrated)
    class Client:
        def attempts_raw(self):
            return []
        def get_raw(self, attempt):
            return None
        def create(self, brief):
            return {}
        def artifact(self, attempt):
            return {"patch": "diff --git a/x.py b/x.py\n", "source_rev": "a" * 40}
    def reconcile(*args, **kwargs):
        assert kwargs["verifier"]({"attempt_id": "att_x", "task": {"acceptance": {"tests": ["tests/x.py"]}}})["ok"]
        assert kwargs["integrator"](job)["ok"]
        return job_cli.reconcile.TickResult(status="idle")
    monkeypatch.setattr(job_cli.reconcile, "tick", reconcile)
    job_cli.tick(tmp_path, "j", client=Client(), repo=tmp_path, runner=runner)
    assert captured == [runner, runner]


def test_authorize_cli_seals_the_explicit_recovery_ceiling(monkeypatch, tmp_path):
    parser = cli._build_parser(argparse.ArgumentParser())
    args = parser.parse_args(["job", "authorize", "--job-id", "j", "--repo", "mojomast/x",
        "--base-rev", "a" * 40, "--scope", "src/**", "--statement", "bounded repair",
        "--max-attempts-per-task", "2", "--contract-corrections-allowed", "0"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    assert job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=object) == 0
    auth = jobs.load_authorization(tmp_path, "j")
    assert auth["recovery"] == {"max_attempts_per_task": 2, "contract_corrections_allowed": 0}
    assert auth["recovery_digest"] == jobs.recovery_digest(auth["recovery"])


def test_recovery_flags_do_not_bypass_the_interactive_gate(monkeypatch, tmp_path):
    parser = cli._build_parser(argparse.ArgumentParser())
    args = parser.parse_args(["job", "authorize", "--job-id", "j", "--repo", "mojomast/x",
        "--base-rev", "a" * 40, "--scope", "src/**", "--statement", "bounded repair",
        "--max-attempts-per-task", "2"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=object) == 1
    assert not (tmp_path / "authorizations" / "j.json").exists()


@pytest.mark.parametrize("flags", [
    ["--max-attempts-per-task", "0"],
    ["--contract-corrections-allowed", "1"],
    ["--recovery-file-scope", "outside/**"],
    ["--recovery-gate", "review"],
])
def test_recovery_flags_cannot_create_invalid_or_wider_authority(monkeypatch, tmp_path, flags):
    parser = cli._build_parser(argparse.ArgumentParser())
    args = parser.parse_args(["job", "authorize", "--job-id", "j", "--repo", "mojomast/x",
        "--base-rev", "a" * 40, "--scope", "src/**", "--statement", "bounded repair", *flags])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    assert job_cli.cmd_job(None, args, state_dir=tmp_path, client_factory=object) == 1
    with pytest.raises(jobs.JobError):
        jobs.load_authorization(tmp_path, "j")


@pytest.mark.parametrize("code", ["sandbox_interpreter_hidden", "unsafe_cache_dir"])
def test_verifier_setup_errors_are_non_dispatching_environment_failures(code):
    failure = recovery.classify_failure({"code": code})
    assert failure["class"] == "environment"
    assert failure["code"] == "toolchain_missing"


def test_configured_runner_verifies_reuses_and_integrates_a_real_patch(tmp_path):
    # Deterministic Linux fixture, explicitly opting into the existing local
    # test mode. This proves wiring and receipts, not kernel isolation.
    from tests.test_verify_local import make_repo, make_patch, lease, NEW_FILES, SUITE
    from bundlebees_plugin import integrate_job
    scanner = execution.scanner({})
    if not scanner:
        pytest.skip("real gitleaks required for this integration fixture")
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    lease(state, base)
    patch = make_patch(repo, base, NEW_FILES)
    runner = execution.SandboxRunner({"verify_python": sys.executable,
                                      "verify_allow_unsafe_local": True})
    kwargs = dict(patch=patch, declared_files=list(NEW_FILES), base_rev=base,
                  tests=["tests/test_newmod.py"], suite_command=SUITE,
                  gitleaks=scanner, runner=runner)
    first = verify_local.verify_and_commit(state, repo, "att_1", **kwargs)
    second = verify_local.verify_and_commit(state, repo, "att_1", **kwargs)
    assert first["ok"] and second["sha"] == first["sha"]
    assert second["test_cache"]["executions"] == 0
    job = {"job_id": "configured", "goal": {"base_rev": base},
           "tasks": [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}],
           "integration": {"wave": 1}}
    result = integrate_job.integrate(state, job, repo, suite_command=SUITE, runner=runner)
    assert result["ok"] is True
