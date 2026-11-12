"""Offline tests for the user-run ``hermes bundlebees`` CLI.

Git operations run against a temporary local repository (and a local bare
remote), so nothing here touches the network, Docker, or SSH. The launcher
result fetch is faked.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import leases, metrics, security
from bundlebees_plugin.client import ClientError
from tests.fakes import FakeCtx, load_plugin

try:
    from bundlebees_plugin import cli
except Exception as exc:  # pragma: no cover - the base has no cli module
    cli = None  # type: ignore[assignment]
    _CLI_IMPORT_ERROR = exc
else:
    _CLI_IMPORT_ERROR = None


ATTEMPT_ID = "att-1"
BRANCH = "feat/x"

IN_LEASE_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 value = 1
+value = 2
"""

OUT_OF_LEASE_PATCH = """diff --git a/src/b.py b/src/b.py
new file mode 100644
--- /dev/null
+++ b/src/b.py
@@ -0,0 +1 @@
+outside = True
"""

SECRET_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 value = 1
+token = "sk-%s"
""" % ("a" * 32)

SAMPLE_TEST = """def test_sample():
    assert True
"""


@pytest.fixture(autouse=True)
def _cli_module_present():
    assert cli is not None, f"bundlebees_plugin.cli is missing on the base: {_CLI_IMPORT_ERROR}"


class FakeClient:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.result_data = result or {}
        self.error = error
        self.calls: list[str] = []

    def result(self, attempt_id: str) -> dict:
        self.calls.append(attempt_id)
        if self.error is not None:
            raise self.error
        return dict(self.result_data)

    def get_raw(self, attempt_id: str) -> dict:
        return {"result": dict(self.result_data)}


def _git(repo: Path, *args: str, check: bool = True, env_extra=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        env=security.child_env(env_extra),
    )


def _repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")

    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sample.py").write_text(SAMPLE_TEST, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
        env=security.child_env(),
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "branch", BRANCH)
    _git(repo, "push", "-u", "origin", BRANCH)
    _git(repo, "checkout", "main")
    return repo, remote, base_sha


def _ctx(client: FakeClient, config: dict | None = None) -> FakeCtx:
    plugin = load_plugin()
    # Sandbox stays "auto" (bwrap/firejail when present; never unshare); the unsafe
    # local fallback only keeps these git-flow tests runnable on hosts with no
    # backend. Real isolation is asserted in test_sandbox.py and below.
    settings = {"verify_allow_unsafe_local": True, **(config or {})}
    ctx = FakeCtx(settings=settings)
    ctx.bundlebees_client = client
    plugin.register(ctx)
    return ctx


def _invoke(ctx: FakeCtx, argv: list[str]) -> int:
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    args = parser.parse_args(argv)
    return record["handler_fn"](args)


def _result(patch: str, **overrides) -> dict:
    result = {
        "attempt_id": ATTEMPT_ID,
        "branch": BRANCH,
        "patch": patch,
        "brief": {
            "branch": BRANCH,
            "assigned_files": ["src/a.py"],
            "tests": ["tests/test_sample.py"],
        },
        "tests": ["tests/test_sample.py"],
        "kanban_task_id": "kb-1",
    }
    result.update(overrides)
    if result.get("base_rev") is None:
        result.pop("base_rev", None)
    return result


def _lease(state: Path, source_rev: str, attempt_id: str = ATTEMPT_ID, **overrides) -> dict:
    brief = {
        "objective": "apply the change",
        "source_rev": source_rev,
        "branch": BRANCH,
        "assigned_files": ["src/a.py"],
        "acceptance": {"criteria": ["sample test passes"], "tests": ["tests/test_sample.py"]},
        "kanban_task_id": "kb-1",
    }
    brief.update(overrides)
    from bundlebees_plugin.brief import validate_brief

    return leases.record_lease(state, attempt_id, validate_brief(brief))


def _verify_argv(repo: Path, state: Path, *extra: str) -> list[str]:
    return [
        "verify",
        ATTEMPT_ID,
        "--workdir",
        str(repo),
        "--branch",
        BRANCH,
        "--state-dir",
        str(state),
        *extra,
    ]


def test_register_cli_registers_bundlebees_group():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    assert "bundlebees" in ctx.cli_commands
    record = ctx.cli_commands["bundlebees"]
    assert callable(record["setup_fn"])
    assert callable(record["handler_fn"])
    assert "bundlebees" in record["help"].lower()

    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)

    verify = parser.parse_args(["verify", ATTEMPT_ID, "--dry-run"])
    assert verify.command == "verify"
    assert verify.attempt_id == ATTEMPT_ID
    assert verify.dry_run is True

    deploy = parser.parse_args(["deploy"])
    assert deploy.command == "deploy"
    assert deploy.execute is False

    doctor = parser.parse_args(["doctor"])
    assert doctor.command == "doctor"


def test_verify_dry_run_applies_lease_runs_test_and_does_not_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc == 0, out
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\nvalue = 2\n"
    assert client.calls == [ATTEMPT_ID]

    # Dry run: no commit and no push; the remote feature branch is untouched.
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert (
        _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        == base_sha
    )
    assert "dry-run" in out.lower()
    assert "tests/test_sample.py" in out
    assert "src/a.py" in out

    payload = json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())
    assert set(payload) == {"attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence"}
    assert payload["evidence"]["exit_code"] == 0
    assert payload["evidence"]["source_rev"] == base_sha
    assert payload["evidence"]["diffstat"] == {"files": 1, "added": 1, "deleted": 0}
    assert payload["evidence"]["backend"] in ("bwrap", "firejail", "none-unsafe-local")
    assert payload["attempt_id"] == ATTEMPT_ID
    assert payload["ok"] is True
    assert payload["sha"] is None
    assert payload["branch"] == BRANCH
    assert payload["files"] == ["src/a.py"]
    assert payload["tests"] == ["tests/test_sample.py"]
    text = json.dumps(payload)
    assert "http" not in text
    assert "token" not in text


def test_verify_rejects_out_of_lease_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "scope_violation" in out
    assert not (repo / "src" / "b.py").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert (
        _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        == base_sha
    )
    payload = json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())
    assert payload["ok"] is False


def test_verify_secret_scan_blocks_output_and_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    secret = "sk-" + "a" * 32
    client = FakeClient(_result(SECRET_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "secret" in out.lower()
    assert secret not in out
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert (
        _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
        == base_sha
    )
    state_text = (state / "verify-results" / f"{ATTEMPT_ID}.json").read_text()
    assert secret not in state_text


def test_verify_refuses_main_as_push_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))
    argv = _verify_argv(repo, state, "--dry-run")
    argv[argv.index("--branch") + 1] = "main"

    rc = _invoke(_ctx(client), argv)
    out = capsys.readouterr().out

    assert rc != 0
    assert "main" in out.lower()
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha


def test_verify_refuses_master_as_push_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, _remote, _base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, _base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=_base_sha))
    argv = _verify_argv(repo, state, "--dry-run")
    argv[argv.index("--branch") + 1] = "master"

    rc = _invoke(_ctx(client), argv)
    out = capsys.readouterr().out

    assert rc != 0
    assert "master" in out.lower()


def test_verify_push_is_mocked_to_feature_branch_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    repo, remote, _base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, _base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=_base_sha))

    real_run_git = cli._run_git
    pushes: list[list[str]] = []

    def wrapper(args, cwd, **kwargs):
        call = list(args)
        if call and call[0] == "push":
            pushes.append(call)
            return subprocess.CompletedProcess(["git", *call], 0, "", "")
        return real_run_git(call, cwd, **kwargs)

    monkeypatch.setattr(cli, "_run_git", wrapper)

    rc = _invoke(_ctx(client), _verify_argv(repo, state))
    out = capsys.readouterr().out

    assert rc == 0, out
    assert pushes == [["push", "origin", f"HEAD:refs/heads/{BRANCH}"]]
    for call in pushes:
        assert "main" not in call
        assert "master" not in call
        assert "--force" not in call
        assert "-f" not in call
        assert "--no-verify" not in call

    payload = json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())
    assert payload["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{40}", payload["sha"] or "")
    assert payload["branch"] == BRANCH
    assert _git(repo, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == payload["sha"]
    # The mocked push never updates the remote.
    assert remote.exists()


def test_verify_real_push_updates_only_feature_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))
    out = capsys.readouterr().out

    assert rc == 0, out
    payload = json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())
    sha = payload["sha"]
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == sha
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == base_sha
    remote_branches = set(
        _git(remote, "branch", "--format=%(refname:short)").stdout.split()
    )
    assert remote_branches == {BRANCH, "main"}
    assert sha in out


def test_verify_no_result_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(error=ClientError("no result yet", "no_result_yet"))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "no_result_yet" in out
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha


def test_verify_uses_configured_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, _remote, _base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, _base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=_base_sha))
    argv = [
        "verify",
        ATTEMPT_ID,
        "--branch",
        BRANCH,
        "--state-dir",
        str(state),
        "--dry-run",
    ]

    rc = _invoke(_ctx(client, {"worktree": str(repo)}), argv)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\nvalue = 2\n"


def test_verify_does_not_touch_kanban_without_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, _base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, _base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=_base_sha))
    calls: list[tuple] = []
    monkeypatch.setattr(
        cli,
        "_run_kanban_comment",
        lambda *a, **k: calls.append((a, k)) or 0,
    )

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    capsys.readouterr()

    assert rc == 0
    assert calls == []


def test_deploy_without_execute_prints_steps_and_does_not_ssh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple] = []
    monkeypatch.setattr(
        cli,
        "_run_ssh",
        lambda *a, **k: calls.append((a, k)) or subprocess.CompletedProcess([], 0, "", ""),
    )

    rc = _invoke(_ctx(FakeClient()), ["deploy"])
    out = capsys.readouterr().out

    assert rc == 0
    assert calls == []
    assert "host setup" in out.lower() or "steps" in out.lower()
    assert "ssh" in out.lower()


def test_deploy_execute_uses_stored_alias_without_printing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    alias = "worker-secret-alias"
    calls: list[tuple] = []

    def fake_ssh(host_alias, env, **kwargs):
        calls.append((host_alias, env))
        return subprocess.CompletedProcess(["ssh", host_alias], 0, "remote ok", "")

    monkeypatch.setattr(cli, "_run_ssh", fake_ssh)

    rc = _invoke(_ctx(FakeClient(), {"deploy_host_alias": alias}), ["deploy", "--execute"])
    out = capsys.readouterr().out

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == alias
    assert {"PATH", "LANG", "LC_ALL"} <= set(calls[0][1])
    assert set(calls[0][1]) <= {"PATH", "LANG", "LC_ALL", "HOME", "USER", "LOGNAME", "TZ", "TMPDIR"}
    assert alias not in out


def test_deploy_execute_without_stored_alias_refuses(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple] = []
    monkeypatch.setattr(cli, "_run_ssh", lambda *a, **k: calls.append((a, k)))

    rc = _invoke(_ctx(FakeClient()), ["deploy", "--execute"])
    out = capsys.readouterr().out

    assert rc != 0
    assert "not_configured" in out
    assert calls == []


def test_doctor_reports_configured_without_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cli, "_measure_schema_tokens", lambda: 0)
    rc = _invoke(_ctx(FakeClient()), ["doctor"])
    out = capsys.readouterr().out

    assert rc in (0, 1)
    assert "token" in out.lower()
    assert "configured" in out.lower()


def test_doctor_token_line_is_readable(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    for configured, expected in ((False, "token: not configured"), (True, "token: configured")):
        ctx = _ctx(FakeClient(), {})
        ctx.bundlebees_token_configured = (lambda value=configured: value)
        _invoke(ctx, ["doctor"])
        out = capsys.readouterr().out
        assert expected in out.splitlines()
        assert "[redacted]" not in out


def test_sandbox_options_weak_override_from_setting_or_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BUNDLEBEES_ALLOW_WEAK_UNSHARE", raising=False)
    assert cli._sandbox_options({})["allow_weak"] is False
    assert cli._sandbox_options({"verify_allow_weak_unshare": "false"})["allow_weak"] is False
    assert cli._sandbox_options({"verify_allow_weak_unshare": True})["allow_weak"] is True
    monkeypatch.setenv("BUNDLEBEES_ALLOW_WEAK_UNSHARE", "1")
    assert cli._sandbox_options({})["allow_weak"] is True


def test_doctor_reports_refused_unshare(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("BUNDLEBEES_ALLOW_WEAK_UNSHARE", raising=False)
    monkeypatch.setattr(cli, "_measure_schema_tokens", lambda: 0)
    _invoke(_ctx(FakeClient(), {"verify_isolation": "unshare"}), ["doctor"])
    out = capsys.readouterr().out
    assert "verify_isolation=unshare refused without the weak override" in out


# ---------------------------------------------------------------------------
# 0.2.0 hardening: lease authority, revision guarantees, sandbox, integrate
# ---------------------------------------------------------------------------


def _verify_result(state: Path) -> dict:
    return json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())


def test_verify_ignores_worker_claimed_scope_and_rejects_out_of_scope_patch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    # The worker tries to widen its own lease in every place it can.
    client = FakeClient(
        _result(
            OUT_OF_LEASE_PATCH,
            base_rev=base_sha,
            assigned_files=["src/a.py", "src/b.py"],
            brief={"assigned_files": ["src/a.py", "src/b.py"], "tests": ["tests/test_sample.py"]},
            observed={"assigned_files": ["src/b.py"]},
        )
    )

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "scope_violation" in out
    assert not (repo / "src" / "b.py").exists()
    payload = _verify_result(state)
    assert payload["ok"] is False
    assert set(payload["evidence"]["ignored_worker_claims"]) >= {"assigned_files", "brief.assigned_files"}
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == base_sha


def test_verify_requires_a_coordinator_lease(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "lease_missing" in out
    assert client.calls == []  # nothing fetched or applied without a lease
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"


def test_verify_detects_a_tampered_lease(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    lease_file = state / "leases" / f"{ATTEMPT_ID}.json"
    lease_file.chmod(0o600)
    record = json.loads(lease_file.read_text())
    record["assigned_files"] = ["src/a.py", "src/b.py"]
    lease_file.write_text(json.dumps(record))
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "lease_tampered" in out
    assert not (repo / "src" / "b.py").exists()


def test_verify_rejects_wrong_source_rev(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    bogus = "0123456789abcdef" * 2 + "01234567"
    _lease(state, bogus)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=bogus))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "source_rev_missing" in out
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"


def test_verify_rejects_a_worker_that_declares_a_different_base(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev="f" * 40))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "base_mismatch" in out


def test_verify_rejects_a_patch_not_built_on_source_rev(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    repo, _remote, base_sha = _repo(tmp_path)
    # The feature branch moves on; the worker built its diff on the NEW tip
    # while claiming the brief's source_rev.
    _git(repo, "checkout", BRANCH)
    (repo / "src" / "a.py").write_text("value = 10\n", encoding="utf-8")
    _git(repo, "commit", "-am", "later")
    _git(repo, "checkout", "main")
    later_patch = IN_LEASE_PATCH.replace(" value = 1\n", " value = 10\n")
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(later_patch, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "patch_not_at_source_rev" in out


def test_verify_fails_closed_without_a_sandbox(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    from bundlebees_plugin import sandbox

    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    monkeypatch.setattr(sandbox, "detect_backend", lambda *a, **k: None)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client, {"verify_allow_unsafe_local": False}), _verify_argv(repo, state))
    out = capsys.readouterr().out

    assert rc != 0
    assert "sandbox_unavailable" in out
    assert "verify_allow_unsafe_local" in out
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == base_sha


def test_verify_runs_worker_tests_with_network_blocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A worker test that reaches a coordinator-side listener must not connect."""
    import socket

    from bundlebees_plugin import sandbox

    if sandbox.detect_backend() is None:
        pytest.skip("no sandbox backend on this host")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        net_test = (
            "import socket\n"
            "def test_coordinator_unreachable():\n"
            "    s = socket.socket()\n"
            "    s.settimeout(2)\n"
            f"    assert s.connect_ex(('127.0.0.1', {port})) != 0\n"
        )
        patch = (
            "diff --git a/tests/test_net.py b/tests/test_net.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/tests/test_net.py\n"
            f"@@ -0,0 +1,{len(net_test.splitlines())} @@\n"
            + "".join("+" + line + "\n" for line in net_test.splitlines())
        )
        repo, _remote, base_sha = _repo(tmp_path)
        state = tmp_path / "state"
        _lease(
            state,
            base_sha,
            assigned_files=["tests/test_net.py"],
            acceptance={"criteria": ["offline"], "tests": ["tests/test_net.py"]},
        )
        client = FakeClient(_result(patch, base_rev=base_sha))

        rc = _invoke(_ctx(client, {"verify_allow_unsafe_local": False}), _verify_argv(repo, state, "--dry-run"))
        out = capsys.readouterr().out
    finally:
        listener.close()

    assert rc == 0, out
    assert _verify_result(state)["evidence"]["backend"] != "none-unsafe-local"


def _real_verify(tmp_path: Path, capsys) -> tuple[Path, Path, str, Path]:
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha, run_id="r1")
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha, usage={"tokens": 1200, "cost_usd": 0.03}))
    rc = _invoke(_ctx(client), _verify_argv(repo, state))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "not accepted until" in out
    _git(repo, "checkout", BRANCH)
    return repo, remote, base_sha, state


def _integrate_argv(repo: Path, state: Path, wave: str = "w1") -> list[str]:
    return [
        "integrate", "--wave", wave, "--attempts", ATTEMPT_ID,
        "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state),
    ]


def test_integrate_accepts_a_wave_only_after_the_full_suite_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from bundlebees_plugin import revision

    repo, _remote, _base, state = _real_verify(tmp_path, capsys)
    assert revision.attempt_accepted(state, ATTEMPT_ID) is False

    rc = _invoke(_ctx(FakeClient()), _integrate_argv(repo, state))
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "accepted: yes" in out
    assert revision.attempt_accepted(state, ATTEMPT_ID) is True

    rc = _invoke(_ctx(FakeClient()), ["metrics", "--run", "r1", "--json", "--state-dir", str(state)])
    summary = json.loads(capsys.readouterr().out)[0]
    assert rc == 0
    assert summary["accepted_changes"] == 1
    assert summary["first_pass_rate"] == 1.0
    assert summary["tokens_per_accepted"] == 1200


def test_integrate_rejects_a_wave_whose_combined_suite_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from bundlebees_plugin import revision

    repo, _remote, _base, state = _real_verify(tmp_path, capsys)
    # Another slice landed a regression on the combined branch.
    (repo / "tests" / "test_combined.py").write_text("def test_combined():\n    assert False\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "combined regression")

    rc = _invoke(_ctx(FakeClient()), _integrate_argv(repo, state))
    out = capsys.readouterr().out

    assert rc != 0
    assert "accepted: no" in out
    assert revision.attempt_accepted(state, ATTEMPT_ID) is False
    events = metrics.read_events(state, "r1")
    integrate = [e for e in events if e["event"] == "integrate"][-1]
    assert integrate["ok"] is False
    assert integrate["regressions"] >= 1


def test_integrate_refuses_unverified_attempts(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo, _remote, _base = _repo(tmp_path)
    _git(repo, "checkout", BRANCH)
    state = tmp_path / "state"

    rc = _invoke(_ctx(FakeClient()), _integrate_argv(repo, state))
    out = capsys.readouterr().out

    assert rc != 0
    assert "accepted: no" in out
    assert "problem: " + ATTEMPT_ID in out


def test_lease_command_records_once_and_rejects_invalid_briefs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    state = tmp_path / "state"
    good = tmp_path / "brief.json"
    good.write_text(json.dumps({
        "objective": "x", "source_rev": "a" * 40, "assigned_files": ["src/a.py"],
        "acceptance": ["tests pass"],
    }))
    argv = ["lease", "att-9", "--brief", str(good), "--state-dir", str(state)]
    assert _invoke(_ctx(FakeClient()), argv) == 0
    assert "recorded" in capsys.readouterr().out
    assert _invoke(_ctx(FakeClient()), argv) != 0
    assert "lease_exists" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"objective": "x", "source_rev": "main", "assigned_files": ["a"], "acceptance": ["y"]}))
    assert _invoke(_ctx(FakeClient()), ["lease", "att-10", "--brief", str(bad), "--state-dir", str(state)]) != 0
    assert "brief_invalid" in capsys.readouterr().out


def test_metrics_note_records_a_human_intervention(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    state = tmp_path / "state"
    assert _invoke(_ctx(FakeClient()), ["metrics", "--note", "re-scoped BH02", "--state-dir", str(state)]) == 0
    capsys.readouterr()
    assert _invoke(_ctx(FakeClient()), ["metrics", "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert "human_interventions" in out.replace(" ", "_").lower() or "intervention" in out.lower()
