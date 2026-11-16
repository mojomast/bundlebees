# Gate watcher — the mechanical planned-to-queued transition

Today's lesson, recorded with the completed-attempt reconciliation sweep, is
that **a campaign transition that depends on coordinator attention is the
failure class**: the sweep exists because finished attempts waited for a human
to look at them. The planned backlog had the same shape — a planned slice
moved to queued only when the coordinator happened to notice that its gate had
cleared. The gate watcher removes that dependency: the planned→queued
transition is now a mechanical beat duty, the same fix pattern as the sweep.

## What it is

`bundlebees_plugin.gate_watcher` keeps one machine-readable planned-work
backlog at `<root>/plans/backlog.json` (schema
`bundlebees.gate-backlog/1`, written atomically, mode 0600, serialized by a
kernel-held lock at `plans/backlog.lock`). Each slice carries a pre-written
dispatch brief and declares its gates:

```json
{
  "schema": "bundlebees.gate-backlog/1",
  "budget": {"lanes": 7, "bees": 10},
  "slices": [
    {
      "id": "F16",
      "title": "F16: wave-6 feature slice (after #44 and its deps)",
      "project": "orbitdesktop",
      "lane": "features",
      "priority": 3,
      "brief": { "objective": "...", "source_rev": "<40-hex>",
                 "assigned_files": ["..."], "acceptance": {"criteria": ["..."]} },
      "gates": [{"type": "pr_merged", "pr": 44}],
      "depends_on": [],
      "status": "planned",
      "transitions": []
    }
  ],
  "transitions": [],
  "decisions": []
}
```

* **status** is exactly one of `planned`, `queued`, `dispatched`,
  `blocked-owner`. There is no separate `blocked` status: a stale gate marks a
  *planned* slice with a durable `blocked` record (`reason`, `gate`,
  `evidence`, `at`) and the watcher re-evaluates it every beat, so a changed
  world (a PR reopens, a retry attempt is recorded) un-blocks without a
  coordinator.
* **gates** is a list; every gate must clear. Types:
  | type | field | clears when | stale when |
  |---|---|---|---|
  | `pr_merged` | `pr` (int) | the PR reports `merged` | the PR is `closed` unmerged |
  | `attempt_succeeded` | exactly one of `attempt` (id), `slice` (backlog slice whose recorded attempt is probed), `fact` (campaign-record landing fact) | the probe reports a success state (`succeeded`, `landed`, …) | the probe reports a failure state (`failed`, `rejected`, …) |
  | `date` | `on` (ISO date/datetime, UTC) | now ≥ the date | never |
  | `owner_decision` | optional `question` | a recorded `approve` decision | a recorded `decline` (terminal for that slice id) |
  | `none` | — | always | never |
* An **unprovable** gate state is `waiting`, never `cleared`: unknown never
  implies success.
* **depends_on** lists sibling slice ids. A dependency is satisfied when the
  dependency slice is `dispatched` *and* its recorded attempt has landed — a
  dependent never starts while the work it builds on is still in flight.

## The beat duty

`run_beat(root, world=…, dispatcher=…, now=…)` is a first-class beat duty, run
on the same cadence as the reconciliation tick (the same pass that sweeps
completed attempts). One beat:

1. **Consumes the decision queue** — a recorded `approve` moves a
   `blocked-owner` slice back into the mechanical flow (straight to `queued`
   when its remaining gates also clear); a `decline` stays terminal.
2. **Evaluates every planned slice's gates** against live state. Gate
   evidence is carried on every transition record.
3. **Queues** every eligible slice (planned→queued, with the gate evidence)
   unless a lease conflict says it cannot run yet.
4. **Dispatches** queued slices while the budget allows
   (queued→dispatched with the attempt id), persisting after each dispatch.
5. **Reports** — `queued`, `dispatched`, `routed_owner`, `stale`,
   `waiting`, `skipped_budget`, `lease_skipped`, `dispatch_errors`,
   `failed_attempts`, `budget`, `changed`.

The world and the dispatcher are injected seams, exactly like the
reconciliation tick's launcher:

```python
from bundlebees_plugin import gate_watcher as gw

class World:                       # exactly these five members
    def pr_state(self, pr): ...            # "merged"|"open"|"closed"|None
    def attempt_state(self, attempt_id): ...
    def fact_state(self, fact): ...        # "landed"|"failed"|other|None
    def active_bee_leases(self): ...       # owner -> assigned files
    def active_bee_count(self): ...        # live bees

class Dispatcher:                  # exactly this one member
    def dispatch(self, payload):   # {"brief", "project", "kanban_task_id"}
        return {"attempt_id": "...", "state": "running"}   # attempt_id required

report = gw.run_beat(root, world=World(), dispatcher=Dispatcher(), now=None)
```

The real wiring reads the campaign record, the git/PR observer
(`git_observer`) and the launcher attempts list; the watcher itself never
opens a socket, runs git, or reads a token.

## Guarantees

* **Idempotent.** A dispatched slice is marked with its attempt id and is
  never re-dispatched. A beat against unchanged state writes nothing — the
  same no-rewrite rule as `campaign.reconcile`.
* **Fail closed.** The world adapter is validated structurally; a probe that
  cannot observe returns None and the gate waits. A dispatcher result without
  an `attempt_id` aborts the beat with `dispatch_result_invalid` and the fix
  text points at the launcher journal — the watcher never marks a slice
  dispatched on an unconfirmed dispatch.
* **Owner gates never dispatch.** The slice routes to the decision queue:
  status `blocked-owner`, an `owner_request` record, and a recorded
  planned→blocked-owner transition. The owner records the decision with
  `record_owner_decision(root, slice_id, decision="approve"|"decline",
  by=…)`; the append-only log lives in the backlog document itself, so the
  decision is a durable record, never a chat message.
* **Budget.** The owner envelope is enforced in code: the lane budget must
  sit inside 5–7 implementation lanes and the bee cap at 10. Dispatch stops
  at `lane_budget` / `bee_cap` and the slice parks in `queued` for the next
  beat.
* **Lease rules.** A slice whose brief leases a file that an active bee
  already holds — or that another queued/dispatched slice holds — is not
  queued or dispatched (`lease_conflict`).
* **Every transition recorded.** Each move is written per slice and in the
  top-level `transitions` log with its gate evidence:
  `{"slice", "from", "to", "at", "reason", "evidence"}`.
* **Deterministic.** The clock is injectable in every call; dispatch order is
  `(priority, created_at, id)`.

## Seeded backlog

`seed_backlog(root)` writes the current known planned slices with the real
gates from the campaign record (create-only; an existing backlog is refused):

| slice | lane | gate |
|---|---|---|
| `ov0-spike` | orbit | none — the spike the orbit lane waits on |
| `go-switch` (Nov 29) | release | `date` 2026-11-29 |
| `F16` | features | `pr_merged` #44 (a merged #44 implies its dependency merges) |
| `F18`, `F19` | features | `depends_on: [F16]` — the campaign record's "after deps", seeded conservatively through the cluster's lead slice |
| `OV-A` | orbit | `attempt_succeeded` on the `ov0-spike` slice's attempt |
| `R02`, `R03` | rails | `pr_merged` #40 (the R02+ series) |
| `F5` | features | `attempt_succeeded` fact `F2` |
| `M2` canary | canary | `attempt_succeeded` fact `F2` |
| `F14` | features | `attempt_succeeded` fact `M1` |
| `wave6-closeout` | closeout | `owner_decision` — the one owner-gated slice |

Seeding uses six lanes (features, orbit, rails, canary, release, closeout),
inside the 5–7 envelope. Two things to re-pin from the campaign record before
the first beat that can dispatch real work:

1. **`source_rev`** — the seeded briefs carry deterministic placeholder
   digests; re-pin each brief's revision from the campaign record.
2. **Exact dependency edges** — `F18`/`F19` are seeded as `depends_on: ["F16"]`
   (a conservative superset of "after deps"); tighten the edges when the full
   campaign record differs.

If a seeded slice's work was already committed outside the backlog (an
already-dispatched spike), re-seed it with its recorded `status`/
`attempt_id` so the watcher adopts rather than repeats it.

## Failure codes

| code | meaning |
|---|---|
| `backlog_exists` / `backlog_missing` / `backlog_corrupt` / `backlog_unreadable` | the backlog document |
| `slice_invalid` / `slice_exists` / `slice_not_found` | slice intake |
| `gate_invalid` | a gate outside the five types or missing its reference |
| `brief_invalid` | a slice's pre-written brief failed the strict brief contract |
| `decision_invalid` | an owner decision outside `approve`/`decline` |
| `budget_invalid` | a lane budget outside 5–7 or a bee cap above 10 |
| `world_incompatible` / `dispatcher_incompatible` | the injected seams |
| `dispatch_result_invalid` | a dispatch without an attempt id; reconcile the journal |
| `clock_invalid` / `identifier_invalid` / `locking_unavailable` | plumbing |

## Tests

`tests/test_gate_watcher.py` (offline, no network, no launcher): gate
evaluation per type, auto-dispatch on gate clear, owner-gate routing and the
decision queue, stale-gate blocking and recovery, lane budget / bee cap /
lease rules, idempotence and no-rewrite, dependency edges, and the seeded
backlog end to end.

```bash
python3 -m pytest tests/test_gate_watcher.py -q
```
