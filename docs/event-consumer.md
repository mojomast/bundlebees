# Coordinator landing event consumer

`bundlebees_plugin.event_consumer` is an out-of-band consumer for events that
bees and adapters land on disk. It is deliberately **not** part of
`queue.run_once` or any other mechanical tick: a handler may be slow or need a
person, and the mechanical queue must keep moving.

## Durable layout

```
events/inbox/<subscriber>/<event_id>.json
events/acks/<subscriber>/<event_id>.json
```

Only these two trees are written. The consumer never creates an approval key,
grant, job, recovery or task record.

An event is any JSON object. The consumer reads:

* `event_id` (or `id`, or the file stem);
* an optional `subscriber` / `subscribers` routing field;
* an optional `session_id` / `session` target for notifications;
* everything else verbatim as the event payload.

A flat `events/inbox/<event_id>.json` record is also read when it names the
subscriber, so older producers do not need a second acknowledgement namespace.
`publish_event(state_dir, event, subscriber=...)` is a small create-only helper
for producers and tests; replaying the same id returns the stored record.

Delivery bookkeeping lives in a reserved `_delivery` field inside the inbox
record. The handler never sees it: it receives the event exactly as the producer
wrote it (minus `_delivery`).

## `consume_once`

```python
consume_once(state_dir, subscriber, handler, *, consumer, now=None)
```

1. Scan the subscriber's inbox for one claimable event.
2. Claim it atomically under `flock`: the claim, owner pid, attempts and lease
   are durable **before** the handler is called.
3. Release the lock and run `handler(event)` outside the tick.
4. If the handler returns a mapping, create the ack exactly once with
   `O_EXCL`/`link` semantics and mark the inbox record `acked`.
5. If the handler raises (or the ack cannot be written), release the event for a
   retry. After `MAX_ATTEMPTS` claims the event is marked `failed` and is not
   delivered again.

A second delivery of an event that already has an ack returns
`{"duplicate": True, ...}` and **does not call the handler**. `consume_once`
returns `None` when there is nothing claimable.

The model is at-least-once, not exactly-once. If the process dies after the
handler ran but before its ack was durable, the stale claim is adopted on the
next pass and the handler may run again. A successful ack is still recorded at
most once.

Handler failures are bounded by claims, not wall-clock time:

| claimant | result |
| --- | --- |
| first failure | `state: "retryable"` |
| `MAX_ATTEMPTS`-th failure | `state: "failed"` (`refused` count in summary) |
| later pass | skipped, no handler call |

`now` accepts epoch seconds, an ISO timestamp or a callable, for deterministic
lease tests.

## `replay_acked`

```python
replay_acked(state_dir, subscriber, event_id)
```

Replay is explicit and is the only way to obtain a second accepted handling
decision. It archives the old ack under
`events/acks/<subscriber>/.replays/`, removes the live ack and resets the inbox
record to `retryable` with a fresh attempt budget. A missing ack returns
`{"replayed": False, "reason": "not_acked"}`.

## `inbox_summary`

```python
inbox_summary(state_dir, subscriber=None)
```

Returns counts (`total`, `pending`, `claimed`, `retryable`, `acked`, `failed`,
`refused`, `duplicates`) either for one subscriber or for all inboxes. Failed
events are counted in both `failed` and `refused` so either spelling is useful
to an operator.

## Hermes notification adapter

```python
notify_coordinator(
    event,
    *,
    endpoint=None,
    api_key=None,
    session=None,
    runner=None,
    timeout_s=60,
)
```

For a stateless / API-server session the documented resume is the gateway wake
path from Hermes `gateway/wake.py`, not a CLI continuation flag. The adapter
sends exactly one request:

```
POST <endpoint>/v1/chat/completions
Authorization: Bearer <key>
X-Hermes-Session-Id: <raw session id>
Content-Type: application/json

{"model": "hermes-agent",
 "messages": [{"role": "user", "content": "<sanitized event message>"}],
 "stream": false}
```

`endpoint` defaults to `HERMES_API_URL` and `api_key` to `HERMES_API_KEY`.
Neither value is logged, returned, or included in a refusal message. The
configured `session` (or, when omitted, the event's own session) must equal the
event's session; a mismatch is refused before any request is made.

The request body carries only an allowlisted projection of the event:
`event_id`, `type`, `job_id`, `task_id`, `attempt_id`, `state`, `source_rev`
and `result_reference`. Arbitrary event fields — messages, tokens, credentials,
private paths — are never forwarded. Values are additionally scrubbed: secret
shapes (`sk-…`, `Bearer …`, `token=…`) become `<redacted>` and absolute POSIX or
Windows paths collapse to their basename. The content is a compact JSON object.

The adapter fails closed:

* no `HERMES_API_URL` (or `endpoint` argument):
  `hermes_notification_unavailable`;
* no `HERMES_API_KEY` (or `api_key` argument):
  `hermes_notification_unavailable`;
* non-http(s) endpoint: `hermes_notification_unavailable`;
* event session differs from the configured session:
  `hermes_session_mismatch`;
* non-2xx gateway status, timeout, or transport error:
  `hermes_notification_failed`.

No refusal returns a record claiming `sent: true` or `delivered: true`. On
success the result carries only delivery metadata: schema, session, transport,
path, model, HTTP status, elapsed time, timeout, and message/response byte
counts and digests. It never carries the endpoint, the key, or an authorization
header.

`runner` is an optional transport seam with the signature
`runner(url, *, headers, body, timeout_s) -> (status, body)`. Tests use it for
transport-level timeouts; most tests use a loopback HTTP server to prove the
wire format without touching an external service.

## Coordinator action queue: terminal completions to actions

`consume_completions` composes the durable inbox with the coordinator action
queue: a terminal launcher bee-landed event (W6) becomes coordinator-action
items (F6), so a worker completion advances the pipeline without the
coordinator polling the launcher and without Hermes being awake. The
composition is **additive**: the mechanical queue tick, the Hermes wake path
and the 10-minute heartbeat are unchanged and remain the backstop for any
completion this consumer has not seen yet.

### Subscription

```python
bee_events.subscribe(state, {"identity": "coordinator-actions", "file": "coordinator-actions"})
result = event_consumer.consume_completions(state)
```

The subscriber is a normal file subscriber. `record_landing` +
`deliver_pending` put each canonical `rizzstack.bee.landed.v1` event in
`events/inbox/coordinator-actions/<event_id>.json`; `consume_completions`
claims one, classifies it and either enqueues actions or skips it.

### Classification

`classify_completion(event)` accepts the canonical CloudEvents envelope
(facts under `data`) or a flat mapping, and returns either completion facts
(`attempt_id`, `state`, `event_id`, `job_id`, `task_id`, `source_rev`,
`result_available`, `result_reference`, `lease_files`) or a skip. Terminal
states are `landed`, `succeeded`, `failed` and `cancelled`; anything else is
skipped as `not_terminal`. Malformed deliveries (missing or unsafe
`attempt_id`, missing `state`) are skipped with a `malformed_*` reason. Every
skip is acked — a malformed event is a decision, not a retry — and counted
durably in `coordinator/actions/.skipped.json`; read the counter with
`skipped_events(state)`. Load-bearing identifiers are validated strictly;
advisory copies (`source_rev`, `result_reference`, `lease_files`) drop unsafe
values instead of refusing the event.

### Action-queue item shape

One durable file per idempotent key under the coordinator state directory:

```
coordinator/actions/<action_id>.json        # the item
coordinator/actions/.locks/<action_id>.lock # claim serialisation
coordinator/completions/<attempt>:<state>.json  # the accepted decision
```

`action_id` is the F6 idempotent key `<kind>:<attempt_id>` with kind `verify`
or `process-completion`; `action_id_for(kind, attempt_id)` builds it:

```json
{
  "schema": "rizzstack.coordinator-action/1",
  "action_id": "verify:att-bee-1",
  "kind": "verify",
  "attempt_id": "att-bee-1",
  "job_id": "job-1",
  "task_id": "task-1",
  "event_id": "urn:rizzstack:bee-landed:…",
  "event_state": "landed",
  "dedupe_key": "att-bee-1:landed",
  "source_rev": "…",
  "lease_files": ["src/a.py"],
  "result_available": true,
  "result_reference": "refs/heads/feat/bee-events",
  "status": "pending",
  "attempts": 0,
  "fence": 0,
  "claimed_by": "",
  "claimed_at": "",
  "claim_pid": 0,
  "claim_expires_at": "",
  "next_attempt_at": "",
  "result": null,
  "error": "",
  "created_at": "…", "updated_at": "…"
}
```

A `landed`/`succeeded` completion enqueues `verify:<attempt>` (the coordinator
should verify the patch) and `process-completion:<attempt>`; a
`failed`/`cancelled` completion enqueues only `process-completion:<attempt>`.

### Exactly once, three layers

* **Event level** — `consume_once` acks each `(subscriber, event_id)` once, so
  duplicate delivery never reaches the handler again.
* **Effect level** — `record_completion` writes one `rizzstack.completion-ledger/1`
  record per `(attempt_id, state)`, create-only; `enqueue_action` publishes
  items create-only under their idempotent key (same key → `created: False`,
  different attempt or kind under a live key → `action_conflict`, a `done`
  item is never resurrected). A crash after the effects but before the ack
  replays into the same items and the recorded decision — zero duplicate
  actions across duplicate delivery, replay and coordinator restart.
* **Claim level** — `claim_action(state, action_id, consumer=…)` claims under
  `flock`, counts one attempt, and returns a fencing token (`fence`) that
  increments on every claim. `complete_action` / `fail_action` require the
  current fence; a stale consumer's token is refused with `stale_fence`, a
  live second claim gets `busy`, and a dead or expired claim is adopted with
  a new fence. `attempts >= MAX_ACTION_ATTEMPTS` (2) marks the item `failed`;
  `actions_summary(state)` counts items by status.

### Failure modes

* **Queue unavailable** — the handler's `enqueue` seam raises
  `ActionQueueError`; the event is released into its durable inbox record
  with `next_attempt_at` backoff (default 30 s, `COMPLETION_MAX_ATTEMPTS`
  claims in total) and the outbox event is never deleted. When the queue
  returns, the next pass processes the buffered event once.
* **Exhausted retries** — the delivery is marked `failed` and never claimed
  silently again; `requeue_failed(state, subscriber, event_id)` is the
  explicit operator reset back to `retryable`.
* **Already processed** — a second event for the same `(attempt_id, state)`
  (for example a different transition of the same landing) is acked as
  `completion_already_processed` without enqueueing anything.

The `enqueue` seam is the integration point for the merged F6 queue backend:
the item id, the create-only idempotent keys and the claim/fence protocol are
the data-level contract; a queue that keeps the same keys can replace the
bundled store without touching the consumer.

Run the suite:

```bash
python3 -m pytest tests/test_event_consumer_actions.py -q
```
