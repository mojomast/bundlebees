"""The five model-facing bundlebees tools.

Every handler returns ``json.dumps(envelope)`` on success, validation failure,
and exception. Settings come from ``ctx.get_config(key, default)``; the token
comes only from ``agent.secret_scope.get_secret``. Target URLs never come from
tool arguments.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from datetime import datetime, timezone
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
from . import contract_drift as _contract_drift
from . import contracts as _contracts
from . import attribution as _attribution
from . import flags
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
_STATUS_ATTEMPTS_DEFAULT_LIMIT = 20
_STATUS_ATTEMPTS_MAX_LIMIT = 200
_STATUS_ATTEMPT_COMPACT_KEYS = (
    "attempt_id",
    "state",
    "project",
    "branch",
    "model",
    "updated_at",
)
_ATTEMPT_OPS = {"get", "events", "cancel", "review"}
_OS_VALUES = {"linux", "macos", "windows"}
_REACH_VALUES = {"same-machine", "lan", "vpn", "ssh"}
_RUNNING_STATES = {"running", "working", "active", "started"}
# The terminal attempt states the tool path writes back to the outbox; kept in
# step with agent_consumer.TOOL_TERMINAL_STATES (imported lazily in
# _tool_write_back to avoid a module-import cycle).
_TOOL_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}
# The contract gate's provenance/acceptance checks run in the live dispatch path
# for visibility only. This constant is the switch: there is no setting, so
# turning enforcement on is a deliberate code change, reviewed like any other.
CONTRACT_GATE_MODE = "report_only"
#: report-only.jsonl rotates to report-only.jsonl.1 past this size (one backup).
REPORT_ONLY_MAX_BYTES = 1_048_576

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


def _attempt_sort_key(attempt: Mapping[str, Any]) -> tuple[str, str]:
    updated = attempt.get("updated_at") or attempt.get("created_at") or ""
    created = attempt.get("created_at") or ""
    return (str(updated), str(created))


def _compact_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: attempt[key]
        for key in _STATUS_ATTEMPT_COMPACT_KEYS
        if key in attempt and attempt[key] is not None
    }


def _attempt_state(attempt: Mapping[str, Any]) -> str:
    return str(attempt.get("state") or attempt.get("status") or "").strip().lower()


def _attempts_limit(value: Any) -> tuple[int | None, str | None]:
    """Return (limit, error) for the requested row count."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None, _err(
            "invalid_argument",
            "limit must be a positive integer",
            "Pass a small limit; the list is capped at "
            f"{_STATUS_ATTEMPTS_MAX_LIMIT}.",
        )
    return min(value, _STATUS_ATTEMPTS_MAX_LIMIT), None


def _attempts_state(value: Any) -> tuple[str | None, str | None]:
    """Return (state, error); a missing value means no filter."""
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, _err(
            "invalid_argument",
            "state must be a non-empty string",
            "Pass an attempt state such as running or succeeded, or omit it.",
        )
    return value.strip().lower(), None


def _attempts_detail(value: Any) -> tuple[bool | None, str | None]:
    """Return (detail, error) for the compact/full row switch."""
    if not isinstance(value, bool):
        return None, _err(
            "invalid_argument",
            "detail must be a boolean",
            "Pass true for full public rows or false for compact rows.",
        )
    return value, None


def _attempts_rows(raw: Any, state: str | None) -> list[Mapping[str, Any]]:
    """Return the public attempt rows, filtered by state when one is given."""
    if isinstance(raw, (list, tuple)):
        attempts = [item for item in raw if isinstance(item, Mapping)]
    else:
        attempts = []
    if state is None:
        return attempts
    return [item for item in attempts if _attempt_state(item) == state]


def _status_attempts(client: Any, args: Mapping[str, Any]) -> str:
    limit, error = _attempts_limit(args.get("limit", _STATUS_ATTEMPTS_DEFAULT_LIMIT))
    if error is not None:
        return error
    state, error = _attempts_state(args.get("state"))
    if error is not None:
        return error
    detail, error = _attempts_detail(args.get("detail", False))
    if error is not None:
        return error

    attempts = _attempts_rows(client.attempts(), state)
    attempts.sort(key=_attempt_sort_key, reverse=True)
    total = len(attempts)
    page = attempts[:limit]
    rows = [dict(item) for item in page] if detail else [_compact_attempt(item) for item in page]
    return _ok(
        op="attempts",
        attempts=rows,
        total=total,
        truncated=total > len(rows),
        untrusted=True,
    )


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
        return _status_attempts(client, args)
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
    problems = _contract_drift.check_brief_drift(brief)
    if problems:
        out["brief_schema_drift"] = _contract_drift.record_brief_schema_drift(
            state_dir, brief, problems
        )
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


def _contract_criterion_ids(contract: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    for criterion in contract.get("acceptance_criteria") or []:
        if isinstance(criterion, Mapping) and isinstance(criterion.get("id"), str):
            ids.append(criterion["id"])
    return ids


def _report_only_gate(
    validated: Mapping[str, Any], brief: Mapping[str, Any]
) -> dict[str, Any]:
    """Run the provenance/acceptance checks as report-only findings.

    Nothing here can refuse the dispatch: a :class:`ContractRefusal` becomes a
    would-refuse entry, and any other exception becomes a ``gate_error`` entry.
    """
    packet = brief.get("packet")
    evidence = brief.get("evidence")
    would_refuse: list[dict[str, Any]] = []
    provenance: Any = None
    try:
        provenance = _contracts.check_provenance(validated, packet=packet)
    except _contracts.ContractRefusal as exc:
        would_refuse.append({"code": exc.code, "error": exc.error, "field": exc.field})
    except Exception as exc:  # noqa: BLE001 - report-only must never refuse
        would_refuse.append(
            {"code": "gate_error", "error": scrub(str(exc)), "field": None}
        )
    acceptance_ids = _contract_criterion_ids(validated)
    try:
        acceptance_ids = _contracts.check_acceptance_ids(
            validated, brief=brief, packet=packet, evidence=evidence
        )
    except _contracts.ContractRefusal as exc:
        would_refuse.append({"code": exc.code, "error": exc.error, "field": exc.field})
    except Exception as exc:  # noqa: BLE001 - report-only must never refuse
        would_refuse.append(
            {"code": "gate_error", "error": scrub(str(exc)), "field": None}
        )
    return {
        "mode": CONTRACT_GATE_MODE,
        "would_refuse": would_refuse,
        "acceptance_ids": acceptance_ids,
        "provenance": provenance,
    }


def _record_gate_findings(
    state_dir: Path | None, brief: Mapping[str, Any], acdd: Mapping[str, Any]
) -> None:
    """Append report-only findings to ``<state_dir>/contract-gate/report-only.jsonl``.

    Best effort: a recording failure must never block or fail the dispatch.
    """
    gate = acdd.get("gate")
    if state_dir is None or not isinstance(gate, Mapping):
        return
    findings = gate.get("would_refuse")
    if not isinstance(findings, list) or not findings:
        return
    at = datetime.now(timezone.utc).isoformat()
    contract_id = acdd.get("contract_id")
    contract_sha256 = acdd.get("contract_sha256")
    project = brief.get("project")
    directory = Path(state_dir) / "contract-gate"
    path = directory / "report-only.jsonl"
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            if path.stat().st_size >= REPORT_ONLY_MAX_BYTES:
                os.replace(path, directory / "report-only.jsonl.1")
        except FileNotFoundError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            for finding in findings:
                if not isinstance(finding, Mapping):
                    continue
                record = {
                    "at": at,
                    "mode": CONTRACT_GATE_MODE,
                    "contract_id": contract_id,
                    "contract_sha256": contract_sha256,
                    "project": project,
                    "code": finding.get("code"),
                    "error": finding.get("error"),
                    "field": finding.get("field"),
                }
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 - recording must never abort a dispatch
        return


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
        "gate": _report_only_gate(validated, brief),
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


def _gate(
    args: Mapping[str, Any],
    state_dir: Path | None = None,
    *,
    require_contract: bool = False,
) -> str | tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run the dispatch gates without dispatching.

    Returns an error envelope string when a gate refuses, otherwise
    ``(brief, acdd, assessed)`` where ``acdd`` is the contract envelope block
    and ``assessed`` is the ``risk.effective_tier`` result. Side-effect free:
    it never builds a client, opens a socket or writes into ``state_dir``.
    """
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

    # RISK TIERS (enforced, fail closed): a declared tier below the path-derived floor is a
    # false declaration and is refused; a high-tier brief (sensitive, invalid or unknown paths,
    # or a human-raised tier) must carry a frozen ACDD contract so test-first provenance exists.
    from . import risk as _risk

    risk_block = brief.get("risk")
    declared = risk_block.get("tier") if isinstance(risk_block, Mapping) else None
    assessed = _risk.effective_tier(declared, brief.get("assigned_files"))
    if assessed.get("raised"):
        return _err(
            "risk_tier_below_floor",
            f"declared risk tier {declared!r} is below the path-derived floor {assessed.get('floor')!r}",
            "Declare the floor tier or higher (a human may raise a tier, never lower it), or "
            "narrow assigned_files.",
        )
    if assessed.get("tier") == "high":
        if _contract.contract_from_brief(brief) is None:
            return _err(
                "contract_missing",
                "high risk tier: " + "; ".join(str(r) for r in assessed.get("reasons", [])[:3])
                + " - a high-tier brief needs a frozen ACDD contract",
                "Attach a frozen acdd/1 contract under brief.contract before dispatch, or narrow "
                "assigned_files to lower the tier.",
            )
        require_contract = True

    # RESEARCH-BEFORE-RETRY: a retry of a recorded failure is refused before
    # any launcher call until accepted findings exist for that failure id.
    # Imported lazily so the tool module's import graph stays free of queues.
    from . import research_gate

    gate_refusal = research_gate.require_findings(state_dir, brief)
    if gate_refusal is not None:
        return _err(
            str(gate_refusal.get("code") or "research_required"),
            str(gate_refusal.get("error") or "research is required before retrying"),
            str(gate_refusal.get("fix") or "Record accepted findings before retrying."),
        )

    acdd = _dispatch_acdd(brief, require_contract)
    if isinstance(acdd, str):
        return acdd

    return brief, acdd, assessed


def _gated_failure_id(brief: Mapping[str, Any], state_dir: Path | None) -> str | None:
    """The failure id the research gate gated: explicit first, else retry_of."""

    explicit = brief.get("failure_id")
    if explicit:
        return explicit.strip() if isinstance(explicit, str) else ""
    retry_of = brief.get("retry_of")
    if state_dir is None or not isinstance(retry_of, str) or not retry_of.strip():
        return None
    from . import research_gate as rg

    failure = rg._read_json(rg.failure_path(state_dir, retry_of.strip()))
    if failure is None:
        return None
    return str(failure.get("failure_id") or "")


def _supplied_findings(brief: Mapping[str, Any], fid: str) -> Mapping[str, Any] | None:
    supplied = brief.get("findings")
    if not isinstance(supplied, Mapping):
        return None
    if str(supplied.get("failure_id") or "") != fid:
        return None
    return supplied


def _attach_accepted_findings(
    brief: dict[str, Any], state_dir: Path | None
) -> dict[str, Any] | str:
    """Attach the gated failure's accepted findings to the brief, or refuse.

    The attach is dark by default: with ``ATTACH_FINDINGS`` not exactly ``1``
    the brief is returned untouched, so the research gate still demands an
    accepted artifact while no state copy travels to the worker. A
    caller-supplied findings block for the gated failure stays authoritative
    and is never replaced by the state copy.
    """
    if not flags.enabled(flags.FLAG_ATTACH_FINDINGS):
        return brief
    return _attach_state_findings(brief, state_dir)


def _attach_state_findings(
    brief: dict[str, Any], state_dir: Path | None
) -> dict[str, Any] | str:
    """The enabled path: read the gated artifact, cap it, copy it onto *brief*.

    The research gate has already proven an accepted artifact exists, and the
    launcher renders ``brief.findings`` into the worker prompt, so the dispatch
    carries the same artifact the gate validated. A brief with neither
    ``failure_id`` nor ``retry_of`` carries none. An artifact beyond
    :data:`bundlebees_plugin.research_gate.ATTACH_MAX_ENTRIES` entries or
    :data:`bundlebees_plugin.research_gate.ATTACH_MAX_BYTES` bytes is refused
    with a recorded reason instead of trimmed.
    """
    if not brief.get("failure_id"):
        if not brief.get("retry_of"):
            return brief
    from . import research_gate as rg

    try:
        fid = _gated_failure_id(brief, state_dir)
    except rg.ResearchGateError as exc:
        return _err(exc.code, exc.message, exc.fix)
    if not fid:
        return brief
    if _supplied_findings(brief, fid) is not None:
        return brief
    if state_dir is None:
        return _err(
            "not_configured",
            "the plugin data directory is unavailable, so the accepted findings cannot be attached",
            "Run bundlebees inside Hermes so plugin_data_dir is available; nothing was dispatched.",
        )
    try:
        artifact = rg._read_json(rg.findings_path(state_dir, fid))
    except rg.ResearchGateError as exc:
        return _err(exc.code, exc.message, exc.fix)
    if artifact is None:
        return _err(
            "research_required",
            f"no accepted findings artifact for failure {fid}",
            "Record accepted findings before retrying; nothing was dispatched.",
        )
    over_cap = rg.attachment_cap(artifact)
    if over_cap is not None:
        return _err(
            "findings_too_large",
            f"accepted findings for failure {fid} exceed the attach cap "
            f"({over_cap['entries']} entries, {over_cap['bytes']} bytes; "
            f"reason={over_cap['reason']}; limits {over_cap['max_entries']} entries, "
            f"{over_cap['max_bytes']} bytes)",
            "Record a smaller accepted findings artifact before retrying; nothing was dispatched.",
        )
    attached = dict(brief)
    attached["findings"] = artifact
    return attached


def _dispatch(
    client: Any,
    args: Mapping[str, Any],
    state_dir: Path | None = None,
    *,
    require_contract: bool = False,
) -> str:
    gated = _gate(args, state_dir, require_contract=require_contract)
    if isinstance(gated, str):
        return gated
    brief, acdd, _assessed = gated

    if state_dir is None:
        return _err(
            "not_configured",
            "the plugin data directory is unavailable, so the coordinator lease cannot be recorded",
            "Run bundlebees inside Hermes so plugin_data_dir is available; nothing was dispatched.",
        )

    attached = _attach_accepted_findings(brief, state_dir)
    if isinstance(attached, str):
        return attached
    brief = attached

    _record_gate_findings(state_dir, brief, acdd)

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

    # A backend may expose attempts_raw (preferred) or attempts; a minimal
    # WorkerBackend with only create + result exposes NEITHER. "Neither" means
    # the adapter has no attempts capability at all, so there are no live
    # attempts to overlap with and dispatch proceeds. Once the capability
    # EXISTS, an unreadable or malformed read still fails closed below.
    reader: Any = None
    for capability in ("attempts_raw", "attempts"):
        candidate = getattr(client, capability, None)
        if callable(candidate):
            reader = candidate
            break
    if reader is None:
        raw_attempts: list[Any] = []
    else:
        try:
            raw_attempts = reader()
        except Exception:
            return _err(
                "launcher_unreadable",
                "live attempts are unreadable, so the lease-overlap check cannot run",
                "Restore launcher access and retry; nothing was dispatched.",
            )
    if not isinstance(raw_attempts, list):
        return _err(
            "launcher_unreadable",
            "live attempts did not return a list, so the lease-overlap check cannot run",
            "Fix the launcher response and retry; nothing was dispatched.",
        )
    if _lease_overlap(raw_attempts, brief):
        return _err(
            "lease_overlap",
            "assigned_files overlap a running attempt on the same project and branch",
            "Wait for the running attempt or choose non-overlapping files. Lease "
            "checks require a readable live attempt list.",
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
        worker_id = (facts.get("worker") or facts.get("worker_id")
                     or canary_settings.get("attribution_worker_id") or "")
        ledger_chain = (facts.get("ledger_chain") or facts.get("ledger")
                        or canary_settings.get("attribution_ledger_chain") or "")
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
    "Launcher health, capacity, attempts, events, containers, or authorized projects. No tokens or hosts.",
    {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": sorted(_STATUS_OPS),
                "description": "View.",
            },
            "attempt_id": {"type": "string", "description": "Attempt id for events."},
            "cursor": {"type": "integer", "description": "Event cursor."},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": _STATUS_ATTEMPTS_MAX_LIMIT,
                "description": "Rows returned; default 20, max 200.",
            },
            "state": {
                "type": "string",
                "description": "Filter by attempt state.",
            },
            "detail": {
                "type": "boolean",
                "description": "Full rows, not compact.",
            },
        },
        "required": ["op"],
        "additionalProperties": False,
    },
)

_DISPATCH_SCHEMA = _schema(
    "bundlebees_dispatch",
    "Validate a strict bee brief (schema, full source_rev, file leases) and start a worker. Optional acdd/1 contract enforced. No patches, tests, or deploys.",
    {
        "type": "object",
        "properties": {
            "brief": {
                "type": ["object", "string"],
                "description": "Object or JSON string; validated in code.",
            },
            "model": {
                "type": "string",
                "description": "Allowed model id; omit for the launcher default.",
            },
            "kanban_task_id": {
                "type": "string",
                "description": "Kanban task id to record.",
            },
        },
        "required": ["brief"],
        "additionalProperties": False,
    },
)

_ATTEMPT_SCHEMA = _schema(
    "bundlebees_attempt",
    "Get, stream events, cancel, or review an attempt. cancel and review need confirm=true.",
    {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": sorted(_ATTEMPT_OPS),
                "description": "Action.",
            },
            "attempt_id": {"type": "string", "description": "Attempt id."},
            "cursor": {"type": "integer", "description": "Event cursor."},
            "confirm": {
                "type": "boolean",
                "description": "Required true for cancel/review.",
            },
            "accepted": {"type": "boolean", "description": "Review decision."},
            "note": {"type": "string", "description": "Optional note; scrubbed."},
        },
        "required": ["op", "attempt_id"],
        "additionalProperties": False,
    },
)

_HOST_PROMPT_SCHEMA = _schema(
    "bundlebees_host_prompt",
    "Paste-ready setup prompt for a remote host agent. No secrets or addresses in or out.",
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
    "Preview the model and credential route a brief would take. Starts nothing.",
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "Route kind."},
            "model": {"type": "string", "description": "Model id."},
            "tier": {"type": "string", "description": "Launcher tier."},
            "brief": {
                "type": "object",
                "description": "Only kind, priority, model sent.",
            },
        },
        "additionalProperties": False,
    },
)

_GO_MAX_SCHEMA = _schema(
    "bundlebees_go_max",
    "go-max: when the owner asks to run as much as possible at once ('go max', "
    "'max parallel'), turn it on; the factory then fills free bee slots up to the "
    "shared cap with approved ready slices, per lane, under host and provider "
    "admission control. status explains per-lane counts and why slots stay empty. "
    "Never cancels or approves anything.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["on", "off", "status"], "description": "on, off or status."},
            "ttl_minutes": {"type": "integer", "minimum": 1, "maximum": 1440,
                            "description": "How long go-max stays on (default 480)."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
)

_SCHEMAS: tuple[tuple[str, dict[str, Any]], ...] = (
    (_STATUS_SCHEMA["name"], _STATUS_SCHEMA),
    (_DISPATCH_SCHEMA["name"], _DISPATCH_SCHEMA),
    (_ATTEMPT_SCHEMA["name"], _ATTEMPT_SCHEMA),
    (_HOST_PROMPT_SCHEMA["name"], _HOST_PROMPT_SCHEMA),
    (_ROUTE_PREVIEW_SCHEMA["name"], _ROUTE_PREVIEW_SCHEMA),
    (_GO_MAX_SCHEMA["name"], _GO_MAX_SCHEMA),
)

_DESCRIPTIONS = {name: schema["description"] for name, schema in _SCHEMAS}


def _go_max(ctx: Any, data: Mapping[str, Any], kwargs: Mapping[str, Any]) -> str:
    from . import go_max as go_max_mod

    state_dir = _state_dir_for(ctx)
    if state_dir is None:
        return _err("not_configured", "the plugin data directory is unavailable",
                    "Run inside Hermes with the bundlebees plugin enabled.")
    try:
        client = _client_for(ctx, kwargs)
    except Exception:  # noqa: BLE001 - on/off never need the launcher; status reports it
        client = None
    try:
        settings = settings_from_ctx(ctx)
    except Exception:  # noqa: BLE001
        settings = {}
    try:
        out = go_max_mod.run_action(
            state_dir,
            str(data.get("action") or ""),
            client=client,
            ttl_minutes=data.get("ttl_minutes"),
            by="hermes-chat",
            policy=go_max_mod.Policy.from_settings(settings if isinstance(settings, Mapping) else {}),
        )
    except go_max_mod.GoMaxError as exc:
        return _err(exc.code, exc.message, exc.fix or "Use action on, off or status.")
    return _ok(**out)


def _make_handler(ctx: Any, tool_name: str) -> Callable[..., str]:
    def handler(args: Any = None, **kwargs: Any) -> str:
        client: Any = None
        try:
            data = _coerce_args(args)
            if tool_name == "bundlebees_host_prompt":
                return _host_prompt(data)
            if tool_name == "bundlebees_go_max":
                return _go_max(ctx, data, kwargs)
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
    # A profile must opt in to exposing its local history to Hermes. No
    # database is opened and no launcher capability is loaded at registration.
    if getattr(ctx, 'get_config', lambda k, d: d)('rails_db', ''):
        from . import rails_tool
        rails_tool.register_tools(ctx)
