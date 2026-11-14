# Bee landing events

`bundlebees_plugin/bee_events.py` is the durable landing-event outbox. It lets
the coordinator record that a bee attempt reached a state transition, then lets
any number of subscribers receive that event exactly once per
`(subscriber, event_id)` pair without a broker, database or daemon. File
subscribers receive a durable inbox record; http(s) subscribers receive an
HMAC-authenticated POST.

The only event `build_event` emits is the canonical
`rizzstack.bee.landed/1` event. Its CloudEvents type is
`rizzstack.bee.landed.v1`, and `data` carries exactly the canonical landing
facts, with no legacy `transition_id`, `lease_sha256`, `artifact_sha256`,
`reference` or `backend` string fields:

```json
{
  "specversion": "1.0",
  "id": "urn:rizzstack:bee-landed:6ce11a6b683fd09e9ff4936eed552209",
  "source": "rizzstack://bundlebees",
  "type": "rizzstack.bee.landed.v1",
  "time": "2026-11-14T14:50:11Z",
  "datacontenttype": "application/json",
  "data": {
    "schema": "rizzstack.bee.landed/1",
    "feature_id": "BEE-1",
    "job_id": "job_1",
    "task_id": "task_1",
    "attempt_id": "att_bee_1",
    "coordinator": {"identity": "coordinator-1", "session": "run_1"},
    "backend": {"harness": "hermes", "model": "deepseek-v4.1-flash"},
    "state": "landed",
    "source_rev": "…40 or 64 hex…",
    "lease": {
      "sha256": "…64 hex…",
      "assigned_files": ["src/a.py", "tests/test_a.py"]
    },
    "result": {
      "available": true,
      "artifact_sha256": "…64 hex…",
      "reference": "refs/heads/feat/bee-events"
    },
    "correlation_id": "corr_1",
    "causation_id": "cause_1"
  }
}
```

`coordinator.session`, `backend.model`, `result.artifact_sha256`,
`result.reference`, `correlation_id` and `causation_id` are optional and are
omitted when absent. `coordinator.identity`, `backend.harness`,
`lease.sha256`, `lease.assigned_files` and `result.available` are part of the
canonical shape. Extra envelope or `data` fields are refused.

The id is deterministic:
`event_id_for(attempt_id, state, transition_id)` is
`urn:rizzstack:bee-landed:` plus the first 32 hex characters of
`sha256("attempt:state:transition")`. Recording the same landing twice is a
no-op; delivery retries reuse the same id so consumers can deduplicate. The
transition is deliberately not copied into `data`; the id is the durable
binding to it.

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

### Build and record a landing

```python
from bundlebees_plugin import bee_events

event = bee_events.build_event(
    {"attempt_id": "att_bee_1", "state": "landed", "transition_id": "tr_0001"},
    feature_id="BEE-1",
    job_id="job_1",
    task_id="task_1",
    coordinator={"identity": "coordinator-1", "session": "run_1"},
    backend={"harness": "hermes", "model": "deepseek-v4.1-flash"},
    source_rev=source_rev,
    lease={"sha256": lease_sha, "assigned_files": ["src/a.py"]},
    result={
        "available": True,
        "artifact_sha256": artifact_sha,
        "reference": "refs/heads/feat/bee-events",
    },
)
bee_events.record_landing(state_dir, event)
```

For compatibility, the aliases `coordinator="name"`, `backend="harness"`,
`lease_sha256=`, `assigned_files=`, `artifact_sha256=` and `reference=` are
folded into the canonical objects. They never appear as extra `data` fields.
`result.available` defaults to true when an artifact or reference is supplied.

`record_landing` is create-only. If the same event id already exists with
identical canonical bytes it returns the stored record with `created: false`.
Different bytes under the same id raise `event_conflict`; the original is never
overwritten. A write failure raises `event_write_failed` and leaves no partial
outbox file.

### Validate against the pinned workflow schema

```python
bee_events.validate_event(event)   # True, or raises EventError
```

Validation first enforces the canonical envelope and exact `data` shape, then
loads the schema from the pinned workflow checkout in `WORKFLOW_SRC` and runs
the whole CloudEvents object through it. Validation is fail-closed: when
`WORKFLOW_SRC` is unset, unreadable, or has no landing schema, it raises
`workflow_schema_unavailable`. The schema is looked up under `schemas/`,
`schema/` or `observability/schemas/` in names including
`bee-landed.schema.json`, `bee.landed.schema.json` and
`rizzstack.bee.landed.schema.json`.

If `jsonschema` is importable it is used; otherwise a conservative
draft-2020-12 subset validator handles the keywords landing schemas use.
Neither path relaxes the pinned schema. `record_landing` performs the
structural, size and security checks but does not claim to have run the pinned
validator; call `validate_event` explicitly when that proof is required.

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

The secret is stored only in the private subscriber record and is omitted from
every value `subscribe` returns. It never appears in an event payload, an inbox
record, a delivery result or an error message. Re-subscribing with identical
settings is idempotent; changing the target or secret under the same identity
raises `subscriber_conflict`, so unsubscribe first.

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

The body is limited to `MAX_HTTP_BODY_BYTES` (256 KiB). One subscriber's
failure does not stop delivery to the others; failures remain pending and are
reported with only the subscriber identity, event id and a stable error code.

### Verify a delivery

```python
bee_events.verify_delivery(headers, raw_body, secret, now=time.time())
# True, or raises EventError
```

`verify_delivery` uses constant-time signature comparison and rejects a
missing or malformed signature, a body that does not match it, a timestamp
outside `tolerance_s` (default 300), an oversized body, a missing secret and a
delivery id already accepted by this process. Pass `state_dir=` to persist
replay markers under the coordinator state directory.

### Acknowledge and replay

```python
bee_events.acknowledge(state_dir, "ops", event["id"])
bee_events.replay(state_dir, event["id"], "ops")
```

An ack requires an existing delivery and is written create-only/fsynced.
Repeating the ack is idempotent. An ack for a never-delivered event raises
`delivery_missing`.

Replay is allowed only when the delivery is missing, or when it exists and has
a durable ack. An outstanding unacknowledged delivery raises `replay_refused`.
A successful replay clears the ack, replaces the inbox record atomically and
increments `attempts`.

### Crash recovery

`pending_deliveries(state_dir)` is a read-only crash scan: it returns
`{"subscriber": ..., "event_id": ...}` for every outbox event with no inbox
record. `deliver_pending` uses the same predicate, so after a restart it
recovers exactly those events. `recover_pending` is an alias for
`deliver_pending`.

## hyvv landing bridge

`bundlebees_plugin/landing_bridge.py` consumes a hyvv canonical outbox directory
and bridges each event into the bundlebees outbox:

```
python3 -m bundlebees_plugin.landing_bridge \
  --state <coordinator-state-dir> \
  --launcher-outbox <hyvv-outbox-dir> \
  [--once]
```

Without `--once` it polls every `--interval` seconds (default 1). With
`--once` it processes the current directory and exits nonzero if any source
failed. Files are accepted as one canonical CloudEvents object, a wrapper with
an `event` object, a JSON list of events, or JSON lines.

For every source file the bridge:

1. validates each event with the pinned workflow validator (`WORKFLOW_SRC`);
2. correlates the event with coordinator records when they exist:
   - `leases/<attempt_id>.json` must name the same `attempt_id`, and its
     `source_rev` and `assigned_files` must equal the event; the event
     `lease.sha256` must equal the lease digest (`sha256`, `lease_sha256`,
     `brief_sha256`, or the canonical identity digest);
   - `jobs/<job_id>.json` must name the same task and attempt when the job
     records attempts, and its goal base revision and task lease must match.
   - if neither a lease nor a job record exists, the event is refused with
     `identity_missing`;
3. records the event in `events/outbox/` idempotently;
4. calls `deliver_pending` and refuses to acknowledge while any of this file's
   events is still pending;
5. writes a durable acknowledgement record under
   `<launcher-outbox>/acked/.acks/<name>.ack.json` and then moves the source to
   `<launcher-outbox>/acked/<name>`.

Any mismatch fails closed before the event is recorded or the source moved.
Duplicate bytes are a no-op: an already-acknowledged source is moved again
without creating another outbox or inbox record. Conflicting bytes under the
same source name, or a different event under the same event id, are refused
and the source stays in place. A crash before recording, between recording and
delivery, after delivery and before the ack, or after the ack and before the
move replays cleanly because the outbox, delivery and ack records are
create-only/fsynced.

## Refusal codes

| Code | Meaning |
|---|---|
| `event_invalid` | Missing/invalid canonical CloudEvents field, id, or extra field. |
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
| `identity_missing` | The bridge found no coordinator lease/job identity for the event. |
| `attempt_mismatch` / `job_mismatch` / `task_mismatch` | Event identity disagrees with the coordinator records. |
| `source_rev_mismatch` | Event `source_rev` differs from the recorded lease/job revision. |
| `lease_mismatch` | Event `lease.sha256` or `assigned_files` differs from the recorded lease. |
| `source_conflict` | Source bytes conflict with an existing acknowledgement or moved file. |
| `delivery_pending` | A source event is not durably delivered yet; the source is not acknowledged. |
| `source_invalid` / `source_oversized` | The source file is not a regular JSON event or is too large. |

Error text is scrubbed before it is exposed, so a refused payload cannot echo a
credential back into a log.

## Security and scope

- No broker, database or daemon. File delivery only writes fsynced files under
  `state_dir/events/`; HTTP delivery makes one bounded POST per pending
  `(subscriber, event_id)` pair.
- Event payloads refuse secrets (`security.looks_like_secret`), URLs, hosts,
  IP addresses and private/absolute paths. The state directory is never
  embedded in an event or delivery.
- Identity, event id, state, transition, lease paths and subscriber names are
  constrained to filesystem-safe values; path traversal is rejected.
- The bridge reads source and coordinator records as regular non-symlink files,
  verifies signed coordinator leases/jobs when present, and refuses on any
  mismatch before writing the bundlebees outbox.
- HTTP delivery is authenticated with HMAC-SHA256 over the exact raw JSON body.
  Signature comparison uses `hmac.compare_digest`; timestamps are checked
  against a configurable tolerance so captured deliveries cannot be replayed
  indefinitely.
- Subscription secrets are never returned by `subscribe`, never included in
  event payloads, delivery records, result summaries, failures or logs, and are
  stored only in the private subscriber file (`0600` inside a `0700`
  directory).
