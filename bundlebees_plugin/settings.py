"""Non-secret bundlebees settings, read through Hermes's real APIs.

Tools and the CLI read each key with ``ctx.get_config(key, default)``, the
documented ``PluginContext`` call that resolves
``plugins.entries.bundlebees.settings.<key>`` for the active profile.
``dashboard/plugin_api.py`` has no ``ctx``; it reads the same subtree with
``hermes_cli.config.load_config_readonly()`` as the Hermes plugin guide
documents. Defaults mirror ``config_schema`` in ``plugin.yaml``. No secret is
ever a setting: the launcher token is read only with ``get_secret``.
"""

from __future__ import annotations

from typing import Any, Mapping

PLUGIN_ID = "bundlebees"

DEFAULTS: dict[str, Any] = {
    "adapter": "orbit-loopback",
    "launcher_url": "",
    "allow_private": False,
    "orbit_url": "http://127.0.0.1:4318",
    "worktree": "",
    "branch": "",
    "deploy_host_alias": "",
    "verify_isolation": "auto",
    "sandbox_tiktoken_cache": "",
    "verify_allow_unsafe_local": False,
    "verify_allow_weak_unshare": False,
    "require_contract": False,
    "verify_timeout_s": 900,
    "integrate_suite": "",
    "integrate_security_tests": "",
}


def settings_from_ctx(ctx: Any) -> dict[str, Any]:
    """Every declared key via ``ctx.get_config(key, default)``."""
    out = dict(DEFAULTS)
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return out
    for key, default in DEFAULTS.items():
        try:
            value = getter(key, default)
        except Exception:
            value = default
        out[key] = default if value is None else value
    return out


def settings_from_hermes_config() -> dict[str, Any]:
    """``plugins.entries.bundlebees.settings`` from the read-only Hermes config."""
    out = dict(DEFAULTS)
    try:
        from hermes_cli.config import load_config_readonly
    except Exception:
        return out
    try:
        config = load_config_readonly() or {}
    except Exception:
        return out
    entry = _mapping(_mapping(_mapping(config).get("plugins")).get("entries")).get(PLUGIN_ID)
    entry = _mapping(entry)
    settings = _mapping(entry.get("settings"))
    legacy = _mapping(entry.get("config"))
    for key, default in DEFAULTS.items():
        if key in settings and settings[key] is not None:
            out[key] = settings[key]
        elif key in legacy and legacy[key] is not None:
            out[key] = legacy[key]
        else:
            out[key] = default
    return out


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
