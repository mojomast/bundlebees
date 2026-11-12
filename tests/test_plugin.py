"""Fake-ctx registration: declared provides_tools match what register() registers."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.fakes import FakeCtx, ROOT, load_plugin


def _declared_tools() -> list[str]:
    data = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    return list(data.get("provides_tools") or [])


def test_register_tool_names_match_manifest():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    declared = _declared_tools()
    assert sorted(ctx.tools) == sorted(declared)


def test_handlers_return_json_strings():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    for name, rec in ctx.tools.items():
        result = rec["handler"]({})
        assert isinstance(result, str), name
        payload = json.loads(result)
        assert isinstance(payload, dict), name


def test_register_adds_attempt_approval_hook_declared_in_manifest():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    data = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert data.get("provides_hooks") == ["pre_tool_call"]
    assert "pre_tool_call" in ctx.hooks


def test_attempt_approval_hook_scope():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    hook = ctx.hooks["pre_tool_call"]
    for op in ("cancel", "review"):
        result = hook(tool_name="bundlebees_attempt", args={"op": op, "attempt_id": "a1"})
        assert result["action"] == "approve"
        assert result["rule_key"] == "bundlebees_attempt_change"
        assert "a1" in result["message"]
    as_json = hook(tool_name="bundlebees_attempt", args=json.dumps({"op": "cancel", "attempt_id": "a2"}))
    assert as_json["action"] == "approve"
    assert hook(tool_name="bundlebees_attempt", args={"op": "get", "attempt_id": "a1"}) is None
    assert hook(tool_name="bundlebees_status", args={"op": "cancel"}) is None
    assert hook(tool_name="terminal", args={"command": "ls"}) is None
    weird = hook(tool_name="bundlebees_attempt", args={"op": "cancel", "attempt_id": "x; rm -rf / $(id)"})
    assert ";" not in weird["message"] and "$" not in weird["message"]


def test_desktop_default_export_matches_hermes_runtime_loader():
    text = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
    assert 'import * as React from "react"' in text
    assert "export default plugin;" in text
    assert 'const PLUGIN_ID = "bundlebees"' in text
    assert "  id: PLUGIN_ID," in text
    assert "register: registerBundlebees" in text
    assert 'area: "panes"' in text
    # The runtime loader only resolves @hermes/plugin-sdk and react imports.
    import re

    specs = set(re.findall(r'^import[^;]*?from\s+"([^"]+)"', text, re.M))
    assert specs <= {"react", "@hermes/plugin-sdk"}
