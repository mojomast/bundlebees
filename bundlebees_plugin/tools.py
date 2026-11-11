"""Tool stubs so provides_tools matches register() before Wave A lands real handlers."""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

TOOL_NAMES = (
    "bundlebees_status",
    "bundlebees_dispatch",
    "bundlebees_attempt",
    "bundlebees_host_prompt",
)

_NOT_IMPLEMENTED = {
    "ok": False,
    "error": "This handler is a skeleton stub; Wave A implements the real tool.",
    "code": "not_implemented",
    "fix": "Wait for the tools and launcher client (BB01) to land on feat/v0.1.0.",
}


def _schema(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "description": "Operation. Implemented in BB01.",
                }
            },
            "additionalProperties": True,
        },
    }


def _handler(_args: dict[str, Any] | None = None, **_kwargs: Any) -> str:
    return json.dumps(_NOT_IMPLEMENTED)


def _call_register_tool(register_tool: Callable[..., Any], **kwargs: Any) -> Any:
    """Keyword-only register; drop output_schema if this Hermes build rejects it."""
    try:
        sig = inspect.signature(register_tool)
        if "output_schema" not in sig.parameters:
            kwargs.pop("output_schema", None)
    except (TypeError, ValueError):
        pass
    try:
        return register_tool(**kwargs)
    except TypeError as exc:
        if "output_schema" in kwargs and "output_schema" in str(exc):
            kwargs.pop("output_schema", None)
            return register_tool(**kwargs)
        raise


def register_tools(ctx) -> None:
    specs = (
        (
            "bundlebees_status",
            "Check CUA bee launcher health, capacity, live attempts, events, containers, or authorized projects. Never prints tokens or hosts.",
        ),
        (
            "bundlebees_dispatch",
            "Validate a bee brief (schema, authorized project, file leases) and start a worker. Does not apply patches, run tests, or deploy.",
        ),
        (
            "bundlebees_attempt",
            "Get, stream events for, cancel, or review one bee attempt. cancel and review require confirm=true.",
        ),
        (
            "bundlebees_host_prompt",
            "Generate a paste-ready setup prompt for an agent on a remote host. No secrets or host addresses in, no secrets or host addresses out.",
        ),
    )
    for name, description in specs:
        _call_register_tool(
            ctx.register_tool,
            name=name,
            toolset="bundlebees",
            schema=_schema(name, description),
            handler=_handler,
            description=description,
            emoji="🐝",
        )
