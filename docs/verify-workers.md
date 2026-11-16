# Verify-worker bees: parallel verification on the action queue

`bundlebees_plugin/verify_workers.py` makes per-attempt verification a
dispatchable item on the F6 coordinator action queue
(`coordinator_actions.py`, the frozen contract). Verification was the last big
serial stage of the coordinator pipeline: the tick's verifier closure ran
`verify_local.verify_and_commit` in-beat, one attempt at a time. On this lane
each queued item is claimed by its own verify-worker bee, which runs the SAME
verifier — the configured interpreter, the same test commands, the same
scanner, the same evidence contract — and returns signed evidence the
coordinator checks. Queued items verify in parallel; the coordinator consumes
evidence instead of running tests serially in-beat.

## The queue item

One action per attempt, enqueued through `enqueue_verify_action`:

```
action_key   verify:<attempt_id>     canonical, idempotent identity
action_id    verify-<attempt_id>     the same name in the queue's safe
                                     path alphabet (a colon is not a path
                                     component there); an attempt id too
                                     long for the alphabet hashes to a
                                     bounded derived id
kind         verify
workflow_ref the job's pinned protocol commit (required)
payload      the pinned verification request
```

`kind=verify` is deliberately not a lease kind, so two verify items on the
same repository never serialize against each other — parallelism across
queued items is the point. Only `merge` and `deploy` hold repo leases.

The payload pins the whole request at dispatch time:

| field | meaning |
| --- | --- |
| `attempt_id` | the attempt being verified |
| `source_rev` | the pinned base revision the lease binds to |
| `workflow_ref` | the protocol revision the job was admitted under |
| `patch`, `patch_sha256` | the exact artifact bytes and their digest |
| `declared_files` | the lease's exact file set |
| `tests`, `suite_command` | the contract's named tests and regression argv |
| `gitleaks` | the configured scanner executable |
| `job_id`, `repo` | the authorizing job and repository identity |

The lease is checked at dispatch: a request whose declared files do not match
the attempt's lease is refused (`lease_scope_mismatch`) before any worker sees
it, so an abbreviated path list can never shrink the verified scope. A replay
whose request changed is refused (`verify_request_changed`) — one attempt has
one verification request.

## The worker bee

`run_verify_action` is the bee's one-call flow; `produce_verify_evidence` and
`publish_verify_result` are its halves, split so a bee can be superseded
between producing and publishing and be refused by the fence instead of
overwriting the adopter's result.

1. Load the item; a done item replays its stored result without re-running
   anything (idempotent by `action_key`).
2. Acquire the claim through the queue: claim a queued item, adopt an expired
   claim, or reuse the fence of its own live claim from a crashed pass.
3. Run `verify_local.verify_and_commit` with the payload's pinned patch,
   declared files, base revision, tests, suite command and scanner, plus the
   bee's configured runner and sandbox policy. The frozen-contract preflight
   runs exactly when the lease declares a contract, as in the in-beat path.
4. Sign the evidence and complete the action through the queue's fenced
   `complete`.

A verifier refusal is a legitimate conclusion: it is published as the action's
negative result (`ok: false` with the verifier's own code), exactly like an
in-beat refusal.

## Signed evidence

The evidence body carries the action identity, the fence value, the owner, the
binding fields and the complete verify record; an HMAC-SHA256 signature covers
its canonical JSON, so editing any signed field invalidates the evidence.

```
schema         bundlebees.verify-evidence/1
action_id      verify-<attempt_id>
action_key     verify:<attempt_id>
fence_seq      the claim's fencing value
owner          the bee's name
attempt_id     the attempt
source_rev     the pinned base revision
workflow_ref   the protocol pin
job_id, repo   the authorizing job and repository
record         the full verify_local record
signature      HMAC-SHA256 over the canonical body
```

The signing key is supplied explicitly to `worker_key` (at least 32 bytes of
secret material). Production callers pass the provisioned key read from the
Hermes secret scope, exactly like the approval signing key; it is never read
from the environment, a config file or the coordinator state directory here.
The verify-worker bee is a host-local bee: the sandbox enforcement is the
verify runner's (see `verify_local.sandbox_runner`), and the record and commit
land in the shared coordinator state and repository the merge gate reads.

## Consumption

`accept_verify_evidence` is the coordinator's check, in order:

1. Evidence schema and signature — an unsigned or forged body is refused.
2. Action identity — the evidence must name the claimed action.
3. Binding — attempt id, source revision, workflow pin, patch pin, named
   tests, declared file set, and the lease's own file set.
4. Record contract — exactly the record checks the merge gate applies
   (`queue.job_accepted_reasons`, `integrate_job`): accepted result, full
   commit sha, branch, red evidence, regression suite, scanner result, mode.
5. Git re-proof — `verify_local.reprove_commit` re-proves the commit exists,
   parents the pinned base and carries the record's file set, without running
   any test.
6. Fenced completion — the queue's `complete` enforces the fence: a worker
   superseded by an adoption is refused with `stale_fence`, and replaying an
   already-accepted evidence is idempotent. Any other conclusion after the
   action is done is an `evidence_conflict`.

## Evidence equality contract

Worker evidence is the ordinary `verify_and_commit` record, so the merge gate
cannot tell a worker-verified attempt from an in-beat one:

* same fields — the evidence must carry exactly `RECORD_FIELDS` (the field set
  of `verify_local._record`);
* same pass/fail semantics — accepted result, red evidence, regression suite,
  commit and branch binding;
* same scanner result — the configured scanner's exit status is part of the
  contract; the verify lane always runs it.

The shadow comparator (`compare_evidence`) compares `COMPARABLE_FIELDS` and
requires exact field-set equality. `VOLATILE_FIELDS` — the execution identity,
cache counters and timestamp — are excluded from equality deliberately and by
name: they describe where and when a verification ran, not what it concluded,
and the bee is a different execution environment. They are still recorded in
every comparison so the exclusion stays visible. The `sha` comparison is
semantic when a repository is supplied: two commits are equal evidence when
both exist, parent the pinned base and carry the record's file set — committer
time makes byte equality across two runs impossible even for identical
content.

## Shadow mode and the switch

The mode flag (`SETTINGS_KEY = "verify_workers"`, resolved by `resolve_mode`
from an explicit argument or the settings mapping) has three values:

| mode | behaviour |
| --- | --- |
| `in-beat` (default) | the pipeline runs the serial verifier exactly as before |
| `shadow` | both paths run on a bounded sample set; the in-beat verdict stays authoritative; every comparison is recorded; divergence alarms |
| `on` | the worker path is the verification; the in-beat verifier does not run |

Shadow mode never switches anything by itself:

* The sample set is bounded (`SHADOW_SAMPLE_LIMIT` per coordinator state
  directory, durable counter under `verify-workers/shadow/budget.json`).
  Beyond the bound, dispatch verifies in-beat only and records the skip
  (`budget_exhausted`) instead of silently growing the dual-run set.
* The worker path runs for real: record reuse and receipt reuse are disabled
  for the shadow run, so the comparison observes an execution rather than
  inheriting the first path's record.
* Every comparison is written to
  `verify-workers/shadow/comparisons/<attempt_id>.json`; an unequal comparison
  additionally appends to `verify-workers/shadow/alarms.jsonl` and raises the
  named `shadow_divergence` alarm. The dispatch wrapper converts the alarm
  into a payload flag while the in-beat verdict stays authoritative — shadow
  mode changes no pipeline outcome, it only measures.

Switch procedure (the flag documents it):

1. Run with `verify_workers=shadow` until the sample set is exhausted with
   zero alarms for the workloads that matter.
2. Audit `comparisons/` directly: field-set equality and no semantic
   divergences, `worker_refusal` absent.
3. Set `verify_workers=on`. Keep monitoring `shadow_alarms_present`; any
   residual alarm is a named, durable record that must be explained before it
   is removed by hand.
4. Rollback is setting the flag back to `in-beat`; records already consumed
   under `on` remain valid evidence (the queue and the record contract do not
   change with the mode).

## Wiring point

The lane is additive and self-contained. The one-line integration is the
pipeline's verifier closure calling `dispatch_verification` with
`resolve_mode(config=settings)`; until that lands, the default (`in-beat`)
keeps the pipeline exactly as it is.

## Prior art and rejected alternatives

Gutting the signed-evidence fixtures to plain strings, or deleting the
signature and MAC assertions, to satisfy a credential scanner was rejected: it
would weaken the very evidence-equality contract the acceptance criteria pin.
Synthetic digest-shaped fixture values are built inertly instead (runs of one
repeated letter), so format assertions hold while no scanner rule fires.

Sibling-lane pitfalls honored here: dispatch refuses abbreviated lease paths
(`lease_scope_mismatch` at enqueue, before any worker runs); no test pins
documentation prose byte-for-byte; and no credential-shaped assignment prose
appears in this document or its tests.
