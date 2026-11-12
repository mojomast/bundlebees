"""Offline tests for the pluggable worker-adapter interface.

No network, no real tokens, no Docker, no SSH. Clients are constructed against
literal loopback addresses and never called, so nothing here reaches a socket.
"""

from __future__ import annotations

import inspect

import pytest

from bundlebees_plugin.adapters import (
    ADAPTERS,
    CodexAdapter,
    OpenCodeAdapter,
    UnsupportedAdapter,
    WorkerAdapter,
    adapter_from_config,
    available_adapters,
    normalize_result,
)
from bundlebees_plugin.client import (
    LauncherClient,
    OrbitAdapterClient,
    client_from_config,
)

TOKEN = "test-token-value"


def _provider() -> str:
    return TOKEN


def test_default_adapter_is_the_same_client_type_as_client_from_config():
    adapter = adapter_from_config({}, token_provider=_provider)
    expected = client_from_config({}, token_provider=_provider)
    assert type(adapter) is type(expected)
    assert isinstance(adapter, OrbitAdapterClient)
    assert adapter.name == "orbit-loopback"
    assert adapter._token() == TOKEN


def test_launcher_adapter_matches_client_from_config():
    config = {"adapter": "launcher", "launcher_url": "http://127.0.0.1:9"}
    adapter = adapter_from_config(config, token_provider=_provider)
    expected = client_from_config(config, token_provider=_provider)
    assert type(adapter) is type(expected)
    assert isinstance(adapter, LauncherClient)
    assert adapter.name == "launcher"
    assert adapter._token() == TOKEN


@pytest.mark.parametrize("alias", ["direct", "http", "https"])
def test_launcher_aliases_resolve_to_the_launcher_client(alias: str):
    config = {"adapter": alias, "launcher_url": "http://127.0.0.1:9"}
    adapter = adapter_from_config(config, token_provider=_provider)
    assert isinstance(adapter, LauncherClient)
    assert adapter.name == "launcher"


def test_unknown_adapter_is_rejected_with_known_names():
    with pytest.raises(UnsupportedAdapter) as excinfo:
        adapter_from_config({"adapter": "telepathy"}, token_provider=_provider)
    exc = excinfo.value
    assert exc.code == "unsupported_adapter"
    for name in ("orbit-loopback", "launcher", "opencode", "codex"):
        assert name in exc.fix
    assert "telepathy" in exc.error


@pytest.mark.parametrize("adapter_cls, adapter_name", [(OpenCodeAdapter, "opencode"), (CodexAdapter, "codex")])
def test_stub_methods_raise_unsupported_with_code_and_fix(adapter_cls, adapter_name):
    adapter = adapter_cls()
    assert adapter.name == adapter_name

    calls = [
        lambda: adapter.health(),
        lambda: adapter.projects(),
        lambda: adapter.attempts(),
        lambda: adapter.create({"objective": "x"}, model="deepseek-v4.1-flash"),
        lambda: adapter.get("att-1"),
        lambda: adapter.events("att-1", cursor=0),
        lambda: adapter.result("att-1"),
        lambda: adapter.cancel("att-1"),
        lambda: adapter.review("att-1", accepted=True, note="ok"),
    ]
    for call in calls:
        with pytest.raises(UnsupportedAdapter) as excinfo:
            call()
        exc = excinfo.value
        assert exc.code == "unsupported_adapter"
        assert adapter_name in exc.error
        assert "docs/ADAPTERS.md" in exc.fix


def test_stub_create_requires_model_keyword():
    """The Protocol keeps ``model`` keyword-only; stubs must not drift from it."""
    signature = inspect.signature(OpenCodeAdapter.create)
    assert signature.parameters["model"].kind is inspect.Parameter.KEYWORD_ONLY


def test_normalize_result_drops_worker_metadata_and_keeps_the_contract():
    raw = {
        "attempt_id": "att-1",
        "patch": "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n",
        "base_rev": "f" * 40,
        "usage": {"tokens": 120, "cost_usd": 0.02, "extra": "dropped"},
        "state": "succeeded",
        "untrusted": False,
        "assigned_files": ["src/a.py"],
        "lease": {"files": ["src/a.py"]},
        "brief": {"objective": "do a thing"},
        "worker_claims": "trust me",
        "scope": "admin",
        "token": "secret-token-value",
    }
    out = normalize_result(raw)
    assert set(out) == {"attempt_id", "patch", "base_rev", "usage", "evidence", "state", "untrusted"}
    assert out["attempt_id"] == "att-1"
    assert out["patch"] == raw["patch"]
    assert out["base_rev"] == "f" * 40
    assert out["usage"] == {"tokens": 120, "cost_usd": 0.02}
    assert out["state"] == "succeeded"
    assert out["untrusted"] is True
    for dropped in ("assigned_files", "lease", "brief", "worker_claims", "scope", "token"):
        assert dropped not in out


def test_normalize_result_tolerates_missing_and_bad_fields():
    out = normalize_result(
        {
            "id": "att-2",
            "diff": "diff --git a/x b/x\n",
            "status": "running",
            "usage": {"tokens": True, "cost_usd": "free", "extra": 1},
        }
    )
    assert out == {
        "attempt_id": "att-2",
        "patch": "diff --git a/x b/x\n",
        "base_rev": None,
        "usage": {"tokens": None, "cost_usd": None},
        "evidence": {"checks": []},
        "state": "running",
        "untrusted": True,
    }


def test_normalize_result_empty_patch_is_none():
    out = normalize_result({"attempt_id": "att-3", "patch": "   ", "state": "succeeded"})
    assert out["patch"] is None
    assert out["base_rev"] is None
    assert out["usage"] == {"tokens": None, "cost_usd": None}


def test_available_adapters_marks_orbit_loopback_default():
    entries = available_adapters()
    by_name = {entry["name"]: entry for entry in entries}
    assert set(by_name) == {"orbit-loopback", "launcher", "opencode", "codex"}
    assert by_name["orbit-loopback"]["default"] is True
    assert by_name["orbit-loopback"]["implemented"] is True
    assert by_name["launcher"]["default"] is False
    assert by_name["launcher"]["implemented"] is True
    assert by_name["opencode"]["implemented"] is False
    assert by_name["codex"]["implemented"] is False
    assert sum(1 for entry in entries if entry["default"]) == 1


def test_adapters_mapping_exposes_the_documented_names():
    assert set(ADAPTERS) == {"orbit-loopback", "launcher", "opencode", "codex"}
    for factory in ADAPTERS.values():
        assert callable(factory)
    assert isinstance(
        ADAPTERS["orbit-loopback"]({}, _provider),
        OrbitAdapterClient,
    )
    assert isinstance(
        ADAPTERS["launcher"]({"launcher_url": "http://127.0.0.1:9"}, _provider),
        LauncherClient,
    )
    assert isinstance(ADAPTERS["opencode"]({}, _provider), OpenCodeAdapter)
    assert isinstance(ADAPTERS["codex"]({}, _provider), CodexAdapter)


def test_client_adapters_satisfy_the_protocol_methods():
    adapter = adapter_from_config({}, token_provider=_provider)
    for method in (
        "health",
        "projects",
        "attempts",
        "create",
        "get",
        "events",
        "result",
        "cancel",
        "review",
    ):
        assert callable(getattr(adapter, method))
    for name in ("model", "kanban_task_id"):
        assert (
            inspect.signature(adapter.create).parameters[name].kind
            is inspect.signature(WorkerAdapter.create).parameters[name].kind
        )
    for name in ("accepted", "note"):
        assert (
            inspect.signature(adapter.review).parameters[name].kind
            is inspect.signature(WorkerAdapter.review).parameters[name].kind
        )


def test_normalize_result_maps_orbit_launcher_shapes_in_the_adapter_layer():
    sha = "ab" * 20
    fake = "not" + "-a-real-" + "value"  # built at runtime so scanners see no literal
    raw = {
        "attempt_id": "att_1",
        "source": {"project": "p", "repo_rev": sha.upper()},
        "patch": {"diff": "diff --git a/x b/x\n"},
        "observed": {"usage": {"total_tokens": 900}, "assigned_files": ["evil.py"]},
        "checks": [{"name": "suite", "command": "pytest token=" + fake, "passed": True, "extra": 1}],
        "worker_claims": {"lease": ["evil.py"]},
    }
    out = normalize_result(raw)
    assert out["base_rev"] == sha
    assert out["patch"] == "diff --git a/x b/x\n"
    assert out["usage"]["tokens"] == 900
    assert out["evidence"]["checks"][0]["passed"] is True
    assert fake not in out["evidence"]["checks"][0]["command"]
    assert "worker_claims" not in out and "observed" not in out


def test_normalize_result_rejects_short_or_named_bases():
    assert normalize_result({"base_rev": "abc1234"})["base_rev"] is None
    assert normalize_result({"base_rev": "main"})["base_rev"] is None


def test_core_verifier_and_contracts_do_not_name_an_agent_or_launcher():
    """Agent-agnostic core: only adapters.py and client.py know runtimes."""
    import pathlib
    import re as _re

    root = pathlib.Path(__file__).resolve().parents[1] / "bundlebees_plugin"
    banned = _re.compile(r"(?i)deepseek|opencode|codex|orbit|repo_rev|local-workers")
    for name in ("brief.py", "leases.py", "revision.py", "sandbox.py", "metrics.py", "schemas/brief.schema.json"):
        text = (root / name).read_text(encoding="utf-8")
        assert not banned.search(text), name
