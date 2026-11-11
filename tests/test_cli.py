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

from bundlebees_plugin import security
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
    ctx = FakeCtx()
    ctx.config = dict(config or {})
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
    return result


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
    client = FakeClient(_result(IN_LEASE_PATCH))

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
    assert set(payload) == {"attempt_id", "ok", "sha", "branch", "files", "tests", "at"}
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
    client = FakeClient(_result(OUT_OF_LEASE_PATCH))

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
    secret = "sk-" + "a" * 32
    client = FakeClient(_result(SECRET_PATCH))

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
    client = FakeClient(_result(IN_LEASE_PATCH))
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
    client = FakeClient(_result(IN_LEASE_PATCH))
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
    client = FakeClient(_result(IN_LEASE_PATCH))

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
    client = FakeClient(_result(IN_LEASE_PATCH))

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
    client = FakeClient(_result(IN_LEASE_PATCH))
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
    client = FakeClient(_result(IN_LEASE_PATCH))
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

    rc = _invoke(_ctx(FakeClient(), {"deploy_host": alias}), ["deploy", "--execute"])
    out = capsys.readouterr().out

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][0] == alias
    assert set(calls[0][1]) == {"PATH", "LANG", "LC_ALL"}
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
