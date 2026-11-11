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
