"""The five model-facing bundlebees tools.

Every handler returns ``json.dumps(envelope)`` on success, validation failure,
and exception. Settings come from ``ctx.get_config(key, default)``; the token
comes only from ``agent.secret_scope.get_secret``. Target URLs never come from
tool arguments.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .brief import BriefError, validate_brief
from .client import (
    ClientError,
    client_from_config,
    public_result,
    public_route,
)
from .settings import settings_from_ctx
from .adapters import UnsupportedAdapter
from . import contract as _contract
from . import attribution as _attribution
from . import leases as _leases
from . import metrics as _metrics
from . import revision as _revision
from .security import (
    config_flag,
    contains_address,
    is_public_git_url,
    looks_like_secret,
    scrub,
)

TOOL_NAMES = (
    "bundlebees_status",
    "bundlebees_dispatch",
    "bundlebees_attempt",
    "bundlebees_host_prompt",
    "bundlebees_route_preview",
)

_MODULE_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
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
# The terminal attempt states the tool path writes back to the outbox; kept in
# step with agent_consumer.TOOL_TERMINAL_STATES (imported lazily in
# _tool_write_back to avoid a module-import cycle).
_TOOL_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}

_FIX_PROJECT = (
    "The owner must add this project to the launcher config and restart the "
    "launcher; this tool does not restart it."
)
_FIX_BRIEF = (
    "Pass an object with objective, a full 40- or 64-character hex source_rev, "
    "non-empty repository-relative assigned_files, and acceptance criteria or "
    "commands."
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
    """Settings via ``ctx.get_config(key, default)`` for each declared key."""
    return settings_from_ctx(ctx)


def _require_contract(ctx: Any) -> bool:
    """The optional ``require_contract`` setting (default false)."""
    return config_flag(_ctx_config(ctx).get("require_contract"), False)


def _client_for(ctx: Any, kwargs: Mapping[str, Any]) -> Any:
    injected = kwargs.get("client")
    if injected is not None:
        return injected
    injected = getattr(ctx, "bundlebees_client", None)
    if injected is not None:
        return injected
    from .adapters import adapter_from_config

    return adapter_from_config(_ctx_config(ctx), state_dir=_state_dir_for(ctx))


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


def _terminal_attempt_state(attempt: Any) -> str | None:
    """The attempt's state when it is a terminal tool-path value, else None."""

    if not isinstance(attempt, Mapping):
        return None
    state = str(attempt.get("state") or attempt.get("status") or "").strip().lower()
    if state in _TOOL_TERMINAL_STATES:
        return state
    return None


def _result_usage(result: Any) -> Any:
    """The usage mapping a result envelope carries, when it does."""

    if not isinstance(result, Mapping):
        return None
    observed = result.get("observed")
    if isinstance(observed, Mapping) and isinstance(observed.get("usage"), Mapping):
        return observed["usage"]
    if isinstance(result.get("usage"), Mapping):
        return result["usage"]
    return None


def _result_artifact(result: Any) -> Any:
    """The artifact digest a result envelope carries, when it does.

    A value that is not a digest is dropped later by the write-back's own
    validation, never echoed.
    """

    if not isinstance(result, Mapping):
        return None
    observed = result.get("observed")
    if isinstance(observed, Mapping) and isinstance(observed.get("sha256"), str):
        return observed["sha256"]
    if isinstance(result.get("artifact_sha256"), str):
        return result["artifact_sha256"]
    return None


def _tool_write_back(
    state_dir: Path | None,
    attempt_id: str,
    attempt: Any,
    *,
    usage: Any = None,
    result_available: bool | None = None,
    artifact_sha256: Any = None,
    branch: Any = None,
) -> dict[str, Any] | None:
    """Best-effort tool-path completion write-back for one terminal observation.

    When the launcher reports a terminal attempt state, the same canonical
    landing event the job path emits is written through the coordinator outbox
    (``agent_consumer.record_tool_completion``). The emission is deliberately
    not part of the op's success: every refusal comes back as a value, a write
    failure is swallowed into a reported refusal, and the op envelope keeps the
    outcome as its own ``write_back`` fact so a missing event is never silently
    read as a worker failure. Duplicate observations of the same terminal state
    deduplicate onto one deterministic event id (``TOOL_TRANSITION_ID``).
    """

    if state_dir is None:
        return None
    state = _terminal_attempt_state(attempt)
    if state is None:
        return None
    try:
        from . import agent_consumer as _agent_consumer

        return dict(
            _agent_consumer.record_tool_completion(
                Path(state_dir),
                attempt_id,
                state=state,
                branch=branch if isinstance(branch, str) and branch.strip() else None,
                usage=usage if isinstance(usage, Mapping) else None,
                result_available=result_available,
                artifact_sha256=artifact_sha256,
                transition_id=_agent_consumer.TOOL_TRANSITION_ID,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the completion path must never break
        return {
            "emitted": False,
            "code": "write_back_failed",
            "error": type(exc).__name__,
        }


def _attempt_write_back(
    state_dir: Path | None,
    attempt_id: str,
    attempt: Any,
    *,
    result: Any = None,
    result_available: bool | None = None,
) -> dict[str, Any]:
    """The envelope fragment carrying one write-back outcome (or nothing)."""

    outcome = _tool_write_back(
        state_dir,
        attempt_id,
        attempt,
        usage=_result_usage(result),
        result_available=result_available,
        artifact_sha256=_result_artifact(result),
        branch=attempt.get("branch") if isinstance(attempt, Mapping) else None,
    )
    return {"write_back": outcome} if outcome is not None else {}


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


def _dispatched(
    client: Any,
    brief: Mapping[str, Any],
    data: Mapping[str, Any],
    args: Mapping[str, Any],
    state_dir: Path | None = None,
    *,
    acdd: Mapping[str, Any] | None = None,
) -> str:
    """Build the dispatch success envelope.

    Hook point: the coordinator can record the lease here after
    ``client.create`` returns, using the normalised *brief* and *client*.
    """
    out: dict[str, Any] = {"op": "dispatch"}
    for key in ("attempt_id", "state", "task_id", "kanban_task_id"):
        if data.get(key) is not None:
            out[key] = data[key]
    kanban_task_id = args.get("kanban_task_id")
    if "kanban_task_id" not in out and kanban_task_id:
        out["kanban_task_id"] = kanban_task_id
    if acdd is not None:
        out["acdd"] = dict(acdd)
    out.update(_record_dispatch(state_dir, data.get("attempt_id"), brief))
    return _ok(**out)


def _record_dispatch(state_dir: Path | None, attempt_id: Any, brief: Mapping[str, Any]) -> dict[str, Any]:
    """Write the immutable coordinator lease and a metrics event for a new attempt.

    The lease is the only file-scope authority ``hermes bundlebees verify``
    accepts; worker-returned metadata never widens it. A failure here does not
    hide the started attempt, but verify will refuse it (fail closed).
    """
    if state_dir is None or not isinstance(attempt_id, str) or not attempt_id:
        return {
            "lease_recorded": False,
            "lease_fix": "Record the brief with `hermes bundlebees lease <attempt_id> --brief <file>` before verify.",
        }
    try:
        _leases.record_lease(state_dir, attempt_id, brief)
    except _leases.LeaseError as exc:
        return {"lease_recorded": False, "lease_code": exc.code, "lease_fix": exc.fix}
    try:
        _metrics.append_event(
            state_dir,
            str(brief.get("run_id") or "default"),
            "dispatch",
            attempt_id=attempt_id,
            **({"retry_of": brief["retry_of"]} if brief.get("retry_of") else {}),
        )
    except Exception:  # noqa: BLE001 - metrics are best-effort evidence
        pass
    return {"lease_recorded": True}


def _dispatch_acdd(
    brief: Mapping[str, Any], require_contract: bool
) -> dict[str, Any] | str:
    """Enforce the optional ACDD contract before any launcher call.

    Returns the ``acdd`` envelope block, or an error envelope string when the
    contract is missing, invalid, unfrozen, changed, stale or widened.
    """
    contract = _contract.contract_from_brief(brief)
    if contract is None:
        if require_contract:
            return _err(
                "contract_missing",
                "brief has no ACDD contract but this project requires one",
                "Attach a frozen acdd/1 contract under brief.contract before dispatch.",
            )
        return {
            "mode": "legacy",
            "note": "no ACDD contract; TDD evidence not required",
        }
    try:
        validated = _contract.validate_contract(contract)
        digest = _contract.check_frozen(contract)
        _contract.check_brief_against_contract(brief, validated)
    except _contract.ContractError as exc:
        return _err(exc.code, exc.error, exc.fix)
    tdd = validated.get("tdd")
    return {
        "mode": "contract",
        "contract_id": validated.get("id"),
        "contract_sha256": digest,
        "tdd_applicable": bool(tdd.get("applicable")) if isinstance(tdd, Mapping) else False,
    }


def _state_dir_for(ctx: Any) -> Path | None:
    """Plugin data dir for leases, waves and metrics (profile-aware)."""
    injected = getattr(ctx, "bundlebees_state_dir", None)
    if injected:
        return Path(str(injected))
    try:
        from .cli import plugin_state_dir
    except Exception:
        return None
    return plugin_state_dir()


def _dispatch(
    client: Any,
    args: Mapping[str, Any],
    state_dir: Path | None = None,
    *,
    require_contract: bool = False,
) -> str:
    raw = args.get("brief")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return _err(
                "brief_invalid",
                "brief is not valid JSON",
                "Pass a JSON object (not a JSON-encoded string of an array).",
            )
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        return _err("brief_invalid", "brief must be an object or JSON object string", _FIX_BRIEF)

    try:
        brief = validate_brief(parsed)
    except BriefError as exc:
        return _err("brief_invalid", exc.error, exc.fix)

    acdd = _dispatch_acdd(brief, require_contract)
    if isinstance(acdd, str):
        return acdd

    if state_dir is None:
        return _err(
            "not_configured",
            "the plugin data directory is unavailable, so the coordinator lease cannot be recorded",
            "Run bundlebees inside Hermes so plugin_data_dir is available; nothing was dispatched.",
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

    # No model means "the launcher decides": its default is the cheapest usable route on that host
    # (Zoo's free pass when a zoo key is readable, else the host's configured model). Substituting a
    # plugin-side default here silently overrides that preference for every dispatch.
    model = args.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return _err("invalid_argument", "model must be a non-empty string",
                        "Use a launcher-allowed model id, or omit it to let the launcher choose.")
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

    # E9 pre-dispatch attribution (owner decision: canary projects only). The intent is written
    # BEFORE the POST so the route facts are durable even if the dispatch dies mid-flight; the
    # returned attempt is bound to it on success. Other projects keep current behaviour.
    attribution_intent = None
    canary_settings = args.get("_settings") if isinstance(args.get("_settings"), Mapping) else {}
    if _attribution.applies_to(canary_settings, brief.get("project")):
        try:
            facts = client.health() if hasattr(client, "health") else {}
        except Exception:  # noqa: BLE001 - an unreadable route means "cannot attribute"
            facts = {}
        facts = facts if isinstance(facts, Mapping) else {}
        endpoint = (facts.get("endpoint") or getattr(client, "base_url", "") or
                    getattr(client, "endpoint", "") or canary_settings.get("launcher_url") or "")
        worker_id = facts.get("worker") or facts.get("worker_id") or canary_settings.get("worker_id") or ""
        ledger_chain = (facts.get("ledger_chain") or facts.get("ledger") or
                        canary_settings.get("ledger_chain") or "")
        try:
            intent = _attribution.intent_for(
                endpoint=endpoint, worker_id=worker_id, ledger_chain=ledger_chain,
                job_id=brief.get("job_id") or args.get("job_id") or canary_settings.get("job_id") or "",
                project=brief.get("project"), contract=brief.get("contract") or "",
                revision=brief.get("source_rev") or "", client=type(client).__name__)
            attribution_intent = _attribution.record_intent(state_dir, intent)
        except _attribution.AttributionError as exc:
            return _err(str(exc.code), str(exc.message), str(exc.fix))
        except Exception as exc:  # noqa: BLE001 - never dispatch unattributed inside the canary
            return _err("attribution_unavailable",
                        f"the canary dispatch could not record its attribution intent: {exc}",
                        "Fix the attribution store before dispatching; nothing was sent.")

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
    if attribution_intent is not None and data.get("attempt_id"):
        try:
            _attribution.bind_attempt(state_dir, attribution_intent["intent_id"], data.get("attempt_id"))
        except Exception:  # noqa: BLE001 - the intent stays unbound; the audit will report adopted
            pass
    return _dispatched(
        client, brief, _unwrap_attempt(data), args, state_dir, acdd=acdd
    )


# ---------------------------------------------------------------------------
# attempt
# ---------------------------------------------------------------------------


def _attempt(client: Any, args: Mapping[str, Any], state_dir: Path | None = None) -> str:
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
        result_payload: Any = None
        result_available: bool | None = None
        if callable(result_fn):
            try:
                result = result_fn(attempt_id)
            except ClientError as exc:
                if exc.code != "no_result_yet":
                    raise
                result_available = False
            else:
                # Model-visible: always reduce to the public view so no client can leak a patch here.
                out["result"] = public_result(result)
                result_payload = result
                result_available = True
        out.update(
            _attempt_write_back(
                state_dir,
                attempt_id,
                out["attempt"],
                result=result_payload,
                result_available=result_available,
            )
        )
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
        out.update(_attempt_write_back(state_dir, attempt_id, data))
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
    if accepted and (state_dir is None or not _revision.attempt_accepted(state_dir, attempt_id)):
        return _err(
            "integration_required",
            "this attempt is not in an accepted wave yet",
            "The user runs `hermes bundlebees integrate --wave <id> --attempts ...` so the full "
            "suite passes on the combined branch; then retry the review.",
        )
    data = client.review(
        attempt_id,
        accepted=accepted,
        note=scrub(note) if note else None,
    )
    attempt_data = _unwrap_attempt(data)
    out = {"op": op, "attempt_id": attempt_id, "accepted": accepted}
    out.update(attempt_data)
    out.update(_attempt_write_back(state_dir, attempt_id, attempt_data))
    return _ok(**out)


# ---------------------------------------------------------------------------
# route preview
# ---------------------------------------------------------------------------


def _route_preview(client: Any, args: Mapping[str, Any]) -> str:
    """Ask the launcher which route a dispatch would take, starting nothing.

    Type validation mirrors dispatch: ``invalid_argument`` envelopes, and the
    client is never touched for a bad argument. The brief is reduced to the
    only fields the route decision reads (kind, priority, model) before it is
    sent.
    """
    model = args.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return _err(
                "invalid_argument",
                "model must be a non-empty string",
                "Use a launcher-allowed model id, or omit it to let the launcher choose.",
            )
        model = model.strip()
    tier = args.get("tier")
    if tier is not None and not isinstance(tier, str):
        return _err(
            "invalid_argument",
            "tier must be a string",
            "Pass a launcher-supported tier name, or omit it.",
        )
    kind = args.get("kind")
    if kind is not None and not isinstance(kind, str):
        return _err(
            "invalid_argument",
            "kind must be a string",
            "Pass the route kind as a string, or omit it.",
        )
    raw_brief = args.get("brief")
    if isinstance(raw_brief, str):
        try:
            raw_brief = json.loads(raw_brief)
        except json.JSONDecodeError:
            return _err(
                "invalid_argument",
                "brief is not valid JSON",
                "Pass a JSON object with optional kind, priority and model keys, or omit brief.",
            )
    if raw_brief is not None and not isinstance(raw_brief, Mapping):
        return _err(
            "invalid_argument",
            "brief must be an object",
            "Pass an object with optional kind, priority and model keys, or omit brief.",
        )
    brief: dict[str, Any] | None = None
    if raw_brief is not None:
        brief = {
            key: raw_brief[key] for key in ("kind", "priority", "model") if key in raw_brief
        }
    # Model-visible: always reduce to the public view so no client can leak a
    # credential value, a base URL or effective.key here.
    route = public_route(client.route_preview(kind=kind, model=model, tier=tier, brief=brief))
    return _ok(op="route_preview", route=route)


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
    "Validate a strict bee brief (schema, full source_rev, file leases) and start a worker. An optional acdd/1 contract is validated and enforced. Does not apply patches, run tests, or deploy.",
    {
        "type": "object",
        "properties": {
            "brief": {
                "type": ["object", "string"],
                "description": "Brief object or JSON object string; validated in code.",
            },
            "model": {
                "type": "string",
                "description": "Launcher-allowed model id; omit to use the launcher's preferred route.",
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

_ROUTE_PREVIEW_SCHEMA = _schema(
    "bundlebees_route_preview",
    "Preview the model and credential route a brief would take. Starts no attempt.",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "Route kind."},
            "model": {"type": "string", "description": "Model id to preview."},
            "tier": {"type": "string", "description": "Launcher tier."},
            "brief": {
                "type": "object",
                "description": "Only kind, priority and model are sent.",
            },
        },
        "additionalProperties": False,
    },
)

_SCHEMAS: tuple[tuple[str, dict[str, Any]], ...] = (
    (_STATUS_SCHEMA["name"], _STATUS_SCHEMA),
    (_DISPATCH_SCHEMA["name"], _DISPATCH_SCHEMA),
    (_ATTEMPT_SCHEMA["name"], _ATTEMPT_SCHEMA),
    (_HOST_PROMPT_SCHEMA["name"], _HOST_PROMPT_SCHEMA),
    (_ROUTE_PREVIEW_SCHEMA["name"], _ROUTE_PREVIEW_SCHEMA),
)

_DESCRIPTIONS = {name: schema["description"] for name, schema in _SCHEMAS}


def _make_handler(ctx: Any, tool_name: str) -> Callable[..., str]:
    def handler(args: Any = None, **kwargs: Any) -> str:
        client: Any = None
        try:
            data = _coerce_args(args)
            if tool_name == "bundlebees_host_prompt":
                return _host_prompt(data)
            client = _client_for(ctx, kwargs)
            if tool_name == "bundlebees_status":
                return _status(client, data)
            if tool_name == "bundlebees_dispatch":
                try:
                    dispatch_settings = settings_from_ctx(ctx)
                except Exception:  # noqa: BLE001 - no settings means "no canary projects configured"
                    dispatch_settings = {}
                return _dispatch(
                    client,
                    {**data, "_settings": dispatch_settings},
                    _state_dir_for(ctx),
                    require_contract=_require_contract(ctx),
                )
            if tool_name == "bundlebees_route_preview":
                return _route_preview(client, data)
            return _attempt(client, data, _state_dir_for(ctx))
        except ClientError as exc:
            return json.dumps(exc.as_envelope())
        except UnsupportedAdapter as exc:
            return json.dumps(exc.as_envelope())
        except AttributeError as exc:
            if client is None or getattr(exc, "obj", None) is not client:
                return _err(
                    "internal_error",
                    str(exc) or "unexpected error",
                    "Retry once; if it persists, check the launcher configuration and Hermes logs.",
                )
            # A minimal WorkerBackend (create + result only) lacks this capability.
            return _err(
                "unsupported_operation",
                f"the configured worker adapter does not support this operation ({exc.name or 'unknown'})",
                "Use an adapter that implements it (see docs/ADAPTERS.md) or a different op.",
            )
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
