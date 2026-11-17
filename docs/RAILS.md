# Rails on the Bundlebees coordinator

Set `rails_db` in the trusted Hermes plugin settings to an absolute path on a local disk. Use a private directory owned by the coordinator. This opts the profile into read-only history queries through `bundlebees_rails` and adds a ledger row to the existing `hermes bundlebees doctor --json` report. Only opt in for a profile allowed to read this ledger. The database, source paths, credentials and peer URL are never model arguments. The tool takes a `rizzstack.rails-query/1` request with one selector.

```sh
hermes bundlebees rails query --kind attempt --id att_example --format json
```

The standalone installed module accepts the same `query` arguments (`python -m rizzstack_rails query ...`, with the local ledger supplied in `RIZZSTACK_RAILS_DB`). Missing data is not sample data. A successful page can still report partial coverage. Check `has_more` and continue with its returned cursor. A healthy doctor read is evidence of local readability only.

## Explicit ingestion

Set `rails_import_sources` to a comma-separated list of sources assigned to this coordinator importer. Leave it empty for read-only installations. Do not assign the same source to server polling as well. A human or the existing scheduler can request one bounded page at a time:

```sh
hermes bundlebees rails-import --source outbox --limit 100
```

| Source | Reads |
|---|---|
| `bundlebees` | Committed coordinator records in the active profile data directory; signed evidence uses the existing verification key when present. |
| `outbox` | The existing `events/outbox` landing records. A landed attempt is not a verified pass. |
| `queue` | Stable retry intake and decision receipts from the fenced coordinator action queue. |
| `hermes` | The local `rails_hermes_db`, restricted to the explicit `rails_session_aliases` JSON mapping from enrolled IDs to public aliases. |
| `launcher` | The existing `adapter: launcher`, configured `launcher_url`, `allow_private` policy and enrolled `rails_launcher_chain`. Configure `BUNDLEBEES_RAILS_READ_TOKEN` in the profile's existing Hermes secret store to match the worker's dedicated Rails read credential. No fallback to the launcher's administrative credential. |

`has_more` means another page is available. Restart the same command with no cursor: local source pages commit their cursor, facts and checkpoint together; the remote source resumes at the ledger's committed origin sequence. A stale cursor or changed immutable prefix is an error, not permission to discard a checkpoint. Reads and doctor never perform an import. Do not put SQLite on a network share.

## Owner retry and crash recovery

`bundlebees_plugin.rails_actions.RetryHandoff(state_dir, reader, authorize=...)` is the trusted in-process hook. The host supplies `authorize(target_event_id, request_id, operation)`, bound to its existing owner principal and current policy; it is called on preview, submission, claim and commit. Never derive it from a request field. Without the check, return `capability_unavailable`. No retry or rewind model tool is registered.

1. `preview(target_event_id, request_id)` resolves an existing Rails attempt event and a canonical outstanding recovery request. It checks the sealed job, authorization ceiling, remaining attempts and frozen contract. The public preview includes the target, scope digest/count, budget, protocol pin, expected queue fence and a revision.
2. After the owner confirms that preview, `submit(target_event_id, request_id, revision)` enqueues exactly one `rails_retry` under a stable identity. Its receipt is a **request**, not approval. Changed inputs require a new preview; a duplicate click returns the original queue ID. Map refusals to their safe `code` only.
3. The coordinator owner calls `claim(queue_action_id)` and `commit(queue_action_id, fence)`. The current fence protects the create-only write of the validated `rizzstack.recovery/1` retry decision. The existing reconciler owns application, dispatch and execution results. Rails does not run a command, apply a patch or start an attempt.
4. If interrupted, inspect the queue. A current holder can call `reconcile(queue_action_id, fence)` to acknowledge an already matching decision. An adopted claim can only reconcile, never initiate a decision. Missing or contradictory evidence stays held for the owner. Ingestion can replay both the immutable intake and the final queue result after a crash between either write and its publication to Rails.

The source emits `intervention.requested` for intake and `intervention.result` with `approved` for a proved retry decision. Neither means a retry succeeded. No ghost is emitted from those facts. `rewind_retry` is unavailable: the current coordinator has no supported fenced Hermes rewind operation. The queue stores mutable claim snapshots, not a journal of every renewal or adoption; Rails does not reconstruct invisible transitions. No network action endpoint or new owner privilege is activated by the plugin.
