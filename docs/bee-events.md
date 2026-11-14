# Bee landing events

`bundlebees_plugin/bee_events.py` is the durable landing-event outbox. It lets
the coordinator record that a bee attempt reached a state transition, then lets
any number of subscribers receive that event exactly once per
`(subscriber, event_id)` pair without a broker, database or daemon. File
subscribers receive a durable inbox record; http(s) subscribers receive an
HMAC-authenticated POST.

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
| `events/verified/<token>.json` | Optional durable replay marker for `verify_delivery(state_dir=...)`. |

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
    "secret": "...",               # required for signed http(s) delivery
    "session": "run_1",            # optional
})
bee_events.unsubscribe(state_dir, "ops")
```

A subscriber needs exactly one of `file` or `url`. URLs must be `http(s)` and
must not embed credentials. An http(s) subscriber also needs a `secret`; without
one `deliver_pending` refuses that subscription with `secret_missing` and leaves
the event pending.

The secret is stored only in the private subscriber record (directory `0700`,
file `0600`) and is omitted from every value `subscribe` returns. It never
appears in an event payload, an inbox record, a delivery result or an error
message. Re-subscribing with identical settings is idempotent; changing the
target or secret under the same identity raises `subscriber_conflict`, so
unsubscribe first. Unsubscribing does not delete existing inbox or ack records.

### Deliver

```python
result = bee_events.deliver_pending(state_dir)
# {"delivered": 2, "skipped": 5, "failed": 0, "pending": 0, ...}
```

`deliver_pending` scans the outbox and every subscription. For each event with
no inbox record it either writes one delivery record atomically (file
transport) or POSTs the canonical event JSON (HTTP transport). Delivery is
at-least-once:

- an event is "delivered" only after its inbox file is durable, or after the
  HTTP endpoint returns a 2xx response;
- a crash, failed write or failed POST leaves the event pending;
- the next call retries with the same `event_id` and `delivery_id`;
- an existing delivery is skipped, never duplicated;
- `attempts` is the number of publish attempts recorded for that pair;
- HTTP posts use a bounded timeout and retry transient failures.

For HTTP, the exact body bytes are signed with HMAC-SHA256 using the
subscription secret. Headers are:

| Header | Value |
|---|---|
| `X-Rizzstack-Timestamp` | Unix epoch seconds when the delivery was signed. |
| `X-Rizzstack-Signature` | `sha256=<hex HMAC of the raw body>`. |
| `X-Rizzstack-Delivery-Id` | Deterministic `dlv_...` id for replay detection. |
| `X-Rizzstack-Event-Id` | The CloudEvents event id. |

The body is limited to `MAX_HTTP_BODY_BYTES` (256 KiB). A larger body is
refused with `delivery_oversized` before any request is made.

One subscriber's failure does not stop delivery to the others; failures are
listed in the result and remain pending. Failure summaries contain only the
subscriber identity, event id and a stable error code, never a URL or secret.

### Verify a delivery

A receiver authenticates a delivery before processing it:

```python
bee_events.verify_delivery(headers, raw_body, secret, now=time.time())
# True, or raises EventError
```

`verify_delivery` is constant-time when comparing signatures. It rejects:

- a missing or malformed `sha256=<hex>` signature (`delivery_signature_invalid`);
- a signature that does not match the exact raw body bytes
  (`delivery_signature_invalid`);
- a timestamp outside `tolerance_s` seconds of `now` (default `300`), including
  future-dated deliveries (`delivery_stale`);
- a body larger than 256 KiB (`delivery_oversized`);
- a missing secret (`secret_missing`);
- a delivery id already accepted by this process (`delivery_replayed`).

Pass `state_dir=` to persist replay markers under the coordinator state
directory; otherwise replay detection is in-memory for the life of the process.
The signature covers the raw body bytes, so always verify before parsing or
re-serializing them.

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
| `subscriber_invalid` | Unsafe identity, target, session, secret or credential-bearing URL. |
| `subscriber_conflict` | Same identity already subscribed to a different target or secret. |
| `delivery_missing` | Ack before delivery. |
| `replay_refused` | Replay of an outstanding, unacknowledged delivery. |
| `secret_missing` | An http(s) subscriber or delivery has no HMAC secret. |
| `delivery_oversized` | HTTP delivery body exceeds 256 KiB. |
| `delivery_timestamp_missing` / `delivery_timestamp_invalid` | Missing or unparseable `X-Rizzstack-Timestamp`. |
| `delivery_stale` | Timestamp is outside the allowed clock-skew window. |
| `delivery_signature_invalid` | Signature is missing, malformed or does not match the body. |
| `delivery_replayed` | Delivery id was already verified. |
| `delivery_http_failed` / `delivery_http_error` | HTTP transport or endpoint failure; the event remains pending. |

Error text is scrubbed before it is exposed, so a refused payload cannot echo a
credential back into a log.

## Security and scope

- No broker, database or daemon. File delivery only writes fsynced files under
  `state_dir/events/`; HTTP delivery makes one bounded POST per pending
  `(subscriber, event_id)` pair.
- Event payloads refuse secrets (`security.looks_like_secret`), URLs, hosts,
  IP addresses and private/absolute paths. The state directory is never
  embedded in an event or delivery.
- Identity, event id, state, transition and subscriber names are constrained
  to filesystem-safe identifiers; path traversal is rejected.
- HTTP delivery is authenticated with HMAC-SHA256 over the exact raw JSON body.
  `X-Rizzstack-Signature` is `sha256=<hex>` and timestamps are checked against a
  configurable tolerance (default 300 seconds) so captured deliveries cannot be
  replayed indefinitely. Signature comparison uses `hmac.compare_digest`.
- Duplicate delivery ids are rejected by `verify_delivery`; pass `state_dir` to
  make replay markers durable across process restarts.
- Subscription secrets are never returned by `subscribe`, never included in
  event payloads, delivery records, result summaries, failures or logs, and are
  stored only in the private subscriber file (`0600` inside a `0700`
  directory). Error messages use stable codes and never echo a URL, body or
  secret.
- HTTP bodies are capped at 256 KiB and requests use a bounded timeout with
  a small number of retries. A failed request leaves the event pending for the
  next delivery pass.
- The module runs on the Python standard library. If `jsonschema` is
  importable it validates the pinned schema; otherwise a conservative
  draft-2020-12 subset validator is used. Neither path relaxes the schema.
