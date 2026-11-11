"""Fake PluginContext and plugin loader for unit tests. No network, no secrets."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]


class FakeCtx:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, Path] = {}
        self.hooks: dict[str, Callable[..., Any]] = {}
        self.cli_commands: dict[str, dict[str, Any]] = {}

    def register_tool(self, name: str, toolset: str, schema: dict, handler, description: str = "", emoji: str = "", **_kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "description": description,
            "emoji": emoji,
        }
        return name

    def register_skill(self, name: str, skill_md: Path) -> None:
        self.skills[name] = Path(skill_md)

    def register_hook(self, name: str, callback, **_kwargs) -> None:
        self.hooks[name] = callback

    def register_cli_command(self, name: str, help: str, setup_fn, handler_fn=None, description: str = ""):
        self.cli_commands[name] = {
            "help": help,
            "setup_fn": setup_fn,
            "handler_fn": handler_fn,
            "description": description,
        }
        return name


def load_plugin():
    """Load the plugin entry at repo-root __init__.py as ``bundlebees``."""
    parent = str(ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if ROOT.name != "bundlebees":
        raise RuntimeError(f"plugin directory must be named bundlebees, got {ROOT.name}")
    sys.modules.pop("bundlebees", None)
    return importlib.import_module("bundlebees")
