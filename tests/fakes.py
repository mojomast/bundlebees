"""Fake PluginContext and plugin loader for unit tests. No network, no secrets.

The fake mirrors the real Hermes 0.23 ``PluginContext`` surface the plugin
uses: ``register_tool``, ``register_skill``, ``register_hook``,
``register_cli_command`` and ``get_config(key, default=None)``. It has no
``config`` attribute and no ``agent``, because real Hermes has neither.
Secrets come from a fake ``agent.secret_scope`` module (``secret_scope``),
the same import path real Hermes provides.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

ROOT = Path(__file__).resolve().parents[1]


class FakeCtx:
    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        self.settings: dict[str, Any] = dict(settings or {})
        self.tools: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, Path] = {}
        self.hooks: dict[str, Callable[..., Any]] = {}
        self.cli_commands: dict[str, dict[str, Any]] = {}
        # Stand-in for plugin_data_dir("bundlebees"): leases, waves, metrics.
        # Real Hermes has no such attribute; tools fall back to plugin_data_dir.
        self.bundlebees_state_dir = Path(tempfile.mkdtemp(prefix="bundlebees-test-state-"))

    def register_tool(self, name: str, toolset: str, schema: dict, handler, description: str = "", emoji: str = "", **_kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "description": description,
            "emoji": emoji,
        }
        return name

    def get_config(self, key: str, default: Any = None) -> Any:
        """Same contract as Hermes: one plugin-relative key, required."""
        if not isinstance(key, str) or not key:
            raise TypeError("get_config() needs a plugin-relative key")
        return self.settings.get(key, default)

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
    """Load the plugin entry at repo-root __init__.py as package ``bundlebees``.

    Loaded from its file, so it works from any checkout directory name, including
    the sandbox's ``/work`` copy (Hermes itself installs the plugin as ``bundlebees``).
    """
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)  # bundlebees_plugin is importable from the repo root
    sys.modules.pop("bundlebees", None)
    spec = importlib.util.spec_from_file_location(
        "bundlebees", ROOT / "__init__.py", submodule_search_locations=[root]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the plugin entry __init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bundlebees"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("bundlebees", None)
        raise
    return module


@contextmanager
def secret_scope(values: Mapping[str, str | None] | None = None, *, raises: bool = False) -> Iterator[dict]:
    """Install a fake ``agent.secret_scope`` with ``get_secret(name, default=None)``."""
    store = dict(values or {})
    calls: dict[str, int] = {}

    def get_secret(name: str, default: str | None = None) -> str | None:
        calls[name] = calls.get(name, 0) + 1
        if raises:
            raise RuntimeError("no profile secret scope active")
        value = store.get(name)
        return default if value is None else value

    saved = {key: sys.modules.get(key) for key in ("agent", "agent.secret_scope")}
    agent_mod = types.ModuleType("agent")
    scope_mod = types.ModuleType("agent.secret_scope")
    scope_mod.get_secret = get_secret
    agent_mod.secret_scope = scope_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.secret_scope"] = scope_mod
    try:
        yield calls
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
