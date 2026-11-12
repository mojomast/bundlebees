"""Hermes plugin entrypoint for bundlebees. Registers skills, tools, and CLI."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Short recovery hint if Tool Search defers the tools.
BUNDLEBEES_SYSTEM_PROMPT_SECTION = (
    "Bundlebees orchestrates CUA bees (cheap coding workers in disposable "
    "sandboxes on a worker machine the user controls). Drive it ONLY with "
    "bundlebees_status, bundlebees_dispatch, bundlebees_attempt, and "
    "bundlebees_host_prompt (toolset bundlebees). If those tools are not in "
    "your visible list, call tool_search([\"bundlebees\"]) or "
    'skill_view("bundlebees:bundlebees-operator"), then use them. '
    "Do not shell out to curl against the launcher or invent a client. "
    "verify and deploy are user-run CLI commands, not model tools. "
    "Never print tokens, private URLs, or bind addresses."
)


_APPROVAL_OPS = {"cancel", "review"}


def approve_attempt_change(tool_name: str = "", args=None, **kwargs):
    """pre_tool_call hook: a real Hermes approval before cancel or review.

    ``bundlebees_attempt`` with ``op`` cancel or review changes a worker's
    state, so Hermes asks the user (the ``confirm=true`` argument alone is
    model-set). Every other tool and op returns None. Hermes fails closed
    when nobody can approve.
    """
    if str(tool_name or "") != "bundlebees_attempt":
        return None
    data = args
    if isinstance(data, str):
        import json

        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        return None
    op = str(data.get("op") or "").strip().lower()
    if op not in _APPROVAL_OPS:
        return None
    attempt = str(data.get("attempt_id") or "").strip()[:80]
    safe = "".join(ch for ch in attempt if ch.isalnum() or ch in "._:-") or "?"
    if op == "review":
        verdict = "accept" if data.get("accepted") is True else "reject"
        message = f"bundlebees: {verdict} worker attempt {safe}"
    else:
        message = f"bundlebees: cancel worker attempt {safe}"
    return {"action": "approve", "message": message, "rule_key": "bundlebees_attempt_change"}


def register(ctx) -> None:
    """Called by Hermes PluginManager. Keep this fast; lazy-import heavy deps in handlers."""
    skills_dir = _ROOT / "skills"
    if skills_dir.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                ctx.register_skill(child.name, skill_md)

    from bundlebees_plugin.tools import register_tools

    register_tools(ctx)

    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_tool_call", approve_attempt_change)

    if hasattr(ctx, "register_cli_command"):
        from bundlebees_plugin.cli import register_cli

        register_cli(ctx)

    if hasattr(ctx, "register_system_prompt_section"):
        try:
            ctx.register_system_prompt_section(
                "bundlebees-operator",
                BUNDLEBEES_SYSTEM_PROMPT_SECTION,
            )
        except Exception:
            pass
