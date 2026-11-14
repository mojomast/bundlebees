"""Launcher adapter tests against a real HTTP server and recorded payloads."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bundlebees_plugin import leases
from bundlebees_plugin.client import LauncherClient
from bundlebees_plugin.launcher_adapter import LauncherAdapter, LeaseBinder
from bundlebees_plugin.reconcile import DispatchRejected, DispatchUncertain

SHA = "817eac479f441396562962e880c1af596638f399"
BRIEF = {
    "objective": "reject malformed source_rev",
    "source_rev": SHA,
    "assigned_files": ["tools/x.py"],
    "acceptance": {"criteria": ["it refuses"], "tests": ["tests/x.py"]},
    "job_id": "job_a",
    "task_id": "T1",
}


class Handler(BaseHTTPRequestHandler):
    """A launcher shaped exactly like the recorded 0.1.16 responses."""

    attempts: list[dict] = []
    post_status = 200
    post_payload: dict | None = None

    def log_message(self, *args):  # quiet
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/attempts":
            return self._send(200, {"attempts": Handler.attempts})
        if self.path.startswith("/v1/attempts/"):
            attempt_id = self.path.rsplit("/", 1)[-1]
            for entry in Handler.attempts:
                if entry.get("attempt_id") == attempt_id:
                    return self._send(200, entry)
            return self._send(404, {"error": "unknown_attempt"})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if Handler.post_status != 200:
            return self._send(Handler.post_status, Handler.post_payload or {"error": "refused"})
        return self._send(200, Handler.post_payload or {"attempt_id": "att_new"})


@pytest.fixture()
def server():
    Handler.attempts = []
    Handler.post_status = 200
    Handler.post_payload = None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def client(base: str) -> LauncherClient:
    return LauncherClient(base, token="test-token")


class Mapping:
    __test__ = True

    def test_attempts_map_through_with_their_brief(self, server):
        Handler.attempts = [
            {
                "attempt_id": "att_1",
                "state": "running",
                "created_at": "2026-11-13T05:00:00Z",
                "brief": BRIEF,
            }
        ]
        adapter = LauncherAdapter(client(server))

        entries = adapter.list_attempts()

        assert entries == [
            {
                "attempt_id": "att_1",
                "state": "running",
                "created_at": "2026-11-13T05:00:00Z",
                "brief": BRIEF,
            }
        ]

    def test_an_attempt_without_an_id_is_dropped_not_guessed(self, server):
        Handler.attempts = [{"state": "running"}, {"attempt_id": "att_2", "state": "running"}]
        adapter = LauncherAdapter(client(server))

        assert [entry["attempt_id"] for entry in adapter.list_attempts()] == ["att_2"]

    def test_a_missing_brief_stays_missing(self, server):
        Handler.attempts = [{"attempt_id": "att_3", "state": "running"}]
        entry = LauncherAdapter(client(server)).list_attempts()[0]
        assert "brief" not in entry

    def test_get_attempt_returns_none_for_unknown(self, server):
        assert LauncherAdapter(client(server)).get_attempt("att_missing") is None

    def test_idempotent_is_false_until_the_server_proves_otherwise(self, server):
        assert LauncherAdapter(client(server)).idempotent is False


class Dispatch:
    __test__ = True

    def test_a_started_attempt_returns_its_id(self, server):
        Handler.post_payload = {"attempt_id": "att_new", "state": "queued"}
        assert LauncherAdapter(client(server)).post_attempt(BRIEF) == {"attempt_id": "att_new"}

    def test_a_validation_refusal_is_rejected_not_uncertain(self, server):
        Handler.post_status = 400
        Handler.post_payload = {"error": "brief.objective is required"}
        with pytest.raises(DispatchRejected):
            LauncherAdapter(client(server)).post_attempt(BRIEF)

    def test_a_server_error_is_uncertain(self, server):
        Handler.post_status = 500
        with pytest.raises(DispatchUncertain):
            LauncherAdapter(client(server)).post_attempt(BRIEF)

    def test_an_unreachable_launcher_is_uncertain(self, server):
        Handler.post_status = 200
        adapter = LauncherAdapter(client(server))
        # Kill the server, then post: the request may or may not have been sent.
        # (A closed port is the same class of ambiguity as a lost response.)
        import socket

        host, port = server.replace("http://", "").split(":")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        dead = LauncherClient(f"http://127.0.0.1:{closed_port}", token="t", timeout=2)
        with pytest.raises(DispatchUncertain):
            LauncherAdapter(dead).post_attempt(BRIEF)


class Binding:
    __test__ = True

    def _lease(self, state: Path) -> dict:
        return leases.record_lease(state, "att_1", dict(BRIEF))

    def test_binding_writes_the_lease_from_the_authorised_brief(self, tmp_path):
        binder = LeaseBinder(tmp_path)
        binder("att_1", dict(BRIEF))

        record = leases.load_lease(tmp_path, "att_1")
        assert record["brief"]["source_rev"] == SHA
        assert leases.lease_files(record) == ["tools/x.py"]

    def test_binding_twice_is_idempotent(self, tmp_path):
        binder = LeaseBinder(tmp_path)
        binder("att_1", dict(BRIEF))
        binder("att_1", dict(BRIEF))

        assert leases.lease_files(leases.load_lease(tmp_path, "att_1")) == ["tools/x.py"]

    def test_binding_refuses_a_lease_that_disagrees(self, tmp_path):
        self._lease(tmp_path)
        other = dict(BRIEF, assigned_files=["tools/other.py"])

        with pytest.raises(Exception) as exc:
            LeaseBinder(tmp_path)("att_1", other)
        assert getattr(exc.value, "code", "") == "lease_mismatch"
