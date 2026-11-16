# Coordinator actions

`bundlebees_plugin/coordinator_actions.py` is the coordinator's durable,
per-item action queue. It records, claims and fences coordinator actions —
merges, deploys, reports — and it does nothing else. It never executes an
action, never spawns a process, never calls a model and never dispatches work:
execution stays with the caller, which presents its fencing value for every
mutation and acts only while it is the current owner.

The design follows the classic Kleppmann lease-and-fence pattern. Every claim
mints a monotonically increasing fencing value from a durable counter; the
value is written and fsynced before the claim record exists, so a crash can
burn a value but can never reuse one. A paused owner that wakes up after
another owner adopted its item presents a value below the current one and is
refused with a precise stale refusal. The lease expiry is the adoption hint;
the fencing value is the guarantee.

## Durable layout

One directory per coordinator state directory, next to the work scheduler:

```
<state>/coordinator/actions/<action_id>.json   # one record per action
<state>/coordinator/actions/fence.seq          # the durable fencing counter
<state>/coordinator/actions/actions.lock       # exclusive flock for mutations
```

Records are replaced atomically (write temp file, `fsync`, `os.replace`, fsync
the directory), created `0600` inside a `0700` directory. Every read-modify-
write holds an exclusive `flock` on `actions.lock`, so claims serialize across
processes, not merely threads.

```json
{
  "schema": "bundlebees.coordinator-actions/1",
  "action_id": "act_merge_42",
  "action_key": "merge-pr-42-attempt-3",
  "kind": "merge",
  "repo": "mojomast/bundlebees",
  "target": "",
  "workflow_ref": "9c69d3a987ea9544a77cdd9b5a259904eb36f006",
  "payload": {"pr": 42},
  "status": "claimed",
  "fence_seq": 2,
  "owner": "bee-7",
  "claimed_at": "2027-01-15T08:00:00Z",
  "lease_expires_at": 1800000900.0,
  "adopted_from": null,
  "result": null,
  "created_at": "2027-01-15T07:55:00Z",
  "updated_at": "2027-01-15T08:00:00Z"
}
```

### Fields

| Field | Meaning |
|---|---|
| `action_id` | Safe path component (letters, digits, `-`, `_`, `.`; at most 128). One file per action. |
| `action_key` | The caller's idempotency key for this logical action. Replay matches on it. |
| `kind` | A short label. Only `merge` and `deploy` hold repo leases. |
| `repo` | The repository the action names, when it has one. Never interpreted. |
| `target` | Optional sub-target inside the repo (for example a branch). Empty means the whole repo. |
| `workflow_ref` | The exact workflow protocol commit (full 40-hex SHA, stored lowercased) under which the action was authorized and will be executed. Empty only for the legacy unpinned shape. See "The protocol pin". |
| `payload` | Caller data the queue stores verbatim and never interprets. |
| `status` | `queued`, `claimed` or `done`. |
| `fence_seq` | The fencing value minted at claim time; `0` while never claimed. |
| `owner` | The current or last owner. Kept after completion for the audit trail. |
| `claimed_at` | ISO timestamp of the last claim or adoption. |
| `lease_expires_at` | Epoch seconds after which the claim is adoptable; `null` when no lease is live. |
| `adopted_from` | The previous owner after a stale-owner adoption, else `null`. |
| `result` | The caller's completion result, stored verbatim. |
| `created_at`, `updated_at` | ISO timestamps, second resolution. |

## Enqueue and idempotent replay

`enqueue(state, action_id, action_key=..., kind=..., repo="", target="",
workflow_ref=..., payload=None, now=None)` is create-only. Replaying an
existing `action_id` with its original `action_key` and shape (kind, repo,
target and `workflow_ref`) returns the stored record unchanged — including
the originally stored payload and pin, however the replay's payload or pin
differs. Nothing else happens: no new file, no fencing value, no timestamp
churn. Three shapes of misuse are refused precisely:

* the same `action_key` under a different `action_id` — `action_key_conflict`,
  naming the action that already holds the key;
* a different `action_key` under the same `action_id` — `action_id_conflict`,
  naming the stored key;
* the same key and shape under a different `workflow_ref` — also
  `action_id_conflict`: the pin is part of the action's authorization, and a
  replay under another protocol commit is a different action.

Unsafe ids, labels, kinds, pins and payloads are refused before anything is
written (`invalid_action_id`, `invalid_action_key`, `invalid_kind`,
`invalid_repo`, `invalid_target`, `invalid_workflow_ref`, `payload_invalid`).

## The protocol pin

Every durable record carries `workflow_ref`, the exact workflow protocol
commit under which the action was authorized and will be executed. The
owner's convention of Nov 13: *workflow is a dependency of execution, not
merely a source of inspiration*. The queue is the coordinator's durable
memory of actions that must happen at most once and never unsupervised, so
each of those actions binds to the protocol revision it ran under, and the
`result` recorded at completion sits in the same record as its pin — verify
and review evidence can always name the protocol the action was executed
under.

The pin is taken at enqueue and never changes: `claim`, `adopt`, `renew` and
`complete` carry it untouched and every returned record surfaces it. A
caller-supplied pin must be a full 40-hex commit SHA and is stored
lowercased; a shorter, malformed or non-string pin is refused with
`invalid_workflow_ref`. Passing `workflow_ref=None` or an empty string — a
declaration that the action has no protocol commit — is refused at the
append with `missing_workflow_ref`. Omitting the argument entirely is the
legacy unpinned shape, kept for callers that do not know their protocol
commit yet: the record still carries the field, empty, exactly the way
main's job records carry it.

The pin is also a write-path invariant, not only an intake check. Every
durable action-record write — the enqueue append and every claim, adoption,
renewal and completion rewrite — runs a validator first: a record that does
not carry the `workflow_ref` field is refused with `missing_workflow_ref`
and left exactly as it was, before any fencing value is minted and before
any lease changes, so a pre-convention record can never be silently
perpetuated through the fence path. Reads stay permissive (`load`,
`list_actions` return such a record as-is) so an operator can see what is
stored; the next mutation is what refuses it.

## Claims and fencing

`claim(state, action_id, owner, ttl_s=900.0, now=None)` claims a queued action
atomically: it refuses an unknown action (`unknown_action`), a done one
(`action_done`), an actively held claim (`claim_active`, naming the owner and
the expiry), and an expired one (`claim_expired`, pointing at adoption). A
refused claim never mints a fencing value.

Each successful claim mints the next `fence_seq` from `fence.seq` and stamps
owner, expiry and timestamps onto the record. Later mutations
(`renew`, `complete`) must present the exact value the claim returned:

* a presented value below the current one is `stale_fence` — the message names
  the presented value, the current one and the owner who now holds it;
* any other mismatch, including a value that was never minted or one minted
  for a different action, is `fence_mismatch`;
* a value that is not an `int` is `fence_invalid`.

The fence, not the lease, is the guarantee: an owner whose lease lapsed but
who was never adopted can still complete on its current `fence_seq`, because
nobody else holds a higher one. Once another owner adopts, that window closes.

## Stale-owner adoption

`adopt(state, action_id, owner, ttl_s=900.0, now=None)` takes over an action
whose claim expired. It refuses an actively held claim (`claim_active`) and an
item that was never claimed (`nothing_to_adopt`). Adoption mints a higher
`fence_seq`, records the previous owner as `adopted_from`, and from that
moment the previous owner's mutations are refused as `stale_fence`.

`renew(state, action_id, fence_seq, ttl_s=900.0, now=None)` extends the lease
for the current fenced owner only. It runs the per-repo gate too: a renewal
that would make a lapsed lease live again across an overlapping live merge or
deploy lease on the same repository is refused with `lease_conflict` and
extends nothing. With no overlapping claim live, a lapsed lease renews on its
still-current fence.

## Per-repo serialization

Only `merge` and `deploy` actions hold repo leases, and only overlapping ones
serialize. Two leases overlap when they name the same repository and their
targets are compatible: an empty target overlaps everything in its repo, and
two non-empty targets overlap only when they are equal. Concretely:

* a merge and a deploy on one repo block each other until completion,
  adoption or expiry;
* two merges on one repo block each other the same way;
* actions in different repos, or on distinct non-empty targets, never wait
  for each other;
* actions outside the merge and deploy family — notes, reports, anything else
  — never block anything, whatever their repo;
* a completed or expired claim holds no lease, so the next overlapping claim
  proceeds immediately.

A conflicting claim is refused with `lease_conflict`, naming the blocking
action, its kind, its owner and the expiry, and it burns no fencing value.

The gate runs wherever a lease could become live: at `claim`, at `adopt` and
at `renew` alike. A renewal that would extend a lapsed lease across an
overlapping live claim is refused with `lease_conflict` and extends nothing —
the expired owner's route back is adoption, which mints a higher fence — so
two live overlapping leases on one repository can never exist.

## Refusals

| Code | Meaning |
|---|---|
| `unknown_action` | No record under that id. |
| `action_done` | The action completed; enqueue a new one. |
| `not_claimed` | A mutation needs a claim and its current `fence_seq`. |
| `claim_active` | Another owner holds an unexpired claim; the message names it. |
| `claim_expired` | The claim lapsed; adoption, not a fresh claim, is the takeover. |
| `nothing_to_adopt` | Adoption needs an expired claim to take over. |
| `lease_conflict` | An overlapping merge or deploy lease is live on the repo; checked at claim, adoption and renewal. |
| `stale_fence` | The presented fencing value was superseded; the message names the current one and its owner. |
| `fence_mismatch` | The presented fencing value is not the current one in any direction. |
| `fence_invalid` | The presented fencing value is not an int. |
| `action_key_conflict` | The key already names another action. |
| `action_id_conflict` | The id already exists with a different key, shape or protocol pin. |
| `missing_workflow_ref` | The record does not carry its `workflow_ref` protocol pin: an enqueue that declared no pin, or a mutation of a stored record that lacks the field. |
| `invalid_workflow_ref` | The supplied `workflow_ref` is not a full 40-hex commit SHA. |
| `payload_invalid`, `result_invalid` | The value does not survive a JSON round trip. |
| `state_corrupt` | A record is unreadable or not this schema; fail closed. |
| `fence_counter_corrupt` | The counter cannot be read or is not this schema; the next value is unknown, so nothing claims. |
| `clock_invalid`, `invalid_ttl`, `locking_unavailable` | Caller or platform misuse. |

## Durability

The counter is replaced and fsynced before the claim record is written, so a
crash between minting and claiming burns the value instead of reusing one;
the next claim mints a strictly higher value. The fault checkpoint
`coordinator_actions_fence_allocated` fires exactly between the two writes so
crash tests can prove this. Without a registered hook the checkpoint is a
no-op, and with no `flock` on the platform the queue refuses to claim at all
rather than prove nothing.

## The queue is not an executor

An AST guard in `tests/test_coordinator_actions.py` parses the module and
asserts three things: every import comes from a small bookkeeping allowlist
(stdlib data handling, `bundlebees_plugin.security`, the lazy fault-checkpoint
lane), no bare name or attribute is an execution verb (`system`, `Popen`,
`spawn`, `fork`, `exec`, `eval`, `run`, `dispatch`, and friends), and the
public surface names no such verb. The queue only ever reads and writes its
own JSON records.
