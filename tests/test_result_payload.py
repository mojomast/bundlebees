"""Bounded response budget for attempt result payloads.

The launcher's ``GET /v1/attempts/<id>/result`` has no cap of its own, so a
verbose result can easily pass the global 1 MiB response cap. These tests pin
the larger, bounded attempt-result budget and prove that ordinary endpoints
keep the global cap and that the patch channel stays byte-exact.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import bundlebees_plugin.client as client_module
from bundlebees_plugin import cli
from bundlebees_plugin.client import ClientError, LauncherClient

AUTH_VALUE = "test-token-value"
ATTEMPT_ID = "att-large"
RESULT_PATH = f"/v1/attempts/{ATTEMPT_ID}/result"
DETAIL_PATH = f"/v1/attempts/{ATTEMPT_ID}"
PATCH = (
    "--- a/fixture.py\n"
    "+++ b/fixture.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)
# Larger than the global 1 MiB cap, built at runtime: no added source line is
# itself a long literal.
VERBOSE_SIZE = 1_200_000


def _verbose_result(size: int = VERBOSE_SIZE) -> dict:
    return {
        "attempt_id": ATTEMPT_ID,
        "state": "succeeded",
        "observed": {"changed_files": ["fixture.py"]},
        "output": "x" * size,
        "patch": {"diff": PATCH, "files": [{"path": "fixture.py"}]},
    }


class FakeHandler(BaseHTTPRequestHandler):
    server_version = "FakeLauncher/large-result"

    def log_message(self, *_args):  # keep test output quiet
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {AUTH_VALUE}":
            return self._send(401, b'{"error": "unauthorized"}')
        path = self.path.split("?", 1)[0]
        status, body = self.server.payloads.get(path, (404, b'{"error": "not found"}'))
        self._send(status, body)


def _serve(httpd, path: str, payload, *, status: int = 200) -> None:
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload).encode("utf-8")
    httpd.payloads[path] = (status, body)


@pytest.fixture()
def httpd():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    server.payloads = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _launcher(httpd, **kwargs) -> LauncherClient:
    base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    return LauncherClient(base_url, token_provider=lambda: AUTH_VALUE, **kwargs)


def test_result_budget_is_bounded_and_documented():
    budget = getattr(client_module, "MAX_RESULT_BYTES", None)
    assert isinstance(budget, int)
    assert client_module.MAX_RESPONSE_BYTES == 1_048_576
    assert budget > client_module.MAX_RESPONSE_BYTES
    assert budget == 24 * 1_048_576
    assert budget == client_module.MAX_ARTIFACT_BYTES


def test_verbose_result_over_one_mib_is_read(httpd):
    _serve(httpd, RESULT_PATH, _verbose_result())
    client = _launcher(httpd)

    envelope = client.result_envelope(ATTEMPT_ID)

    assert envelope["attempt_id"] == ATTEMPT_ID
    assert len(envelope["output"]) == VERBOSE_SIZE
    # The result envelope is the apply channel: its patch stays byte-exact.
    assert envelope["patch"]["diff"] == PATCH
    assert envelope["patch"]["files"] == [{"path": "fixture.py"}]
    assert client.result(ATTEMPT_ID)["attempt_id"] == ATTEMPT_ID


def test_verbose_attempt_detail_over_one_mib_is_read(httpd):
    _serve(httpd, DETAIL_PATH, _verbose_result())
    client = _launcher(httpd)

    raw = client.get_raw(ATTEMPT_ID)
    assert len(raw["output"]) == VERBOSE_SIZE
    assert raw["patch"]["diff"] == PATCH
    assert client.get(ATTEMPT_ID)["attempt_id"] == ATTEMPT_ID


def test_verify_result_path_keeps_the_patch_exact(httpd):
    _serve(httpd, RESULT_PATH, _verbose_result())

    result = cli._fetch_result(_launcher(httpd), ATTEMPT_ID)

    assert result["patch"]["diff"] == PATCH


def test_ordinary_endpoint_keeps_the_one_mib_cap(httpd):
    _serve(httpd, "/v1/attempts", {"attempts": [], "output": "x" * VERBOSE_SIZE})
    _serve(httpd, RESULT_PATH, _verbose_result())
    client = _launcher(httpd)

    with pytest.raises(ClientError) as excinfo:
        client.attempts_raw()
    assert excinfo.value.code == "response_too_large"

    # Reading the larger result must not widen the cap afterwards.
    assert client.result_envelope(ATTEMPT_ID)["patch"]["diff"] == PATCH
    with pytest.raises(ClientError) as excinfo:
        client.attempts_raw()
    assert excinfo.value.code == "response_too_large"


def test_oversized_result_still_fails_closed(httpd, monkeypatch):
    monkeypatch.setattr(client_module, "MAX_RESULT_BYTES", VERBOSE_SIZE - 1)
    _serve(httpd, RESULT_PATH, _verbose_result())

    with pytest.raises(ClientError) as excinfo:
        _launcher(httpd).result_envelope(ATTEMPT_ID)
    assert excinfo.value.code == "response_too_large"


def test_malformed_verbose_result_is_a_typed_error(httpd):
    body = b'{"attempt_id": "' + ATTEMPT_ID.encode() + b'", "output": "' + b"x" * 1_200_000
    _serve(httpd, RESULT_PATH, body)

    with pytest.raises(ClientError) as excinfo:
        _launcher(httpd).result_envelope(ATTEMPT_ID)
    assert excinfo.value.code == "launcher_error"
    assert excinfo.value.error == "launcher returned an invalid JSON response"
