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
notify_coordinator(event, *, runner=None, session=None)
```

Hermes has no documented "send a coordinator notification" gateway API, so this
adapter does not invent one. It probes the installed CLI's own help output for a
documented session continuation (`--resume` / `--continue`) and invokes the
documented one-shot form:

```
hermes chat --resume <session> -Q -q <message>
```

or `--continue` when that is the flag the installed CLI documents.

The adapter fails closed:

* no `hermes` binary / `HERMES_BIN` and no probe seam: `ProposalError`
  `hermes_notification_unavailable`;
* help output with no continuation flag: `hermes_notification_unavailable`;
* event session differs from the requested coordinator session:
  `hermes_session_mismatch`;
* timeout or non-zero continuation exit: `hermes_notification_failed`.

No refusal returns a record claiming `sent: true`. On success the result carries
only the public command (the message is replaced by its digest), the session,
the continuation flag, exit code and bounded stream digests.

`runner` is the same injection seam used by `invoke_hermes`; tests use it to
simulate a missing CLI, a CLI with no continuation support, and a failed
continuation without touching the network.
