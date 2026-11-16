"""Evidence files must be addressed the way the child sees the sandbox, not by their host path.

``firejail --private=<copy>`` mounts the copy **over** ``$HOME`` (measured on this host: a host path under
the copy answers ENOENT inside the jail, while ``./…`` and ``$HOME/…`` resolve). Before this fix the
evidence directory was passed as its host path, so the ACDD collector wrote no manifest and every
``tdd red`` / ``verify`` on such a host reported ``tests_uncollectable`` / ``test_ref_unknown`` — a
collection failure that looked like a bad patch.

The same law applies to ``TIKTOKEN_CACHE_DIR``: ``--private-tmp`` masks ``/tmp`` and ``/var/tmp`` (and the
private home mount masks the coordinator home), so a cache there must be staged into the sandbox copy and
named relative to the child's cwd — exactly like a staged interpreter. And since such a cache can only be
used by staging it, ``_resolve_tiktoken_cache`` refuses one under a masked root outright: staging it would
copy a piece of (or all of) a masked root into the disposable sandbox.

The real-firejail child test reports the cache path letter-pair encoded on purpose: ``_output_tail`` scrubs
every tail through ``security.scrub``, whose ``_LONG_KEY_RE`` redacts any 32+ char ``[A-Za-z0-9+/=_-]`` run
holding a digit and a letter — a cache path matches, so a raw-path assert parses scrubbed text and is
structurally unpassable regardless of product behavior.
"""
from __future__ import annotations

import json
import os
import re
import shutil
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


class TestMaskedCachePredicate:
    """Which cache locations the firejail wrapper masks, decided without running it."""

    def test_tmp_and_var_tmp_are_masked_by_private_tmp(self):
        assert sandbox._cache_masked_in_sandbox(Path("/tmp")) is True
        assert sandbox._cache_masked_in_sandbox(Path("/tmp/tiktoken-cache")) is True
        assert sandbox._cache_masked_in_sandbox(Path("/var/tmp")) is True
        assert sandbox._cache_masked_in_sandbox(Path("/var/tmp/tiktoken-cache")) is True

    def test_the_coordinator_home_is_masked_by_the_private_home_mount(self):
        assert sandbox._cache_masked_in_sandbox(Path.home() / ".cache" / "tiktoken") is True

    def test_paths_outside_the_masked_roots_stay_visible(self):
        assert sandbox._cache_masked_in_sandbox(Path("/opt/bundlebees/tiktoken-cache")) is False
        assert sandbox._cache_masked_in_sandbox(Path("/usr/share/tiktoken")) is False


class TestTiktokenCachePath:
    """The firejail child's ``TIKTOKEN_CACHE_DIR`` must name a directory that exists in the jail.

    ``--private-tmp`` replaces ``/tmp`` and ``/var/tmp`` with fresh tmpfs mounts, so a host cache under
    either is ENOENT inside the jail: tiktoken silently rebuilds (or fails offline). The cache must be
    staged into the sandbox copy and named relative to the child's cwd, like a staged interpreter.
    """

    @pytest.fixture(autouse=True)
    def _fake_jail(self, monkeypatch, tmp_path: Path):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.seen: list[dict] = []  # state of the staged copy at the moment each call ran

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(command, **kwargs):
            self.calls.append([str(part) for part in command])
            self.envs.append(dict(kwargs.get("env") or {}))
            staged = Path(kwargs.get("cwd") or ".") / sandbox._SANDBOX_TIKTOKEN_DIR
            self.seen.append(
                {
                    "exists": staged.is_dir(),
                    "files": sorted(p.name for p in staged.iterdir()) if staged.is_dir() else [],
                }
            )
            return Done()

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        self.work = tmp_path / "work"
        (self.work / "tests").mkdir(parents=True)
        self.cache = tmp_path / "tiktoken-cache"
        self.cache.mkdir()
        (self.cache / "fd9f0f3d3f6f4fe9").write_text("cached-encoding", encoding="utf-8")
        # Hermetic against the host layout (the #35 review finding): when TMPDIR is a directory under
        # the account home, tmp_path — and with it the cache — lives inside the real home and
        # _resolve_tiktoken_cache refused every run in this class. Pin what the code calls "home" to a
        # sibling of the cache, and empty the masked roots so the tmp_path cache is not refused as
        # living under /tmp (the refusal itself is pinned by TestResolveRefusesMaskedRoots against the
        # real roots). Which verdict _cache_masked_in_sandbox returns is pinned per test too, because
        # no real location is masked and resolve-approved at the same time any more; the verdict logic
        # itself stays pinned by TestMaskedCachePredicate against the real roots and home.
        monkeypatch.setenv("HOME", str(tmp_path / "sandbox-home"))
        monkeypatch.setattr(sandbox, "_MASKED_TMP_ROOTS", ())
        self.cache_masked = True
        monkeypatch.setattr(
            sandbox, "_cache_masked_in_sandbox", lambda _cache: self.cache_masked
        )

    def _run(self, backend: str, **kw):
        return sandbox.run_in_sandbox(
            self.work,
            ["/bin/true"],
            backend=backend,
            tiktoken_cache=self.cache,
            probe=lambda name: True,
            which=lambda name: "/usr/bin/" + name,
            **kw,
        )

    def _jail_visible(self, value: str) -> bool:
        """The env value is either the relative staged path or a host path the jail still sees."""
        if value == f"./{sandbox._SANDBOX_TIKTOKEN_DIR}":
            return True
        for root in sandbox._MASKED_TMP_ROOTS:
            if value == root or value.startswith(root.rstrip("/") + "/"):
                return False  # masked: ENOENT inside the jail
        home = str(Path.home())
        if value == home or value.startswith(home.rstrip("/") + "/"):
            return False  # masked by the private home mount
        return os.path.isdir(value)

    def test_the_firejail_env_names_a_jail_visible_cache(self):
        self._run("firejail")
        assert self.envs, self.calls
        value = self.envs[0].get("TIKTOKEN_CACHE_DIR", "")
        assert self._jail_visible(value), value
        assert value != str(self.cache), "the masked host cache path is passed through verbatim"

    def test_a_masked_cache_is_staged_into_the_sandbox_copy(self):
        self._run("firejail")
        value = self.envs[0]["TIKTOKEN_CACHE_DIR"]
        assert value == f"./{sandbox._SANDBOX_TIKTOKEN_DIR}", value
        # At the moment the wrapper ran, the staged copy existed in the child's cwd with the cache's
        # content — the child can read it before the disposable copy is deleted.
        assert self.seen[0]["exists"] is True, self.seen
        assert self.seen[0]["files"] == ["fd9f0f3d3f6f4fe9"], self.seen
        assert str(self.cache) not in " ".join(self.calls[0]), self.calls[0]

    def test_a_genuinely_visible_cache_is_left_as_is(self):
        # The verdict is pinned instead of derived from the host layout, so the passthrough case does
        # not depend on this host offering a writable directory outside /tmp, /var/tmp and home; the
        # predicate's own real-world verdicts stay pinned by TestMaskedCachePredicate.
        self.cache_masked = False
        self._run("firejail")
        assert self.envs[0]["TIKTOKEN_CACHE_DIR"] == str(self.cache)
        assert self.seen[0]["exists"] is False, "nothing is staged for a visible cache"

    def test_bubblewrap_still_binds_the_cache_at_the_fixed_inside_path(self):
        self._run("bwrap")
        assert self.envs[0]["TIKTOKEN_CACHE_DIR"] == sandbox._TIKTOKEN_CACHE_INSIDE
        pairs = [
            (self.calls[0][i + 1], self.calls[0][i + 2])
            for i, part in enumerate(self.calls[0])
            if part == "--ro-bind"
        ]
        assert (str(self.cache), sandbox._TIKTOKEN_CACHE_INSIDE) in pairs, pairs
        assert self.seen[0]["exists"] is False, "bwrap binds the cache instead of staging it"
        assert sandbox._SANDBOX_TIKTOKEN_DIR not in " ".join(self.calls[0])

    def test_unshare_keeps_the_host_path(self):
        self._run("unshare", allow_weak=True)
        assert self.envs[0]["TIKTOKEN_CACHE_DIR"] == str(self.cache)


class TestResolveRefusesMaskedRoots:
    """``_resolve_tiktoken_cache`` must refuse a cache under a masked root outright.

    ``firejail --private-tmp`` replaces ``/tmp`` and ``/var/tmp`` with fresh tmpfs, so a cache there
    can only be used by staging it into the sandbox copy — which would copy a piece of (or all of) a
    masked root into the disposable sandbox. Refuse the location instead of silently staging it.
    """

    @pytest.mark.parametrize(
        "candidate",
        ["/tmp", "/var/tmp", "/tmp/bundlebees-cache-sub", "/var/tmp/bundlebees-cache-sub"],
    )
    def test_a_cache_under_a_masked_root_is_refused(self, candidate: str):
        with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
            sandbox._resolve_tiktoken_cache(candidate)
        assert excinfo.value.code == "unsafe_cache_dir"
        assert "must not live under" in excinfo.value.error, excinfo.value.error


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


class TestRealFirejailTiktokenCache:
    """The cache directory the firejail child's env names must exist inside the jail."""

    def test_the_child_sees_an_existing_cache_directory(self):
        from tests.fakes import require_sandbox_interpreter

        require_sandbox_interpreter("firejail")
        shm = Path("/dev/shm")
        if not shm.is_dir():
            pytest.skip("/dev/shm is not available on this host")
        work = Path(tempfile.mkdtemp(prefix="bb-tiktoken-real-"))
        # Hermetic cache outside every masked root (/tmp, /var/tmp, the coordinator home), so the
        # jail genuinely sees it and the child's TIKTOKEN_CACHE_DIR must be the passthrough host path.
        cache = Path(tempfile.mkdtemp(prefix="bb-tiktoken-real-cache-", dir=str(shm)))
        (cache / "fd9f0f3d3f6f4fe9").write_text("cached-encoding", encoding="utf-8")
        script = (
            "import json, os\n"
            "d = os.environ.get('TIKTOKEN_CACHE_DIR', '')\n"
            # The path is reported as two lowercase letters per character because _output_tail scrubs
            # every tail through security.scrub: _LONG_KEY_RE redacts any 32+ char
            # [A-Za-z0-9+/=_-] run holding a digit and a letter — the /dev/shm cache path matches
            # (measured: base64 is redacted too), so a raw path cannot survive the observation
            # channel. Lowercase a-z carries no digit, so the encoded value is left untouched.
            "enc = ''.join(chr(97 + ord(c) // 26) + chr(97 + ord(c) % 26) for c in d)\n"
            "print(json.dumps({'isdir': os.path.isdir(d), "
            "'files': sorted(os.listdir(d)) if d and os.path.isdir(d) else [], "
            "'dir_enc': enc}))\n"
        )
        try:
            result = sandbox.run_in_sandbox(
                work,
                [sys.executable, "-c", script],
                backend="firejail",
                tiktoken_cache=cache,
                timeout=120,
            )
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        assert result.get("exit_code") == 0, (result.get("exit_code"), result.get("output_tail"))
        line = [ln for ln in result["output_tail"].splitlines() if ln.startswith("{")][-1]
        out = json.loads(line)
        assert out["isdir"] is True, out
        assert out["files"] == ["fd9f0f3d3f6f4fe9"], out
        # Decode the letter pairs back to the exact path and demand the passthrough value: a /dev/shm
        # cache is outside every masked root, so the child's TIKTOKEN_CACHE_DIR must equal the host
        # path exactly — compared only after decoding, since raw-path asserts cannot survive the
        # output scrubber.
        encoded = out["dir_enc"]
        decoded = "".join(
            chr(26 * (ord(encoded[i]) - 97) + (ord(encoded[i + 1]) - 97))
            for i in range(0, len(encoded), 2)
        )
        assert decoded == str(cache), out
