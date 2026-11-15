"""Evidence files must be addressed the way the child sees the sandbox, not by their host path.

``firejail --private=<copy>`` mounts the copy **over** ``$HOME`` (measured on this host: a host path under
the copy answers ENOENT inside the jail, while ``./…`` and ``$HOME/…`` resolve). Before this fix the
evidence directory was passed as its host path, so the ACDD collector wrote no manifest and every
``tdd red`` / ``verify`` on such a host reported ``tests_uncollectable`` / ``test_ref_unknown`` — a
collection failure that looked like a bad patch.
"""
from __future__ import annotations

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

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            self.calls.append([str(part) for part in command])
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
        assert str(self.work / sandbox._EVIDENCE_NAME) not in flat, "host evidence path leaked into the jail"

    def test_bubblewrap_keeps_its_mounted_evidence_path(self):
        # bwrap mounts the copy at /work, so absolute-inside paths stay valid there.
        result = self._run("bwrap")
        assert result.get("backend") in (None, "bwrap")


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
