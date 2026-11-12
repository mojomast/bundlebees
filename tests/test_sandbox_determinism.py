"""Approved acceptance tests for contract BS1-sandbox-determinism (review finding on bundlebees #8, RED first).

The full offline suite must give the same verdict inside the verifier sandbox as on a normal host, without
granting network or host-home access:
- ``localhost`` resolves inside bwrap through a synthetic, minimal ``/etc/hosts`` (and ``nsswitch.conf``); the
  host's own hosts file is never exposed.
- The sandbox ``HOME`` is a private, writable directory that is not itself a bind target, so tests that compare
  ``Path.home()`` with the bwrap command line behave as on a host.
- An operator-provided tiktoken cache directory (setting ``sandbox_tiktoken_cache``) is bound read-only and named by
  ``TIKTOKEN_CACHE_DIR``; a cache inside the coordinator's home is refused; with no setting nothing is bound.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import sandbox, settings
from bundlebees_plugin.cli import _sandbox_options


def _bwrap() -> None:
    if sandbox.detect_backend() != "bwrap":
        pytest.skip("bubblewrap is not usable on this host")


def _run(tmp_path: Path, script: str, **kw) -> dict:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    result = sandbox.run_in_sandbox(work, [sys.executable, "-c", script], backend="bwrap", timeout=60, **kw)
    assert result["exit_code"] == 0, result
    line = [ln for ln in result["output_tail"].splitlines() if ln.startswith("{")][-1]
    return json.loads(line)


def test_localhost_resolves_through_a_synthetic_hosts_file(tmp_path: Path):
    _bwrap()
    out = _run(tmp_path, (
        "import json, socket\n"
        "addrs = sorted({a[4][0] for a in socket.getaddrinfo('localhost', 80)})\n"
        "hosts = open('/etc/hosts').read()\n"
        "print(json.dumps({'addrs': addrs, 'hosts': hosts}))\n"
    ))
    assert out["addrs"] and set(out["addrs"]) <= {"127.0.0.1", "::1"}
    names = set()
    for line in out["hosts"].splitlines():
        line = line.split("#", 1)[0].split()
        if line:
            assert line[0] in {"127.0.0.1", "::1"}, line
            names.update(line[1:])
    assert names and names <= {"localhost", "ip6-localhost", "ip6-loopback"}, names


def test_network_stays_off_even_with_a_hosts_file(tmp_path: Path):
    _bwrap()
    out = _run(tmp_path, (
        "import json, socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3); r = 'connected'\n"
        "except OSError as e:\n"
        "    r = 'refused'\n"
        "print(json.dumps({'net': r}))\n"
    ))
    assert out["net"] == "refused"


def test_sandbox_home_is_private_writable_and_not_a_bind_target(tmp_path: Path):
    _bwrap()
    out = _run(tmp_path, (
        "import json, os\n"
        "from pathlib import Path\n"
        "h = Path.home(); p = h / 'probe'; p.write_text('x')\n"
        "print(json.dumps({'home': str(h), 'env': os.environ['HOME'], 'entries': sorted(os.listdir(h))}))\n"
    ))
    assert out["home"] == out["env"]
    # Not compared with the runner's Path.home() string: when the suite itself runs inside the verifier sandbox
    # (self-integration) both are /home/sandbox/home yet distinct private directories. Emptiness proves isolation.
    assert out["home"] != "/home/sandbox"
    assert out["home"] not in sandbox.build_command("bwrap", tmp_path / "c", tmp_path / "h", ["true"])
    assert out["entries"] == ["probe"]


def test_a_configured_tiktoken_cache_is_bound_read_only(tmp_path: Path):
    _bwrap()
    cache = tmp_path / "tiktoken-cache"
    cache.mkdir()
    (cache / "fd9f0f3d3f6f4fe9").write_text("cached-encoding")
    out = _run(tmp_path, (
        "import json, os\n"
        "d = os.environ.get('TIKTOKEN_CACHE_DIR', '')\n"
        "files = sorted(os.listdir(d)) if d else []\n"
        "try:\n"
        "    open(os.path.join(d, 'new'), 'w').write('x'); w = 'writable'\n"
        "except OSError:\n"
        "    w = 'read-only'\n"
        "print(json.dumps({'dir': d, 'files': files, 'write': w}))\n"
    ), tiktoken_cache=cache)
    assert out["dir"] and out["files"] == ["fd9f0f3d3f6f4fe9"] and out["write"] == "read-only"
    assert str(cache) != out["dir"], "the host path is not shown inside the sandbox"
    assert not (cache / "new").exists()


def test_no_tiktoken_cache_by_default(tmp_path: Path):
    _bwrap()
    out = _run(tmp_path, "import json, os\nprint(json.dumps({'dir': os.environ.get('TIKTOKEN_CACHE_DIR')}))\n")
    assert out["dir"] is None


def test_a_tiktoken_cache_inside_the_coordinator_home_is_refused(tmp_path: Path):
    inside = Path.home() / ".cache" / "tiktoken"
    with pytest.raises(sandbox.SandboxUnavailable) as exc:
        sandbox.run_in_sandbox(tmp_path, ["true"], backend="bwrap", tiktoken_cache=inside)
    assert exc.value.code == "unsafe_cache_dir"


def test_the_cache_setting_reaches_the_sandbox_options(tmp_path: Path):
    assert settings.DEFAULTS.get("sandbox_tiktoken_cache") == ""
    assert "tiktoken_cache" not in _sandbox_options({}) or _sandbox_options({})["tiktoken_cache"] is None
    opts = _sandbox_options({"sandbox_tiktoken_cache": str(tmp_path)})
    assert Path(opts["tiktoken_cache"]) == tmp_path
    text = Path(__file__).resolve().parents[1].joinpath("plugin.yaml").read_text()
    assert "sandbox_tiktoken_cache:" in text
