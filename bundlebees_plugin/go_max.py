"""go-max: fill bee slots safely when the owner asks (docs/contracts/go-max.md).

One durable mode record turns the behaviour on for a bounded time.  While it is
on, the resident factory service asks this module, once per pass, how many new
bees it may start and on which provider lane, using the launcher's own view of
every attempt (``GET /v1/capacity``, schema ``hyvv.capacity/1``).

Everything here fails closed: a missing, stale or unreadable input admits zero
new bees and names the reason.  Nothing here cancels, approves, merges or
raises the shared cap; dispatch itself stays with the factory's existing,
pre-approval-gated path.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import grants as grants_mod
from . import model_route
from .risk import effective_tier

__all__ = [
    "MODE_SCHEMA",
    "STATUS_SCHEMA",
    "CAPACITY_SCHEMA",
    "LANES",
    "DEFAULT_TTL_S",
    "MAX_TTL_S",
    "PRESSURE_COOLDOWN_S",
    "GoMaxError",
    "Policy",
    "mode_path",
    "status_path",
    "ledger_path",
    "enable",
    "disable",
    "read_mode",
    "lane_of",
    "route",
    "admit",
    "plan",
    "record_dispatch",
    "dispatched_today",
    "build_status",
    "write_status",
    "read_status",
    "summary_text",
    "default_lane_reader",
    "FactoryGate",
    "route_brief",
    "public_capacity",
]

MODE_SCHEMA = "rizzstack.go-max/1"
STATUS_SCHEMA = "rizzstack.go-max-status/1"
LEDGER_SCHEMA = "rizzstack.go-max-ledger/1"
CAPACITY_SCHEMA = "hyvv.capacity/1"
LANES = ("kimi", "nous", "go", "other")
DEFAULT_TTL_S = 8 * 3600
MAX_TTL_S = 24 * 3600
PRESSURE_COOLDOWN_S = 120
MIN_PER_BEE_RAM_MB = 4096
MIN_PER_BEE_CPUS = 2
#: Reasons that mean the host is under pressure (start a cooldown).
PRESSURE_REASONS = frozenset({"ram_pressure", "cpu_pressure", "docker_unhealthy"})
#: Informational reasons: they bound the fill but are not why slots stay empty.
INFO_REASONS = frozenset({"ramp_step"})
_KIMI_MODELS = frozenset({model_route.COMPLEX_MODEL, "kimi-for-coding"})
_GO_MODELS = frozenset({model_route.DEFAULT_MODEL, "deepseek-v4.1-flash", "deepseek-flash"})


class GoMaxError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = str(code), str(message), str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _shared_cap() -> int:
    value = getattr(grants_mod, "GLOBAL_MAX_BEES", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GoMaxError("cap_unreadable", f"the shared bee cap is unreadable: {value!r}",
                         "Restore grants.GLOBAL_MAX_BEES.")
    return value


def _default_lanes() -> dict[str, int]:
    return {"kimi": 4, "nous": int(model_route.NOUS_MAX_ACTIVE), "go": _shared_cap(), "other": 0}


@dataclass(frozen=True)
class Policy:
    cap: int = field(default_factory=_shared_cap)
    per_bee_ram_mb: int = MIN_PER_BEE_RAM_MB
    per_bee_cpus: int = MIN_PER_BEE_CPUS
    host_ram_reserve_mb: int = 4096
    cpu_busy_max_pct: float = 85.0
    lane_max: Mapping[str, int] = field(default_factory=_default_lanes)
    step_max: int = 4
    daily_attempt_budget: int = 48
    capacity_max_age_s: float = 120.0

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None) -> "Policy":
        """Build a policy from plugin settings; never below the per-bee floor or above the cap."""

        base = cls()
        data = settings if isinstance(settings, Mapping) else {}

        def num(key: str, default: Any, kind: type = int) -> Any:
            raw = data.get(key)
            if raw is None or raw == "" or isinstance(raw, bool):
                return default
            try:
                return kind(raw)
            except (TypeError, ValueError):
                return default

        lanes = dict(base.lane_max)
        for lane in ("kimi", "nous", "go"):
            lanes[lane] = max(0, min(base.cap, num(f"go_max_lane_{lane}", lanes[lane])))
        return replace(
            base,
            cap=max(1, min(base.cap, num("go_max_cap", base.cap))),
            per_bee_ram_mb=max(MIN_PER_BEE_RAM_MB, num("go_max_per_bee_ram_mb", base.per_bee_ram_mb)),
            per_bee_cpus=max(MIN_PER_BEE_CPUS, num("go_max_per_bee_cpus", base.per_bee_cpus)),
            host_ram_reserve_mb=max(0, num("go_max_host_ram_reserve_mb", base.host_ram_reserve_mb)),
            cpu_busy_max_pct=max(1.0, min(100.0, num("go_max_cpu_busy_max_pct", base.cpu_busy_max_pct, float))),
            lane_max=lanes,
            step_max=max(1, min(base.cap, num("go_max_step", base.step_max))),
            daily_attempt_budget=max(0, num("go_max_daily_attempt_budget", base.daily_attempt_budget)),
        )

    def public(self) -> dict[str, Any]:
        return {
            "cap": self.cap,
            "per_bee_ram_mb": self.per_bee_ram_mb,
            "per_bee_cpus": self.per_bee_cpus,
            "host_ram_reserve_mb": self.host_ram_reserve_mb,
            "cpu_busy_max_pct": self.cpu_busy_max_pct,
            "lane_max": dict(self.lane_max),
            "step_max": self.step_max,
            "daily_attempt_budget": self.daily_attempt_budget,
        }


# ---------------------------------------------------------------------------
# Durable files
# ---------------------------------------------------------------------------


def mode_path(state_dir: Any) -> Path:
    return Path(state_dir) / "factory" / "go-max.json"


def status_path(state_dir: Any) -> Path:
    return Path(state_dir) / "factory" / "go-max-status.json"


def ledger_path(state_dir: Any) -> Path:
    return Path(state_dir) / "factory" / "go-max-ledger.jsonl"


def _clock(now: Any = None) -> float:
    if callable(now):
        now = now()
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return float(now)
    return time.time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _write_json(path: Path, body: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2, sort_keys=True, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _safe_by(by: Any) -> str:
    text = "".join(ch for ch in str(by or "owner") if ch.isalnum() or ch in "-_.@ ")[:64].strip()
    return text or "owner"


def enable(state_dir: Any, *, by: str = "owner", ttl_s: float = DEFAULT_TTL_S, now: Any = None) -> dict[str, Any]:
    """Turn go-max on for a bounded time. Dispatches nothing by itself."""

    at = _clock(now)
    ttl = max(60.0, min(float(MAX_TTL_S), float(ttl_s or DEFAULT_TTL_S)))
    body = {
        "schema": MODE_SCHEMA,
        "enabled": True,
        "requested_by": _safe_by(by),
        "requested_at": at,
        "expires_at": at + ttl,
        "requested_at_iso": _iso(at),
        "expires_at_iso": _iso(at + ttl),
    }
    _write_json(mode_path(state_dir), body)
    return body


def disable(state_dir: Any, *, by: str = "owner", now: Any = None) -> dict[str, Any]:
    at = _clock(now)
    body = {"schema": MODE_SCHEMA, "enabled": False, "requested_by": _safe_by(by),
            "requested_at": at, "requested_at_iso": _iso(at)}
    _write_json(mode_path(state_dir), body)
    return body


def read_mode(state_dir: Any, now: Any = None) -> dict[str, Any]:
    """``{"enabled", "reason", ...}``; anything unreadable or expired is off."""

    path = mode_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"enabled": False, "reason": "off"}
    except OSError:
        return {"enabled": False, "reason": "mode_unreadable"}
    try:
        body = json.loads(raw)
    except ValueError:
        return {"enabled": False, "reason": "mode_unreadable"}
    if not isinstance(body, Mapping) or body.get("schema") != MODE_SCHEMA:
        return {"enabled": False, "reason": "mode_unreadable"}
    if body.get("enabled") is not True:
        return {"enabled": False, "reason": "off", "requested_by": body.get("requested_by")}
    expires = body.get("expires_at")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return {"enabled": False, "reason": "mode_unreadable"}
    at = _clock(now)
    if at >= float(expires):
        return {"enabled": False, "reason": "expired", "expired_at": float(expires)}
    return {
        "enabled": True,
        "reason": "on",
        "requested_by": body.get("requested_by"),
        "expires_at": float(expires),
        "expires_at_iso": _iso(float(expires)),
        "remaining_s": round(float(expires) - at),
    }


# ---------------------------------------------------------------------------
# Lanes and routing
# ---------------------------------------------------------------------------


def lane_of(model: Any) -> str:
    """Provider lane of a model id: kimi, nous, go or other."""

    text = str(model or "").strip().lower()
    if not text:
        return "other"
    if text.startswith("nous:"):
        return "nous"
    if text in _KIMI_MODELS or text.startswith("kimi") or text.startswith("kimi:"):
        return "kimi"
    if text.startswith("go:") or text in _GO_MODELS:
        return "go"
    return "other"


def route(
    brief: Mapping[str, Any],
    *,
    failed_attempts: int | None = None,
    allowed_models: Iterable[str] | None = None,
) -> dict[str, Any]:
    """``{model, lane, reasons, source}`` for one brief. Refuses temperature 0."""

    if not isinstance(brief, Mapping):
        raise GoMaxError("brief_unreadable", "the brief is not an object")
    temperature = brief.get("temperature")
    if temperature is not None and not isinstance(temperature, bool):
        try:
            if float(temperature) == 0.0:
                raise GoMaxError("temperature_zero", "the brief asks for temperature 0",
                                 "Remove the temperature; these models must not run at 0.")
        except (TypeError, ValueError):
            pass
    pinned = brief.get("model")
    if isinstance(pinned, str) and pinned.strip():
        return {"model": pinned.strip(), "lane": lane_of(pinned), "reasons": ["pinned"], "source": "pinned"}
    try:
        picked = model_route.select_worker_model(
            brief, failed_attempts=failed_attempts, allowed_models=allowed_models
        )
    except ValueError as exc:
        raise GoMaxError("brief_unreadable", str(exc)) from exc
    model = str(picked.get("model") or "")
    return {"model": model, "lane": lane_of(model), "reasons": list(picked.get("reasons") or []),
            "source": str(picked.get("source") or "rule")}


def _needs_owner(brief: Mapping[str, Any], approval: Mapping[str, Any] | None = None) -> bool:
    for source in (brief, approval or {}):
        if source.get("needs_owner_yes") is True or source.get("owner_decision"):
            return True
    files = [f for f in brief.get("assigned_files") or () if isinstance(f, str)]
    risk = brief.get("risk")
    declared = risk.get("tier") if isinstance(risk, Mapping) else None
    try:
        return effective_tier(declared, files)["tier"] == "high"
    except Exception:  # noqa: BLE001 - an unreadable tier is treated as needing the owner
        return True


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _lane_counts(value: Any) -> dict[str, int]:
    data = value if isinstance(value, Mapping) else {}
    return {lane: max(0, _int(data.get(lane)) or 0) for lane in LANES}


def public_capacity(payload: Any) -> dict[str, Any] | None:
    """Keep only the documented ``hyvv.capacity/1`` fields (drops anything else)."""

    if not isinstance(payload, Mapping) or payload.get("schema") != CAPACITY_SCHEMA:
        return None
    host = payload.get("host") if isinstance(payload.get("host"), Mapping) else {}
    docker = payload.get("docker") if isinstance(payload.get("docker"), Mapping) else {}
    limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), Mapping) else {}
    by_lane = limits.get("by_lane") if isinstance(limits.get("by_lane"), Mapping) else {}
    ok = docker.get("ok")
    return {
        "schema": CAPACITY_SCHEMA,
        "observed_at": _num(payload.get("observed_at")),
        "max_concurrent": _int(payload.get("max_concurrent")),
        "active": _int(payload.get("active")),
        "queued": _int(payload.get("queued")),
        "split_brain": payload.get("split_brain") is True if isinstance(payload.get("split_brain"), bool) else None,
        "active_by_lane": _lane_counts(payload.get("active_by_lane")),
        "queued_by_lane": _lane_counts(payload.get("queued_by_lane")),
        "host": {
            "mem_total_mb": _int(host.get("mem_total_mb")),
            "mem_available_mb": _int(host.get("mem_available_mb")),
            "cpu_count": _int(host.get("cpu_count")),
            "cpu_busy_pct": _num(host.get("cpu_busy_pct")),
        },
        "docker": {
            "ok": ok if isinstance(ok, bool) else None,
            "age_s": _num(docker.get("age_s")),
            "ncpu": _int(docker.get("ncpu")),
            "mem_total_mb": _int(docker.get("mem_total_mb")),
            "containers_running": _int(docker.get("containers_running")),
            "detail": "".join(ch for ch in str(docker.get("detail") or "")[:32] if ch.isalnum() or ch in "_-"),
        },
        "rate_limits": {
            "window_s": _int(limits.get("window_s")),
            "by_lane": {
                lane: {
                    "count": max(0, _int((by_lane.get(lane) or {}).get("count")) or 0)
                    if isinstance(by_lane.get(lane), Mapping) else 0,
                    "last_at": _num((by_lane.get(lane) or {}).get("last_at"))
                    if isinstance(by_lane.get(lane), Mapping) else None,
                }
                for lane in LANES
            },
        },
    }


def _closed(reasons: list[str], policy: Policy, lanes: dict[str, Any] | None = None,
            limits: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "free_slots": 0,
        "reasons": sorted(set(reasons)),
        "limits": limits or {},
        "lanes": lanes or {lane: {"active": 0, "queued": 0, "room": 0, "paused": True,
                                  "reason": "admission_closed"} for lane in LANES},
        "policy": policy.public(),
    }


def admit(
    capacity: Any,
    policy: Policy,
    *,
    now: Any = None,
    dispatched_today: int | None,
    last_pressure_at: float | None = None,
) -> dict[str, Any]:
    """How many new bees may start now, and per-lane room. Pure; fails closed."""

    at = _clock(now)
    cap_doc = public_capacity(capacity)
    if cap_doc is None:
        return _closed(["capacity_unreadable"], policy)
    reasons: list[str] = []
    active, queued, max_c = cap_doc["active"], cap_doc["queued"], cap_doc["max_concurrent"]
    if active is None or queued is None or max_c is None or cap_doc["observed_at"] is None:
        return _closed(["capacity_unreadable"], policy)
    if abs(at - cap_doc["observed_at"]) > policy.capacity_max_age_s:
        reasons.append("capacity_stale")
    docker, host = cap_doc["docker"], cap_doc["host"]
    if docker["ok"] is False:
        reasons.append("docker_unhealthy")
    elif docker["ok"] is not True:
        reasons.append("docker_unknown")
    if cap_doc["split_brain"] is not False:
        reasons.append("launcher_split_brain")
    if queued > 0:
        reasons.append("launcher_queue_backlog")

    in_flight = active + queued
    limits: dict[str, Any] = {}
    limits["shared"] = policy.cap - in_flight
    if limits["shared"] <= 0:
        reasons.append("shared_cap_reached")
    limits["launcher"] = max_c - in_flight
    if limits["launcher"] <= 0:
        reasons.append("launcher_full")

    avail = host["mem_available_mb"]
    if avail is None:
        reasons.append("host_memory_unknown")
        limits["ram"] = 0
    else:
        limits["ram"] = (avail - policy.host_ram_reserve_mb) // policy.per_bee_ram_mb
        if limits["ram"] <= 0:
            reasons.append("ram_pressure")
    vm_mem = docker["mem_total_mb"]
    if docker["ok"] is True:
        if vm_mem is None:
            reasons.append("docker_memory_unknown")
            limits["vm"] = 0
        else:
            limits["vm"] = vm_mem // policy.per_bee_ram_mb - active
            if limits["vm"] <= 0:
                reasons.append("docker_memory_full")

    busy = host["cpu_busy_pct"]
    cpus = host["cpu_count"] or docker["ncpu"]
    if busy is None or not cpus:
        reasons.append("host_cpu_unknown")
        limits["cpu"] = 0
    else:
        if busy >= policy.cpu_busy_max_pct:
            reasons.append("cpu_pressure")
        limits["cpu"] = cpus // policy.per_bee_cpus - active
        if limits["cpu"] <= 0:
            reasons.append("cpu_full")

    if dispatched_today is None:
        reasons.append("budget_unreadable")
        limits["budget"] = 0
    else:
        limits["budget"] = policy.daily_attempt_budget - int(dispatched_today)
        if limits["budget"] <= 0:
            reasons.append("budget_exhausted")

    if last_pressure_at is not None and 0 <= at - float(last_pressure_at) < PRESSURE_COOLDOWN_S:
        reasons.append("pressure_cooldown")

    act_l, q_l = cap_doc["active_by_lane"], cap_doc["queued_by_lane"]
    rl = cap_doc["rate_limits"]["by_lane"]
    lanes: dict[str, Any] = {}
    for lane in LANES:
        room = max(0, int(policy.lane_max.get(lane, 0)) - act_l[lane] - q_l[lane])
        paused = rl[lane]["count"] > 0
        reason = "lane_rate_limited" if paused else ("lane_full" if room <= 0 else "open")
        lanes[lane] = {"active": act_l[lane], "queued": q_l[lane], "room": 0 if paused else room,
                       "paused": paused, "reason": reason, "rate_limits": rl[lane]["count"]}

    if reasons:
        return _closed(reasons, policy, lanes=lanes, limits=limits)
    free = min(int(v) for v in limits.values())
    if free > policy.step_max:
        free = policy.step_max
        reasons.append("ramp_step")
    limits["step"] = policy.step_max
    return {"free_slots": max(0, free), "reasons": reasons, "limits": limits, "lanes": lanes,
            "policy": policy.public()}


def plan(admission: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Choose candidates in order within free slots and lane room. Pure."""

    free = int(admission.get("free_slots") or 0)
    rooms = {lane: int((admission.get("lanes") or {}).get(lane, {}).get("room") or 0) for lane in LANES}
    paused = {lane: bool((admission.get("lanes") or {}).get(lane, {}).get("paused")) for lane in LANES}
    admitted: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    for cand in candidates:
        work_id = str(cand.get("work_id") or "")
        lane = str(cand.get("lane") or "")
        hold = None
        if cand.get("reason") and not cand.get("lane"):
            hold = str(cand["reason"])
        elif cand.get("approved") is not True:
            hold = "not_approved"
        elif cand.get("needs_owner") is not False:
            hold = "needs_owner_yes"
        elif lane not in LANES:
            hold = "lane_unknown"
        elif paused[lane]:
            hold = "lane_rate_limited"
        elif rooms[lane] <= 0:
            hold = "lane_full"
        elif free <= 0:
            hold = "no_free_slots"
        if hold:
            held.append({"work_id": work_id, "lane": lane or None, "reason": hold})
            continue
        free -= 1
        rooms[lane] -= 1
        admitted.append({"work_id": work_id, "lane": lane, "model": cand.get("model")})
    return {"admit": admitted, "held": held}


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def record_dispatch(state_dir: Any, work_id: str, *, lane: str, model: Any, now: Any = None) -> dict[str, Any]:
    at = _clock(now)
    line = {"schema": LEDGER_SCHEMA, "work_id": str(work_id), "lane": str(lane),
            "model": str(model or ""), "at": at, "day": _day(at)}
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return line


def dispatched_today(state_dir: Any, now: Any = None) -> int | None:
    """go-max dispatches in the current UTC day; None when the ledger is unreadable."""

    day = _day(_clock(now))
    path = ledger_path(state_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    count = 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            return None
        if isinstance(entry, Mapping) and entry.get("day") == day:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def summary_text(record: Mapping[str, Any]) -> str:
    mode = record.get("mode") or {}
    adm = record.get("admission") or {}
    lanes = record.get("lanes") or {}
    launcher = record.get("launcher") or {}
    lane_bits = ", ".join(
        f"{lane} {lanes.get(lane, {}).get('active', 0)} active/{lanes.get(lane, {}).get('room', 0)} room"
        for lane in ("kimi", "nous", "go")
    )
    head = (
        f"go-max {'on' if mode.get('enabled') else 'off'}: {adm.get('free_slots', 0)} free slot(s); "
        f"launcher {launcher.get('active', '?')} active, {launcher.get('queued', '?')} queued "
        f"of {record.get('policy', {}).get('cap', '?')} shared; {lane_bits}; "
        f"admitted {sum(int(l.get('admitted', 0)) for l in lanes.values())}."
    )
    empty = record.get("empty_reasons") or []
    return head + (f" Slots stay empty because: {', '.join(empty)}." if empty else "")


def build_status(
    state_dir: Any,
    capacity: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    policy: Policy | None = None,
    now: Any = None,
    admission: Mapping[str, Any] | None = None,
    plan_result: Mapping[str, Any] | None = None,
    allowed_models: Sequence[str] | None = None,
    last_pressure_at: float | None = None,
    stock: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    at = _clock(now)
    pol = policy or Policy()
    mode = read_mode(state_dir, now=at)
    adm = dict(admission) if admission is not None else admit(
        capacity, pol, now=at, dispatched_today=dispatched_today(state_dir, now=at),
        last_pressure_at=last_pressure_at,
    )
    result = dict(plan_result) if plan_result is not None else plan(adm, candidates)
    cap_doc = public_capacity(capacity) or {}
    lanes: dict[str, Any] = {}
    for lane in LANES:
        info = dict((adm.get("lanes") or {}).get(lane) or {})
        lanes[lane] = {
            "active": int(info.get("active") or 0),
            "queued": int(info.get("queued") or 0),
            "room": int(info.get("room") or 0),
            "paused": bool(info.get("paused")),
            "reason": str(info.get("reason") or ""),
            "ready": sum(1 for c in candidates if c.get("lane") == lane),
            "admitted": sum(1 for a in result.get("admit") or () if a.get("lane") == lane),
        }
    held_counts: dict[str, int] = {}
    for item in result.get("held") or ():
        key = str(item.get("reason") or "held")
        held_counts[key] = held_counts.get(key, 0) + 1
    empty: list[str] = []
    if not mode.get("enabled"):
        empty.append("mode_off" if mode.get("reason") in ("off", None) else f"mode_{mode.get('reason')}")
    if int(adm.get("free_slots") or 0) <= 0:
        empty.extend(r for r in adm.get("reasons") or () if r not in INFO_REASONS)
    if not candidates:
        empty.append("no_ready_approved_work")
    empty.extend(f"held:{key}" for key in sorted(held_counts) if key != "no_free_slots")
    record = {
        "schema": STATUS_SCHEMA,
        "observed_at": at,
        "observed_at_iso": _iso(at),
        "mode": mode,
        "policy": pol.public(),
        "launcher": {"active": cap_doc.get("active"), "queued": cap_doc.get("queued"),
                     "max_concurrent": cap_doc.get("max_concurrent")},
        "host": cap_doc.get("host"),
        "docker": cap_doc.get("docker"),
        "admission": {"free_slots": int(adm.get("free_slots") or 0), "reasons": list(adm.get("reasons") or ()),
                      "limits": dict(adm.get("limits") or {})},
        "lanes": lanes,
        "admitted": list(result.get("admit") or ()),
        "held": held_counts,
        "empty_reasons": list(dict.fromkeys(empty)),
        "allowed_models": [m for m in (allowed_models or ()) if isinstance(m, str)][:64],
        "last_pressure_at": last_pressure_at,
    }
    if stock is not None:
        record["stock"] = dict(stock)
    record["summary"] = summary_text(record)
    return record


def write_status(state_dir: Any, record: Mapping[str, Any]) -> None:
    _write_json(status_path(state_dir), record)


def read_status(state_dir: Any) -> dict[str, Any]:
    try:
        body = json.loads(status_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) and body.get("schema") == STATUS_SCHEMA else {}


# ---------------------------------------------------------------------------
# Factory gate
# ---------------------------------------------------------------------------


def _board_brief(state: Path, work_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not work_id.startswith("bd_"):
        return None, None
    path = state / "factory" / "board-approvals" / f"{work_id[3:]}.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    brief = body.get("brief") if isinstance(body, Mapping) else None
    return (dict(brief) if isinstance(brief, Mapping) else None), (dict(body) if isinstance(body, Mapping) else None)


def default_lane_reader(state_dir: Any) -> Callable[[Mapping[str, Any], str], dict[str, Any]]:
    """Predict the lane a factory item would dispatch on (fails closed to brief_unreadable)."""

    state = Path(state_dir)

    def reader(item: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        work_id = str(item.get("work_id") or "")
        brief, approval = _board_brief(state, work_id)
        failed = 0
        if brief is None:
            from . import job as job_mod

            job = job_mod.load_job(state, job_id)
            tasks = [t for t in job.get("tasks") or () if isinstance(t, Mapping)]
            pending = [t for t in tasks if str(t.get("state") or "pending") in ("pending", "dispatching", "retry")]
            task = (pending or tasks or [None])[0]
            if task is None:
                raise GoMaxError("brief_unreadable", f"job {job_id!r} has no task")
            brief = {"assigned_files": list(task.get("lease") or item.get("lease_files") or ()),
                     "project": str(task.get("project") or ""), "objective": str(task.get("objective") or "")}
            failed = len(task.get("attempts") or ())
        picked = route(brief, failed_attempts=failed)
        return {"lane": picked["lane"], "model": picked["model"], "needs_owner": _needs_owner(brief, approval)}

    return reader


def _default_stocker(state: Path) -> dict[str, Any]:
    from . import board_dispatch

    report = board_dispatch.stock(state, board=board_dispatch.board_from_environment())
    return {"status": "ok", "checked": report.get("checked", 0), "stocked": len(report.get("stocked") or ()),
            "skipped": len(report.get("skipped") or ())}


class FactoryGate:
    """Per-pass admission gate for :class:`factory_driver.FactoryService`.

    Inert unless go-max is on.  ``begin_pass`` reads the launcher capacity once;
    ``check`` decides one item; ``note`` consumes a slot after a dispatch;
    ``finish_pass`` writes the status record.
    """

    def __init__(
        self,
        state_dir: Any,
        *,
        capacity_reader: Callable[[], Any],
        allowed_reader: Callable[[], Any] | None = None,
        policy: Policy | None = None,
        lane_reader: Callable[[Mapping[str, Any], str], Mapping[str, Any]] | None = None,
        stocker: Callable[[Path], Mapping[str, Any]] | None = None,
        now: Any = None,
    ) -> None:
        self.state = Path(state_dir)
        self._capacity_reader = capacity_reader
        self._allowed_reader = allowed_reader
        self.policy = policy or Policy()
        self._lane_reader = lane_reader or default_lane_reader(self.state)
        self._stocker = stocker if stocker is not None else _default_stocker
        self._now = now
        self.capacity: Any = None
        self.admission: dict[str, Any] = {}
        self.allowed_models: list[str] = []
        self.last_pressure_at: float | None = None
        self._free = 0
        self._rooms: dict[str, int] = {}
        self._paused: dict[str, bool] = {}
        self.admitted: list[dict[str, Any]] = []
        self.held: list[dict[str, Any]] = []
        self.stock: dict[str, Any] | None = None

    def enabled(self) -> bool:
        return bool(read_mode(self.state, now=_clock(self._now)).get("enabled"))

    def stock_board(self) -> dict[str, Any]:
        try:
            self.stock = dict(self._stocker(self.state))
        except Exception as exc:  # noqa: BLE001 - stocking never stops the pass
            self.stock = {"status": "error", "code": str(getattr(exc, "code", type(exc).__name__))[:64]}
        return self.stock

    def begin_pass(self) -> dict[str, Any]:
        at = _clock(self._now)
        try:
            self.capacity = self._capacity_reader()
        except Exception:  # noqa: BLE001 - unreadable capacity admits nothing
            self.capacity = None
        try:
            allowed = self._allowed_reader() if self._allowed_reader else []
            if isinstance(allowed, Mapping):
                allowed = allowed.get("models") or []
            self.allowed_models = [m for m in allowed or () if isinstance(m, str)]
        except Exception:  # noqa: BLE001 - no allow-list means no model routing
            self.allowed_models = []
        previous = read_status(self.state).get("last_pressure_at")
        self.last_pressure_at = float(previous) if isinstance(previous, (int, float)) else None
        self.admission = admit(
            self.capacity, self.policy, now=at,
            dispatched_today=dispatched_today(self.state, now=at),
            last_pressure_at=self.last_pressure_at,
        )
        pressure = bool(PRESSURE_REASONS & set(self.admission.get("reasons") or ()))
        if pressure:
            self.last_pressure_at = at
        self._free = int(self.admission.get("free_slots") or 0)
        lanes = self.admission.get("lanes") or {}
        self._rooms = {lane: int((lanes.get(lane) or {}).get("room") or 0) for lane in LANES}
        self._paused = {lane: bool((lanes.get(lane) or {}).get("paused")) for lane in LANES}
        self.admitted, self.held = [], []
        if pressure:
            self.finish_pass()  # persist the cooldown start even if the pass ends early
        return self.admission

    def check(self, item: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        work_id = str(item.get("work_id") or "")
        if self._free <= 0:
            return self._hold(work_id, None, "no_free_slots")
        try:
            info = dict(self._lane_reader(item, job_id))
        except GoMaxError as exc:
            return self._hold(work_id, None, exc.code)
        except Exception:  # noqa: BLE001 - an unreadable brief is never dispatched by go-max
            return self._hold(work_id, None, "brief_unreadable")
        lane = str(info.get("lane") or "")
        if info.get("needs_owner") is not False:
            return self._hold(work_id, lane, "needs_owner_yes")
        if lane not in LANES:
            return self._hold(work_id, None, "lane_unknown")
        if self._paused.get(lane):
            return self._hold(work_id, lane, "lane_rate_limited")
        if self._rooms.get(lane, 0) <= 0:
            return self._hold(work_id, lane, "lane_full")
        return {"allowed": True, "reason": "allowed", "work_id": work_id, "lane": lane, "model": info.get("model")}

    def _hold(self, work_id: str, lane: str | None, reason: str) -> dict[str, Any]:
        verdict = {"allowed": False, "reason": reason, "work_id": work_id, "lane": lane}
        self.held.append({"work_id": work_id, "lane": lane, "reason": reason})
        return verdict

    def note(self, work_id: str, verdict: Mapping[str, Any]) -> None:
        lane = str(verdict.get("lane") or "other")
        self._free -= 1
        self._rooms[lane] = self._rooms.get(lane, 0) - 1
        self.admitted.append({"work_id": str(work_id), "lane": lane, "model": verdict.get("model")})
        record_dispatch(self.state, work_id, lane=lane, model=verdict.get("model"), now=_clock(self._now))

    def finish_pass(self) -> dict[str, Any]:
        candidates = [{"work_id": a["work_id"], "lane": a["lane"]} for a in self.admitted] + [
            {"work_id": h["work_id"], "lane": h["lane"]} for h in self.held
        ]
        record = build_status(
            self.state, self.capacity, candidates, policy=self.policy, now=_clock(self._now),
            admission=self.admission, plan_result={"admit": self.admitted, "held": self.held},
            allowed_models=self.allowed_models, last_pressure_at=self.last_pressure_at, stock=self.stock,
        )
        try:
            write_status(self.state, record)
        except OSError:
            pass  # a status write never changes a dispatch decision
        return record


# ---------------------------------------------------------------------------
# Dispatch brief routing (used by reconcile._dispatch_one)
# ---------------------------------------------------------------------------


def route_brief(state_dir: Any, brief: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    """While go-max is on, give an unpinned dispatch brief its lane's model.

    Restricted to the launcher allow-list recorded by the last gated pass; with no
    allow-list (or any error) the brief is returned unchanged, which keeps the
    launcher's default lane.
    """

    out = dict(brief)
    try:
        if isinstance(out.get("model"), str) and out["model"].strip():
            return out
        if state_dir is None or not read_mode(state_dir).get("enabled"):
            return out
        allowed = [m for m in read_status(state_dir).get("allowed_models") or () if isinstance(m, str)]
        if not allowed:
            return out
        failed = len((task or {}).get("attempts") or ())
        picked = route(out, failed_attempts=failed, allowed_models=allowed)
        if picked["model"] in allowed and lane_of(picked["model"]) != "other":
            out["model"] = picked["model"]
    except Exception:  # noqa: BLE001 - routing is an optimisation; unrouted is the default lane
        return dict(brief)
    return out


# ---------------------------------------------------------------------------
# On-demand status and actions (Hermes tool and CLI)
# ---------------------------------------------------------------------------


def ready_candidates(state_dir: Any, *, lane_reader: Callable[..., Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Ready scheduler items, classified the way the factory gate would (read-only)."""

    from . import factory_driver, work_scheduler

    state = Path(state_dir)
    reader = lane_reader or default_lane_reader(state)
    try:
        ready = work_scheduler.ready_items(state)
    except Exception:  # noqa: BLE001 - no readable queue means no candidates
        return []
    out: list[dict[str, Any]] = []
    for item in ready:
        work_id = str(item.get("work_id") or "")
        cand: dict[str, Any] = {"work_id": work_id, "lane": None, "model": None,
                                "approved": False, "needs_owner": True}
        try:
            approval = factory_driver.read_dispatch_approval(state, work_id)
        except Exception:  # noqa: BLE001
            approval = None
        cand["approved"] = bool(approval and approval.get("approved") is True)
        try:
            mapping = json.loads((state / "factory" / "work-jobs" / f"{work_id}.json").read_text(encoding="utf-8"))
            job_id = str(mapping.get("job_id") or "")
            info = dict(reader(item, job_id))
            cand.update(lane=info.get("lane"), model=info.get("model"), needs_owner=info.get("needs_owner") is not False)
        except GoMaxError as exc:
            cand["reason"] = exc.code
        except Exception:  # noqa: BLE001
            cand["reason"] = "brief_unreadable"
        out.append(cand)
    return out


def snapshot_status(
    state_dir: Any,
    *,
    client: Any = None,
    policy: Policy | None = None,
    now: Any = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build (and by default persist) the status record from live inputs."""

    extra: list[str] = []
    capacity = None
    allowed: list[str] = []
    if client is None:
        extra.append("capacity_offline")
    elif not hasattr(client, "host_capacity"):
        extra.append("capacity_api_unavailable")
    else:
        try:
            capacity = client.host_capacity()
        except Exception:  # noqa: BLE001
            capacity = None
        if capacity is None:
            extra.append("capacity_unreadable")
        try:
            health = client.health() if hasattr(client, "health") else {}
            allowed = [m for m in (health or {}).get("models") or () if isinstance(m, str)]
        except Exception:  # noqa: BLE001
            allowed = []
    previous = read_status(state_dir).get("last_pressure_at")
    record = build_status(
        state_dir, capacity, ready_candidates(state_dir), policy=policy, now=now,
        allowed_models=allowed or read_status(state_dir).get("allowed_models") or [],
        last_pressure_at=float(previous) if isinstance(previous, (int, float)) else None,
    )
    record["empty_reasons"] = list(dict.fromkeys(extra + list(record["empty_reasons"])))
    record["summary"] = summary_text(record)
    if write:
        try:
            write_status(state_dir, record)
        except OSError:
            pass
    return record


def run_action(
    state_dir: Any,
    action: str,
    *,
    client: Any = None,
    ttl_minutes: Any = None,
    by: str = "owner",
    policy: Policy | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """``on`` / ``off`` / ``status``. Never dispatches: the factory fills on its next pass."""

    verb = str(action or "").strip().lower()
    if verb in ("on", "go", "max", "start", "enable"):
        ttl = DEFAULT_TTL_S
        if ttl_minutes not in (None, ""):
            try:
                ttl = float(ttl_minutes) * 60.0
            except (TypeError, ValueError) as exc:
                raise GoMaxError("invalid_argument", "ttl_minutes must be a number") from exc
        mode = enable(state_dir, by=by, ttl_s=ttl, now=now)
    elif verb in ("off", "stop", "disable"):
        mode = disable(state_dir, by=by, now=now)
    elif verb != "status":
        raise GoMaxError("invalid_argument", f"unknown go-max action {action!r}", "Use on, off or status.")
    status = snapshot_status(state_dir, client=client, policy=policy, now=now)
    return {"mode": status["mode"], "status": status, "summary": status["summary"]}
