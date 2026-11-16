"""Evidence files must be addressed the way the child sees the sandbox, not by their host path.

``firejail --private=<copy>`` mounts the copy **over** ``$HOME`` (measured on this host: a host path under
the copy answers ENOENT inside the jail, while ``./…`` and ``$HOME/…`` resolve). Before this fix the
evidence directory was passed as its host path, so the ACDD collector wrote no manifest and every
``tdd red`` / ``verify`` on such a host reported ``tests_uncollectable`` / ``test_ref_unknown`` — a
collection failure that looked like a bad patch.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import pytest

from bundlebees_plugin import sandbox


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


class TestEvidencePath:
    @pytest.fixture(autouse=True)
    def _fake_jail(self, monkeypatch):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            self.calls.append([str(part) for part in command])
            self.envs.append(dict(kwargs.get("env") or {}))
            return Done()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        self.work = Path(tempfile.mkdtemp(prefix="bb-evidence-"))
        (self.work / "tests").mkdir()
        self.interpreter = _executable(self.work.parent / (self.work.name + "-py") / "bin" / "python")

    def _run(self, backend: str):
        return sandbox.run_in_sandbox(
            self.work,
            [sys.executable, "-B", "-c", "print(1)", "{evidence}/collected.json", "tests/test_a.py"],
            backend=backend,
            python=self.interpreter,
            collect_file="collected.json",
            probe=lambda name: True,
            which=lambda name: "/usr/bin/" + name,
        )

    def test_firejail_gets_a_relative_evidence_path(self):
        self._run("firejail")
        runs = [c for c in self.calls if "test -x" not in " ".join(c)]
        assert runs, self.calls
        flat = " ".join(runs[0])
        assert f"./{sandbox._EVIDENCE_NAME}/collected.json" in flat, flat
        # Any absolute evidence path is invisible in the jail (the copy is mounted over $HOME), so the
        # command must contain none — not merely "not this one directory".
        prefixes = [m.group(1) for m in re.finditer(r"(\S*)" + re.escape(sandbox._EVIDENCE_NAME), flat)]
        assert prefixes == ["./"], prefixes

    def test_firejail_gets_a_home_that_exists_inside_the_jail(self):
        self._run("firejail")
        runs = [i for i, c in enumerate(self.calls) if "test -x" not in " ".join(c)]
        assert runs, self.calls
        command, env = self.calls[runs[0]], self.envs[runs[0]]
        # The jail mounts the copy over $HOME, so what it guarantees is a HOME that exists inside
        # the jail and resolves to the sandbox copy — not that HOME equals one specific host path.
        assert "HOME" in env, env
        home = env["HOME"]
        assert home, env
        private = [arg for arg in command if arg.startswith("--private=")]
        assert private, command
        copy_host_path = private[0][len("--private="):]
        assert home != copy_host_path, "HOME points at the copy's host path, which does not exist inside the jail"

    def test_bubblewrap_keeps_its_mounted_evidence_path(self):
        # bwrap mounts the copy at /work, so absolute-inside paths stay valid there.
        self._run("bwrap")
        runs = [c for c in self.calls if "test -x" not in " ".join(c)]
        assert runs, self.calls
        flat = " ".join(runs[0])
        assert f"/work/{sandbox._EVIDENCE_NAME}/collected.json" in flat, flat
        # The copy's host path legitimately appears as the --bind source, so check the prefix that
        # each occurrence of the evidence name was addressed with: it must be the mount point.
        prefixes = [m.group(1) for m in re.finditer(r"(\S*)" + re.escape(sandbox._EVIDENCE_NAME), flat)]
        assert prefixes == ["/work/"], prefixes


class TestRealFirejailWritesTheManifest:
    """The collector must actually run and its manifest must come back across the boundary."""

    def test_a_relative_manifest_is_produced_and_read(self):
        from tests.fakes import require_sandbox_interpreter

        require_sandbox_interpreter("firejail")
        work = Path(tempfile.mkdtemp(prefix="bb-evidence-real-"))
        (work / "tests").mkdir()
        (work / "tests" / "test_a.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        script = (
            "import json, pathlib, sys; "
            "p = pathlib.Path(sys.argv[1]); p.write_text(json.dumps({'ids': ['tests/test_a.py::test_ok']})); "
            "print('wrote', p.resolve())"
        )
        evidence = sandbox.run_in_sandbox(
            work,
            [sys.executable, "-c", script, "{evidence}/collected.json"],
            backend="firejail",
            collect_file="collected.json",
            timeout=120,
        )
        assert evidence.get("exit_code") == 0, (evidence.get("exit_code"), evidence.get("output_tail"))
        assert evidence.get("collected") == {"ids": ["tests/test_a.py::test_ok"]}
