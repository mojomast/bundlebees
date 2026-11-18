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

from .security import config_flag

PLUGIN_ID = "bundlebees"

#: The F2 enforcement flag. Durable plugin configuration, resolved at the
#: campaign/repository/execution-profile boundary; never session state and
#: never read from a brief.
REQUIRE_CONTRACT_KEY = "require_contract"

DEFAULTS: dict[str, Any] = {
    "adapter": "orbit-loopback",
    "launcher_url": "",
    "allow_private": False,
    "orbit_url": "http://127.0.0.1:4318",
    "coordinator": "",
    "worktree": "",
    "branch": "",
    "deploy_host_alias": "",
    "verify_isolation": "auto",
    "verify_python": "",
    "gitleaks_path": "",
    "sandbox_tiktoken_cache": "",
    "provisioning_enabled": False,
    "provisioning_host": "",
    "verify_allow_unsafe_local": False,
    "verify_allow_weak_unshare": False,
    "require_contract": False,
    # Pre-dispatch worker attribution (E9). Declared here as well as in plugin.yaml:
    # settings_from_ctx only returns DEFAULTS keys, and a key missing here made the gate
    # silently inert while its unit tests (which inject settings directly) stayed green.
    "attribution_canary_projects": [],
    "attribution_worker_id": "",
    "attribution_ledger_chain": "",
    "verify_timeout_s": 900,
    "integrate_suite": "",
    "integrate_security_tests": "",
    "integrate_base": "",
}


def require_contract(
    config: Mapping[str, Any],
    *,
    campaign: str | None = None,
    repository: str | None = None,
) -> bool:
    """The durable ``require_contract`` enforcement flag (default off).

    The value is read from the settings mapping this module produces — the
    active execution profile's plugin settings (``settings_from_ctx``) or the
    repository's Hermes profile file (``settings_from_hermes_config``). A plain
    boolean opts the whole scope in. A mapping scopes the flag per campaign or
    repository::

        {"require_contract": {"default": false,
                              "campaigns": {"reliability": true},
                              "repositories": {"mojomast/bundlebees": true}}}

    An explicit campaign or repository value wins over the default — including
    an explicit ``false``, which opts that one scope out of enforcement. A
    scope that is absent, ``None`` or unparsable does not vote and the default
    decides; an unparsable or absent default is off, so enforcement can never
    be enabled by accident. The campaign table is consulted before the
    repository table. The brief carries no vote: callers must not pass brief
    values here.
    """
    if not isinstance(config, Mapping):
        return False
    value = config.get(REQUIRE_CONTRACT_KEY)
    if isinstance(value, Mapping):
        for table, name in (
            (value.get("campaigns"), campaign),
            (value.get("repositories"), repository),
        ):
            vote = _scope_vote(table, name)
            if vote is not None:
                return vote
        return config_flag(value.get("default"), False)
    return config_flag(value, False)


def _scope_vote(table: Any, name: Any) -> bool | None:
    """The named scope's own vote from *table*, or ``None`` when it is silent.

    A parseable boolean-shaped value votes that value — an explicit ``false``
    included, so it can outvote a ``default: true``. An empty name, a missing
    table, an absent key, ``None`` or an unparsable value does not vote.
    """
    if not name or not isinstance(table, Mapping):
        return None
    return _flag_or_none(table.get(str(name)))


def _flag_or_none(value: Any) -> bool | None:
    """``config_flag``'s parse rules, but ``None`` for anything unparsable."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return None


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
