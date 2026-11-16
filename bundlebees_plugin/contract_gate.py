"""The ``require_contract`` dispatch gate (reliability campaign F2).

This module is the enforcement boundary a dispatcher calls *before* any
launcher call. :func:`gate` refuses with a machine-readable
:class:`ContractRefusal` (``contract_required`` and friends) or returns the
``acdd`` envelope block; :func:`dispatch` refuses first and only then performs
the send, so a refused brief never reaches the launcher.

Configuration is durable: :func:`enforcement_config` resolves the flag through
``bundlebees_plugin.settings`` — the active execution profile's plugin settings
or the repository's Hermes profile file — never from session state and never
from the brief. A restart re-reads the same configuration and re-derives the
same decision, which is what makes enforcement survive restarts and recovery.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Mapping

from . import contracts as _contracts
from . import settings as _settings

__all__ = [
    "ContractRefusal",
    "enforcement_config",
    "gate",
    "dispatch",
    "main",
]

ContractRefusal = _contracts.ContractRefusal


def enforcement_config(ctx: Any = None) -> dict[str, Any]:
    """Durable settings for the active scope.

    ``ctx`` is the Hermes ``PluginContext`` of the running execution profile;
    without one, the repository's Hermes profile file is read. Both paths are
    re-read on every call: the flag is configuration, not session state.
    """
    if ctx is not None and callable(getattr(ctx, "get_config", None)):
        return _settings.settings_from_ctx(ctx)
    return _settings.settings_from_hermes_config()


def gate(
    brief: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    packet: Any = None,
    evidence: Any = None,
    campaign: str | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """Run the fail-closed contract gate; raise or return the ``acdd`` block."""
    return _contracts.pre_dispatch(
        brief,
        config=config,
        packet=packet,
        evidence=evidence,
        campaign=campaign,
        repository=repository,
    )


def dispatch(
    brief: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    send: Callable[..., Any],
    packet: Any = None,
    evidence: Any = None,
    campaign: str | None = None,
    repository: str | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], Any]:
    """Gate first, then send. A refusal never calls *send*.

    ``send`` is the launcher call (for example the client's create); it
    receives *brief* and any keyword arguments unchanged. Returns
    ``(acdd_block, send_result)``.
    """
    block = gate(
        brief,
        config=config,
        packet=packet,
        evidence=evidence,
        campaign=campaign,
        repository=repository,
    )
    return block, send(brief, **kwargs)


def main(argv: list[str] | None = None, *, config: Mapping[str, Any] | None = None) -> int:
    """``python3 -m bundlebees_plugin.contract_gate check BRIEF.json``.

    A dry run of the gate against a brief file: it prints the same
    machine-readable envelope the dispatch decision is made from and exits 0
    when the gate admits the brief, 1 when it refuses. Configuration comes
    from the Hermes profile file unless a mapping is passed explicitly.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m bundlebees_plugin.contract_gate",
        description="Dry-run the require_contract dispatch gate against a brief.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="gate one brief file")
    check.add_argument("brief", help="path to a brief JSON file")
    args = parser.parse_args(argv)

    try:
        with open(args.brief, "r", encoding="utf-8") as handle:
            brief = json.load(handle)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "code": "brief_invalid",
                          "error": f"the brief cannot be read: {exc}",
                          "fix": "Pass a readable brief JSON file."}, sort_keys=True))
        return 1
    resolved = config if config is not None else enforcement_config()
    try:
        block = gate(brief, config=resolved)
    except ContractRefusal as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **block}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
