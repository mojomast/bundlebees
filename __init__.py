"""Hermes plugin entrypoint for bundlebees. Registers skills, tools, and CLI."""

from __future__ import annotations

from pathlib import Path


def register(ctx) -> None:
    """Called by Hermes PluginManager. Keep this fast; lazy-import heavy deps in handlers."""
    skills_dir = Path(__file__).resolve().parent / "skills"
    if skills_dir.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                ctx.register_skill(child.name, skill_md)

    try:
        from bundlebees_plugin.tools import register_tools

        register_tools(ctx)
    except ImportError:
        pass

    if hasattr(ctx, "register_cli_command"):
        try:
            from bundlebees_plugin.cli import register_cli

            register_cli(ctx)
        except ImportError:
            pass
