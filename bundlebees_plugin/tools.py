"""The four model-facing bundlebees tools.

Every handler returns ``json.dumps(envelope)`` on success, validation failure,
and exception. Secrets are read only through the client's ``get_secret``
provider; target URLs never come from tool arguments.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .client import (
    DEFAULT_MODEL,
    ClientError,
    client_from_agent,
)
from .security import contains_address, is_public_git_url, looks_like_secret, scrub

TOOL_NAMES = (
    "bundlebees_status",
    "bundlebees_dispatch",
    "bundlebees_attempt",
    "bundlebees_host_prompt",
)

_MODULE_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_BRIEF_SCHEMA = _MODULE_DIR / "schemas" / "brief.schema.json"
_STUB_TEMPLATE = _MODULE_DIR / "templates" / "host-setup-prompt.md"
_SKILL_TEMPLATE = (
    _PLUGIN_ROOT
    / "skills"
    / "bundlebees-operator"
    / "templates"
    / "host-setup-prompt.md"
)

_STATUS_OPS = {"health", "capacity", "attempts", "events", "containers", "projects"}
_ATTEMPT_OPS = {"get", "events", "cancel", "review"}
_OS_VALUES = {"linux", "macos", "windows"}
_REACH_VALUES = {"same-machine", "lan", "vpn", "ssh"}
_RUNNING_STATES = {"running", "working", "active", "started"}

_FIX_PROJECT = (
    "The owner must add this project to the launcher config and restart the "
    "launcher; this tool does not restart it."
)
_FIX_BRIEF = (
    "Pass a brief object or a JSON string with at least a non-empty objective; "
    "code-work briefs also need assigned_files."
)


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _ok(**fields: Any) -> str:
    payload: dict[str, Any] = {"ok": True}
    payload.update(fields)
    return json.dumps(payload)


def _err(code: str, error: str, fix: str) -> str:
    payload = {
        "ok": False,
        "error": scrub(str(error)),
        "code": code,
        "fix": scrub(str(fix)),
    }
    return json.dumps(payload)


def _coerce_args(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, Mapping):
        return dict(args)
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _ctx_config(ctx: Any) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if callable(config):
        try:
            config = config()
        except Exception:
            config = None
    if isinstance(config, Mapping):
        return dict(config)
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _client_for(ctx: Any, kwargs: Mapping[str, Any]) -> Any:
    injected = kwargs.get("client")
    if injected is not None:
        return injected
    injected = getattr(ctx, "bundlebees_client", None)
    if injected is not None:
        return injected
    agent = kwargs.get("agent")
    if agent is None:
        agent = getattr(ctx, "agent", None)
    return client_from_agent(agent, _ctx_config(ctx))


# ---------------------------------------------------------------------------
# Brief validation (small local validator; no nested schema in the tool spec)
# ---------------------------------------------------------------------------


def _load_brief_schema() -> dict[str, Any]:
    return json.loads(_BRIEF_SCHEMA.read_text(encoding="utf-8"))


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_value(value: Any, spec: Mapping[str, Any], path: str) -> str | None:
    expected = spec.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, item) for item in types):
            return f"{path} has the wrong type"
    if isinstance(value, str):
        minimum = spec.get("minLength")
        if isinstance(minimum, int) and len(value.strip()) < minimum:
            return f"{path} is too short"
    if isinstance(value, list):
        minimum_items = spec.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            return f"{path} needs at least {minimum_items} item(s)"
        item_spec = spec.get("items") or {}
        if isinstance(item_spec, Mapping):
            for index, item in enumerate(value):
                problem = _validate_value(item, item_spec, f"{path}[{index}]")
                if problem:
                    return problem
    if isinstance(value, Mapping):
        for required in spec.get("required", []):
            if required not in value:
                return f"{path} is missing required field {required!r}"
        for key, child_spec in (spec.get("properties") or {}).items():
            if key in value and isinstance(child_spec, Mapping):
                problem = _validate_value(value[key], child_spec, f"{path}.{key}")
                if problem:
                    return problem
    return None


def _validate_brief(brief: Mapping[str, Any]) -> str | None:
    try:
        schema = _load_brief_schema()
    except (OSError, json.JSONDecodeError):
        return "brief schema is unavailable"
    objective = brief.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return "brief.objective must be a non-empty string"
    return _validate_value(brief, schema, "brief")


def _project_key(project: Any) -> str | None:
    if isinstance(project, str):
        return project
    if isinstance(project, Mapping):
        value = project.get("key") or project.get("project") or project.get("name")
        return str(value) if value else None
    return None


def _norm_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text or None


def _scrub_events(events: Any) -> list[Any]:
    if not isinstance(events, list):
        return []
    return [scrub(item) for item in events]


def _unwrap_attempt(data: Any) -> Mapping[str, Any]:
    if isinstance(data, Mapping):
        for key in ("attempt", "data", "worker"):
            nested = data.get(key)
            if isinstance(nested, Mapping):
                return nested
        return data
    return {}


def _lease_overlap(attempts: Any, brief: Mapping[str, Any]) -> bool:
    if not isinstance(attempts, list):
        return False
    project = brief.get("project")
    branch = brief.get("branch")
    wanted = {
        path
        for path in (_norm_path(item) for item in brief.get("assigned_files") or [])
        if path
    }
    if not wanted:
        return False
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        state = str(attempt.get("state") or attempt.get("status") or "").lower()
        if state not in _RUNNING_STATES:
            continue
        if project and attempt.get("project") not in (None, project):
            continue
        if branch and attempt.get("branch") not in (None, branch):
            continue
        leased = {
            path
            for path in (
                _norm_path(item) for item in attempt.get("assigned_files") or []
            )
            if path
        }
        if wanted & leased:
            return True
    return False


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _status(client: Any, args: Mapping[str, Any]) -> str:
    op = args.get("op")
    if op not in _STATUS_OPS:
        return _err(
            "invalid_argument",
            "op must be one of: " + ", ".join(sorted(_STATUS_OPS)),
            "Pass a supported status op.",
        )
    if op == "health":
        return _ok(op=op, health=client.health())
    if op == "capacity":
        return _ok(op=op, capacity=client.health())
    if op == "attempts":
        return _ok(op=op, attempts=client.attempts(), untrusted=True)
    if op == "events":
        attempt_id = args.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            return _err(
                "invalid_argument",
                "events requires attempt_id",
                "Pass the attempt id whose events you want.",
            )
        cursor = args.get("cursor")
        return _ok(
            op=op,
            attempt_id=attempt_id,
            cursor=cursor,
            events=_scrub_events(client.events(attempt_id, cursor=cursor)),
            untrusted=True,
        )
    if op == "containers":
        return _ok(op=op, containers=client.containers())
    return _ok(op=op, projects=client.projects())


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _dispatch(client: Any, args: Mapping[str, Any]) -> str:
    raw = args.get("brief")
    if isinstance(raw, str):
        try:
            brief = json.loads(raw)
        except json.JSONDecodeError:
            return _err(
                "brief_invalid",
                "brief is not valid JSON",
                "Pass a JSON object (not a JSON-encoded string of an array).",
            )
    else:
        brief = raw
    if not isinstance(brief, Mapping):
        return _err("brief_invalid", "brief must be an object or JSON object string", _FIX_BRIEF)
    brief = dict(brief)

    problem = _validate_brief(brief)
    if problem:
        return _err("brief_invalid", problem, _FIX_BRIEF)

    code_work = any(
        key in brief for key in ("commands", "deliverables", "acceptance", "assigned_files")
    )
    assigned = brief.get("assigned_files")
    if code_work:
        if (
            not isinstance(assigned, list)
            or not assigned
            or not all(isinstance(item, str) and item.strip() for item in assigned)
        ):
            return _err(
                "brief_invalid",
                "code-work briefs require a non-empty assigned_files list",
                "Add assigned_files with the exact files this worker is allowed to change.",
            )

    project = brief.get("project")
    if isinstance(project, str) and project:
        try:
            projects = client.projects()
        except Exception:
            projects = None
        if projects is not None:
            keys = {key for key in (_project_key(item) for item in projects) if key}
            if keys and project not in keys:
                return _err(
                    "project_unauthorized",
                    "project is not authorized on the launcher",
                    _FIX_PROJECT,
                )

    try:
        raw_attempts = (
            client.attempts_raw()
            if hasattr(client, "attempts_raw")
            else client.attempts()
        )
    except Exception:
        raw_attempts = []
    if _lease_overlap(raw_attempts, brief):
        return _err(
            "lease_overlap",
            "assigned_files overlap a running attempt on the same project and branch",
            "Wait for the running attempt or choose non-overlapping files. Lease "
            "checks are best-effort against live attempts.",
        )

    model = args.get("model") or DEFAULT_MODEL
    if not isinstance(model, str) or not model.strip():
        return _err("invalid_argument", "model must be a non-empty string", "Use a launcher-allowed model id.")
    model = model.strip()
    try:
        health = client.health()
    except Exception:
        health = {}
    allowed_models = health.get("models") if isinstance(health, Mapping) else None
    if isinstance(allowed_models, list) and allowed_models and model not in allowed_models:
        return _err(
            "model_not_allowed",
            "model is not allow-listed on the launcher",
            "Use one of: " + ", ".join(str(item) for item in allowed_models),
        )

    kanban_task_id = args.get("kanban_task_id")
    if kanban_task_id is not None and not isinstance(kanban_task_id, str):
        return _err(
            "invalid_argument",
            "kanban_task_id must be a string",
            "Pass the Hermes task id as a string, or omit it.",
        )

    try:
        data = client.create(
            brief,
            model=model,
            kanban_task_id=kanban_task_id or None,
        )
    except ClientError as exc:
        if exc.code == "project_unauthorized":
            return _err(exc.code, exc.error, _FIX_PROJECT)
        raise
    data = _unwrap_attempt(data)
    out: dict[str, Any] = {"op": "dispatch"}
    for key in ("attempt_id", "state", "task_id", "kanban_task_id"):
        if data.get(key) is not None:
            out[key] = data[key]
    if "kanban_task_id" not in out and kanban_task_id:
        out["kanban_task_id"] = kanban_task_id
    return _ok(**out)


# ---------------------------------------------------------------------------
# attempt
# ---------------------------------------------------------------------------


def _attempt(client: Any, args: Mapping[str, Any]) -> str:
    op = args.get("op")
    if op not in _ATTEMPT_OPS:
        return _err(
            "invalid_argument",
            "op must be one of: " + ", ".join(sorted(_ATTEMPT_OPS)),
            "Pass a supported attempt op.",
        )
    attempt_id = args.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        return _err(
            "invalid_argument",
            "attempt_id is required",
            "Pass the attempt id returned by bundlebees_dispatch.",
        )
    attempt_id = attempt_id.strip()

    if op == "get":
        out: dict[str, Any] = {
            "op": op,
            "attempt": _unwrap_attempt(client.get(attempt_id)),
        }
        result_fn = getattr(client, "result", None)
        if callable(result_fn):
            try:
                result = result_fn(attempt_id)
            except ClientError as exc:
                if exc.code != "no_result_yet":
                    raise
            else:
                out["result"] = result
        return _ok(**out)

    if op == "events":
        cursor = args.get("cursor")
        return _ok(
            op=op,
            attempt_id=attempt_id,
            cursor=cursor,
            events=_scrub_events(client.events(attempt_id, cursor=cursor)),
            untrusted=True,
        )

    if args.get("confirm") is not True:
        return _err(
            "confirm_required",
            f"{op} requires confirm=true",
            "Ask the user first, then retry with confirm=true.",
        )

    if op == "cancel":
        data = _unwrap_attempt(client.cancel(attempt_id))
        out = {"op": op, "attempt_id": attempt_id}
        out.update(data)
        return _ok(**out)

    accepted = args.get("accepted")
    if not isinstance(accepted, bool):
        return _err(
            "review_invalid",
            "review requires accepted to be true or false",
            "Pass accepted=true to approve or accepted=false to reject.",
        )
    note = args.get("note")
    if note is not None and not isinstance(note, str):
        return _err("invalid_argument", "note must be a string", "Pass a short text note or omit it.")
    data = client.review(
        attempt_id,
        accepted=accepted,
        note=scrub(note) if note else None,
    )
    out = {"op": op, "attempt_id": attempt_id, "accepted": accepted}
    out.update(_unwrap_attempt(data))
    return _ok(**out)


# ---------------------------------------------------------------------------
# host prompt
# ---------------------------------------------------------------------------


def _load_template() -> str:
    for path in (_SKILL_TEMPLATE, _STUB_TEMPLATE):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("host setup prompt template is missing")


def _fill_template(template: str, values: Mapping[str, str]) -> str:
    out = template
    for key, value in values.items():
        for pattern in (
            r"{{\s*" + re.escape(key) + r"\s*}}",
            r"\$\{" + re.escape(key) + r"\}",
            r"<" + re.escape(key) + r">",
        ):
            out = re.sub(pattern, lambda _match, text=value: text, out, flags=re.IGNORECASE)
    return out


def _host_prompt(args: Mapping[str, Any]) -> str:
    os_name = args.get("os")
    if os_name not in _OS_VALUES:
        return _err("invalid_argument", "os must be linux, macos, or windows", "Choose a supported OS.")
    reach = args.get("reach")
    if reach not in _REACH_VALUES:
        return _err(
            "invalid_argument",
            "reach must be same-machine, lan, vpn, or ssh",
            "Pass the reach method enum, not an address.",
        )
    provider = args.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return _err("invalid_argument", "provider is required", "Pass a provider name/id such as opencode-go.")
    provider = provider.strip()
    if looks_like_secret(provider) or contains_address(provider):
        return _err("unsafe_input", "provider must not contain a secret or address", "Use a provider name or id only.")

    max_concurrent = args.get("max_concurrent")
    if (
        not isinstance(max_concurrent, int)
        or isinstance(max_concurrent, bool)
        or not 1 <= max_concurrent <= 10
    ):
        return _err(
            "invalid_argument",
            "max_concurrent must be an integer from 1 to 10",
            "Choose a small concurrency limit.",
        )

    repos = args.get("repos")
    if not isinstance(repos, list) or not repos:
        return _err(
            "invalid_argument",
            "repos must be a non-empty list of names or public https git URLs",
            "Pass repo names or public https repository URLs, never credentials.",
        )
    clean_repos: list[str] = []
    for repo in repos:
        if not isinstance(repo, str) or not repo.strip():
            return _err("invalid_argument", "repos entries must be non-empty strings", "Use repo names or public https URLs.")
        repo = repo.strip()
        if looks_like_secret(repo):
            return _err("unsafe_input", "repos must not contain credentials or secrets", "Use a public https git URL with no userinfo.")
        if "://" in repo or repo.startswith("//"):
            if not is_public_git_url(repo):
                return _err(
                    "unsafe_input",
                    "repo URL must be a public https git URL with no credentials",
                    "Use an https URL on a public git host, or just the repo name.",
                )
        else:
            if contains_address(repo):
                return _err("unsafe_input", "repo names must not contain an address", "Use a plain repo name or a public https URL.")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", repo):
                return _err("invalid_argument", "repo name has unsupported characters", "Use owner/name or a public https URL.")
        clean_repos.append(repo)

    branch_rules = args.get("branch_rules", "")
    if branch_rules is None:
        branch_rules = ""
    if not isinstance(branch_rules, str):
        return _err("invalid_argument", "branch_rules must be text", "Pass short branch rules or omit them.")
    if branch_rules.strip() and (
        looks_like_secret(branch_rules) or contains_address(branch_rules)
    ):
        return _err(
            "unsafe_input",
            "branch_rules must not contain secrets or addresses",
            "Describe branch rules without credentials, URLs, or host addresses.",
        )

    prompt = _fill_template(
        _load_template(),
        {
            "os": os_name,
            "reach": reach,
            "provider": provider,
            "repos": "\n".join(f"- {repo}" for repo in clean_repos),
            "max_concurrent": str(max_concurrent),
            "branch_rules": branch_rules.strip()
            or "Follow the repository's documented branch and review rules.",
        },
    )
    return _ok(prompt=prompt)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": parameters,
    }


_STATUS_SCHEMA = _schema(
    "bundlebees_status",
    "Check worker launcher health, capacity, attempts, events, containers, or authorized projects. Never returns tokens or hosts.",
    {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": sorted(_STATUS_OPS),
                "description": "Status view.",
            },
            "attempt_id": {"type": "string", "description": "Attempt id (events)."},
            "cursor": {"type": "integer", "description": "Event cursor."},
        },
        "required": ["op"],
        "additionalProperties": False,
    },
)

_DISPATCH_SCHEMA = _schema(
    "bundlebees_dispatch",
    "Validate a bee brief (schema, authorized project, file leases) and start a worker. Does not apply patches, run tests, or deploy.",
    {
        "type": "object",
        "properties": {
            "brief": {
                "type": ["object", "string"],
                "description": "Brief object or JSON object string; validated in code.",
            },
            "model": {
                "type": "string",
                "description": "Launcher-allowed model id; defaults to deepseek-v4.1-flash.",
            },
            "kanban_task_id": {
                "type": "string",
                "description": "Optional Hermes kanban task id to record and return.",
            },
        },
        "required": ["brief"],
        "additionalProperties": False,
    },
)

_ATTEMPT_SCHEMA = _schema(
    "bundlebees_attempt",
    "Get, stream events for, cancel, or review one bee attempt. cancel and review require confirm=true.",
    {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": sorted(_ATTEMPT_OPS),
                "description": "Attempt operation.",
            },
            "attempt_id": {"type": "string", "description": "Attempt id."},
            "cursor": {"type": "integer", "description": "Event cursor."},
            "confirm": {
                "type": "boolean",
                "description": "Must be true for cancel and review.",
            },
            "accepted": {"type": "boolean", "description": "Review decision."},
            "note": {"type": "string", "description": "Optional review note; scrubbed."},
        },
        "required": ["op", "attempt_id"],
        "additionalProperties": False,
    },
)

_HOST_PROMPT_SCHEMA = _schema(
    "bundlebees_host_prompt",
    "Generate a paste-ready setup prompt for an agent on a remote host. No secrets or host addresses in, no secrets or host addresses out.",
    {
        "type": "object",
        "properties": {
            "os": {"type": "string", "enum": ["linux", "macos", "windows"]},
            "reach": {
                "type": "string",
                "enum": ["same-machine", "lan", "vpn", "ssh"],
                "description": "Reach method, never an address.",
            },
            "provider": {"type": "string", "description": "Provider name/id."},
            "repos": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Repo names or public https git URLs.",
            },
            "max_concurrent": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Concurrent workers.",
            },
            "branch_rules": {"type": "string", "description": "Optional branch rules."},
        },
        "required": ["os", "reach", "provider", "repos", "max_concurrent"],
        "additionalProperties": False,
    },
)

_SCHEMAS: tuple[tuple[str, dict[str, Any]], ...] = (
    (_STATUS_SCHEMA["name"], _STATUS_SCHEMA),
    (_DISPATCH_SCHEMA["name"], _DISPATCH_SCHEMA),
    (_ATTEMPT_SCHEMA["name"], _ATTEMPT_SCHEMA),
    (_HOST_PROMPT_SCHEMA["name"], _HOST_PROMPT_SCHEMA),
)

_DESCRIPTIONS = {name: schema["description"] for name, schema in _SCHEMAS}


def _make_handler(ctx: Any, tool_name: str) -> Callable[..., str]:
    def handler(args: Any = None, **kwargs: Any) -> str:
        try:
            data = _coerce_args(args)
            if tool_name == "bundlebees_host_prompt":
                return _host_prompt(data)
            client = _client_for(ctx, kwargs)
            if tool_name == "bundlebees_status":
                return _status(client, data)
            if tool_name == "bundlebees_dispatch":
                return _dispatch(client, data)
            return _attempt(client, data)
        except ClientError as exc:
            return json.dumps(exc.as_envelope())
        except Exception as exc:  # noqa: BLE001 - tool boundary must always answer
            return _err(
                "internal_error",
                str(exc) or "unexpected error",
                "Retry once; if it persists, check the launcher configuration and Hermes logs.",
            )

    return handler


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


def register_tools(ctx: Any) -> None:
    for name, schema in _SCHEMAS:
        _call_register_tool(
            ctx.register_tool,
            name=name,
            toolset="bundlebees",
            schema=schema,
            handler=_make_handler(ctx, name),
            description=_DESCRIPTIONS[name],
            emoji="🐝",
        )
