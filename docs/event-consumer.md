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
