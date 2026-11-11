"""Offline HTTP-client tests against a local fake launcher/Orbit endpoint."""

from __future__ import annotations

import json
import threading
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from bundlebees_plugin.client import (
    ClientError,
    LauncherClient,
    OrbitAdapterClient,
    public_attempt,
    public_container,
    public_health,
    public_project,
    public_result,
)

TOKEN = "test-token-value"
REQUESTS: list[tuple[str, str, dict]] = []


class FakeHandler(BaseHTTPRequestHandler):
    server_version = "FakeLauncher/0.1.9"

    def log_message(self, *_args):  # keep test output quiet
        return

    def _json(self, status: int, payload=None):
        body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _record(self, body=None):
        REQUESTS.append((self.command, self.path, body or {}))

    def do_GET(self):  # noqa: N802
        self._record()
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/v1/health":
            return self._json(
                200,
                {
                    "ok": True,
                    "version": "0.1.9",
                    "max_concurrent": 2,
                    "active": 1,
                    "queued": 0,
                    "model": {"id": "deepseek-v4.1-flash", "base_url": "http://10.0.0.9"},
                    "bind": "0.0.0.0:9999",
                    "token_file": "/home/worker/.orbit/token",
                },
            )
        if path == "/v1/attempts":
            return self._json(200, {"attempts": [self._attempt("a1")]})
        if path == "/v1/attempts/a1":
            return self._json(200, self._attempt("a1"))
        if path == "/v1/attempts/a2":
            return self._json(200, self._attempt("a2"))
        if path == "/v1/attempts/a1/events":
            cursor = parse_qs(parsed.query).get("cursor", [None])[0]
            return self._json(
                200,
                {
                    "cursor": 3,
                    "events": [
                        {
                            "seq": 1,
                            "text": "token=leakedvalue connected to http://10.1.2.3/x",
                            "host": "worker.internal",
                        }
                    ],
                    "requested_cursor": cursor,
                },
            )
        if path == "/v1/attempts/a1/result":
            return self._json(409, {"error": "no result yet"})
        if path == "/v1/attempts/a2/result":
            return self._json(
                200,
                {
                    "attempt_id": "a2",
                    "state": "succeeded",
                    "observed": {
                        "changed_files": ["src/a.py", "/etc/passwd", "../escape"],
                        "sha256": "a" * 64,
                        "usage": {"tokens": 12},
                    },
                    "worker_claims": "I also changed secret=/tmp/x",
                    "token": "result-token",
                },
            )
        if path == "/v1/projects":
            return self._json(
                200,
                {
                    "projects": [
                        {
                            "key": "proj",
                            "repo": "https://github.com/example/repo.git",
                            "sync_branch": "main",
                            "local_path": "/home/worker/repos/proj",
                        }
                    ]
                },
            )
        if path == "/v1/containers":
            return self._json(
                200,
                {
                    "containers": [
                        {
                            "image": "ghcr.io/example/worker@sha256:" + "b" * 64,
                            "digest": "sha256:" + "b" * 64,
                            "runtime": "docker",
                            "memory_mb": 4096,
                            "toolchain": ["python", "node"],
                            "bind": "0.0.0.0:9999",
                        }
                    ]
                },
            )
        if path == "/v1/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/v1/huge":
            return self._json(200, {"value": "x" * 4096})
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        self._record(body)
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        path = urlparse(self.path).path
        if path == "/v1/attempts":
            project = body.get("brief", {}).get("project") or body.get("project")
            if project == "forbidden":
                return self._json(403, {"error": "project not authorized"})
            return self._json(200, {"attempt_id": "a1", "task_id": "t1", "state": "queued"})
        if path == "/v1/attempts/a1/cancel":
            return self._json(200, {"attempt_id": "a1", "state": "cancelled"})
        if path == "/v1/attempts/a1/review":
            return self._json(
                200,
                {
                    "attempt_id": "a1",
                    "state": "reviewed",
                    "accepted": bool(body.get("accepted")),
                },
            )
        if path == "/api/local-workers":
            op = body.get("op")
            if op == "list":
                return self._json(
                    200,
                    {
                        "ok": True,
                        "version": "0.1.9",
                        "max_concurrent": 2,
                        "active": 1,
                        "queued": 0,
                        "model": "deepseek-v4.1-flash",
                        "attempts": [self._attempt("a1")],
                        "projects": [
                            {"key": "proj", "repo": "https://github.com/example/repo.git"}
                        ],
                        "containers": [{"digest": "sha256:" + "c" * 64, "runtime": "docker"}],
                        "bind": "127.0.0.1:4318",
                    },
                )
            if op == "create":
                brief = body.get("brief") or {}
                if brief.get("project") == "forbidden":
                    return self._json(403, {"error": "project not authorized"})
                return self._json(
                    200, {"attempt_id": "orbit-a1", "task_id": "orbit-t1", "state": "queued"}
                )
            if op == "get":
                attempt_id = body.get("attempt_id")
                if attempt_id == "a2":
                    return self._json(200, {"attempt_id": "a2", "state": "succeeded"})
                return self._json(
                    200,
                    {
                        "attempt": self._attempt(attempt_id or "a1"),
                        "events": [{"seq": 1, "text": "token=leakedvalue http://10.1.2.3/"}],
                    },
                )
            if op == "cancel":
                return self._json(200, {"attempt_id": body.get("attempt_id"), "state": "cancelled"})
            if op == "review":
                return self._json(
                    200,
                    {
                        "attempt_id": body.get("attempt_id"),
                        "state": "reviewed",
                        "accepted": bool(body.get("accepted")),
                    },
                )
            return self._json(400, {"error": "unknown op"})
        return self._json(404, {"error": "not found"})

    @staticmethod
    def _attempt(attempt_id: str) -> dict:
        return {
            "attempt_id": attempt_id,
            "task_id": "t1",
            "project": "proj",
            "branch": "feat/x",
            "state": "succeeded" if attempt_id == "a2" else "running",
            "assigned_files": ["src/a.py"],
            "created_at": "2026-11-11T00:00:00Z",
            "updated_at": "2026-11-11T00:01:00Z",
            "event_cursor": 3,
            "detail": "token=leakedvalue http://10.1.2.3/",
            "host": "worker.internal",
            "local_path": "/home/worker/repo",
            "token_file": "/home/worker/.orbit/token",
        }


@pytest.fixture(autouse=True)
def _clear_requests():
    REQUESTS.clear()
    yield
    REQUESTS.clear()


@pytest.fixture()
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _launcher(base_url: str, **kwargs) -> LauncherClient:
    return LauncherClient(base_url, token_provider=lambda: TOKEN, **kwargs)


def test_health_is_sanitized(base_url: str):
    health = _launcher(base_url).health()
    text = json.dumps(health)
    assert health["model"] == "deepseek-v4.1-flash"
    assert health["version"] == "0.1.9"
    for leaked in ("10.0.0.9", "0.0.0.0", "token_file", "bind", "/home/worker"):
        assert leaked not in text


def test_missing_token_fails_closed_before_request(base_url: str):
    client = LauncherClient(base_url, token_provider=lambda: None)
    with pytest.raises(ClientError) as excinfo:
        client.health()
    assert excinfo.value.code == "not_configured"
    assert REQUESTS == []


def test_bad_token_maps_to_unauthorized(base_url: str):
    client = LauncherClient(base_url, token_provider=lambda: "wrong")
    with pytest.raises(ClientError) as excinfo:
        client.health()
    assert excinfo.value.code == "unauthorized"


def test_create_success(base_url: str):
    client = _launcher(base_url)
    attempt = client.create(
        {"objective": "do work", "project": "proj", "assigned_files": ["src/a.py"]},
        model="deepseek-v4.1-flash",
    )
    assert attempt["attempt_id"] == "a1"
    method, path, body = REQUESTS[-1]
    assert (method, path) == ("POST", "/v1/attempts")
    assert body["model"] == "deepseek-v4.1-flash"


def test_create_403_maps_to_project_unauthorized(base_url: str):
    client = _launcher(base_url)
    with pytest.raises(ClientError) as excinfo:
        client.create({"objective": "do work", "project": "forbidden"})
    assert excinfo.value.code == "project_unauthorized"


def test_get_is_sanitized(base_url: str):
    attempt = _launcher(base_url).get("a1")
    text = json.dumps(attempt)
    assert attempt["attempt_id"] == "a1"
    for leaked in ("worker.internal", "/home/worker", "token_file", "leakedvalue", "10.1.2.3"):
        assert leaked not in text


def test_attempts_are_sanitized(base_url: str):
    attempts = _launcher(base_url).attempts()
    text = json.dumps(attempts)
    assert attempts[0]["attempt_id"] == "a1"
    assert "worker.internal" not in text
    assert "/home/worker" not in text


def test_events_are_scrubbed(base_url: str):
    events = _launcher(base_url).events("a1", cursor=0)
    text = json.dumps(events)
    assert events[0]["seq"] == 1
    for leaked in ("leakedvalue", "10.1.2.3", "worker.internal"):
        assert leaked not in text


def test_result_409_maps_to_no_result_yet(base_url: str):
    with pytest.raises(ClientError) as excinfo:
        _launcher(base_url).result("a1")
    assert excinfo.value.code == "no_result_yet"


def test_result_is_sanitized_and_paths_are_relative(base_url: str):
    result = _launcher(base_url).result("a2")
    text = json.dumps(result)
    assert result["attempt_id"] == "a2"
    assert result["observed"]["changed_files"] == ["src/a.py"]
    assert "/etc/passwd" not in text
    assert "../escape" not in text
    assert "result-token" not in text
    assert "worker_claims" not in result


def test_cancel_and_review(base_url: str):
    client = _launcher(base_url)
    assert client.cancel("a1")["state"] == "cancelled"
    review = client.review("a1", accepted=True, note="looks good")
    assert review["accepted"] is True


def test_projects_and_containers_are_sanitized(base_url: str):
    client = _launcher(base_url)
    projects = client.projects()
    containers = client.containers()
    ptext = json.dumps(projects)
    ctext = json.dumps(containers)
    assert projects[0]["key"] == "proj"
    assert projects[0]["repo"] == "https://github.com/example/repo.git"
    assert "/home/worker" not in ptext
    assert containers[0]["runtime"] == "docker"
    assert "0.0.0.0" not in ctext
    assert "bind" not in ctext


def test_redirect_is_revalidated_without_contacting_metadata(base_url: str):
    client = _launcher(base_url)
    with pytest.raises(ClientError) as excinfo:
        client.request_json("GET", "/v1/redirect")
    assert excinfo.value.code == "unsafe_url"


def test_response_size_limit(base_url: str):
    client = LauncherClient(base_url, token_provider=lambda: TOKEN, max_bytes=32)
    with pytest.raises(ClientError) as excinfo:
        client.health()
    assert excinfo.value.code == "response_too_large"


def test_unsafe_base_url_is_rejected():
    with pytest.raises(ClientError) as excinfo:
        LauncherClient("http://169.254.169.254", token_provider=lambda: TOKEN)
    assert excinfo.value.code == "unsafe_url"


def test_orbit_adapter_ops(base_url: str):
    client = OrbitAdapterClient(base_url, token_provider=lambda: TOKEN)
    assert client.health()["model"] == "deepseek-v4.1-flash"
    created = client.create({"objective": "do work", "project": "proj"})
    assert created["attempt_id"] == "orbit-a1"
    assert client.get("a1")["attempt_id"] == "a1"
    assert client.events("a1")[0]["seq"] == 1
    assert client.cancel("a1")["state"] == "cancelled"
    assert client.review("a1", accepted=True)["accepted"] is True
    with pytest.raises(ClientError) as excinfo:
        client.result("a1")
    assert excinfo.value.code == "no_result_yet"


def test_orbit_create_403_maps_to_project_unauthorized(base_url: str):
    client = OrbitAdapterClient(base_url, token_provider=lambda: TOKEN)
    with pytest.raises(ClientError) as excinfo:
        client.create({"objective": "do work", "project": "forbidden"})
    assert excinfo.value.code == "project_unauthorized"


def test_public_mapping_cannot_leak_bind_or_token():
    assert "bind" not in public_health({"ok": True, "bind": "0.0.0.0:1", "token": "x"})
    assert "token" not in public_attempt({"attempt_id": "a", "token": "x"})
    assert "/home/x" not in json.dumps(public_project({"key": "p", "local_path": "/home/x"}))
    assert "bind" not in public_container({"digest": "sha256:" + "d" * 64, "bind": "0.0.0.0:1"})
    assert public_result({"observed": {"changed_files": ["/etc/passwd"]}})["observed"]["changed_files"] == []



def test_launcher_pins_resolved_ip_and_sends_hostname_host_header(monkeypatch):
    recorded: dict[str, str] = {}

    class HostHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            recorded["host"] = self.headers.get("Host") or ""
            recorded["path"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true, "status": "ok"}')

        def log_message(self, format, *args):  # noqa: A003
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), HostHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        from bundlebees_plugin import security as security_mod

        real_gai = security_mod.socket.getaddrinfo

        def fake_gai(host, svc, *args, **kwargs):
            if host == "launcher.example.test":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return real_gai(host, svc, *args, **kwargs)

        monkeypatch.setattr(security_mod.socket, "getaddrinfo", fake_gai)
        client = LauncherClient(
            f"http://launcher.example.test:{port}",
            token_provider=lambda: TOKEN,
            allow_loopback=True,
        )
        client.health()
        assert recorded["host"] == f"launcher.example.test:{port}"
        assert "127.0.0.1" not in recorded["host"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_client_from_agent_requires_allow_private_for_lan_launcher():
    from bundlebees_plugin.client import client_from_agent, ClientError

    class Agent:
        secret_scope = type("S", (), {"get_secret": staticmethod(lambda name: TOKEN)})()

    with pytest.raises(ClientError) as excinfo:
        client_from_agent(
            Agent(),
            {"adapter": "launcher", "launcher_url": "http://10.0.0.5:8080", "allow_private": "false"},
        )
    assert excinfo.value.code == "unsafe_url"

    client = client_from_agent(
        Agent(),
        {"adapter": "launcher", "launcher_url": "http://10.0.0.5:8080", "allow_private": True},
    )
    assert client._allow_private is True
