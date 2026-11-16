# W6 design: a native, durable bee-completion signal

**Status:** design document only. No code changes are part of this change; the diff is exactly this file.
**Card:** `t_51bcfe2a` — "the launcher/plugin should emit a completion signal the coordinator can wait on durably."
**Anchors:** every `file:line` reference in this document was verified against this tree at commit `4be4e7b`.

**Audience:** a maintainer who knows neither this repository (`bundlebees`) nor its neighbours deeply. Section 1 maps what exists today and where it lives. Section 2 states the failures this design must solve. Sections 3–6 are the design itself: candidate mechanisms, the recommendation, the rollout, and the risks.

## 0. The one-paragraph version

A bundlebees "bee" is a coding worker started on a remote launcher. Today, the only way the coordinating agent (a Hermes session driving the bundlebees MCP tools) can learn that a bee finished is to ask — and the only thing that makes it ask is a cron rule on the Hermes host. Two recorded failures show this is fragile: a one-shot wakeup cron whose fire time fell inside a gateway-restart window was silently dropped, so a finished bee sat unprocessed until a human nudged; and a coordinator woken by a stale or duplicated signal can collide with another live session working the same lane. This document designs the native fix: the launcher gains a blocking wait operation, the plugin exposes it as `bundlebees_attempt op=await`, and the coordinator's own tool call *becomes* the continuation — a level-triggered wait on a durable fact, with no schedule artifact left to drop. The existing 10-minute heartbeat stays, as the backstop and the multi-bee sweep, not as the primary wait.

## 1. Where continuation stands today

### 1.1 Three processes, one direction of control

Three programs matter, and requests flow in exactly one direction between them:

1. **The Hermes gateway** hosts the coordinator. The coordinator is an agent session; bundlebees reaches it as an MCP toolset registered *inside* the gateway process (`bundlebees_plugin/tools.py:928`, `register_tools(ctx)`, registering the five tools named in `TOOL_NAMES`, `tools.py:38-44`). The plugin has no socket, no port, and no lifecycle of its own — it lives and dies with the gateway.
2. **The worker launcher** is a separate process exposing a read-mostly `/v1` HTTP API (health, capacity, attempts list, per-attempt get + events, containers, projects, plus dispatch/cancel/review and a result endpoint). Its identity is pinned in `docs/stack-manifest.md` (launcher `0.1.16`).
3. **The bundlebees plugin's HTTP client** (`bundlebees_plugin/client.py`) only ever dials out. Every request goes through `_pinned_request` (`client.py:122`), which resolves and validates the configured endpoint and opens one connection per call. Nothing in `client.py` or `tools.py` binds a listener. The token comes only from Hermes' secret scope (`client.py:39`, `get_launcher_token` at `client.py:1604`); target URLs are always configuration, never tool arguments (`tools.py` module docstring, lines 1-7).

There are two client classes sharing one transport base (`_BaseClient`, `client.py:461`): `LauncherClient` talks directly to the launcher's `/v1` API (`client.py:867`), and `OrbitAdapterClient` talks to the default loopback Orbit adapter via `POST /api/local-workers` with an `op` field (`client.py:1324`, `_op` at `client.py:1478`). Which one runs is a plugin setting (`adapter`, default `orbit-loopback`; `bundlebees_plugin/settings.py:18-40`, `plugin.yaml:50-55`).

### 1.2 The model-facing tool surface (`bundlebees_plugin/tools.py`)

Every handler returns exactly one JSON envelope string — `ok: true` plus fields, or `ok: false` with `error`, `code`, and a `fix` (`_ok`/`_err`, `tools.py:79-92`). The envelope is the whole contract between the model and the machinery.

**`bundlebees_status`** — read-only views of the launcher (`_STATUS_OPS`, `tools.py:57`; handler `_status`, `tools.py:204-236`):

| op | What it does | Anchor |
|---|---|---|
| `health` | Launcher health snapshot via `client.health()` | `tools.py:212-213` |
| `capacity` | Currently a synonym for health (`capacity()` calls `health()` in both clients) | `tools.py:214-215`, `client.py:873-874`, `client.py:1490-1491` |
| `attempts` | The live attempts list via `client.attempts()` | `tools.py:217` |
| `events` | One attempt's event page, cursor-addressed; requires `attempt_id` | `tools.py:218-233` |
| `containers` | Container digests/toolchains via `client.containers()` | `tools.py:234-235` |
| `projects` | Authorized projects via `client.projects()` | `tools.py:236` |

**`bundlebees_attempt`** — per-attempt operations (`_ATTEMPT_OPS`, `tools.py:58`; handler `_attempt`, `tools.py:465-546`):

| op | What it does | Anchor |
|---|---|---|
| `get` | Full attempt snapshot; attaches the public result when the launcher has one, and surfaces the typed `no_result_yet` refusal otherwise | `tools.py:482-497` |
| `events` | Event page for one attempt, cursor-addressed | `tools.py:499-507` |
| `cancel` | Cancel the attempt; requires `confirm=true` (and a Hermes approval prompt) | `tools.py:509-520` |
| `review` | Accept/reject; requires `confirm=true`, a boolean `accepted`, and — for acceptance — an already-accepted integration wave (`integration_required`) | `tools.py:509-514`, `522-546`, `532-538` |

**`bundlebees_dispatch`** starts a bee (`_dispatch`, `tools.py:347-457`). Three behaviours matter for this design: the brief is validated before any launcher call; the requested file lease is checked against *running* attempts (`_lease_overlap`, `tools.py:165-196`, using `_RUNNING_STATES = {"running", "working", "active", "started"}` at `tools.py:61`) so a second dispatch cannot silently overlap live work; and on success the coordinator-side signed lease is recorded (`_record_dispatch`, `tools.py:271-297`, calling `leases.record_lease`). The lease is the authority the user-run `hermes bundlebees verify` later refuses to work without — worker-returned metadata never widens it.

### 1.3 The HTTP client and its sanitizing mappers (`bundlebees_plugin/client.py`)

All reads and writes funnel through two helpers: `request_raw`/`request_json` (`client.py:588`, `client.py:683`). Both are **one-shot request/response**: build the request, open a pinned connection, read up to `MAX_RESPONSE_BYTES` (1 MiB, `client.py:42`), close. The only timeout is `DEFAULT_TIMEOUT = 10.0` seconds per request (`client.py:41`), applied per connection, not per logical operation. Redirects are followed only within the same origin (`client.py:638-669`); the bearer token never crosses origins.

The model-visible views are produced by the `public_*` mappers, which allowlist fields and scrub strings so upstream tokens, hosts, and private paths cannot reach an envelope:

- `public_attempt` (`client.py:248-276`) — the attempt snapshot: `attempt_id`, `task_id`/`kanban_task_id`, `project`, `branch`, `model`, the timestamps (`created_at`, `queued_at`, `started_at`, `updated_at`, `finished_at`), `state` (read from `state` or `status`), and scrubbed `detail`/`error`.
- `public_result` (`client.py:375-409`) — the reduced result: state, observed changed files/usage/stats/sha256, and per-check rows; worker self-claims are dropped by design.
- `public_events` (`client.py:302`), `public_health` (`client.py:225`), `public_project` (`client.py:307`), `public_container` (`client.py:321`), `public_route` (`client.py:412`) — the same pattern for the other surfaces.

Per the launcher contract, every attempt carries a `state` whose terminal values are `succeeded`, `failed`, and `cancelled`, an `updated_at` timestamp, and a `detail` string. Those are exactly the fields `public_attempt` forwards, so the plugin can already *see* completion — it just cannot *wait* for it.

### 1.4 The fact this card exists to change: no blocking/await op exists today

Stated explicitly, with the proof from the tree:

- Neither `_STATUS_OPS` (`tools.py:57`) nor `_ATTEMPT_OPS` (`tools.py:58`) contains a wait, await, poll, or subscribe op. The complete op vocabulary is: `health, capacity, attempts, events, containers, projects | get, events, cancel, review`.
- Both client classes implement only immediate request/response methods (`client.py:867-1095` and `client.py:1324-1596`). The closest thing to "not finished" in the entire vocabulary is the typed error `no_result_yet`, which the client produces by mapping the launcher's HTTP 409 (`client.py:568-569`) and which `bundlebees_attempt op=get` surfaces as a failure envelope (`tools.py:486-497`, `client.py:895-909`, `client.py:1511-1525`).
- The transport has no streaming, no long-poll, and no per-request timeout override: every call is capped at the same 10 seconds (`client.py:41`), and a call that needs longer simply does not exist.

So today, "wait for the bee" can only mean one of: the model re-issuing `op=get` in a loop (burning tokens and turns), or something outside the model deciding when the coordinator should look again. That something is the next section.

### 1.5 How the coordinator gets woken today

The operational continuation recipe is recorded in the **rizzstack-provisioning-stack skill** (host-side documentation, outside this repository). It has three parts:

1. **A recurring 10-minute heartbeat cron** with `catch_up=true`. Because missed runs are re-fired after a restart, this heartbeat is restart-safe by construction — it is the one continuation signal that cannot be lost to a gateway restart.
2. **A no-LLM state-change scout.** The heartbeat turn first reads the launcher attempts list (the same data behind `bundlebees_status op=attempts`) with no model call, compares it to the last snapshot it recorded, and only wakes/prompts the coordinator when something changed.
3. **A dispatch-time one-shot wakeup cron rule**, created when a bee is dispatched, scheduled at `budget.timeout_s + margin`, with `catch_up=true` mandatory.

The in-repo primitives these recipes rest on: `hermes_adapter.notify_coordinator` (`bundlebees_plugin/hermes_adapter.py:2357`) is the documented gateway wake path — a single `POST` to `WAKE_PATH = /v1/chat/completions` (`hermes_adapter.py:2005`) carrying the raw session id header and a sanitized, allowlisted projection of the event (`_WAKE_FIELDS`, `hermes_adapter.py:2012`); and the factory driver's durable wake coalescer (`bundlebees_plugin/factory_driver.py:254`, `WakeCoalescer`) keeps per-session generation/cursor records with one-active-turn semantics under `<state>/factory/wakes/`, with stable action ids (`wake_action_id`, `factory_driver.py:130`) and crash reconciliation (`_reconcile_wake_action`, `factory_driver.py:1399`).

What the coordinator does once woken is the polling loop of §1.4: `bundlebees_status op=attempts` to see the board, `bundlebees_attempt op=get` on anything interesting.

## 2. The failure record this design must solve

### 2.1 Failure A — the dropped one-shot wakeup (recorded incident)

A dispatch-time one-shot wakeup cron rule was created with `catch_up=false`. Its fire time — `budget.timeout_s + margin`, computed once at dispatch — landed inside a gateway-restart window. The scheduler never fired it and never repaired it: it left `next_run_at` in the past with `last_run_at` null, which is silent death, not an error. The bee landed; the coordinator stayed idle; the work stalled until a human noticed and nudged.

Two things make this class poisonous:

- **Edge-triggered vs level-triggered.** A one-shot cron is edge-triggered: it fires if and only if the scheduler is alive at the computed instant. A restart window deletes the edge, and nothing re-evaluates it later. `catch_up=true` converts the rule to level-triggered (missed runs re-fire), which is why the provisioning-stack skill marks it mandatory — but the flag lives on the host's rule, created outside this plugin's reach, and nothing enforces it at rule-creation time.
- **The plugin cannot even see the problem.** The plugin never learns whether a wakeup rule exists, whether it survived, or whether it fired. From inside the gateway, a dropped wake is indistinguishable from "nothing happened yet".

### 2.2 Failure B — the stale-signal collision (recorded class)

A coordinator woken by a stale or duplicated signal can collide with a *live interactive session* already working the same lane: two coordinator turns both acting on one kanban card — duplicated comments, racing reviews, double-handling of one attempt.

The repository already fences part of this:

- `cancel` and `review` require `confirm=true` and surface a Hermes approval prompt (`tools.py:509-514`; README tool table, `README.md:344`), so a human sees the second actor before damage.
- An accepting review requires the attempt to be in an accepted integration wave (`integration_required`, `tools.py:532-538`), and that wave is produced by the user-run `hermes bundlebees integrate` against the dispatching coordinator's own state.
- The lease authority is per-coordinator: the signed lease lives in the dispatching coordinator's state directory (`tools.py:271-297`), and verify refuses a missing or mismatched lease — a second coordinator cannot verify, and therefore cannot legitimately accept, another's attempt.
- `_lease_overlap` (`tools.py:165-196`) prevents a *second dispatch* over files a live attempt already holds.

What is *not* fenced is the wake itself: a woken turn is a second coordinator turn regardless of which signal woke it, and today nothing tells the woken turn "another turn is already on this lane — defer."

### 2.3 The structural gap, in one sentence

Continuation today is a property of the **host scheduler**, not of the **work**: the durable fact (the attempt's terminal state) lives in the launcher, the wake (a cron rule) lives in the gateway host, and the only thing connecting them is timestamp arithmetic done once at dispatch. Any durable fix has to move the "is the bee finished?" question onto a surface both sides already share — the launcher's attempt record — and give the coordinator a way to wait on it that does not depend on any schedule artifact.

## 3. Candidate mechanisms

Three full candidates and one variant are evaluated. The comparison table is in §3.5.

### 3.1 Candidate A — launcher-side blocking/long-poll wait, exposed as `bundlebees_attempt op=await`

**Launcher side (a new capability in the launcher, which is a separate product).** The launcher gains a blocking read on one attempt, e.g. `GET /v1/attempts/{id}/wait` (or, for the Orbit loopback adapter, `POST /api/local-workers` with `op: "await"`). Semantics:

- If the attempt is already terminal (`succeeded`/`failed`/`cancelled`), respond **immediately** with the attempt document and `settled: true`. The op is level-triggered and idempotent: waiting on a finished thing returns at once.
- If the attempt is running, hold the request open until the state becomes terminal or a deadline expires, then respond with the current document and `settled: false`. **Timeout is a normal 200 response, not an error status** — this matters because the plugin maps 4xx/5xx into typed error envelopes (`client.py:542-586`), and a wait timing out must not look like a launcher failure.
- Accept `known_state` and `known_updated_at` echoes from the caller: if the attempt has changed since the caller's last observation, return immediately even if the new state is not terminal (it may have progressed to a different running state). This closes the gap between two consecutive wait segments.
- Cap `timeout_s` server-side (e.g. ≤ 60 s) so no connection is held open indefinitely; refuse a larger ask with a 400 and the cap in the message.
- The response body is the existing attempt document — the same fields `public_attempt` already maps — plus the wait metadata. No new data exposure.

**Plugin side (this repository).** Add `await` to `_ATTEMPT_OPS` (`tools.py:58`) and to the `bundlebees_attempt` schema enum (`tools.py:779-802`). The handler:

1. takes `attempt_id` (required), an optional model-facing `timeout_s`, and optional `known_state`/`known_updated_at` echoes;
2. loops launcher long-polls internally until the attempt settles, the plugin's own budget (`await_max_s`, a new plugin setting, conservative default — e.g. 240 s, kept well under any MCP/gateway tool-call ceiling) expires, or the model-facing `timeout_s` expires;
3. on settlement returns `settled: true` with the attempt snapshot and — exactly like `op=get` today (`tools.py:486-497`) — the `result` block when one exists, and the `no_result_yet` mapping when a terminal attempt has no result (a cancelled or failed attempt may legitimately have none);
4. on budget expiry returns `ok: true, settled: false, state: …, updated_at: …, waited_s: …` plus the recipe for the next call (`"call again with known_state=<state>"`) — the envelope itself teaches the model how to continue the wait without re-deriving it.

Two design constraints are mandatory:

- **Capability fallback.** A configured adapter may not support the wait op (an older launcher release, or an Orbit adapter without `await` parity). The existing pattern for optional capabilities is graceful: `op=get` treats `client.result` as optional (`tools.py:486-487`), and a missing method surfaces as `unsupported_operation` with a fix (`tools.py:888-900`). `await` follows it: when the client has no wait op, the handler degrades to a bounded client-side poll of `op=get` at a fixed interval (`await_poll_s`, e.g. 10–15 s) inside the same internal budget, and marks the envelope `mode: "poll_fallback"` so logs and the model can see it is not long-polling. The envelope shape is identical either way.
- **Terminal-on-failure.** `failed` and `cancelled` settle the wait exactly like `succeeded`. The coordinator must learn about failure as promptly as success, and `detail` carries the launcher's reason.

**Tradeoffs.**

- *For:* the coordinator's own tool call **is** the continuation — there is no schedule artifact to create, enforce, or drop, which removes Failure A's mechanism entirely; latency drops from "up to the next heartbeat" to "seconds after completion"; no LLM tokens are spent while the call blocks; the wait is a *consumer* of a durable launcher-side fact, so a gateway restart mid-wait loses nothing (§4.3); it composes with the heartbeat instead of replacing it (§6.4).
- *Against:* it needs launcher-side work and a release (the pin in `docs/stack-manifest.md` moves); it holds a gateway worker/thread for the duration of each bounded segment (mitigated by the internal budget and the server cap, but real under many concurrent bees); each expired segment costs one small model turn to decide to re-issue; and if the coordinator session is gone entirely — not just restarting mid-call — something still has to revive it, which is why the heartbeat stays.

### 3.2 Candidate B — durable launcher-side completion record, surfaced as `completed_since` and consumed by the heartbeat

**Launcher side.** Every transition of an attempt into a terminal state appends an immutable completion record to a cursor-addressable log: `attempt_id`, terminal `state`, a result hash (e.g. `result_sha256`), the timestamp (`finished_at`/`updated_at`), and the correlating ids (`project`, `branch`, `kanban_task_id`). The launcher already runs a mechanism of exactly this shape for landing events: the authenticated export `GET /v1/landing/events?limit=…&after=…` with schema `rizzstack.bee-landed-export/1`, page cursor, and raw-bytes preservation so consumers can verify hashes (`client.py:719-723`, `landing_events` at `client.py:1036-1088`, and the raw-span parser at `client.py:798-864`). Attempt-terminal records could ride the same export or a sibling `GET /v1/completions?since=<cursor>`.

**Plugin side.** A new status op — `bundlebees_status op=completed_since cursor=<n>` — returns the page plus `next_cursor`. The heartbeat's no-LLM scout consumes it: any new record means "wake the coordinator, here are the concrete attempt ids"; the cursor is stored in the plugin state directory, so after any gateway restart the scout re-reads its cursor and immediately sees completions that landed during the restart window.

**Tradeoffs.**

- *For:* no long-held requests and near-zero launcher cost; restart-safety by construction (a durable, cursor-addressed log is the most restart-friendly shape there is); it upgrades the scout from "diff the whole attempts list" to "read only what is new"; consumption is naturally de-duplicating (each record consumed once per cursor); it is the *right* long-term data source for the heartbeat.
- *Against:* latency is still bounded by the heartbeat period — up to ~10 minutes after completion, which is better than "never" but is precisely the delay the incident complained about; and, decisively, it **does not fix the wake itself**: the continuation still depends on a host cron existing, being `catch_up=true`, and firing. It repairs the scout's *content*, not the scheduler's *reliability*. If the heartbeat dies, a landed bee still waits for a human. It also does not make the coordinator's own tool call the continuation, which is what the card asks for.

### 3.3 Candidate C — push/webhook from launcher to plugin

The launcher would POST a completion event (a `rizzstack.bee.landed/1`-shaped payload) to an HTTP endpoint served by the plugin, and the plugin would wake the coordinator on receipt.

Evaluated honestly, this **likely cannot work**, and the architecture explains why:

- **The plugin is not a server.** It is an MCP toolset registered inside the Hermes gateway process (`tools.py:928`). It has no listening socket, no port, no routing table, and no lifecycle of its own. Its entire network posture is outbound: `_pinned_request` (`client.py:122`) dials a configured, validated endpoint and closes the connection. There is no code path in this repository that accepts an inbound connection.
- **A receiver would be a new network surface.** Serving webhooks means binding a port in or beside the gateway process, with its own authentication (the HMAC machinery in `bundlebees_plugin/bee_events.py` exists, but only for *outbound* signed delivery from the coordinator side — a receiver would have to be built first), firewall and host-policy implications across the documented reach classes (`same-machine`, `lan`, `vpn`, `ssh` — `tools.py:60`), and a second credential to manage. That contradicts the plugin's security posture, in which URLs are configuration, never arguments, and nothing listens (`client.py` module docstring, lines 1-9).
- **Push dies in exactly the window that caused Failure A.** Any listener inside the gateway process dies with the gateway. A push arriving during a restart window is lost unless the launcher retries with backoff and durable per-target state — at which point the launcher has re-implemented a durable queue and the consumer is polling it: polling with extra steps and a new attack surface.
- **The repo's own precedent is pull, not push.** Where the launcher and coordinator need asynchronous contact today, the design is a durable, consumer-pulled surface: the `/v1/landing/events` authenticated export (`client.py:1036`), the hyvv landing bridge that consumes a launcher outbox directory (`docs/bee-events.md`, "hyvv landing bridge"), and the durable outbox/inbox/ack trees in `bee_events.py`. Nothing in the stack pushes *into* the gateway.

Verdict: architecturally mismatched. Recorded here so the next proposer does not re-derive the conclusion.

### 3.4 Candidate D (variant of B) — a resident relay process on the coordinator host

Instead of (or in addition to) the cron-driven scout, a resident process polls the launcher — or, once it exists, the completion log of Candidate B — and delivers wakes through the machinery that already ships: the durable wake coalescer (`factory_driver.py:254`) and `notify_coordinator` (`hermes_adapter.py:2357`), with at-least-once delivery, generation cursors, and crash reconciliation for ambiguous wake outcomes (`factory_driver.py:1399-1500`). The factory driver already has a resident mode (`factory_driver --interval`, `docs/afk-operations.md`) and a documented host-unit template with restart-on-failure (`docs/CAMPAIGN-CONTINUITY.md`, "Scheduler").

- *For:* every primitive exists today except the completion surface; delivery is durable and restart-safe independent of the gateway (the relay holds the cursor, not the gateway); it converts the "no-LLM scout" from a fragile cron convention into a real supervised process.
- *Against:* it adds an owner-operated daemon — installing and supervising it is a host change requiring the owner's explicit yes (exactly the gate `docs/CAMPAIGN-CONTINUITY.md` records for the systemd unit); latency is the poll interval; and the wait still does not live in the coordinator's hands — the relay decides when the coordinator looks. It is the strongest *existing* machinery and the natural fallback if the launcher cannot ship a wait op, but it does not satisfy the card's "the coordinator can wait on it" framing.

### 3.5 Comparison

| | A: `op=await` long-poll | B: completion log + scout | C: push/webhook | D: resident relay |
|---|---|---|---|---|
| Removes the dropped-edge class (Failure A) | Yes — no schedule artifact | Partially — wake still cron-dependent | No — pushes lost in restart windows | Yes for the relay's own cursor |
| Latency after completion | Seconds | Up to heartbeat period | Seconds | Poll interval |
| Survives gateway restart mid-wait | Yes (§4.3) | Yes | No | Yes |
| New launcher capability required | Yes (wait op) | Yes (completion log/export event) | n/a (receiver impossible) | Optional (works on today's API) |
| New host component required | No | No | Yes (listener) | Yes (owner-gated daemon) |
| Coordinator's own call is the continuation | Yes | No | No | No |
| LLM cost while waiting | None while blocked; one small turn per expired segment | One wake turn per batch | One turn per push | One turn per wake |
| Composes with the 10-minute heartbeat | Yes (backstop) | Yes (it *is* the scout's upgrade) | — | Yes |

## 4. Recommendation

### 4.1 The pick

**Candidate A: the launcher-side blocking/long-poll wait op, exposed by the plugin as `bundlebees_attempt op=await`, with the bounded internal loop and the immediate-return-if-terminal semantics specified in §3.1.** One mechanism is recommended; Candidate B's completion log is explicitly *deferred* as a compatible follow-on hardening of the heartbeat scout, not part of this card.

### 4.2 Why it solves the recorded failure

Failure A's mechanism was a dropped edge: an edge-triggered, one-shot schedule whose firing instant could be deleted by a gateway restart, with nothing to re-evaluate it. `op=await` contains no schedule artifact at all. The continuation is the coordinator's own tool call, which is level-triggered by construction: the call asks "is this attempt terminal *now* (or within this deadline)?" — and the answer is read from the launcher's durable attempt record every time. There is no `next_run_at` to strand in the past, because there is no next run.

It also directly serves the card's framing: "a completion signal the coordinator can wait on durably." The signal is durable because it is a property of the launcher's attempt record (`state`/`updated_at`/`detail`, §1.3), not of any process's memory; the waiting is native because the coordinator's tool call *is* the wait.

### 4.3 Why a gateway restart mid-wait loses nothing

The key property: **the wait is a consumer of a durable fact, not the holder of it.** Walk the restart:

1. A coordinator turn calls `bundlebees_attempt op=await attempt_id=att_x`. The plugin holds one long-poll to the launcher; the gateway turn is blocked on the tool result, spending no tokens.
2. The gateway restarts. The in-flight tool call is destroyed — along with the turn, the session's runtime state, and everything else in the gateway process. Nothing was *stored* there that mattered: the fact being waited on (the attempt's terminal state) never left the launcher.
3. The session is revived — by the user, or by the 10-minute heartbeat cron, which is `catch_up=true` and therefore re-fires after any restart (§1.5). The revived turn re-issues `op=await` (or simply `op=get`); because the op is idempotent and returns immediately on a terminal attempt, it gets the settled answer at once, with the `result` block attached exactly as `op=get` produces it today.
4. Worst case, the wait degrades to heartbeat latency — minutes, never infinity. Under the old mechanism the worst case was *never*, which is the incident.

The launcher side is equally safe: if the *launcher* restarts mid-long-poll, the client sees a dropped connection — a transport error mapped to `launcher_unavailable` today (`client.py:626-631`). The await handler treats that as `settled: false` with an error note (one bounded retry, then return), because the durable fact is untouched and the next segment will re-read it.

### 4.4 What it does not solve, and what stays anyway

- If no turn is running and none is scheduled, nothing calls `op=await`. The 10-minute heartbeat remains the guaranteed revival path (§6.4). The card does not remove the heartbeat; it removes the *dispatch-time one-shot* class.
- The wait costs one gateway worker per waiting segment. The plugin's `await_max_s` budget and the launcher's server cap bound both.
- Candidate C is rejected (§3.3). Candidate D remains the documented fallback if the launcher cannot ship the wait op.

## 5. Staged rollout

Ordering principle: a server capability cannot be feature-detected into existence, so the launcher ships first — but the plugin ships *independently usable* by falling back to bounded polling on any launcher without the op, which decouples the two releases. RED-first means the failing test is written and committed before the implementation it judges (the repository's own convention — the ACDD/TDD protocol in `docs/ACDD.md` §5, and the CHANGELOG's "built test-first" entries for prior features). Each stage below names the file(s) that change and what its RED-first test asserts before the code exists.

**Stage 0 — this document.** Done; no code.

**Stage 1 — launcher side (launcher repository; this repo only records the contract).**
Specify and implement the long-poll wait op per §3.1. RED-first tests (launcher repo): an already-terminal attempt returns immediately with `settled: true`; a running attempt returns no later than `timeout_s` with `settled: false` and its state unchanged; a transition to terminal during the poll returns promptly with the new state and `updated_at` newer than `known_updated_at`; an unknown id is 404; a `timeout_s` above the server cap is a 400 naming the cap; connection occupancy is bounded (the server closes cleanly at the deadline). Gate: a launcher release carrying the op; `docs/stack-manifest.md`'s launcher pin is updated in this repo as part of the stage that adopts it.

**Stage 2 — plugin transport (`bundlebees_plugin/client.py`).**
Add the wait call to both clients: a wait method on `LauncherClient` (e.g. `await_attempt`) and an `op: "await"` passthrough on `OrbitAdapterClient._op` — plus the one transport change this genuinely needs, a **per-request timeout override**, since `_pinned_request` currently takes only the client-wide 10-second timeout (`client.py:41`, `client.py:617-623`). RED-first tests in `tests/test_client.py`: a long-poll response is decoded into the settled body; a deadline expiry returns `settled: false` and is *not* mapped to a typed error (the existing 408/504 → `launcher_unavailable` mapping must not swallow a normal wait timeout); same-origin redirect and token rules still hold for the wait endpoint (`client.py:638-669`); the per-request override does not leak into other calls. These tests fail before Stage 2's code exists and pass after.

**Stage 3 — plugin tool (`bundlebees_plugin/tools.py`, `bundlebees_plugin/settings.py`, `plugin.yaml`).**
Add `await` to `_ATTEMPT_OPS` (`tools.py:58`) and the schema enum (`tools.py:779-802`); add the `await_max_s` / `await_poll_s` settings to `DEFAULTS` (`settings.py:18-40`) and `plugin.yaml`'s `config_schema`; implement the handler loop, the poll-fallback mode, and the durable waiter record (§6.2) following the `factory/wakes` generation pattern (`factory_driver.py:254`). RED-first tests in `tests/test_tools.py`, using the existing `FakeClient` pattern (`tests/test_tools.py:24-135`): `op=await` settles on `succeeded`, `failed`, and `cancelled`, attaching `result` exactly as `op=get` does; the still-running envelope carries `settled: false` plus the next-call recipe and honours the internal budget; a client without the wait op produces the `poll_fallback` envelope rather than an error; a second concurrent wait on the same attempt is deferred with `waiter_active` rather than stacked; the schema-token delta is measured against the README's recorded budget (the combined tool schemas are tracked at 585 of a 900 o200k budget, `README.md:29`).

**Stage 4 — docs, skill, and operator playbook (this repo).**
README tool table and the bundlebees-operator skill gain the "wait, don't schedule" guidance; `plugin.yaml`'s tool description line is updated; `CHANGELOG.md` gains the entry (a changelog-conformance test, `tests/test_changelog_entries.py`, already gates this); the provisioning-stack skill's dispatch-time one-shot rule is marked deprecated *in its own repo* once Stage 5 lands.

**Stage 5 — owner-gated host steps (never performed by a code change).**
Exactly the gate `docs/CAMPAIGN-CONTINUITY.md` records for host changes: (a) updating the **installed checkout** — the copy of the plugin the running gateway actually imports — is the owner's action, not a commit; (b) the **gateway reload/restart** that makes the new toolset register is the owner's action; (c) deploying the launcher release from Stage 1 is the owner's action; (d) retiring the dispatch-time one-shot cron rule in the provisioning-stack skill playbook happens only after (a)–(c) are confirmed. The heartbeat cron is explicitly *not* retired.

## 6. Risks and failure modes

### 6.1 Restart windows

- **Gateway restart mid-wait:** analysed in §4.3 — the in-flight call dies, the durable fact survives, the revived session re-issues and gets an immediate answer; worst case is heartbeat latency.
- **Launcher restart mid-long-poll:** the client sees a dropped connection; the handler returns `settled: false` with an error note after one bounded retry rather than surfacing `launcher_unavailable` as if the wait had failed semantically. The next segment re-reads the attempt.
- **Scheduler-restart windows:** no schedule artifact exists on the await path, so the class of Failure A is not merely mitigated but absent. The heartbeat keeps `catch_up=true` as its own restart guarantee.

### 6.2 Duplicate continuation — two coordinators on one lane

The design narrows the recorded collision class (§2.2) with layered guards rather than a single new one:

1. **The durable waiter record.** While a turn waits on an attempt, the plugin records it durably under the plugin state directory — keyed by attempt id, carrying the waiting session's identity and a generation, in the style of `factory/wakes` (`factory_driver.py:254`). A heartbeat-driven (or any second) turn that finds a live waiter for an attempt gets `waiter_active` instead of a second long-poll, and the envelope tells it to defer. Stale records expire on a TTL with pid liveness, the same pattern `_expired` uses in the coalescer (`factory_driver.py:313-321`).
2. **Lease authority already fences acting.** Verify and accepted review fail closed without the dispatching coordinator's lease (`tools.py:271-297`, `tools.py:532-538`); a second coordinator state directory does not have it.
3. **Destructive actions stay confirm-gated.** `cancel`/`review` require `confirm=true` and a Hermes approval prompt (`tools.py:509-520`), so a human sees any second actor.
4. **Dispatch-time overlap stays refused** by `_lease_overlap` (`tools.py:165-196`).

Residual risk, stated honestly: two *interactive* coordinators in different state directories remain possible; the design makes them visible (waiter records) and gates their damage (leases, confirm prompts), it does not make them impossible.

### 6.3 Missed signals

The only way to miss a completion is "no call was waiting and the heartbeat did not wake" — which is today's baseline, unchanged. What improves: a wake can never again arrive *uselessly early* (the old one-shot's failure mode in reverse), because `op=await`/`op=get` answer immediately on a terminal attempt — every wake is decisive. And if the heartbeat scout is later upgraded with Candidate B's completion log, the sweep becomes cursor-based and even cheaper; the design leaves that seam open deliberately.

### 6.4 De-duplication with the 10-minute heartbeat — compose, don't replace

The heartbeat is retained and repurposed, for three reasons: it is the only restart-safe revival path that exists today (§1.5); it is the multi-bee sweep that no single `op=await` can be (one turn reviewing N running attempts); and it is the fallback that bounds the await design's worst case. The division of labour after this change:

- **The dispatch-time one-shot wakeup rule is retired** — it was the dropped-edge artifact, and `op=await` replaces its entire purpose.
- **A working turn** that has dispatched and has nothing else to do issues `op=await` and *holds the continuation in its own tool call*.
- **A heartbeat turn** first reads the waiter records: for attempts with a live waiter it defers (`waiter_active`); for running attempts with no waiter it may issue one bounded `op=await` itself — turning the heartbeat from "wake and hope" into "wake and hold", with the same restart safety, since a heartbeat turn that dies mid-wait is re-fired by `catch_up=true` and the next turn re-issues.

### 6.5 Resource and timeout semantics

Many concurrent bees mean many held requests: bounded by the launcher's server cap (≤ 60 s per long-poll) on the launcher and by `await_max_s` on the gateway side; the poll-fallback mode bounds the no-op case for older launchers. A long-poll that signalled timeout via an error status would collide with the client's error mapping (`client.py:542-586`) and manufacture false `launcher_unavailable` failures — hence the contract's "timeout is a 200 with `settled: false`" rule (§3.1), which is a correctness requirement, not a nicety.

### 6.6 Security and surface-area

The wait op adds no new data exposure (the response is the existing attempt document through the existing `public_attempt`/`public_result` mappers), no new URL or host surface (arguments are an attempt id and integers; URLs remain configuration-only), no new credential, and additive-only schema change (one enum value plus optional integer/string echoes). Scrubbing rules are untouched. The schema-token delta is measured in Stage 3 against the recorded budget (`README.md:29`).

## 7. Open questions

1. **Launcher implementation venue.** The wait op lives in the launcher product (pinned `0.1.16`, `docs/stack-manifest.md`); the contract in §3.1 is this repo's proposal of record, but the implementation and its release cadence are the launcher team's.
2. **Orbit loopback parity.** Whether the default `orbit-loopback` adapter gains `op: "await"` in the same release, or whether `poll_fallback` is the expected mode for loopback deployments for a while — decide with the Orbit owners at Stage 1.
3. **Gateway tool-call ceiling.** The MCP/gateway's wall-clock limit on one tool call is not recorded in this repo; measure it before choosing the shipped default of `await_max_s` (the design's 240 s is a placeholder chosen to be safely conservative).
4. **Provisioning-stack skill retirement step.** Deprecating the dispatch-time one-shot rule is an edit in the skill's own repository; sequence it with Stage 5's owner-gated confirmation.
