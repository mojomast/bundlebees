"""``verify_python``: the interpreter the sandbox starts must be reachable inside it.

``firejail --private=<copy>`` masks the coordinator home, and a Hermes install whose interpreter
lives there cannot start pytest; the wrapper only says ``no suitable <path> executable found``.
The fix is explicit: ``verify_python`` (setting) or ``BUNDLEBEES_VERIFY_PYTHON`` (env) names an
interpreter the jail can exec, and a hidden one refuses early with ``sandbox_interpreter_hidden``.

Reachability is probed through the real wrapper, never inferred from the path: this host masks
``~/.hermes/tools/python-*`` while a separate mount under ``~/.cache/uv`` stays visible.

Offline only: ``subprocess.run`` is faked here, so no backend executes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from bundlebees_plugin import sandbox


def _exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class TestResolveSandboxPython:
    @pytest.fixture(autouse=True)
    def _tree(self, monkeypatch):
        monkeypatch.delenv(sandbox.PYTHON_ENV, raising=False)
        root = Path(tempfile.mkdtemp(prefix="bb-interp-"))
        self.outer = _exe(root / "var" / "bundlebees-py" / "bin" / "python3")

    def test_unset_means_the_coordinators_own_interpreter(self):
        assert sandbox.resolve_sandbox_python(None) is None

    def test_env_var_is_the_fallback(self, monkeypatch):
        monkeypatch.setenv(sandbox.PYTHON_ENV, self.outer)
        assert sandbox.resolve_sandbox_python(None) == self.outer

    def test_an_explicit_python_beats_the_env_var(self, monkeypatch):
        monkeypatch.setenv(sandbox.PYTHON_ENV, str(Path(self.outer).with_name("absent")))
        assert sandbox.resolve_sandbox_python(self.outer) == self.outer

    def test_it_must_be_absolute(self):
        with pytest.raises(sandbox.SandboxUnavailable) as caught:
            sandbox.resolve_sandbox_python("./python3")
        assert caught.value.code == "invalid_argument"

    def test_it_must_be_executable(self):
        plain = Path(self.outer).with_name("plain.txt")
        plain.write_text("x", encoding="utf-8")
        plain.chmod(0o644)
        with pytest.raises(sandbox.SandboxUnavailable) as caught:
            sandbox.resolve_sandbox_python(str(plain))
        assert caught.value.code == "invalid_argument"

    def test_a_missing_python_is_refused(self):
        with pytest.raises(sandbox.SandboxUnavailable) as caught:
            sandbox.resolve_sandbox_python(str(Path(self.outer).with_name("absent")))
        assert caught.value.code == "invalid_argument"


class TestSwapInterpreter:
    def test_swaps_the_interpreter_position_only(self):
        argv = [sys.executable, "-B", "-m", "pytest", "tests/test_x.py"]
        out = sandbox.swap_interpreter(argv, "/var/tmp/bundlebees-py/bin/python3")
        assert out == ["/var/tmp/bundlebees-py/bin/python3", "-B", "-m", "pytest", "tests/test_x.py"]
        assert out is not argv
        assert argv[0] == sys.executable  # the caller's list is untouched

    def test_leaves_a_foreign_command_alone(self):
        assert sandbox.swap_interpreter(["/bin/true"], "/var/tmp/py") == ["/bin/true"]

    def test_no_substitute_is_a_no_op(self):
        assert sandbox.swap_interpreter([sys.executable], None) == [sys.executable]
        assert sandbox.swap_interpreter([], "/var/tmp/py") == []


class TestRunInSandbox:
    """The refusal must happen before the worker's command starts."""

    @pytest.fixture(autouse=True)
    def _tree(self, monkeypatch):
        monkeypatch.delenv(sandbox.PYTHON_ENV, raising=False)
        root = Path(tempfile.mkdtemp(prefix="bb-run-"))
        self.outer = _exe(root / "var" / "bundlebees-py" / "bin" / "python3")
        self.work = root / "work"
        self.work.mkdir()
        self.probes: list[int] = []      # what the faked jail reports, in order
        self.calls: list[list[str]] = []

        class Done:
            def __init__(self, returncode=0):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""

        def fake_run(command, **kwargs):
            command = list(command)
            if any("test -x" in str(part) for part in command):
                return Done(self.probes.pop(0) if self.probes else 1)
            self.calls.append(command)
            return Done()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    def _run(self, argv=None):
        return sandbox.run_in_sandbox(
            self.work,
            list(argv or [sys.executable, "-c", "print(1)"]),
            backend="firejail",
            probe=lambda name: True,
            which=lambda name: "/usr/bin/" + name,
        )

    def test_a_hidden_interpreter_refuses_before_running_the_command(self):
        self.probes = [1]
        with pytest.raises(sandbox.SandboxUnavailable) as caught:
            self._run()
        assert caught.value.code == "sandbox_interpreter_hidden"
        assert "verify_python" in caught.value.fix
        assert "no suitable" in caught.value.error  # names the wrapper message it replaces
        assert self.calls == []

    def test_a_visible_interpreter_runs_unchanged(self):
        self.probes = [0]
        self._run()
        assert len(self.calls) == 1
        assert str(sys.executable) in self.calls[0]

    def test_a_configured_interpreter_that_the_jail_hides_is_staged_into_the_copy(self, monkeypatch):
        monkeypatch.setenv(sandbox.PYTHON_ENV, self.outer)
        self.probes = [1, 0]  # visible on the host, hidden in the jail, visible once staged
        self._run()
        command = self.calls[0]
        assert command[0] == "firejail"
        staged = [part for part in command if sandbox._SANDBOX_PYTHON_DIR in str(part)]
        assert staged, command
        assert command[-1] == "print(1)"  # the rest of the argv is untouched

    def test_a_staged_interpreter_that_is_still_hidden_refuses(self, monkeypatch):
        monkeypatch.setenv(sandbox.PYTHON_ENV, self.outer)
        self.probes = [1, 1]
        with pytest.raises(sandbox.SandboxUnavailable) as caught:
            self._run()
        assert caught.value.code == "sandbox_interpreter_hidden"
        assert self.calls == []

    def test_a_visible_configured_interpreter_is_used_as_is(self, monkeypatch):
        monkeypatch.setenv(sandbox.PYTHON_ENV, self.outer)
        self.probes = [0]
        self._run()
        command = self.calls[0]
        assert self.outer in command
        assert sandbox._SANDBOX_PYTHON_DIR not in " ".join(command)

    def test_a_foreign_command_is_never_probed_or_swapped(self, monkeypatch):
        monkeypatch.setenv(sandbox.PYTHON_ENV, self.outer)
        # No probe is queued, so a probe would answer "hidden" and refuse; reaching the command
        # proves none ran.
        self._run(["/bin/true"])
        assert len(self.calls) == 1
        assert "/bin/true" in self.calls[0]
        assert self.outer not in self.calls[0]


class TestStageInterpreter:
    def test_a_regular_interpreter_gains_the_execute_bit(self, tmp_path):
        host = Path(_exe(tmp_path / "venv" / "bin" / "python3"))
        host.chmod(0o644)  # as if copied without the execute bit
        staged = sandbox.stage_interpreter(tmp_path / "work", str(host))
        assert staged == f"./{sandbox._SANDBOX_PYTHON_DIR}/bin/python3"  # cwd-relative by design
        target = tmp_path / "work" / staged[2:]
        assert target.is_file() and not target.is_symlink()
        assert target.stat().st_mode & 0o500 == 0o500

    def test_a_venv_symlink_is_staged_without_following_it(self, tmp_path):
        # Every normal venv links bin/python at its base interpreter; chmod would EPERM there.
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (venv / "bin" / "python").symlink_to("/usr/bin/python3.12")
        staged = sandbox.stage_interpreter(tmp_path / "work", str(venv / "bin" / "python"))
        assert staged == f"./{sandbox._SANDBOX_PYTHON_DIR}/bin/python"
        target = tmp_path / "work" / staged[2:]
        assert target.is_symlink()
        assert os.readlink(target) == "/usr/bin/python3.12"
        assert (tmp_path / "work" / sandbox._SANDBOX_PYTHON_DIR / "pyvenv.cfg").is_file()


class TestDeclaration:
    def test_the_setting_is_declared(self):
        from bundlebees_plugin import settings

        assert settings.DEFAULTS["verify_python"] == ""

    def test_the_plugin_manifest_documents_it(self):
        manifest = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text(encoding="utf-8")
        )
        entry = manifest["config_schema"]["verify_python"]
        assert entry["type"] == "str"
        assert "interpreter" in entry["description"].lower()

    def test_sandbox_options_pass_it_through(self):
        from bundlebees_plugin import cli

        assert cli._sandbox_options({"verify_python": "/var/tmp/py/bin/python3"})["python"] == (
            "/var/tmp/py/bin/python3"
        )
        assert cli._sandbox_options({})["python"] is None
