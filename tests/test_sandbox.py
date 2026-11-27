"""Offline tests for the disposable verification sandbox.

No network, Docker, SSH, or real credentials are used. Tests that need a real
isolation backend skip when the host has none (for example when unprivileged
user namespaces are disabled). The worktree is always a ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import sandbox
from bundlebees_plugin.security import scrub
from tests.fakes import require_sandbox_interpreter


def _no_backends(name: str) -> None:
    return None


def _no_probe(name: str) -> bool:
    return False


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def test_detect_backend_auto_prefers_the_strongest_available():
    installed = {"firejail"}

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in installed else None

    assert sandbox.detect_backend(which=which, probe=lambda name: True) == "firejail"

    installed.update({"bwrap", "unshare"})
    assert sandbox.detect_backend(which=which, probe=lambda name: True) == "bwrap"

    # A binary that exists but whose probe fails is skipped.
    assert (
        sandbox.detect_backend(which=which, probe=lambda name: name != "bwrap")
        == "firejail"
    )


def test_detect_backend_auto_never_selects_unshare():
    every = lambda name: f"/usr/bin/{name}"  # noqa: E731
    # Only unshare works: auto must fail closed, even with the weak override.
    for allow_weak in (False, True):
        assert (
            sandbox.detect_backend(
                which=every,
                probe=lambda name: name == "unshare",
                allow_weak=allow_weak,
            )
            is None
        )
    probed: list[str] = []
    sandbox.detect_backend(which=every, probe=lambda n: probed.append(n) or False)
    assert "unshare" not in probed
    assert "unshare" not in sandbox.STRONG_BACKENDS
    assert "unshare" in sandbox.WEAK_BACKENDS


def test_detect_backend_explicit_unshare_needs_the_override():
    every = lambda name: f"/usr/bin/{name}"  # noqa: E731
    assert sandbox.detect_backend("unshare", which=every, probe=lambda n: True) is None
    assert (
        sandbox.detect_backend("unshare", allow_weak=True, which=every, probe=lambda n: True)
        == "unshare"
    )


def test_weak_override_reads_setting_and_env():
    assert sandbox.weak_override_enabled(False, {}) is False
    assert sandbox.weak_override_enabled(True, {}) is True
    assert sandbox.weak_override_enabled(False, {sandbox.WEAK_OVERRIDE_ENV: "1"}) is True
    assert sandbox.weak_override_enabled(False, {sandbox.WEAK_OVERRIDE_ENV: "true"}) is True
    assert sandbox.weak_override_enabled(False, {sandbox.WEAK_OVERRIDE_ENV: "0"}) is False
    assert sandbox.weak_override_enabled(False, {sandbox.WEAK_OVERRIDE_ENV: ""}) is False


def test_run_in_sandbox_refuses_explicit_unshare_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv(sandbox.WEAK_OVERRIDE_ENV, raising=False)
    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.run_in_sandbox(
            tmp_path,
            [sys.executable, "-c", "print('never runs')"],
            backend="unshare",
            which=lambda name: f"/usr/bin/{name}",
            probe=lambda name: True,
        )
    exc = excinfo.value
    assert exc.code == "sandbox_unavailable"
    assert "filesystem" in exc.error
    assert "verify_allow_weak_unshare" in exc.fix
    assert sandbox.WEAK_OVERRIDE_ENV in exc.fix


def test_run_in_sandbox_refuses_unshare_even_with_unsafe_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Asking for unshare by name is a configuration error, not a reason to
    # fall through to the unisolated path.
    monkeypatch.delenv(sandbox.WEAK_OVERRIDE_ENV, raising=False)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.run_in_sandbox(
            tmp_path,
            [sys.executable, "-c", "print('never runs')"],
            backend="unshare",
            allow_unsafe_local=True,
            which=lambda name: f"/usr/bin/{name}",
            probe=lambda name: True,
        )


def test_run_in_sandbox_auto_fails_closed_when_only_unshare_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(sandbox.WEAK_OVERRIDE_ENV, "1")
    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.run_in_sandbox(
            tmp_path,
            [sys.executable, "-c", "print('never runs')"],
            backend="auto",
            which=lambda name: "/usr/bin/unshare" if name == "unshare" else None,
            probe=lambda name: True,
        )
    assert "bubblewrap or firejail" in excinfo.value.error
    assert "never picked automatically" in excinfo.value.fix


def test_detect_backend_auto_returns_none_when_no_probe_succeeds():
    assert (
        sandbox.detect_backend(
            which=lambda name: f"/usr/bin/{name}",
            probe=lambda name: False,
        )
        is None
    )


def test_detect_backend_explicit_unavailable_returns_none():
    assert sandbox.detect_backend("bwrap", which=_no_backends, probe=lambda n: True) is None
    assert (
        sandbox.detect_backend(
            "bwrap",
            which=lambda name: "/usr/bin/bwrap",
            probe=lambda name: False,
        )
        is None
    )


def test_detect_backend_explicit_available_returns_name():
    assert (
        sandbox.detect_backend(
            "firejail",
            which=lambda name: "/usr/bin/firejail",
            probe=lambda name: True,
        )
        == "firejail"
    )


def test_detect_backend_rejects_unknown_names():
    with pytest.raises(ValueError):
        sandbox.detect_backend("docker", which=_no_backends, probe=lambda n: True)


# ---------------------------------------------------------------------------
# Fail closed / unsafe local
# ---------------------------------------------------------------------------


def test_run_in_sandbox_fails_closed_without_a_backend(tmp_path: Path):
    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.run_in_sandbox(
            tmp_path,
            [sys.executable, "-c", "print('never runs')"],
            backend="auto",
            which=_no_backends,
            probe=_no_probe,
        )

    exc = excinfo.value
    assert exc.code == "sandbox_unavailable"
    assert "verify_allow_unsafe_local" in exc.fix
    assert "bubblewrap" in exc.fix.lower()
    assert "never runs" not in str(exc)


def test_run_in_sandbox_unsafe_local_runs_and_labels_itself(tmp_path: Path):
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", "print('hello-local')"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["backend"] == "none-unsafe-local"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert "hello-local" in result["output_tail"]


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def test_sandbox_env_is_fresh_and_minimal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BUNDLEBEES_SENTINEL_SECRET", "sentinel-do-not-leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

    home = tmp_path / "sandbox-home"
    tmp = tmp_path / "sandbox-tmp"
    env = sandbox.sandbox_env(home, tmp)

    assert set(env) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "TMPDIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONHASHSEED",
        "CI",
    }
    assert "BUNDLEBEES_SENTINEL_SECRET" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["HOME"] == str(home)
    assert env["HOME"] != str(Path.home())
    assert env["TMPDIR"] == str(tmp)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONHASHSEED"] == "0"
    assert env["CI"] == "1"


def test_run_in_sandbox_does_not_pass_ambient_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("BUNDLEBEES_SENTINEL_SECRET", "sentinel-do-not-leak")
    script = (
        "import os\n"
        "print(os.environ.get('BUNDLEBEES_SENTINEL_SECRET', 'ABSENT'))\n"
    )
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", script],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["exit_code"] == 0
    assert "sentinel-do-not-leak" not in result["output_tail"]
    assert "ABSENT" in result["output_tail"]


# ---------------------------------------------------------------------------
# Wrapper commands
# ---------------------------------------------------------------------------


def test_build_command_bwrap_isolates_and_never_binds_root_or_home(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    home = tmp_path / "home"
    argv = ["/usr/bin/python3", "-c", "print(1)"]

    cmd = sandbox.build_command("bwrap", copy_dir, home, argv)

    assert cmd[0] == "bwrap"
    assert "--unshare-all" in cmd
    assert "--unshare-user" in cmd
    assert "--uid" in cmd and "65534" in cmd
    assert "--gid" in cmd and "65534" in cmd
    assert "--die-with-parent" in cmd
    assert "--new-session" in cmd
    assert "--proc" in cmd and "/proc" in cmd
    assert "--dev" in cmd and "/dev" in cmd
    assert "--tmpfs" in cmd and "/tmp" in cmd
    assert "--ro-bind-try" in cmd and "/usr" in cmd
    assert "--bind" in cmd
    assert str(copy_dir) in cmd and "/work" in cmd
    assert str(home) in cmd and "/home/sandbox" in cmd
    assert "--chdir" in cmd
    assert cmd[cmd.index("--chdir") + 1] == "/work"
    assert cmd[-3:] == argv
    assert "/" not in cmd
    assert str(Path.home()) not in cmd


def test_build_command_firejail_isolates_network(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    home = tmp_path / "home"
    cmd = sandbox.build_command("firejail", copy_dir, home, ["/bin/true"])

    assert cmd[0] == "firejail"
    assert "--net=none" in cmd
    assert "--private=" + str(copy_dir) in cmd
    assert "--private-tmp" in cmd
    assert "--caps.drop=all" in cmd
    assert "--nonewprivs" in cmd
    assert "--seccomp" in cmd
    assert "--nogroups" in cmd
    assert cmd[-1] == "/bin/true"
    assert "/" not in cmd
    assert str(Path.home()) not in cmd


def test_build_command_unshare_isolates_network_and_pid(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    home = tmp_path / "home"
    cmd = sandbox.build_command("unshare", copy_dir, home, ["/bin/true"])

    assert cmd[0] == "unshare"
    for flag in ("--user", "--net", "--ipc", "--uts", "--pid", "--fork", "--kill-child"):
        assert flag in cmd
    assert cmd[-1] == "/bin/true"
    assert "/" not in cmd
    assert str(Path.home()) not in cmd


def test_build_command_rejects_unknown_backend(tmp_path: Path):
    with pytest.raises(ValueError):
        sandbox.build_command("docker", tmp_path, tmp_path, ["true"])


# ---------------------------------------------------------------------------
# Copy isolation and evidence
# ---------------------------------------------------------------------------


def test_run_in_sandbox_removes_copy_and_leaves_worktree_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep.txt").write_text("original", encoding="utf-8")

    created: list[Path] = []
    real_mkdtemp = sandbox.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = Path(real_mkdtemp(*args, **kwargs))
        created.append(path)
        return str(path)

    monkeypatch.setattr(sandbox.tempfile, "mkdtemp", recording_mkdtemp)

    script = (
        "from pathlib import Path\n"
        "Path('sandbox-wrote.txt').write_text('x', encoding='utf-8')\n"
        "print('wrote')\n"
    )
    result = sandbox.run_in_sandbox(
        work,
        [sys.executable, "-c", script],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["exit_code"] == 0
    assert "wrote" in result["output_tail"]
    assert (work / "keep.txt").read_text(encoding="utf-8") == "original"
    assert not (work / "sandbox-wrote.txt").exists()
    assert created, "expected a temporary copy directory"
    assert not created[0].exists()


def test_run_in_sandbox_parses_junit_summary(tmp_path: Path):
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "ev = Path(sys.argv[1])\n"
        "ev.mkdir(parents=True, exist_ok=True)\n"
        "(ev / 'junit.xml').write_text(\n"
        "    '<?xml version=\"1.0\"?>'\n"
        "    '<testsuite tests=\"7\" failures=\"2\" errors=\"1\" skipped=\"3\"></testsuite>',\n"
        "    encoding='utf-8',\n"
        ")\n"
    )
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", script, "{evidence}"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["exit_code"] == 0
    assert result["junit"] == {
        "tests": 7,
        "failures": 2,
        "errors": 1,
        "skipped": 3,
    }


def test_run_in_sandbox_rejects_doctype_in_junit(tmp_path: Path):
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "ev = Path(sys.argv[1])\n"
        "ev.mkdir(parents=True, exist_ok=True)\n"
        "(ev / 'junit.xml').write_text(\n"
        "    '<!DOCTYPE testsuite [<!ENTITY x \"y\">]>'\n"
        "    '<testsuite tests=\"1\" failures=\"0\" errors=\"0\" skipped=\"0\"/>',\n"
        "    encoding='utf-8',\n"
        ")\n"
    )
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", script, "{evidence}"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["junit"] is None


def test_run_in_sandbox_ignores_preplanted_evidence(tmp_path: Path):
    planted = tmp_path / ".bundlebees-evidence"
    planted.mkdir()
    (planted / "junit.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )

    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", "print('no evidence')"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["exit_code"] == 0
    assert result["junit"] is None


def test_run_in_sandbox_scrubs_output(tmp_path: Path):
    secret = "sk-" + "a" * 32
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", f"print({secret!r})"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )

    assert secret not in result["output_tail"]
    assert "[redacted]" in result["output_tail"]


def test_run_in_sandbox_reports_timeout(tmp_path: Path):
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        backend="auto",
        allow_unsafe_local=True,
        timeout=1,
        which=_no_backends,
        probe=_no_probe,
    )

    assert result["timed_out"] is True
    assert result["exit_code"] != 0
    assert result["duration_s"] >= 0.0


def test_pytest_argv_uses_the_evidence_placeholder():
    assert sandbox.pytest_argv([]) == [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--junitxml={evidence}/junit.xml",
    ]
    assert sandbox.pytest_argv(["tests/test_x.py"])[-1] == "tests/test_x.py"


# ---------------------------------------------------------------------------
# Real isolation (skipped when the host has no usable backend)
# ---------------------------------------------------------------------------


def _real_backend() -> str:
    try:
        backend = sandbox.detect_backend()
    except Exception as exc:  # pragma: no cover - defensive
        pytest.skip(f"backend detection failed: {exc}")
    if backend is None:
        pytest.skip("no sandbox backend is available on this host")
    return backend


def test_real_sandbox_blocks_network(tmp_path: Path):
    backend = _real_backend()
    require_sandbox_interpreter(backend)
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "    print('CONNECTED')\n"
        "except Exception:\n"
        "    print('BLOCKED')\n"
    )
    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", script],
        backend=backend,
        timeout=30,
    )

    assert "CONNECTED" not in result["output_tail"]
    assert result["exit_code"] != 0 or "BLOCKED" in result["output_tail"]


def test_real_bwrap_hides_host_files(tmp_path: Path):
    backend = _real_backend()
    if backend != "bwrap":
        pytest.skip("the host-filesystem check is bwrap-specific")

    # A file outside the worktree copy (stand-in for ~/.ssh, ~/.gitconfig,
    # .env) must not be readable inside the sandbox.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "coordinator-secret.txt"
    secret_file.write_text("coordinator-only\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    host_home = str(Path.home())
    script = (
        "import os, sys\n"
        "try:\n"
        "    open(sys.argv[1]).read()\n"
        "    print('FILE_VISIBLE')\n"
        "except Exception:\n"
        "    print('FILE_HIDDEN')\n"
        "try:\n"
        "    entries = set(os.listdir(sys.argv[2]))\n"
        "except Exception:\n"
        "    entries = set()\n"
        "leaks = sorted(entries & {'.ssh', '.gitconfig', '.config', '.env', '.netrc', '.hermes', '.bash_history'})\n"
        "print('HOME_LEAKS=' + ','.join(leaks))\n"
    )
    result = sandbox.run_in_sandbox(
        work,
        [sys.executable, "-c", script, str(secret_file), host_home],
        backend=backend,
        timeout=30,
    )

    assert "FILE_HIDDEN" in result["output_tail"], result
    assert "FILE_VISIBLE" not in result["output_tail"]
    assert "HOME_LEAKS=\n" in result["output_tail"] + "\n"


def test_real_sandbox_hides_the_sentinel_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    backend = _real_backend()
    require_sandbox_interpreter(backend)
    monkeypatch.setenv("BUNDLEBEES_SENTINEL_SECRET", "sentinel-do-not-leak")
    script = "import os; print(os.environ.get('BUNDLEBEES_SENTINEL_SECRET', 'ABSENT'))\n"

    result = sandbox.run_in_sandbox(
        tmp_path,
        [sys.executable, "-c", script],
        backend=backend,
        timeout=30,
    )

    assert "sentinel-do-not-leak" not in result["output_tail"]
    assert "ABSENT" in result["output_tail"]


# ---------------------------------------------------------------------------
# A checkout's HEAD survives the copy (without its history)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    """Run git in *cwd* with a hermetic identity; return stripped stdout."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Sandbox Probe",
            "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
            "GIT_COMMITTER_NAME": "Sandbox Probe",
            "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
        }
    )
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"
    return done.stdout.strip()


def _make_checkout(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-q", "-m", "first commit")
    return _git(root, "rev-parse", "HEAD")


def test_stage_git_head_stages_a_minimal_detached_head(tmp_path: Path):
    source = tmp_path / "checkout"
    sha = _make_checkout(source)
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    assert sandbox.stage_git_head(copy_dir, source) == sha

    git_dir = copy_dir / ".git"
    assert (git_dir / "HEAD").read_text(encoding="utf-8") == sha + "\n"
    assert (git_dir / "config").read_text(encoding="utf-8") == (
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    )
    assert (git_dir / "objects").is_dir()
    assert list((git_dir / "objects").iterdir()) == []
    assert (git_dir / "refs").is_dir()
    assert list((git_dir / "refs").iterdir()) == []
    assert sorted(entry.name for entry in git_dir.iterdir()) == [
        "HEAD",
        "config",
        "objects",
        "refs",
    ]
    assert not (git_dir / "logs").exists()
    # The staged HEAD is resolvable in the copy, which is what code_rev_of reads.
    done = subprocess.run(
        ["git", "-C", str(copy_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == sha


def test_stage_git_head_leaves_a_plain_tree_without_git(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("plain\n", encoding="utf-8")

    # A bare repository and a directory inside a checkout are not checkouts of
    # their own either; none of them may leave a .git behind.
    checkout = tmp_path / "checkout"
    _make_checkout(checkout)
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "--bare", "-q", str(checkout), str(bare))
    inside = checkout / "subdir"
    inside.mkdir()

    for source in (plain, bare, inside):
        assert sandbox.stage_git_head(copy_dir, source) is None
        assert not (copy_dir / ".git").exists()


def test_stage_git_head_leaves_an_empty_or_unborn_repository_without_git(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    empty = tmp_path / "empty"
    (empty / ".git").mkdir(parents=True)
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")

    for source in (empty, broken, unborn):
        assert sandbox.stage_git_head(copy_dir, source) is None
        assert not (copy_dir / ".git").exists()


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_jail(monkeypatch: pytest.MonkeyPatch, observe):
    """Let real git run and hand every jail invocation to *observe*."""
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        command = [str(part) for part in command]
        if command and command[0] == "git":
            return real_run(command, **kwargs)
        return observe(command, kwargs)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)


def _run_through_fake_jail(work: Path):
    return sandbox.run_in_sandbox(
        work,
        ["/bin/true"],
        backend="firejail",
        which=lambda name: "/usr/bin/" + name,
        probe=lambda name: True,
    )


def test_run_in_sandbox_stages_the_checkout_head_into_the_jail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "checkout"
    sha = _make_checkout(source)
    assert len(sha) == 40 and all(char in "0123456789abcdef" for char in sha)
    seen: dict[str, object] = {}

    def observe(command, kwargs):
        copy_dir = Path(kwargs["cwd"])
        git_dir = copy_dir / ".git"
        seen["command"] = command
        seen["copy_dir"] = copy_dir
        seen["head"] = (git_dir / "HEAD").read_text(encoding="utf-8")
        seen["config"] = (git_dir / "config").read_text(encoding="utf-8")
        seen["refs"] = sorted(entry.name for entry in (git_dir / "refs").iterdir())
        seen["objects"] = sorted(entry.name for entry in (git_dir / "objects").iterdir())
        return _Done()

    _fake_jail(monkeypatch, observe)
    result = _run_through_fake_jail(source)

    assert result["exit_code"] == 0
    assert seen["command"][0] == "firejail"
    assert seen["head"] == sha + "\n"
    assert seen["config"] == (
        "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    )
    assert seen["refs"] == []
    assert seen["objects"] == []
    # The fake saw the copy while it existed; run_in_sandbox removed it afterwards.
    assert not seen["copy_dir"].exists()


def test_run_in_sandbox_refuses_a_broken_git_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "broken"
    (work / ".git").mkdir(parents=True)
    (work / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")
    calls: list[list[str]] = []

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    assert caught.value.code == "sandbox_git_broken"
    assert ".git" in caught.value.fix
    assert calls == []


def test_run_in_sandbox_refuses_a_checkout_without_a_resolvable_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "unborn"
    work.mkdir()
    _git(work, "init", "-q")
    calls: list[list[str]] = []

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    assert caught.value.code == "sandbox_git_head_unresolved"
    assert calls == []


@pytest.mark.parametrize("target", ["HEAD", "config", "refs", "objects"])
def test_run_in_sandbox_refuses_when_git_staging_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
):
    source = tmp_path / "checkout"
    _make_checkout(source)
    calls: list[list[str]] = []
    real_write_text = Path.write_text
    real_mkdir = Path.mkdir

    def write_text(self, *args, **kwargs):
        if target in ("HEAD", "config") and self.name == target and self.parent.name == ".git":
            raise OSError(28, "No space left on device", str(self))
        return real_write_text(self, *args, **kwargs)

    def mkdir(self, *args, **kwargs):
        if target in ("refs", "objects") and self.name == target and self.parent.name == ".git":
            raise OSError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(source)

    assert caught.value.code == "sandbox_git_head_unwritable"
    assert ".git" in caught.value.fix
    assert calls == []


def test_run_in_sandbox_leaves_a_plain_directory_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "plain"
    work.mkdir()
    (work / "file.txt").write_text("plain\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def observe(command, kwargs):
        copy_dir = Path(kwargs["cwd"])
        seen["git"] = (copy_dir / ".git").exists() or (copy_dir / ".git").is_symlink()
        seen["file"] = (copy_dir / "file.txt").read_text(encoding="utf-8")
        return _Done()

    _fake_jail(monkeypatch, observe)
    result = _run_through_fake_jail(work)

    assert result["exit_code"] == 0
    assert seen["git"] is False
    assert seen["file"] == "plain\n"


def test_run_in_sandbox_leaves_a_bare_repository_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = tmp_path / "checkout"
    _make_checkout(checkout)
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "--bare", "-q", str(checkout), str(bare))
    seen: dict[str, object] = {}

    def observe(command, kwargs):
        copy_dir = Path(kwargs["cwd"])
        seen["git"] = (copy_dir / ".git").exists() or (copy_dir / ".git").is_symlink()
        return _Done()

    _fake_jail(monkeypatch, observe)
    result = _run_through_fake_jail(bare)

    assert result["exit_code"] == 0
    assert seen["git"] is False


def test_run_in_sandbox_stages_a_checkout_reached_by_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "co"
    sha = _make_checkout(source)
    seen: dict[str, object] = {}

    def observe(command, kwargs):
        seen["head"] = (Path(kwargs["cwd"]) / ".git" / "HEAD").read_text(encoding="utf-8")
        return _Done()

    _fake_jail(monkeypatch, observe)
    monkeypatch.chdir(tmp_path)
    result = _run_through_fake_jail(Path("co"))

    assert result["exit_code"] == 0
    assert seen["head"] == sha + "\n"


def test_run_in_sandbox_stages_a_checkout_reached_through_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "co"
    sha = _make_checkout(source)
    link = tmp_path / "co-link"
    link.symlink_to(source, target_is_directory=True)
    seen: dict[str, object] = {}

    def observe(command, kwargs):
        seen["head"] = (Path(kwargs["cwd"]) / ".git" / "HEAD").read_text(encoding="utf-8")
        return _Done()

    _fake_jail(monkeypatch, observe)
    result = _run_through_fake_jail(link)

    assert result["exit_code"] == 0
    assert seen["head"] == sha + "\n"


def test_run_in_sandbox_classifies_before_copying_a_broken_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "broken"
    (work / ".git").mkdir(parents=True)
    (work / ".git" / "HEAD").write_text("not a ref\n", encoding="utf-8")
    calls: list[object] = []
    real_copytree = sandbox.shutil.copytree

    def copytree(*args, **kwargs):
        calls.append(args)
        return real_copytree(*args, **kwargs)

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    monkeypatch.setattr(sandbox.shutil, "copytree", copytree)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    assert caught.value.code == "sandbox_git_broken"
    assert calls == []


def _sha256_checkout(work: Path) -> str:
    work.mkdir()
    _git(work, "init", "-q", "--object-format=sha256")
    (work / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "first commit")
    sha = _git(work, "rev-parse", "HEAD")
    assert len(sha) == 64 and all(char in "0123456789abcdef" for char in sha)
    return sha


def _unsupported_head_message(source: Path, revision: str) -> str:
    return (
        f"the git checkout at {source} has a 64-hex revision in HEAD ({revision}) "
        "that the sandbox cannot stage; only a 40-hex SHA-1 revision is "
        "representable in the jail's .git/HEAD (a 64-hex HEAD is most often a "
        "SHA-256 object-format repository)"
    )


def _unsupported_head_fix() -> str:
    return (
        "Re-run the verification from a checkout whose HEAD is a 40-hex SHA-1 "
        "commit. A 64-hex HEAD (most often a SHA-256 object-format repository) is "
        "refused rather than staged without a revision the jail can name; the "
        "sandbox stages only a 40-hex SHA-1 revision into .git/HEAD."
    )


def test_run_in_sandbox_refuses_a_sha256_checkout_with_a_truthful_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "sha256"
    sha = _sha256_checkout(work)
    source = work.resolve()
    calls: list[list[str]] = []

    real_copytree = sandbox.shutil.copytree
    real_mkdir = Path.mkdir
    copies: list[Path] = []
    git_seen_during_copy: list[bool] = []
    staged_git_dirs: list[Path] = []

    def copytree(src, dst, *args, **kwargs):
        result = real_copytree(src, dst, *args, **kwargs)
        destination = Path(dst)
        copies.append(destination)
        git_seen_during_copy.append(
            (destination / ".git").exists() or (destination / ".git").is_symlink()
        )
        return result

    def mkdir(self, *args, **kwargs):
        if self.name == ".git":
            staged_git_dirs.append(self)
        return real_mkdir(self, *args, **kwargs)

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    monkeypatch.setattr(sandbox.shutil, "copytree", copytree)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    # A 64-hex HEAD is refused before anything is staged: the copy itself never
    # carries .git and no .git directory is created while staging.
    assert len(copies) == 1
    assert git_seen_during_copy == [False]
    assert staged_git_dirs == []

    assert str(caught.value) == scrub(_unsupported_head_message(source, sha))
    assert caught.value.fix == _unsupported_head_fix()
    assert "64" in str(caught.value)
    assert "40" in caught.value.fix or "SHA-1" in caught.value.fix
    assert "unborn" not in str(caught.value)
    assert "unborn" not in caught.value.fix
    assert calls == []


def test_run_in_sandbox_refuses_a_sha256_checkout_with_a_distinct_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "sha256"
    _sha256_checkout(work)
    calls: list[list[str]] = []

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    assert caught.value.code == "sandbox_git_head_unsupported"
    assert calls == []


def test_run_in_sandbox_refuses_a_sha1_checkout_whose_head_is_64_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "sha1-with-64-hex-head"
    _make_checkout(work)
    revision = "0123456789abcdef" * 4
    (work / ".git" / "HEAD").write_text(revision + "\n", encoding="utf-8")
    source = work.resolve()
    calls: list[list[str]] = []

    def observe(command, kwargs):
        calls.append(command)
        return _Done()

    _fake_jail(monkeypatch, observe)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    assert caught.value.code == "sandbox_git_head_unsupported"
    assert str(caught.value) == scrub(_unsupported_head_message(source, revision))
    assert caught.value.fix == _unsupported_head_fix()
    assert "64" in str(caught.value)
    assert "40" in caught.value.fix or "SHA-1" in caught.value.fix
    assert "unborn" not in str(caught.value)
    assert "unborn" not in caught.value.fix
    assert calls == []


def test_run_in_sandbox_reads_the_sha256_head_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    work = tmp_path / "sha256"
    _sha256_checkout(work)
    reads: list[Path] = []
    real_read = sandbox._head_revision_of_checkout

    def read_once(source):
        reads.append(source)
        return real_read(source)

    def observe(command, kwargs):
        return _Done()

    _fake_jail(monkeypatch, observe)
    monkeypatch.setattr(sandbox, "_head_revision_of_checkout", read_once)
    with pytest.raises(sandbox.SandboxUnavailable) as caught:
        _run_through_fake_jail(work)

    assert len(reads) == 1
    assert caught.value.code == "sandbox_git_head_unsupported"


# --- pinned portable tools --------------------------------------------------


def test_needed_pinned_tools_reads_literal_tool_names(tmp_path: Path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_install.py").write_text(
        'import shutil\nuv = shutil.which("uv")\npwsh = shutil.which("pwsh")\nnot_uv = "uvicorn"\n',
        encoding="utf-8",
    )
    assert sandbox._needed_pinned_tools(tmp_path) == {"uv", "pwsh"}


def test_stage_pinned_tools_copies_only_needed_names(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    uv = _write_script(tmp_path / "uv", "#!/bin/sh\necho uv\n")
    pwsh = _write_script(tmp_path / "pwsh", "#!/bin/sh\necho pwsh\n")
    env = sandbox.stage_pinned_tools(
        copy_dir,
        {"PATH": "/usr/bin:/bin"},
        tools=[("uv", str(uv)), ("pwsh", str(pwsh))],
        needed={"uv"},
    )
    assert (copy_dir / ".bundlebees-tools" / "uv").is_file()
    assert not (copy_dir / ".bundlebees-tools" / "pwsh").exists()
    assert env["PATH"].startswith("./.bundlebees-tools:")
    assert env["PATH"].endswith("/usr/bin:/bin")


def test_run_in_sandbox_puts_the_pinned_tool_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    tool = _write_script(tmp_path / "uv", "#!/bin/sh\necho staged-uv\n")
    monkeypatch.setattr(sandbox, "PINNED_TOOLS", (("uv", str(tool)),))
    work = tmp_path / "work"
    (work / "tests").mkdir(parents=True)
    (work / "tests" / "test_build.py").write_text(
        'import shutil\nshutil.which("uv")\n', encoding="utf-8"
    )
    result = sandbox.run_in_sandbox(
        work,
        ["/bin/sh", "-c", "command -v uv; uv"],
        allow_unsafe_local=True,
        timeout=120,
    )
    assert result["exit_code"] == 0, result
    assert ".bundlebees-tools/uv" in result["output_tail"]
    assert "staged-uv" in result["output_tail"]
