"""Tool-layer tests with a fake launcher client (no network)."""

from __future__ import annotations

import json

import pytest

from bundlebees_plugin.client import ClientError
from tests.fakes import FakeCtx, ROOT, load_plugin


class FakeAgent:
    def __init__(self, token: str | None = None) -> None:
        self.secret_scope = self
        self._token = token

    def get_secret(self, _name: str) -> str | None:
        return self._token


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.health_data = {
            "ok": True,
            "version": "0.1.9",
            "max_concurrent": 2,
            "active": 1,
            "queued": 0,
            "paused": False,
            "model": "deepseek-v4.1-flash",
            "models": ["deepseek-v4.1-flash"],
        }
        self.attempts_data: list[dict] = []
        self.projects_data = [
            {"key": "proj", "repo": "https://github.com/example/repo.git", "sync_branch": "main"}
        ]
        self.events_data = [
            {"seq": 1, "text": "token=leakedvalue http://10.1.2.3/x", "untrusted": True}
        ]
        self.result_data = {
            "attempt_id": "a1",
            "observed": {"changed_files": ["src/a.py"], "usage": {"tokens": 4}},
            "checks": [{"name": "unit", "ok": True}],
        }
        self.create_error: ClientError | None = None
        self.health_error: Exception | None = None
        self.result_error: ClientError | None = None

    def health(self):
        self.calls.append(("health", (), {}))
        if self.health_error:
            raise self.health_error
        return dict(self.health_data)

    def attempts(self):
        self.calls.append(("attempts", (), {}))
        return [dict(a) for a in self.attempts_data]

    def attempts_raw(self):
        return [dict(a) for a in self.attempts_data]

    def projects(self):
        self.calls.append(("projects", (), {}))
        return [dict(p) for p in self.projects_data]

    def containers(self):
        self.calls.append(("containers", (), {}))
        return [{"digest": "sha256:" + "a" * 64, "runtime": "docker", "memory_mb": 2048}]

    def create(self, brief, model=None, kanban_task_id=None):
        self.calls.append(("create", (brief,), {"model": model, "kanban_task_id": kanban_task_id}))
        if self.create_error:
            raise self.create_error
        return {
            "attempt_id": "a1",
            "task_id": "t1",
            "state": "queued",
            "kanban_task_id": kanban_task_id,
        }

    def get(self, attempt_id):
        self.calls.append(("get", (attempt_id,), {}))
        return {"attempt_id": attempt_id, "task_id": "t1", "state": "running"}

    def events(self, attempt_id, cursor=None):
        self.calls.append(("events", (attempt_id,), {"cursor": cursor}))
        return [dict(e) for e in self.events_data]

    def result(self, attempt_id):
        self.calls.append(("result", (attempt_id,), {}))
        if self.result_error:
            raise self.result_error
        return dict(self.result_data)

    def cancel(self, attempt_id):
        self.calls.append(("cancel", (attempt_id,), {}))
        return {"attempt_id": attempt_id, "state": "cancelled"}

    def review(self, attempt_id, accepted, note=None):
        self.calls.append(("review", (attempt_id,), {"accepted": accepted, "note": note}))
        return {"attempt_id": attempt_id, "state": "reviewed", "accepted": accepted}


def _ctx():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    return ctx


def _call(ctx, name, args, client=None, agent=None):
    if client is not None:
        ctx.bundlebees_client = client
    if agent is not None:
        ctx.agent = agent
    result = ctx.tools[name]["handler"](args)
    assert isinstance(result, str)
    return json.loads(result)


def _brief(**overrides):
    brief = {
        "objective": "Implement the feature",
        "project": "proj",
        "branch": "feat/x",
        "source_rev": "abc123",
        "assigned_files": ["src/a.py"],
        "commands": ["python3 -m pytest -q"],
        "acceptance": ["tests pass"],
        "budget": {"timeout_s": 600, "max_tool_calls": 20},
    }
    brief.update(overrides)
    return brief


def test_tool_names_match_provides_tools():
    ctx = _ctx()
    assert sorted(ctx.tools) == sorted(
        [
            "bundlebees_status",
            "bundlebees_dispatch",
            "bundlebees_attempt",
            "bundlebees_host_prompt",
        ]
    )
    import yaml

    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert sorted(ctx.tools) == sorted(manifest["provides_tools"])


def test_every_handler_returns_a_json_string():
    client = FakeClient()
    ctx = _ctx()
    cases = [
        ("bundlebees_status", {"op": "health"}),
        ("bundlebees_dispatch", {"brief": _brief()}),
        ("bundlebees_attempt", {"op": "get", "attempt_id": "a1"}),
        (
            "bundlebees_host_prompt",
            {
                "os": "linux",
                "reach": "same-machine",
                "provider": "opencode-go",
                "repos": ["example/repo"],
                "max_concurrent": 2,
            },
        ),
    ]
    for name, args in cases:
        result = ctx.tools[name]["handler"](args) if not hasattr(ctx, "bundlebees_client") else None
        if result is not None:
            assert isinstance(result, str)
        payload = _call(ctx, name, args, client=client)
        assert payload["ok"] is True, (name, payload)


@pytest.mark.parametrize(
    "op", ["health", "capacity", "attempts", "containers", "projects"]
)
def test_status_ops(op: str):
    payload = _call(_ctx(), "bundlebees_status", {"op": op}, client=FakeClient())
    assert payload["ok"] is True
    assert payload["op"] == op


def test_status_health_has_model_id_only():
    payload = _call(_ctx(), "bundlebees_status", {"op": "health"}, client=FakeClient())
    health = payload["health"]
    assert health["model"] == "deepseek-v4.1-flash"
    text = json.dumps(payload)
    assert "bind" not in text
    assert "token" not in text


def test_status_events_requires_attempt_id():
    payload = _call(_ctx(), "bundlebees_status", {"op": "events"}, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] == "invalid_argument"


def test_status_events_are_scrubbed_and_untrusted():
    payload = _call(
        _ctx(),
        "bundlebees_status",
        {"op": "events", "attempt_id": "a1"},
        client=FakeClient(),
    )
    text = json.dumps(payload)
    assert payload["ok"] is True
    assert payload["untrusted"] is True
    assert "leakedvalue" not in text
    assert "10.1.2.3" not in text


def test_status_invalid_op():
    payload = _call(_ctx(), "bundlebees_status", {"op": "nope"}, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] == "invalid_argument"


def test_dispatch_accepts_object_and_json_string():
    client = FakeClient()
    ctx = _ctx()
    first = _call(ctx, "bundlebees_dispatch", {"brief": _brief()}, client=client)
    assert first["ok"] is True
    assert first["attempt_id"] == "a1"
    assert first["state"] == "queued"
    assert first["task_id"] == "t1"
    second = _call(ctx, "bundlebees_dispatch", {"brief": json.dumps(_brief())}, client=client)
    assert second["ok"] is True
    assert client.calls[-1][2]["model"] == "deepseek-v4.1-flash"


def test_dispatch_rejects_invalid_brief():
    payload = _call(_ctx(), "bundlebees_dispatch", {"brief": {}}, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] == "brief_invalid"


def test_dispatch_rejects_bad_json_string():
    payload = _call(_ctx(), "bundlebees_dispatch", {"brief": "{not json"}, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] == "brief_invalid"


def test_dispatch_code_work_requires_assigned_files():
    brief = _brief()
    brief.pop("assigned_files")
    payload = _call(_ctx(), "bundlebees_dispatch", {"brief": brief}, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] == "brief_invalid"
    assert "assigned_files" in payload["fix"]


def test_dispatch_detects_lease_overlap():
    client = FakeClient()
    client.attempts_data = [
        {
            "attempt_id": "running-1",
            "state": "running",
            "project": "proj",
            "branch": "feat/x",
            "assigned_files": ["src/a.py"],
        }
    ]
    brief = _brief(assigned_files=["src/a.py"])
    payload = _call(_ctx(), "bundlebees_dispatch", {"brief": brief}, client=client)
    assert payload["ok"] is False
    assert payload["code"] == "lease_overlap"


def test_dispatch_maps_403_to_project_unauthorized():
    client = FakeClient()
    client.create_error = ClientError("forbidden", "project_unauthorized")
    payload = _call(_ctx(), "bundlebees_dispatch", {"brief": _brief()}, client=client)
    assert payload["ok"] is False
    assert payload["code"] == "project_unauthorized"
    assert "restart" in payload["fix"]


def test_dispatch_rejects_model_not_on_launcher():
    payload = _call(
        _ctx(),
        "bundlebees_dispatch",
        {"brief": _brief(), "model": "not-a-model"},
        client=FakeClient(),
    )
    assert payload["ok"] is False
    assert payload["code"] == "model_not_allowed"


def test_dispatch_records_kanban_task_id():
    payload = _call(
        _ctx(),
        "bundlebees_dispatch",
        {"brief": _brief(), "kanban_task_id": "k1"},
        client=FakeClient(),
    )
    assert payload["ok"] is True
    assert payload["kanban_task_id"] == "k1"


def test_attempt_get_without_result_is_ok():
    client = FakeClient()
    client.result_error = ClientError("no result yet", "no_result_yet")
    payload = _call(_ctx(), "bundlebees_attempt", {"op": "get", "attempt_id": "a1"}, client=client)
    assert payload["ok"] is True
    assert payload["attempt"]["attempt_id"] == "a1"
    assert "result" not in payload


def test_attempt_get_includes_public_result():
    payload = _call(_ctx(), "bundlebees_attempt", {"op": "get", "attempt_id": "a1"}, client=FakeClient())
    assert payload["ok"] is True
    assert payload["result"]["observed"]["changed_files"] == ["src/a.py"]


def test_attempt_events_are_scrubbed():
    payload = _call(
        _ctx(),
        "bundlebees_attempt",
        {"op": "events", "attempt_id": "a1", "cursor": 0},
        client=FakeClient(),
    )
    text = json.dumps(payload)
    assert payload["ok"] is True
    assert payload["untrusted"] is True
    assert "leakedvalue" not in text
    assert "10.1.2.3" not in text


@pytest.mark.parametrize("op", ["cancel", "review"])
def test_attempt_cancel_and_review_require_confirm(op: str):
    args = {"op": op, "attempt_id": "a1"}
    if op == "review":
        args["accepted"] = True
    payload = _call(_ctx(), "bundlebees_attempt", args, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] == "confirm_required"


def test_attempt_cancel_with_confirm():
    payload = _call(
        _ctx(),
        "bundlebees_attempt",
        {"op": "cancel", "attempt_id": "a1", "confirm": True},
        client=FakeClient(),
    )
    assert payload["ok"] is True
    assert payload["state"] == "cancelled"


def test_attempt_review_requires_accepted_bool():
    payload = _call(
        _ctx(),
        "bundlebees_attempt",
        {"op": "review", "attempt_id": "a1", "confirm": True},
        client=FakeClient(),
    )
    assert payload["ok"] is False
    assert payload["code"] == "review_invalid"


def test_attempt_review_with_confirm_scrubs_note():
    client = FakeClient()
    payload = _call(
        _ctx(),
        "bundlebees_attempt",
        {
            "op": "review",
            "attempt_id": "a1",
            "confirm": True,
            "accepted": True,
            "note": "token=leakedvalue looks fine",
        },
        client=client,
    )
    assert payload["ok"] is True
    assert "leakedvalue" not in json.dumps(payload)
    assert "leakedvalue" not in client.calls[-1][2]["note"]


def test_attempt_maps_no_result_yet():
    client = FakeClient()
    client.result_error = ClientError("no result yet", "no_result_yet")
    payload = _call(
        _ctx(),
        "bundlebees_attempt",
        {"op": "review", "attempt_id": "a1", "confirm": True, "accepted": True},
        client=client,
    )
    # review does not need a result, but a client-level no_result_yet must map.
    assert payload["ok"] is True or payload["code"] == "no_result_yet"


def test_attempt_invalid_op_and_missing_id():
    missing = _call(_ctx(), "bundlebees_attempt", {"op": "get"}, client=FakeClient())
    assert missing["ok"] is False
    assert missing["code"] == "invalid_argument"
    bad = _call(_ctx(), "bundlebees_attempt", {"op": "nope", "attempt_id": "a1"}, client=FakeClient())
    assert bad["ok"] is False
    assert bad["code"] == "invalid_argument"


def test_host_prompt_fills_template():
    payload = _call(
        _ctx(),
        "bundlebees_host_prompt",
        {
            "os": "linux",
            "reach": "same-machine",
            "provider": "opencode-go",
            "repos": ["example/repo", "https://github.com/example/other.git"],
            "max_concurrent": 3,
            "branch_rules": "Feature branches only.",
        },
        client=FakeClient(),
    )
    assert payload["ok"] is True
    prompt = payload["prompt"]
    assert "linux" in prompt
    assert "opencode-go" in prompt
    assert "{{" not in prompt
    assert "Feature branches only." in prompt


@pytest.mark.parametrize(
    "overrides",
    [
        {"os": "plan9"},
        {"reach": "carrier-pigeon"},
        {"max_concurrent": 99},
        {"provider": "10.0.0.5"},
        {"branch_rules": "token=leakedvalue"},
        {"repos": ["https://user:pass@github.com/example/repo.git"]},
        {"repos": ["http://github.com/example/repo.git"]},
        {"repos": ["https://127.0.0.1/repo.git"]},
        {"repos": ["169.254.169.254"]},
    ],
)
def test_host_prompt_rejects_secrets_and_addresses(overrides):
    args = {
        "os": "linux",
        "reach": "same-machine",
        "provider": "opencode-go",
        "repos": ["example/repo"],
        "max_concurrent": 2,
    }
    args.update(overrides)
    payload = _call(_ctx(), "bundlebees_host_prompt", args, client=FakeClient())
    assert payload["ok"] is False
    assert payload["code"] in {"invalid_argument", "unsafe_input"}


def test_missing_secret_fails_closed_without_client():
    ctx = _ctx()
    ctx.agent = FakeAgent(None)
    payload = json.loads(ctx.tools["bundlebees_status"]["handler"]({"op": "health"}))
    assert payload["ok"] is False
    assert payload["code"] == "not_configured"


def test_handler_exception_becomes_envelope():
    client = FakeClient()
    client.health_error = RuntimeError("boom token=leakedvalue")
    payload = _call(_ctx(), "bundlebees_status", {"op": "health"}, client=client)
    assert payload["ok"] is False
    assert payload["code"] == "internal_error"
    assert "leakedvalue" not in json.dumps(payload)


def test_combined_schema_budget_stays_under_900():
    tiktoken = pytest.importorskip("tiktoken")
    ctx = _ctx()
    schemas = [rec["schema"] for rec in ctx.tools.values()]
    body = json.dumps(schemas, separators=(",", ":"), ensure_ascii=False)
    o200k = len(tiktoken.get_encoding("o200k_base").encode(body))
    assert o200k <= 900, (o200k, body)


def test_dispatch_allows_objective_only_brief():
    payload = _call(
        _ctx(),
        "bundlebees_dispatch",
        {"brief": {"objective": "Research options and report back"}},
        client=FakeClient(),
    )
    assert payload["ok"] is True
    assert payload["attempt_id"] == "a1"


def test_host_prompt_needs_no_launcher_or_token():
    ctx = _ctx()
    ctx.agent = FakeAgent(None)
    payload = json.loads(
        ctx.tools["bundlebees_host_prompt"]["handler"](
            {
                "os": "macos",
                "reach": "ssh",
                "provider": "ollama",
                "repos": ["https://github.com/example/repo.git"],
                "max_concurrent": 1,
            }
        )
    )
    assert payload["ok"] is True
    assert "macos" in payload["prompt"]
