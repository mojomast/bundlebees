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

The `launcher` source's supported transport is a direct request to the peer's
literal address. Plain `http` accepts only IPv4 loopback `127.0.0.0/8` and IPv6
loopback `::1`. With `--allow-private` (or the equivalent `allow_private`
setting), it also accepts exactly RFC 1918 `10.0.0.0/8`, `172.16.0.0/12` and
`192.168.0.0/16`, plus RFC 6598 `100.64.0.0/10`. The request still carries the
dedicated bearer token from `BUNDLEBEES_RAILS_READ_TOKEN`; `allow_private` never
removes authentication. Every other plain-`http` peer is refused before the
token is read or any network I/O occurs: TEST-NET-1/2/3 (`192.0.2.0/24`,
`198.51.100.0/24`, `203.0.113.0/24`), benchmarking `198.18.0.0/15`, `240.0.0.0/4`,
`0.0.0.0/8`, limited broadcast `255.255.255.255`, multicast, link-local, IPv6
ULA, global/public IP literals and DNS hostnames. Use `https` for peers outside
the plain-`http` set; the `https` transport arm is unchanged.

The worker's Rails reader accepts exactly one `Host` header equal to the
connection's own address and port and refuses any `Origin`; a direct literal
request satisfies that without rewriting. The separately installed
`rails_host_proxy.py` loopback forwarder
(`rizzstack-rails-host-proxy.service`, `127.0.0.1:47923` to `127.0.0.1:47922`)
rewrites `Host`. It is a temporary, unsupported workaround and is not part of
the supported path; it is not needed for the direct literal transport and does
not satisfy the peer's Host check.

`has_more` means another page is available. Restart the same command with no cursor: local source pages commit their cursor, facts and checkpoint together; the remote source resumes at the ledger's committed origin sequence. A stale cursor or changed immutable prefix is an error, not permission to discard a checkpoint. Reads and doctor never perform an import. Do not put SQLite on a network share.

## Owner retry and crash recovery

`bundlebees_plugin.rails_actions.RetryHandoff(state_dir, reader, authorize=...)` is the trusted in-process hook. The host supplies `authorize(target_event_id, request_id, operation)`, bound to its existing owner principal and current policy; it is called on preview, submission, claim and commit. Never derive it from a request field. Without the check, return `capability_unavailable`. No retry or rewind model tool is registered.

1. `preview(target_event_id, request_id)` resolves an existing Rails attempt event and a canonical outstanding recovery request. It checks the sealed job, authorization ceiling, remaining attempts and frozen contract. The public preview includes the target, scope digest/count, budget, protocol pin, expected queue fence and a revision.
2. After the owner confirms that preview, `submit(target_event_id, request_id, revision)` enqueues exactly one `rails_retry` under a stable identity. Its receipt is a **request**, not approval. Changed inputs require a new preview; a duplicate click returns the original queue ID. Map refusals to their safe `code` only.
3. The coordinator owner calls `claim(queue_action_id)` and `commit(queue_action_id, fence)`. The current fence protects the create-only write of the validated `rizzstack.recovery/1` retry decision. The existing reconciler owns application, dispatch and execution results. Rails does not run a command, apply a patch or start an attempt.
4. If interrupted, inspect the queue. A current holder can call `reconcile(queue_action_id, fence)` to acknowledge an already matching decision. An adopted claim can only reconcile, never initiate a decision. Missing or contradictory evidence stays held for the owner. Ingestion can replay both the immutable intake and the final queue result after a crash between either write and its publication to Rails.

The source emits `intervention.requested` for intake and `intervention.result` with `approved` for a proved retry decision. Neither means a retry succeeded. No ghost is emitted from those facts. `rewind_retry` is unavailable: the current coordinator has no supported fenced Hermes rewind operation. The queue stores mutable claim snapshots, not a journal of every renewal or adoption; Rails does not reconstruct invisible transitions. No network action endpoint or new owner privilege is activated by the plugin.

## Scheduled ingest outside a Hermes session

`python -m bundlebees_plugin.rails_ingest --ledger /abs/ledger.sqlite3 --state-dir /abs/state --source bundlebees --source outbox`
imports each named source a bounded page at a time until it reports no more
pages. Sources and the authorities each may write are exactly those
`rails-import` grants (`bundlebees`, `outbox`, `queue`, `launcher`); the remote
`hermes` source stays with `rails-import`. The state is read only; only the
ledger is written; one run holds an exclusive lock beside the ledger. Output is
one JSON line with per-source counts or generic error codes, never a path.

The runner may add `--source launcher` to admit the worker launcher's
read-only `rizzstack.rails-export/1` pages. Because it can start outside a
Hermes session it cannot read plugin settings or the secret store, so the
launcher source takes explicit arguments:

```sh
python -m bundlebees_plugin.rails_ingest --ledger /abs/ledger.sqlite3 \
  --state-dir /abs/state --source bundlebees --source launcher \
  --launcher-url https://worker.example:8443 --launcher-chain <origin-chain> \
  --rails-token-file /abs/worker-rails-read.txt [--allow-private]
```

`--launcher-url`, `--launcher-chain` and `--rails-token-file` are required as a
set when `launcher` is named, and influence no other source. Configuration is
validated before any ledger write, even when `launcher` is listed after another
source: the URL must be an absolute `http`/`https` URL with a host and no
userinfo, query or fragment; the chain must be a valid origin identity; and the
credential file must be a regular readable file whose value is 32..512 printable
non-space ASCII characters. Any failure prints `{"ok": false, "error": {"code":
"not_configured"}}` and exits `2` without creating the ledger. The launcher keeps
a per-origin-chain cursor in the ledger and refuses a halted chain with
`source_fork`. A page error leaves earlier committed pages committed and reports
`ok: false` with a generic code.

`--source launcher-rails` reads the same export strictly, for bounded admission
of worker `usage.observed` receipts. It stages every page of the pending range
without writing, then runs the ledger's own origin validator over the whole
range, and only then commits the pages. A page error, malformed record, schema
mismatch, fork or gap therefore exits `3` with the ledger and the per-chain
cursor untouched; the source's error object adds the failing `cursor` and a
scrubbed `transport` label (for example `peer_unavailable 503: peer returned
status 503`) so the timer log shows where the export stopped. It refuses rather
than truncates when the pending range exceeds `--max-pages`, and it refuses
`ledger_validation_unavailable` instead of writing an unproven range. A hard
kill between per-page commits can leave a contiguous committed prefix; the
cursor matches that prefix, so the next run resumes and never double-counts.
Use `launcher`, not this source, for a large first backfill.

The shipped `contrib/systemd/bundlebees-rails-ingest.{service,timer}` units stay
local-only (`PrivateNetwork=yes`) and are not changed by this slice. A unit that
grants the launcher source needs network access and is a separate deployment
decision.

The bundlebees and outbox sources refuse a state root that a group or other
user can write or traverse (`source_unsafe`). Keep the plugin data directory
owner-only (`0700`). A plain file beside the contract directories under
`evidence/` is skipped; a symlink there is still refused.

`contrib/systemd/bundlebees-rails-ingest.{service,timer}` run it every minute
from a pinned checkout with no network and the ledger directory as the only
writable path. The Orbit Rails reader (`orbitdesktop contrib/rails_server`)
then serves that ledger read-only.
