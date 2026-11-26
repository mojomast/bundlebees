# go-max: fill bee slots safely when the owner asks

Status: contract for `bundlebees_plugin/go_max.py` (tests: `tests/test_go_max.py`).

## Why

The owner wants "as much work as possible at once, safely", on request. Before
go-max, the factory counted only its own claims against the shared cap, never
looked at host pressure, never stocked the board automatically, and dispatched
every slice on the launcher default model.

## Trigger

One durable mode record, `<state>/factory/go-max.json` (`rizzstack.go-max/1`):

| Surface | On | Off | Status |
| --- | --- | --- | --- |
| Hermes chat ("go max", "max parallel") | `bundlebees_go_max {"action":"on"}` | `{"action":"off"}` | `{"action":"status"}` |
| CLI | `hermes bundlebees go-max on [--ttl-minutes N]` | `go-max off` | `go-max status [--json]` |
| Orbit / Hive card | reads `<state>/factory/go-max-status.json` | | |

The mode expires (default 8 h, max 24 h). An unreadable or expired record reads
as **off**. Turning it on never dispatches by itself: the resident factory
service does the filling on its next pass.

## Admission (pure, fail-closed)

Input: the launcher's `GET /v1/capacity` (`hyvv.capacity/1`), which counts every
attempt on the launcher (Hermes Forge, Hermes direct, box bots, the factory).

`free = min(shared_cap - (active+queued), max_concurrent - (active+queued),
ram_slots, vm_slots, cpu_slots, budget_left, step_max)`, where

* `ram_slots = (mem_available_mb - host_ram_reserve_mb) // per_bee_ram_mb`
* `vm_slots = docker.mem_total_mb // per_bee_ram_mb - active`
* `cpu_slots = cpu_count // per_bee_cpus - active`
* per bee: 4096 MB and 2 CPUs (settings may raise these, never lower them)

`free` is **0** with a stable reason code when any input is missing or bad:
`capacity_unreadable`, `capacity_stale`, `docker_unhealthy`, `docker_unknown`,
`launcher_split_brain`, `launcher_queue_backlog` (the launcher already holds
queued attempts), `shared_cap_reached`, `launcher_full`, `host_memory_unknown`,
`ram_pressure`, `docker_memory_full`, `host_cpu_unknown`, `cpu_pressure`,
`cpu_full`, `budget_exhausted`, `budget_unreadable`, `pressure_cooldown`.
`ramp_step` is informational (the per-pass step limit bound the fill).
After a pressure reason the gate holds for `pressure_cooldown_s` (120 s).

## Lanes

| Lane | Models | Default cap |
| --- | --- | --- |
| kimi | Kimi direct (`k3.1-pro`, `kimi-*`) — complex slices | 4 |
| nous | `nous:deepseek-v4.1-flash-1017`, `nous:hy4-flash` — low-risk | 8 (`model_route.NOUS_MAX_ACTIVE`) |
| go | `go:deepseek-v4.1-flash` (OpenCode Go) — everything else | 12 |
| other | anything else | 0 (never auto-filled) |

The lane is `model_route.select_worker_model(brief)` (a pinned `brief.model`
wins). A lane with a provider 429 inside the capacity window is paused
(`lane_rate_limited`). A lane at its cap holds (`lane_full`). A brief with
`temperature: 0` is refused (`temperature_zero`).

While go-max is on, the factory's dispatch brief gets `model` from the same
router, restricted to the launcher's allow-list (`/v1/health` models). Without
an allow-list it is left unset (the launcher default lane).

## Eligibility

A ready item is filled only when all of these hold:

* the factory's existing pre-approval checks pass (approval record, scope, job);
* the slice is not marked as needing the owner (`needs_owner_yes`,
  `owner_decision`) and its effective risk tier is not `high` (`needs_owner_yes`);
* its lane is open and has room.

go-max never cancels, never approves, never merges, and never raises the shared
cap above `grants.GLOBAL_MAX_BEES` (12).

## Board stocking

While on, each factory pass first runs `board_dispatch.stock` (ready cards with a
recorded owner approval only). A board error is reported in the pass result and
never stops dispatch decisions.

## Status

`rizzstack.go-max-status/1`, written each gated pass and on demand:
`mode`, `admission` (free slots, every limit, reasons), `lanes` (per lane:
active, queued, ready, admitted, room, paused, reason), `held` (per-reason
counts), `empty_reasons` (why slots stay empty, including
`no_ready_approved_work`), `allowed_models`, and a one-paragraph `summary`.
It never contains tokens, URLs, hosts or paths.

## Ledger and budget

Every go-max dispatch appends one line to `<state>/factory/go-max-ledger.jsonl`
(`work_id`, `lane`, `model`, UTC day). `daily_attempt_budget` (default 48) caps
go-max dispatches per UTC day; an unreadable ledger is `budget_unreadable`.
