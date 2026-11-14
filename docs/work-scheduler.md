# Coordinator work scheduler

`bundlebees_plugin/work_scheduler.py` is the coordinator's durable, deterministic
work queue. It answers three questions and nothing else:

1. Which work is eligible right now?
2. Who owns each active item?
3. Which item, if any, may a landing event safely preempt?

It is deliberately **agent-agnostic**. A queue record names *what* should
happen and which files it leases; it carries no prompt, argv, model id,
adapter, approval, scope or budget. The scheduler never calls a model, never
dispatching a bee, and never sleeps or spawns anything. Dispatch stays outside
this module.

## Durable queue

One JSON file per coordinator state directory:

```
<state>/coordinator/work.json
<state>/coordinator/work.lock
```

`work.json` is replaced atomically (write temp file, `fsync`, `os.replace`,
fsync the directory) and is written with sorted keys and a sorted item list.
Two coordinators that perform the same operations with the same clock produce
byte-identical queues. Every mutation takes an exclusive `flock` on
`work.lock`, so a claim is serialised across processes, not merely across
threads.

```json
{
  "schema": "bundlebees.work-scheduler/1",
  "items": [
    {
      "work_id": "ws_plan_api",
      "title": "Plan the API",
      "kind": "research",
      "priority": 3,
      "status": "pending",
      "depends_on": ["ws_contract"],
      "owner": "",
      "lease_files": ["src/api.py"],
      "worktree": "",
      "checkpoint": null,
      "created_at": "2027-01-15T08:00:00Z",
      "updated_at": "2027-01-15T08:00:00Z"
    }
  ]
}
```

### Fields

| Field | Meaning |
|---|---|
| `work_id` | Safe path component (letters, digits, `-`, `_`, `.`; at most 96). Stable identity. |
| `title` | Human label. Never interpreted. |
| `kind` | `research`, `spec`, `tests`, `docs`, `integration-plan`, `triage`, `feature`. |
| `priority` | `0`–`9`; **0 is most urgent**. |
| `status` | `pending`, `blocked`, `claimed`, `done`. |
| `depends_on` | Upstream `work_id`s that must be `done` first. Duplicates dropped, order kept. |
| `owner` | Non-empty only while `claimed`; one owner holds at most one item. |
| `lease_files` | Normalised worktree-relative file or directory paths this item needs. |
| `worktree` | Where the owner works; opaque metadata for the scheduler. |
| `checkpoint` | `{note, evidence[], at}`, or `null`. Written only by `checkpoint()`. |
| `created_at`, `updated_at` | UTC second-resolution ISO timestamps from the injected clock. |

## API

All mutating calls accept `now`, either epoch seconds, an ISO timestamp or a
callable returning either. Omitting it uses the wall clock; tests inject a
fixed clock so ordering and timestamps are deterministic.

### `add_item(state, *, work_id, title="", kind="feature", priority=5, depends_on=(), lease_files=(), worktree="", now=None)`

Create-only. Replaying an identical item returns the stored record and writes
nothing. A different `title`, `kind`, `priority`, `depends_on`, `lease_files`
or `worktree` at the same `work_id` raises `work_exists`; intent is never
silently overwritten. Self-dependency, an unknown kind, a priority outside
0–9, an unsafe id or a lease path escaping the worktree is refused.

### `list_items(state)`

Every item, ordered by `created_at` then `work_id`.

### `ready_items(state, active_bee_leases=None)`

`pending` items whose dependencies are all `done` and whose `lease_files` do
not intersect an active lease. `active_bee_leases` is an owner-to-files
mapping (or a flat iterable of files) for bees running outside the queue;
claims recorded inside the queue always count as active too.

Order is deterministic: **priority (0 first), then `created_at`, then
`work_id`**. A dependency that does not exist yet is never satisfied, so the
dependent item stays out of the ready set instead of being guessed at.

### `claim(state, work_id, owner, worktree="", *, now=None, max_active=None)`

Atomic. Under the queue lock it refuses when:

- the item is not `pending` (an existing claim by the same owner is
  idempotent and returns the record);
- a dependency is unfinished — `dependency_blocked`;
- the item or one of its leased files is already active — `lease_conflict`;
- the owner already holds an item — `owner_busy` (max one active item per
  owner);
- bounded concurrency is exhausted — `capacity_exhausted`.

Capacity is `MAX_ACTIVE` (4) by default. Planning kinds (`research`, `spec`,
`docs`, `integration-plan`, `triage`) may use at most
`MAX_ACTIVE - RESERVED_FOR_LANDING` slots. One slot is always reserved so a
landing event never waits for planning work to drain. Delivery kinds (`tests`,
`feature`) may use the reserved slot.

### `checkpoint(state, work_id, note, evidence=(), *, now=None)`

Only a `claimed` item may checkpoint. `note` must be non-empty. The record is
replaced atomically and **fsynced file and directory before the call
returns**; a crash after return cannot lose an acknowledged checkpoint. The
checkpoint is advisory evidence for a human or the owner — the scheduler never
executes it.

### `complete(state, work_id, *, owner=None, now=None)`

`claimed -> done`, freeing the owner and worktree. Idempotent once `done`.
When `owner` is given it must match the stored owner (`not_owner` otherwise).
Completing a dependency is what makes its dependents eligible.

### `release(state, work_id, *, owner=None, now=None)`

`claimed -> pending`, clearing owner and worktree but **keeping** the last
checkpoint. Idempotent when already `pending`; the current owner is enforced.

### `resume(state, work_id, *, now=None)`

Reports where to pick up after a landing event or a process restart. Every
call re-reads the queue from disk, so resume survives a crash by construction.
Requires a checkpoint (`no_checkpoint` otherwise). A `blocked` item whose
dependencies are now `done` returns to `pending` (`resumed: true`); a blocked
item with unmet dependencies stays blocked. Returns:

```json
{
  "work_id": "ws_plan_api",
  "status": "pending",
  "owner": "",
  "worktree": "",
  "checkpoint": {"note": "...", "evidence": [], "at": "..."},
  "resumed": true,
  "reason": "unblocked"
}
```

### `preempt_for_event(state, event, *, now=None)`

A **query that never mutates the queue**. It returns an advisory checkpoint
action only, so the coordinator can never interrupt a non-idempotent operation
from here. `event` is a mapping with `kind` (one of `landing`, `land`,
`merge`, `integration`), optional `lease_files`, and optional `max_active`
(matching the coordinator's configured bound).

- Non-landing event → `{"action": "none", "reason": "event_not_landing"}`.
  Planning events never preempt.
- Capacity already free → `{"action": "none", "reason": "capacity_available"}`.
- No active planning item is sitting on a durable checkpoint →
  `{"action": "none", "reason": "no_safe_checkpoint"}`. An item that has never
  checkpointed is never proposed, because it may be mid-operation.
- Otherwise → `{"action": "checkpoint", "work_id", "owner", "worktree",
  "checkpoint", "then": "release", "safe": true,
  "interrupts_non_idempotent": false}`.

The victim is deterministic: overlap with the event's `lease_files` first
(freeing the files the landing needs), then **least urgent** (`priority` 9
first), then oldest `created_at`, then `work_id`. The coordinator sends the
owner to its checkpoint and then calls `release()`; the scheduler itself does
not interrupt anyone.

### `sleep_when_idle(ready)`

The idle signal, and deliberately a no-op: it never sleeps, waits, dispatches
or changes state. When `ready` (the result of `ready_items`) is empty it
returns `{"sleep": true, "reason": "no_eligible_work"}`; otherwise
`{"sleep": false, "reason": "work_available"}` with the work ids. A
work-conserving coordinator calls it only to decide whether it may wait for
the next event.

## Invariants

- **Deterministic.** No randomness, no wall-clock ordering, no set iteration
  in output. Same clock + same calls = same bytes.
- **Create-only.** `add_item` never rewrites intent; a duplicate id with
  different content is an error.
- **One owner, one item.** Claims are all-or-nothing under `flock`.
- **Leases are respected.** A pending item is not ready and cannot be claimed
  while another active owner holds any of its files.
- **Durable checkpoints.** fsynced before acknowledgement; resume reads disk.
- **Safe preemption only.** Only a checkpointed planning item may be proposed;
  the proposal is advisory and carries no state change.
- **Reserved landing capacity.** Planning can never consume the last slot.
- **No agent.** No model call, no bee dispatch, no approval, scope or budget
  mutation. `tests/test_work_scheduler.py` parses the module's AST to keep it
  that way.

## Example

```python
from bundlebees_plugin import work_scheduler as ws

ws.add_item(state, work_id="ws_contract", kind="spec", priority=2,
            lease_files=["src/contract.py"])
ws.add_item(state, work_id="ws_api", kind="feature", priority=4,
            depends_on=["ws_contract"], lease_files=["src/api.py"])

ws.ready_items(state)                       # -> [ws_contract]
ws.claim(state, "ws_contract", "bee-1", "wt-1")
ws.checkpoint(state, "ws_contract", "tests green", ["pytest -q: 40 passed"])
ws.complete(state, "ws_contract", owner="bee-1")

ws.ready_items(state, {"bee-9": ["src/api.py"]})   # []: another bee leases api.py
ws.ready_items(state, {"bee-9": ["src/other.py"]}) # [ws_api]

event = {"kind": "landing", "lease_files": ["src/api.py"]}
ws.preempt_for_event(state, event)          # advisory only
ws.sleep_when_idle(ws.ready_items(state))   # no-op signal
```

Run the suite:

```bash
python3 -m pytest tests/test_work_scheduler.py -q
```
