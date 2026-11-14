"""Offline tests for the dark-mode bundlebees dashboard backend and pane assets."""

from __future__ import annotations

import json
import re
from pathlib import Path
import tomllib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bundlebees_plugin.client import ClientError
from tests.fakes import ROOT

from dashboard import plugin_api as API

SAFE_MODEL = "deepseek-v4.1-flash"
LEAKS = (
    "leakedvalue",
    "10.1.2.3",
    "0.0.0.0",
    "/home/worker",
    "worker.internal",
    "result-token",
    "base_url",
    "token_file",
)


class FakeBackend:
    """Returns deliberately leaky raw records; the API layer must sanitize."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_health: Exception | None = None
        self.concurrency = 2
        self.result_error = ClientError("no result yet", "no_result_yet", "wait", status=409)

    def health(self):
        self.calls.append(("health",))
        if self.fail_health:
            raise self.fail_health
        return {
            "ok": True,
            "version": "0.1.9",
            "max_concurrent": self.concurrency,
            "active": 1,
            "queued": 0,
            "paused": False,
            "model": {"id": SAFE_MODEL, "base_url": "http://10.1.2.3:8080"},
            "models": [{"id": SAFE_MODEL, "base_url": "http://10.1.2.3:8080"}],
            "bind": "0.0.0.0:9999",
            "token_file": "/home/worker/.orbit/token",
        }

    def containers(self):
        self.calls.append(("containers",))
        return [
            {
                "digest": "sha256:" + "b" * 64,
                "runtime": "docker",
                "memory_mb": 4096,
                "toolchain": ["python", "node"],
                "bind": "0.0.0.0:9999",
                "image": "ghcr.io/example/worker@sha256:" + "b" * 64,
            }
        ]

    def projects(self):
        self.calls.append(("projects",))
        return [
            {
                "key": "proj",
                "repo": "https://github.com/example/repo.git",
                "sync_branch": "main",
                "local_path": "/home/worker/repos/proj",
                "token": "result-token",
            }
        ]

    def attempts(self):
        self.calls.append(("attempts",))
        return [self._attempt("a1")]

    def get(self, attempt_id):
        self.calls.append(("get", attempt_id))
        return self._attempt(attempt_id)

    def events(self, attempt_id, cursor=None):
        self.calls.append(("events", attempt_id, cursor))
        return [
            {
                "seq": 1,
                "text": "token=leakedvalue connected to http://10.1.2.3/x",
                "host": "worker.internal",
                "local_path": "/home/worker/repo",
            }
        ]

    def result(self, attempt_id):
        self.calls.append(("result", attempt_id))
        raise self.result_error

    def cancel(self, attempt_id):
        self.calls.append(("cancel", attempt_id))
        return {
            "attempt_id": attempt_id,
            "state": "cancelled",
            "detail": "token=leakedvalue http://10.1.2.3/",
        }

    def review(self, attempt_id, accepted, note=None):
        self.calls.append(("review", attempt_id, accepted, note))
        return {
            "attempt_id": attempt_id,
            "state": "reviewed",
            "accepted": accepted,
            "detail": note,
        }

    def set_concurrency(self, value):
        self.calls.append(("set_concurrency", value))
        self.concurrency = value
        return {"ok": True, "max_concurrent": value}

    @staticmethod
    def _attempt(attempt_id: str) -> dict:
        return {
            "attempt_id": attempt_id,
            "task_id": "t1",
            "project": "proj",
            "branch": "feat/x",
            "state": "running",
            "created_at": "2026-11-11T00:00:00Z",
            "detail": "token=leakedvalue connected to http://10.1.2.3/x",
            "host": "worker.internal",
            "local_path": "/home/worker/repo",
            "token_file": "/home/worker/.orbit/token",
        }


@pytest.fixture()
def backend() -> FakeBackend:
    API.reset_state()
    fake = FakeBackend()
    API.set_backend(fake)
    yield fake
    API.reset_state()


@pytest.fixture()
def client(backend: FakeBackend) -> TestClient:
    app = FastAPI()
    app.include_router(API.router, prefix="/api/plugins/bundlebees")
    return TestClient(app)


def _json(response):
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def _assert_no_leaks(payload) -> None:
    text = json.dumps(payload)
    for leaked in LEAKS:
        assert leaked not in text, (leaked, text)


def test_missing_backend_is_a_structured_error():
    API.reset_state()
    app = FastAPI()
    app.include_router(API.router, prefix="/api/plugins/bundlebees")
    response = TestClient(app).get("/api/plugins/bundlebees/health")
    payload = _json(response)
    assert response.status_code == 503
    assert payload["ok"] is False
    assert payload["code"] == "not_configured"
    assert payload["error"] and payload["fix"]


def test_unauthenticated_backend_maps_to_structured_error(client, backend):
    backend.fail_health = ClientError(
        "unauthorized", "unauthorized", "check the token", status=401
    )
    response = client.get("/api/plugins/bundlebees/health")
    payload = _json(response)
    assert response.status_code == 401
    assert payload["ok"] is False
    assert payload["code"] == "unauthorized"
    assert payload["error"] and payload["fix"]


def test_health_is_scrubbed_and_has_model_id_only(client):
    payload = _json(client.get("/api/plugins/bundlebees/health"))
    assert payload["ok"] is True
    health = payload["health"]
    assert health["model"] == SAFE_MODEL
    assert health["version"] == "0.1.9"
    assert payload["launcher_configured"] is True
    _assert_no_leaks(payload)


def test_containers_have_digest_runtime_memory_and_toolchain_only(client):
    payload = _json(client.get("/api/plugins/bundlebees/containers"))
    assert payload["ok"] is True
    container = payload["containers"][0]
    assert container["digest"].startswith("sha256:")
    assert container["runtime"] == "docker"
    assert container["memory_mb"] == 4096
    assert container["toolchain"] == ["python", "node"]
    _assert_no_leaks(payload)


def test_models_are_ids_only(client):
    payload = _json(client.get("/api/plugins/bundlebees/models"))
    assert payload["ok"] is True
    assert payload["models"] == [SAFE_MODEL]
    _assert_no_leaks(payload)


def test_projects_show_public_repo_and_no_local_paths(client):
    payload = _json(client.get("/api/plugins/bundlebees/projects"))
    assert payload["ok"] is True
    project = payload["projects"][0]
    assert project == {
        "key": "proj",
        "repo": "https://github.com/example/repo.git",
        "sync_branch": "main",
    }
    _assert_no_leaks(payload)


def test_get_concurrency_and_put_validation(client, backend):
    payload = _json(client.get("/api/plugins/bundlebees/concurrency"))
    assert payload["ok"] is True
    assert payload["max_concurrent"] == 2
    assert payload["persisted"] is False

    for bad in (0, 11, "2", True, None):
        response = client.put(
            "/api/plugins/bundlebees/concurrency", json={"max_concurrent": bad}
        )
        assert response.status_code == 400
        body = _json(response)
        assert body["ok"] is False
        assert body["code"] == "invalid_argument"

    response = client.put(
        "/api/plugins/bundlebees/concurrency", json={"max_concurrent": 3}
    )
    body = _json(response)
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["max_concurrent"] == 3
    assert body["persisted"] is False
    assert ("set_concurrency", 3) in backend.calls


def test_attempts_and_attempt_detail_are_scrubbed(client):
    payload = _json(client.get("/api/plugins/bundlebees/attempts"))
    assert payload["ok"] is True
    assert payload["attempts"][0]["attempt_id"] == "a1"
    _assert_no_leaks(payload)

    detail = _json(client.get("/api/plugins/bundlebees/attempts/a1"))
    assert detail["ok"] is True
    assert detail["attempt"]["attempt_id"] == "a1"
    _assert_no_leaks(detail)


def test_attempt_events_are_scrubbed_and_untrusted(client):
    payload = _json(client.get("/api/plugins/bundlebees/attempts/a1/events"))
    assert payload["ok"] is True
    assert payload["untrusted"] is True
    assert payload["events"][0]["seq"] == 1
    _assert_no_leaks(payload)


def test_attempt_path_traversal_is_rejected(client):
    for attempt_id in ("..", "%2e%2e", "..%2fetc", "a%2f..%2fb"):
        response = client.get(
            f"/api/plugins/bundlebees/attempts/{attempt_id}/events"
        )
        assert response.status_code in (400, 404)
        body = _json(response)
        assert body["ok"] is False
        assert body["code"] in {"invalid_argument", "not_found"}


def test_cancel_and_review_are_post_and_require_confirmation(client, backend):
    missing = _json(client.post("/api/plugins/bundlebees/attempts/a1/cancel", json={}))
    assert missing["ok"] is False
    assert missing["code"] == "confirm_required"

    cancelled = _json(
        client.post(
            "/api/plugins/bundlebees/attempts/a1/cancel", json={"confirm": True}
        )
    )
    assert cancelled["ok"] is True
    assert cancelled["attempt"]["state"] == "cancelled"
    _assert_no_leaks(cancelled)

    missing_review = _json(
        client.post(
            "/api/plugins/bundlebees/attempts/a1/review",
            json={"confirm": True},
        )
    )
    assert missing_review["ok"] is False
    assert missing_review["code"] == "review_invalid"

    reviewed = _json(
        client.post(
            "/api/plugins/bundlebees/attempts/a1/review",
            json={
                "confirm": True,
                "accepted": True,
                "note": "token=leakedvalue http://10.1.2.3/",
            },
        )
    )
    assert reviewed["ok"] is True
    assert reviewed["accepted"] is True
    _assert_no_leaks(reviewed)
    assert backend.calls[-1][3] == "token=[redacted] [redacted-url]"


def test_verify_results_come_from_plugin_data_and_are_sanitized(tmp_path, client):
    results = tmp_path / "verify-results"
    results.mkdir()
    (results / "a1.json").write_text(
        json.dumps(
            {
                "attempt_id": "a1",
                "ok": True,
                "sha": "a" * 40,
                "branch": "feat/x",
                "files": ["src/a.py", "/etc/passwd", "../escape"],
                "tests": [
                    {"name": "unit", "ok": True},
                    {
                        "name": "secret-scan",
                        "ok": True,
                        "detail": "token=leakedvalue http://10.1.2.3/",
                    },
                ],
                "at": "2026-11-11T00:00:00Z",
                "host": "worker.internal",
                "token_file": "/home/worker/.orbit/token",
            }
        ),
        encoding="utf-8",
    )
    API.set_data_dir(tmp_path)
    payload = _json(client.get("/api/plugins/bundlebees/verify-results"))
    assert payload["ok"] is True
    assert payload["results"][0]["attempt_id"] == "a1"
    assert payload["results"][0]["files"] == ["src/a.py"]
    _assert_no_leaks(payload)


def test_verify_results_reject_traversal_attempt_id(tmp_path, client):
    API.set_data_dir(tmp_path)
    response = client.get(
        "/api/plugins/bundlebees/verify-results", params={"attempt_id": ".."}
    )
    assert response.status_code == 400
    body = _json(response)
    assert body["ok"] is False
    assert body["code"] == "invalid_argument"


def test_host_prompt_get_has_no_host_value_and_post_reuses_tool_rules(client):
    get_payload = _json(client.get("/api/plugins/bundlebees/host-prompt"))
    assert get_payload["ok"] is True
    assert get_payload["deploy_command"] == "hermes bundlebees deploy"
    assert "host" not in get_payload
    _assert_no_leaks(get_payload)

    posted = _json(
        client.post(
            "/api/plugins/bundlebees/host-prompt",
            json={
                "os": "linux",
                "reach": "same-machine",
                "provider": "opencode-go",
                "repos": ["example/repo"],
                "max_concurrent": 2,
            },
        )
    )
    assert posted["ok"] is True
    assert "bundlebees host setup prompt" in posted["prompt"]
    assert "10.1.2.3" not in posted["prompt"]

    unsafe = client.post(
        "/api/plugins/bundlebees/host-prompt",
        json={
            "os": "linux",
            "reach": "same-machine",
            "provider": "sk-" + "a" * 32,
            "repos": ["example/repo"],
            "max_concurrent": 2,
        },
    )
    assert unsafe.status_code == 400
    body = _json(unsafe)
    assert body["ok"] is False
    assert body["code"] == "unsafe_input"


def test_desktop_pane_uses_namespace_relative_rest_and_dark_styles():
    source = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert "authedFetch" not in source
    assert not re.search(r"https?://", source)
    for match in re.finditer(r"ctx\.rest\(\s*(['\"])(.*?)\1", source):
        path = match.group(2)
        assert path.startswith("/")
        assert not path.startswith("//")
    lowered = source.lower()
    assert "dark" in lowered
    assert "#0" in lowered or "rgb(1" in lowered or "rgb(2" in lowered
    assert "hermes bundlebees deploy" in source
    assert "acepted" not in lowered


def test_manifest_matches_the_hermes_dashboard_contract():
    manifest = json.loads(
        (ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "bundlebees"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["version"] == project["project"]["version"]
    # Hermes mounts dashboard/<api> at /api/plugins/<name>/ only when "api" is set.
    assert manifest["api"] == "plugin_api.py"
    assert (ROOT / "dashboard" / manifest["api"]).is_file()
    assert manifest["tab"]["hidden"] is True
    for stale in ("router", "module", "mount", "routes", "tabs", "entry"):
        assert stale not in manifest


def test_router_serves_every_pane_route():
    paths = {getattr(route, "path", "") for route in API.router.routes}
    expected = {
        "/health",
        "/containers",
        "/models",
        "/concurrency",
        "/projects",
        "/attempts",
        "/attempts/{attempt_id}",
        "/attempts/{attempt_id}/events",
        "/attempts/{attempt_id}/cancel",
        "/attempts/{attempt_id}/review",
        "/verify-results",
        "/host-prompt",
    }
    assert expected <= paths


def test_default_data_dir_uses_plugin_data_dir(monkeypatch, tmp_path):
    import sys
    import types

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-home"))
    storage = types.ModuleType("plugins.plugin_storage")
    called = []

    def plugin_data_dir(name):
        called.append(name)
        path = tmp_path / "profile-home" / "plugin-data" / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    storage.plugin_data_dir = plugin_data_dir
    pkg = types.ModuleType("plugins")
    pkg.plugin_storage = storage
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    path = API._default_data_dir()
    assert called == ["bundlebees"]
    assert path == (tmp_path / "profile-home" / "plugin-data" / "bundlebees").resolve()
    assert "env-home" not in str(path)


def test_no_data_dir_outside_hermes_means_no_results(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
    API.reset_state()
    assert API._default_data_dir() is None
    assert API._load_verify_results() == []
