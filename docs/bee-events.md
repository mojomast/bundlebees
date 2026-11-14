# Bee landing events

`bundlebees_plugin/bee_events.py` is the durable landing-event outbox. It lets
the coordinator record that a bee attempt reached a state transition, then lets
any number of local subscribers receive that event exactly once per
`(subscriber, event_id)` pair without a broker, database, server or network
call.

The event type is `rizzstack.bee-landed/1`. The wire shape is CloudEvents 1.0:

```json
{
  "specversion": "1.0",
  "id": "evt_6ce11a6b683fd09e9ff4936eed552209",
  "source": "rizzstack://bundlebees",
  "type": "rizzstack.bee-landed/1",
  "subject": "attempt/att_bee_1",
  "time": "2026-11-14T14:50:11Z",
  "datacontenttype": "application/json",
  "data": {
    "schema": "rizzstack.bee-landed/1",
    "attempt_id": "att_bee_1",
    "state": "accepted",
    "transition_id": "tr_0001",
    "feature_id": "BEE-1",
    "job_id": "job_1",
    "task_id": "task_1",
    "coordinator": "coordinator-1",
    "lease_sha256": "…64 hex…",
    "artifact_sha256": "…64 hex…",
    "reference": "refs/heads/feat/bee-events",
    "backend": "hermes"
  }
}
```

The id is deterministic: `event_id_for(attempt_id, state, transition_id)` is
the SHA-256 of those three facts. Recording the same landing twice is a no-op;
delivery retries reuse the same id so consumers can deduplicate safely.

## State layout

All paths are under the coordinator's `state_dir`:

| Path | Meaning |
|---|---|
| `events/outbox/<event_id>.json` | Immutable landing event, created once. |
| `events/subscribers/<identity>.json` | Durable subscription record. |
| `events/inbox/<identity>/<event_id>.json` | Delivered event plus `delivery_id`, `attempts`, `delivered_at`. |
| `events/acks/<identity>/<event_id>.json` | Durable consumer acknowledgement. |
| `events/delivery/<identity>/<event_id>.json` | Attempt counter used across crashes. |

Directories are `0700`; files are `0600`. Outbox, subscriber and ack records
are published with `O_EXCL`/link semantics and fsynced before the parent
directory is fsynced. Deliveries are created the same way; only `replay`
replaces an existing delivery, atomically.

## Operations

### Record a landing

```python
from bundlebees_plugin import bee_events

event = bee_events.build_event(
    {"attempt_id": "att_bee_1", "state": "accepted", "transition_id": "tr_0001"},
    feature_id="BEE-1",
    job_id="job_1",
    task_id="task_1",
    coordinator="coordinator-1",
    lease_sha256=lease_sha,
    artifact_sha256=artifact_sha,
    reference="refs/heads/feat/bee-events",
    backend="hermes",
)
bee_events.record_landing(state_dir, event)
```

`record_landing` is create-only. If the same event id already exists with
identical canonical bytes it returns the stored record with `created: false`.
Different bytes under the same id raise `event_conflict`; the original is never
overwritten. A write failure raises `event_write_failed` and leaves no partial
outbox file.

### Validate against the pinned workflow schema

```python
bee_events.validate_event(event)   # True, or raises EventError
```

Validation is fail-closed: when `WORKFLOW_SRC` is unset, unreadable, or has no
landing schema, it raises `workflow_schema_unavailable`. When the checkout is
present, the schema is loaded from the pinned revision (normally
`schemas/bee-landed.schema.json`) and the whole CloudEvents envelope is
validated. `record_landing` performs the structural, size and security checks
but does not claim to have run the pinned validator; call `validate_event`
explicitly after setting `WORKFLOW_SRC` when that proof is required.

### Subscribe and unsubscribe

```python
bee_events.subscribe(state_dir, {
    "identity": "ops",
    "file": "inbox.jsonl",          # or "url": "https://example.invalid/hook"
    "session": "run_1",             # optional
})
bee_events.unsubscribe(state_dir, "ops")
```

A subscriber needs exactly one of `file` or `url`. URLs must be `http(s)` and
must not embed credentials; the subscription is metadata and this module never
makes an outbound request. Re-subscribing with identical settings is
idempotent; changing the target under the same identity raises
`subscriber_conflict`, so unsubscribe first. Unsubscribing does not delete
existing inbox or ack records.

### Deliver

```python
result = bee_events.deliver_pending(state_dir)
# {"delivered": 2, "skipped": 5, "failed": 0, "pending": 0, ...}
```

`deliver_pending` scans the outbox and every subscription. For each event with
no inbox record it writes one delivery record atomically. Delivery is
at-least-once:

- an event is "delivered" only after its inbox file is durable;
- a crash or a failed write leaves the event pending;
- the next call retries with the same `event_id` and `delivery_id`;
- an existing delivery is skipped, never duplicated;
- `attempts` is the number of publish attempts recorded for that pair.

One subscriber's failure does not stop delivery to the others; failures are
listed in the result and remain pending.

### Acknowledge

```python
bee_events.acknowledge(state_dir, "ops", event["id"])
```

An ack requires an existing delivery and is written create-only/fsynced.
Repeating the ack is idempotent and returns the original record. An ack for a
never-delivered event raises `delivery_missing`.

### Replay

```python
bee_events.replay(state_dir, event["id"], "ops")
```

Replay is allowed only when the delivery is missing, or when it exists and has
a durable ack. An outstanding unacknowledged delivery raises `replay_refused`:
a consumer still processing the first copy must not get a silent duplicate.
A successful replay clears the ack, replaces the inbox record atomically and
increments `attempts`, so the consumer must acknowledge the new delivery.

### Crash recovery

`pending_deliveries(state_dir)` is a read-only crash scan: it returns
`{"subscriber": ..., "event_id": ...}` for every outbox event with no inbox
record. `deliver_pending` uses the same predicate, so after a restart it
recovers exactly those events. `recover_pending` is an alias for
`deliver_pending`.

A normal recovery loop is:

```python
for item in bee_events.pending_deliveries(state_dir):
    print("pending", item["subscriber"], item["event_id"])
bee_events.deliver_pending(state_dir)
```

## Refusal codes

| Code | Meaning |
|---|---|
| `event_invalid` | Missing/invalid CloudEvents field, id or digest. |
| `event_id_mismatch` | `id` does not equal `event_id_for(attempt, state, transition)`. |
| `event_secret` | A payload field looks like a credential. |
| `event_private` | A payload field is a URL/host/address or private/absolute path. |
| `event_oversized` | Event exceeds `MAX_EVENT_BYTES` (64 KiB). |
| `event_conflict` | Same event id with different bytes. |
| `event_write_failed` | A durable write failed; no partial record is promised. |
| `event_missing` | Replay named an event not in the outbox. |
| `workflow_schema_unavailable` | `WORKFLOW_SRC`/pinned schema is unavailable. |
| `event_schema_invalid` | Pinned schema rejected the envelope. |
| `subscriber_invalid` | Unsafe identity, target, session or credential-bearing URL. |
| `subscriber_conflict` | Same identity already subscribed to a different target. |
| `delivery_missing` | Ack before delivery. |
| `replay_refused` | Replay of an outstanding, unacknowledged delivery. |

Error text is scrubbed before it is exposed, so a refused payload cannot echo a
credential back into a log.

## Security and scope

- No broker, database, daemon or outbound network request. The only side
  effects are fsynced files under `state_dir/events/`.
- Event payloads refuse secrets (`security.looks_like_secret`), URLs, hosts,
  IP addresses and private/absolute paths. The state directory is never
  embedded in an event or delivery.
- Identity, event id, state, transition and subscriber names are constrained
  to filesystem-safe identifiers; path traversal is rejected.
- Subscription URLs are stored as opaque metadata and are never dialled.
- The module runs on the Python standard library. If `jsonschema` is
  importable it validates the pinned schema; otherwise a conservative
  draft-2020-12 subset validator is used. Neither path relaxes the schema.
