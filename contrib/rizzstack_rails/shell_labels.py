"""Public shell labels. Never echo an unknown program, subcommand or argument."""
from __future__ import annotations

_FAMILIES = frozenset('pytest unittest python python3 ls cat rg grep git npm pnpm cargo make '
                      'ruff mypy tsc node shell bash sh powershell pwsh uv pip go docker '
                      'git-status git-diff git-log'.split())
_TOOLS = frozenset('terminal shell bash powershell python read_file write_file patch grep glob '
                   'read write edit search test browser webfetch'.split())


def command_family(value):
    """The ledger carries a classification, not a raw script to reinterpret."""
    if not isinstance(value, str):
        return 'unknown'
    value = value.lower().replace(' ', '-')
    return value if value in _FAMILIES else 'unknown'


def shell_label(script):
    """Optional producer helper using R07's pinned, non-executing vibeline parser."""
    if not isinstance(script, str) or len(script) > 8192:
        return 'unknown'
    try:
        from vibeline.shell import commands
    except ImportError:
        return command_family(script)
    names = commands(script)
    return command_family(names[0]) if len(names) == 1 else 'unknown'


def tool_label(name):
    return name if isinstance(name, str) and name in _TOOLS else 'other'
