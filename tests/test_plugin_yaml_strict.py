"""plugin.yaml must parse under a strict YAML loader (no duplicate keys)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin.yaml"


def _strict_load(text: str):
    from ruamel.yaml import YAML
    from ruamel.yaml.constructor import DuplicateKeyError

    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        return yaml.load(text), None
    except DuplicateKeyError as exc:
        return None, exc


def test_plugin_yaml_strict_no_duplicate_keys():
    data, err = _strict_load(PLUGIN.read_text(encoding="utf-8"))
    assert err is None, f"duplicate key in plugin.yaml: {err}"
    assert isinstance(data, dict)
    assert data.get("name")
    assert data.get("version")
    tools = data.get("provides_tools") or []
    assert len(tools) == len(set(tools))


def test_plugin_yaml_has_single_name_and_version_line():
    text = PLUGIN.read_text(encoding="utf-8")
    assert text.count("\nname:") + (1 if text.startswith("name:") else 0) == 1
    assert sum(1 for line in text.splitlines() if line.startswith("version:")) == 1


def test_strict_loader_rejects_duplicate_name():
    bad = textwrap.dedent(
        """\
        name: bundlebees
        name: bundlebees
        version: "0.0.0"
        """
    )
    data, err = _strict_load(bad)
    assert data is None and err is not None


def test_provides_tools_match_registered_names():
    """Optional: wire this to your fake-ctx registration test."""
    data, err = _strict_load(PLUGIN.read_text(encoding="utf-8"))
    assert err is None
    declared = list(data.get("provides_tools") or [])
    # Import your register_tools and compare — placeholder asserts non-empty when tools exist.
    if declared:
        assert all(isinstance(t, str) and t for t in declared)
