"""Approved round-trip test: real Orbit API -> bundlebees verifier (contract BA1-artifact-client, RED first).

Runs the real Orbit ``/api/local-workers`` route from an orbitdesktop checkout (``ORBIT_SRC``) under node,
in front of a stand-in launcher that finishes each attempt with a scripted patch. bundlebees dispatches a
frozen contract through ``bundlebees_dispatch`` (signed coordinator lease), Orbit dispatches it, and
``hermes bundlebees verify`` fetches the patch through ``op: artifact`` and records a signed GREEN. The
test then repeats verify after an Orbit restart and checks the patch hash is unchanged. It is skipped,
and reported as not run, when ``ORBIT_SRC`` or node 22+ is unavailable, unless ``BUNDLEBEES_REQUIRE_ROUNDTRIP=1``
(set in CI), which turns a skip into a failure.

Amended by BA2-artifact-capability: a valid shared key plus an impersonated coordinator name must fail, and an
oversized worker result is refused by Orbit while streaming and by bundlebees before anything is applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bundlebees_plugin.client import ClientError, OrbitAdapterClient
from tests.fakes import FakeCtx, load_plugin
from tests.test_cli_acdd import BRANCH, GOOD_PATCH, APPROVED_TEST, _ctx, _frozen, _invoke, _records, _repo, _write

ORBIT_SRC = os.environ.get("ORBIT_SRC", "")
NODE = shutil.which("node")
FIXTURE = Path(__file__).with_name("orbit_roundtrip_server.mjs")

_READY = bool(ORBIT_SRC and Path(ORBIT_SRC, "server", "local-workers-route.mjs").is_file() and NODE and shutil.which("bwrap"))
_REQUIRED = os.environ.get("BUNDLEBEES_REQUIRE_ROUNDTRIP") == "1"
pytestmark = pytest.mark.skipif(
    not _READY and not _REQUIRED,
    reason="ORBIT_SRC (an orbitdesktop checkout), node and bwrap are needed for the real Orbit round trip",
)
ARTIFACT_LIMIT = 65536  # Orbit's ORBIT_WORKER_ARTIFACT_MAX_BYTES in the fixture


def test_round_trip_prerequisites_are_present_when_required():
    assert _READY, "BUNDLEBEES_REQUIRE_ROUNDTRIP=1 but ORBIT_SRC, node 22+ or bwrap is missing"


def _start(runtime: Path, patch_file: Path, port: int = 0) -> tuple[subprocess.Popen, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(runtime), "ORBIT_SRC": ORBIT_SRC,
           "ROUNDTRIP_RUNTIME": str(runtime), "ROUNDTRIP_PATCH_FILE": str(patch_file), "ROUNDTRIP_ORBIT_PORT": str(port),
           "ROUNDTRIP_ARTIFACT_MAX_BYTES": str(ARTIFACT_LIMIT)}
    proc = subprocess.Popen([NODE, str(FIXTURE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    line = proc.stdout.readline()
    if not line:
        proc.kill()
        raise RuntimeError("orbit fixture did not start: " + (proc.stderr.read() or "")[-500:])
    return proc, json.loads(line)["url"]


def _key(runtime: Path) -> str:
    path = runtime / "collab-board.key"
    if not path.exists():
        path.write_text(hashlib.sha256(os.urandom(16)).hexdigest())
    return path.read_text().strip()


def _wait_state(url: str, key: str, attempt_id: str, want: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(url + "/api/local-workers", data=json.dumps({"op": "get", "attempt_id": attempt_id}).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        state = json.load(urllib.request.urlopen(req, timeout=10)).get("state")
        if state == want:
            return
        time.sleep(0.2)
    raise AssertionError(f"attempt never reached {want}")


def _clean(repo: Path) -> None:
    # verify --dry-run leaves the patch applied for inspection; reset before the next retry.
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard"], check=True)
    subprocess.run(["git", "-C", str(repo), "clean", "-qfd"], check=True)


def test_real_orbit_api_to_bundlebees_verifier_round_trip(tmp_path, capsys):
    runtime = tmp_path / "orbit-runtime"
    runtime.mkdir()
    key = _key(runtime)
    patch_file = tmp_path / "worker.patch"
    patch_file.write_text(GOOD_PATCH)
    proc, url = _start(runtime, patch_file)
    try:
        repo, base = _repo(tmp_path)
        state = tmp_path / "state"
        frozen = _frozen(base)
        path = _write(tmp_path / "frozen.json", frozen)
        assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0

        orbit = OrbitAdapterClient(url, token_provider=lambda: key, coordinator="bb-roundtrip",
                                   capability_dir=state / "artifact-caps")
        # Strict settings: worker code runs only in the real bubblewrap sandbox (fails closed otherwise).
        ctx = FakeCtx(settings={})
        ctx.bundlebees_client = orbit
        ctx.bundlebees_state_dir = state
        load_plugin().register(ctx)
        brief = {"objective": "make value two", "source_rev": base, "branch": BRANCH, "project": "demo",
                 "assigned_files": ["src/a.py"], "acceptance": {"criteria": ["AC-001"], "tests": [APPROVED_TEST]},
                 "tests": [APPROVED_TEST], "contract": frozen}
        reply = json.loads(ctx.tools["bundlebees_dispatch"]["handler"]({"brief": brief, "model": "deepseek-v4.1-flash"}))
        assert reply["ok"] and reply["lease_recorded"], reply
        attempt_id = reply["attempt_id"]
        assert attempt_id.startswith("att_")
        _wait_state(url, key, attempt_id, "succeeded")
        capsys.readouterr()

        argv = ["verify", attempt_id, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state), "--dry-run"]
        assert _invoke(ctx, argv) == 0
        out = capsys.readouterr().out
        sha = hashlib.sha256(GOOD_PATCH.encode()).hexdigest()
        assert "green: valid" in out and f"patch_sha256: {sha}" in out and "value = 2" not in out
        assert "sandbox: bwrap" in out, out
        assert [r["phase"] for r in _records(state)] == ["red", "green"]

        # The model-visible tool view of the same attempt carries no patch and no box paths.
        view = ctx.tools["bundlebees_attempt"]["handler"]({"op": "get", "attempt_id": attempt_id})
        assert "value = 2" not in view and str(runtime) not in view

        caps = state / "artifact-caps"
        assert (caps / f"{attempt_id}.cap").stat().st_mode & 0o777 == 0o600
        # A wrong key is refused by Orbit.
        with pytest.raises(ClientError) as exc:
            OrbitAdapterClient(url, token_provider=lambda: "0" * 64, capability_dir=caps).artifact(attempt_id)
        assert exc.value.code == "unauthorized"
        # Impersonation: the valid shared key and the SAME coordinator name, but a forged capability, fail at Orbit.
        forged = tmp_path / "forged-caps"
        forged.mkdir(mode=0o700)
        (forged / f"{attempt_id}.cap").write_text("x" * 43)
        with pytest.raises(ClientError) as exc:
            OrbitAdapterClient(url, token_provider=lambda: key, coordinator="bb-roundtrip", capability_dir=forged).artifact(attempt_id)
        assert exc.value.code == "forbidden"
        # And a raw request with the shared key and coordinator name but no capability is refused by Orbit itself.
        req = urllib.request.Request(url + "/api/local-workers",
                                     data=json.dumps({"op": "artifact", "attempt_id": attempt_id, "coordinator": "bb-roundtrip"}).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as http_exc:
            urllib.request.urlopen(req, timeout=10)
        assert http_exc.value.code == 403 and b"VALUE = 2" not in http_exc.value.read().upper()

        # Restart Orbit on the same runtime dir: the retry gets the same artifact and verify stays green.
        port = int(url.rsplit(":", 1)[1])
        proc.terminate(); proc.wait(timeout=10)
        proc, url = _start(runtime, patch_file, port)
        _clean(repo)
        assert _invoke(ctx, argv) == 0
        assert f"patch_sha256: {sha}" in capsys.readouterr().out

        # A substituted result on disk after the first read is refused end to end.
        result_file = runtime / "local-workers" / f"{attempt_id}.result.json"
        manifest = json.loads(result_file.read_text())
        manifest["patch"]["diff"] = GOOD_PATCH.replace("value = 2", "value = 3")
        result_file.write_text(json.dumps(manifest))
        _clean(repo)
        assert _invoke(ctx, argv) != 0
        assert "artifact_invalid" in capsys.readouterr().out

        # Oversized ingestion: a worker result over Orbit's limit is cut off while streaming, never persisted,
        # and verify refuses it with a stable code and nothing applied.
        big = GOOD_PATCH + "+" + "x" * (ARTIFACT_LIMIT * 4) + "\n"
        patch_file.write_text(big)
        reply = json.loads(ctx.tools["bundlebees_dispatch"]["handler"]({"brief": brief, "model": "deepseek-v4.1-flash"}))
        assert reply["ok"], reply
        big_id = reply["attempt_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                orbit.artifact(big_id)
            except ClientError as err:
                if err.code == "response_too_large":
                    break
            time.sleep(0.2)
        else:
            raise AssertionError("oversized result was not refused with response_too_large")
        assert not (runtime / "local-workers" / f"{big_id}.result.json").exists()
        _clean(repo)
        bad = ["verify", big_id, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state), "--dry-run"]
        assert _invoke(ctx, bad) != 0
        assert "response_too_large" in capsys.readouterr().out
        assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
    finally:
        proc.terminate()
        proc.wait(timeout=10)
