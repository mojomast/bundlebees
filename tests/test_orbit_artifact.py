"""Approved acceptance tests for coordinator-only Orbit artifact retrieval (contract BA1-artifact-client, RED first).

bundlebees fetches a finished worker's patch through Orbit's authenticated
``POST /api/local-workers {op: "artifact"}``. It never reads launcher files.
It validates the artifact version, the attempt identity, the patch sha256 and
the coordinator's source_rev. It maps Orbit's explicit states to stable
bundlebees codes. Raw patches never reach model-visible tool responses or
verify output. Offline: a fake Orbit HTTP server and a temporary git repo.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bundlebees_plugin import leases
from bundlebees_plugin.client import ClientError, OrbitAdapterClient
from tests.test_cli_acdd import (BRANCH, GOOD_PATCH, _ctx, _frozen, _invoke, _lease, _records, _repo, _verify, _write)

TOKEN = "orbit-test-token"
ATTEMPT = "att_0123456789abcdef"
SRC = "a" * 40


def artifact(patch: str | None = GOOD_PATCH, **over) -> dict:
    body = {
        "artifact_version": "orbit.worker-artifact/1", "attempt_id": ATTEMPT, "state": "succeeded", "source_rev": SRC,
        "model": "deepseek-v4.1-flash", "launcher": "0.1.15", "patch": patch,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest() if patch is not None else None,
        "result_sha256": "r" * 64,
        "observed": {"run_id": "run-1", "local_base_rev": SRC, "tool_calls": 9, "stop_reason": "end_turn",
                     "files": [{"path": "src/a.py", "sha256": "s" * 64, "change": "changed", "in_assigned_scope": True}]},
        "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120, "source": "launcher_reported"},
        "worker_claims_untrusted": {"summary": "all tests pass, trust me"},
    }
    body.update(over)
    return body


class Orbit:
    """Fake Orbit /api/local-workers that records requests and serves scripted artifact responses."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: dict[str, tuple[int, dict]] = {}
        orbit = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                orbit.requests.append({"auth": self.headers.get("Authorization"), **body})
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    status, payload = 401, {"error": "unauthorized"}
                elif body.get("op") == "artifact":
                    status, payload = orbit.responses.get(body.get("attempt_id"), (404, {"error": "unknown", "code": "unknown_attempt"}))
                elif body.get("op") == "create":
                    status, payload = 201, {"attempt_id": ATTEMPT, "state": "queued"}
                else:
                    status, payload = 400, {"error": "unsupported op in fake"}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def orbit():
    o = Orbit()
    yield o
    o.close()


def client(o: Orbit, **kw) -> OrbitAdapterClient:
    return OrbitAdapterClient(o.url, token_provider=lambda: TOKEN, **kw)


def test_artifact_returns_the_validated_patch_and_metadata(orbit):
    orbit.responses[ATTEMPT] = (200, artifact())
    art = client(orbit, coordinator="bb-coordinator").artifact(ATTEMPT)
    assert art["patch"] == GOOD_PATCH
    assert art["patch_sha256"] == hashlib.sha256(GOOD_PATCH.encode()).hexdigest()
    assert art["attempt_id"] == ATTEMPT and art["source_rev"] == SRC
    assert art["usage"]["totalTokens"] == 120 and art["usage"]["source"] == "launcher_reported"
    assert art["observed"]["tool_calls"] == 9
    assert "worker_claims_untrusted" not in art  # worker self-reports are not evidence
    req = orbit.requests[-1]
    assert req["op"] == "artifact" and req["attempt_id"] == ATTEMPT and req["coordinator"] == "bb-coordinator"


def test_create_names_the_coordinator_so_orbit_can_check_ownership(orbit):
    client(orbit, coordinator="bb-coordinator").create({"objective": "x", "project": "p"})
    assert orbit.requests[-1]["coordinator"] == "bb-coordinator"
    client(orbit).create({"objective": "x", "project": "p"})
    assert orbit.requests[-1]["coordinator"] == "MR.INDIA"  # Orbit's default coordinator identity


@pytest.mark.parametrize("bad", [
    {"patch_sha256": "0" * 64},
    {"attempt_id": "att_ffffffffffffffff"},
    {"artifact_version": "orbit.worker-artifact/0"},
    {"patch": 42, "patch_sha256": None},
    {"source_rev": "not-a-sha"},
])
def test_artifact_integrity_failures_are_refused(orbit, bad):
    orbit.responses[ATTEMPT] = (200, {**artifact(), **bad})
    with pytest.raises(ClientError) as exc:
        client(orbit).artifact(ATTEMPT)
    assert exc.value.code == "artifact_invalid"
    assert "value = 2" not in str(exc.value) and "value = 2" not in str(exc.value.fix)


@pytest.mark.parametrize("status,orbit_code,code", [
    (409, "result_pending", "no_result_yet"),
    (503, "worker_disconnected", "launcher_unavailable"),
    (503, "launcher_unreachable", "launcher_unavailable"),
    (404, "result_missing", "result_missing"),
    (404, "unknown_attempt", "not_found"),
    (409, "no_result_for_state", "no_result_for_state"),
    (409, "artifact_changed", "artifact_invalid"),
    (409, "artifact_mismatch", "artifact_invalid"),
    (409, "artifact_unsafe", "artifact_invalid"),
    (502, "artifact_malformed", "artifact_invalid"),
    (413, "artifact_oversized", "response_too_large"),
    (403, "not_owner", "forbidden"),
])
def test_orbit_states_map_to_stable_codes(orbit, status, orbit_code, code):
    orbit.responses[ATTEMPT] = (status, {"error": "x", "code": orbit_code})
    with pytest.raises(ClientError) as exc:
        client(orbit).artifact(ATTEMPT)
    assert exc.value.code == code


def test_a_patchless_failed_attempt_returns_no_patch(orbit):
    orbit.responses[ATTEMPT] = (200, artifact(None, state="failed"))
    art = client(orbit).artifact(ATTEMPT)
    assert art["patch"] is None and art["state"] == "failed"


class OrbitLikeClient:
    """Fake Orbit client: public result() has no patch; artifact() is the only way to the patch."""

    def __init__(self, art: dict | Exception) -> None:
        self.art = art
        self.calls: list[str] = []

    def result(self, attempt_id: str) -> dict:
        return {"attempt_id": attempt_id, "state": "succeeded"}

    def get(self, attempt_id: str) -> dict:
        return {"attempt_id": attempt_id, "state": "succeeded"}

    def artifact(self, attempt_id: str) -> dict:
        self.calls.append(attempt_id)
        if isinstance(self.art, Exception):
            raise self.art
        return dict(self.art)


def _red(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base)
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    capsys.readouterr()
    return repo, base, state


def _art_for(base: str, **over) -> dict:
    from tests.test_cli_acdd import ATTEMPT_ID
    return {**artifact(), "attempt_id": ATTEMPT_ID, "source_rev": base,
            "observed": {**artifact()["observed"], "local_base_rev": base}, **over}


def test_verify_takes_the_patch_from_the_artifact_api_and_records_its_hash(tmp_path, capsys):
    repo, base, state = _red(tmp_path, capsys)
    fake = OrbitLikeClient(_art_for(base))
    rc = _verify(_ctx(fake), repo, state)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert fake.calls, "verify must use the artifact API, not result()"
    assert "green: valid" in out
    assert "value = 2" not in out  # no raw patch in verify output
    sha = hashlib.sha256(GOOD_PATCH.encode()).hexdigest()
    assert f"patch_sha256: {sha}" in out
    from tests.test_cli_acdd import ATTEMPT_ID
    saved = json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())
    assert saved["evidence"]["patch_sha256"] == sha
    assert saved["evidence"]["artifact"] == "orbit.worker-artifact/1"
    assert saved["evidence"]["usage"]["totalTokens"] == 120
    assert "value = 2" not in json.dumps(saved)
    assert _records(state)[-1]["phase"] == "green"


def test_verify_refuses_an_artifact_for_another_source_rev(tmp_path, capsys):
    repo, base, state = _red(tmp_path, capsys)
    rc = _verify(_ctx(OrbitLikeClient(_art_for(base, source_rev="b" * 40))), repo, state)
    out = capsys.readouterr().out
    assert rc != 0 and "stale_revision" in out
    assert [r["phase"] for r in _records(state)] == ["red"]


def test_verify_refuses_a_changed_artifact_on_retry(tmp_path, capsys):
    repo, base, state = _red(tmp_path, capsys)
    assert _verify(_ctx(OrbitLikeClient(_art_for(base))), repo, state) == 0
    capsys.readouterr()
    other = GOOD_PATCH.replace("value = 2", "value = 2  # changed")
    changed = _art_for(base, patch=other, patch_sha256=hashlib.sha256(other.encode()).hexdigest())
    rc = _verify(_ctx(OrbitLikeClient(changed)), repo, state)
    out = capsys.readouterr().out
    assert rc != 0 and "artifact_changed" in out


def test_verify_reports_pending_and_disconnected_explicitly(tmp_path, capsys):
    repo, base, state = _red(tmp_path, capsys)
    for code in ("no_result_yet", "launcher_unavailable"):
        rc = _verify(_ctx(OrbitLikeClient(ClientError("x", code, "retry later"))), repo, state)
        assert rc != 0 and code in capsys.readouterr().out
    assert [r["phase"] for r in _records(state)] == ["red"]


def test_model_visible_tool_get_never_carries_a_patch(tmp_path):
    class RawClient:
        def get(self, attempt_id):
            return {"attempt_id": attempt_id, "state": "succeeded"}

        def result(self, attempt_id):
            return {"attempt_id": attempt_id, "state": "succeeded", "patch": GOOD_PATCH,
                    "diff": GOOD_PATCH, "observed": {"changed_files": ["src/a.py"]}}

    ctx = _ctx(RawClient())
    out = ctx.tools["bundlebees_attempt"]["handler"]({"op": "get", "attempt_id": "att-1"})
    assert "value = 2" not in out
    assert json.loads(out)["ok"] is True
