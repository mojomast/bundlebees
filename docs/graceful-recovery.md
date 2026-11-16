# Graceful recovery for environment-faulted attempts

`bundlebees_plugin/graceful_recovery.py` is the mechanism that made today's
incident class recoverable. The incident, verbatim from the owner directive:
*a 2.6-hour debugging bee died on a worker transport fault, its diagnosis was
recovered only by manually mining the event stream, and a failed manual fire
that had taken the coordinator claim poisoned the scheduler for ~14 minutes.*
The owner's standing rule: **make sure next time this happens we recover from
it gracefully.**

Four mechanisms, all durable and all mechanical — no model is consulted, and
no prose note ever authorises a dispatch:

1. **Env-fault detection** — a failed attempt whose evidence names the worker
   transport/workspace substrate is classified an *environment* failure, the
   class that must never blind-retry.
2. **Event-stream recovery digest** — for an env-failed attempt, the recovery
   pass extracts the worker narration and key findings from the durable event
   stream into a structured, hashed digest.
3. **Resume-with-evidence re-dispatch** — the recovery pass composes a resume
   brief that embeds the digest (explicitly labelled `recovered-findings`) and
   re-dispatches the same lease/branch; a blind retry without a digest is
   refused by the mechanism itself.
4. **Claim release on death** — a beat/attempt that dies after taking the
   coordinator claim releases the claim file once death is proven; a
   stale-claim takeover after a documented threshold is the fallback.

## 1. Env-fault detection

`classify_env_fault(record)` classifies a failed attempt's evidence. It
accepts a failure mapping (`code`/`error`/`detail`/...), a plain string, or an
already-computed verdict, and returns:

```json
{
  "env_fault": true,
  "class": "environment",
  "code": "shell_transport_failed",
  "detail": "shell transport failed: worker pty hit EOF after 2.6h",
  "matched": ["shell transport failed"],
  "blind_retry": "refused"
}
```

The stable codes for the substrate (`ENV_FAULT_CODES`):

| Code                    | Matches                                                             |
| ----------------------- | ------------------------------------------------------------------- |
| `shell_transport_failed`| shell transport failed/died, worker transport failed, pty EOF/died  |
| `workspace_invalid`     | `workspace_invalid`, workspace invalid/missing/vanished, worktree missing |
| `stream_closed`         | stream closed, output stream ended, session stream closed           |
| `container_engine_eof`  | container engine EOF, dockerd/containerd went away                  |

Observed transport-class codes are classified under their own name
(`transport_error`, `transport_failed`, `worker_lost`, `worker_crashed`,
`connection_lost`, `broken_pipe`), and free text naming "connection lost",
"broken pipe" or a transport "EOF" classifies as `transport_error` /
`stream_closed`. All of them carry `class: "environment"` and
`blind_retry: "refused"`.

Rules the classifier enforces deliberately:

* **The specific phrase beats the declared generic code.** A record that says
  `{"code": "worker_failed", "detail": "shell transport failed: pty EOF"}` is
  classified by its detail.
* **A bare `worker_failed` without transport detail is *not* an environment
  fault.** This classifier never guesses in favour of the
  never-blind-retry class; a patch failure goes to the pinned recovery
  protocol (`recovery/<job>/`), not here.
* An environment fault means the worker never returned a verdict: the patch
  is neither exonerated nor convicted, and `blind_retry` is `refused`.

## 2. Event-stream recovery digest

Durable layout under the plugin state directory:

```
graceful/<attempt_id>/stream.jsonl   # the durable event stream (append-only, fsynced)
graceful/<attempt_id>/digest.json    # the recovery digest (create-only, hashed)
graceful/<attempt_id>/resume.json    # the resume record (create-only)
```

`record_stream_event` appends one JSONL event (scrubbed, fsynced);
`read_stream` reads them back in order. `recovery_digest(state_dir,
attempt_id)` extracts:

* **narration** — every `worker:message` text, in stream order;
* **key findings**, bucketed from the narration's own leading labels
  (`Root cause:`, `Suspect:`, `Repro:`, `Env lessons:`) or taken verbatim
  from explicit `finding` records (explicit beats inferred):
  `root_cause` (what was diagnosed), `suspects`, `repro` (the shell
  commands), `lessons` (env lessons);
* **env_fault** — the stream's terminal transport fault, classified with
  `classify_env_fault`;
* **source** — the stream path and record counts, and a `digest_sha256`
  self-digest over the canonical body.

`save_digest` is create-only and idempotent on identical bytes; `load_digest`
re-verifies `digest_sha256` on every load and refuses an edited digest
(`digest_mismatch`). The digest is evidence, never a scratchpad.

### The real fixture: `att_0cdf5d9d`

The acceptance fixture in `tests/test_graceful_recovery.py` is today's real
workbench case: attempt `att_0cdf5d9d`, a 2.6-hour debugging bee on the
survival panel. Its stream produced exactly what the manual mine produced:

* **root cause** — the unified survival-classification pass re-derives each
  row's class on every render, so a mid-pass re-render drops rows;
* **suspect** — `menu [hidden]`: hiding the menu unmounts the panel before
  the classification pass finishes;
* **repro** — `pytest workbench/tests/test_survival_panel.py::test_row_survives_menu_hidden -x`;
* **env lessons** — node22 in this workbench image only inside tmux (the pty
  transport dies otherwise) and `WORKBENCH_CLASSIFY=1` or the pass is skipped;
* the terminal `shell_transport_failed` record (pty EOF, stream closed).

## 3. Resume-with-evidence re-dispatch

`compose_resume_brief(state_dir, dead_attempt_id=..., base_brief=...)`
composes the dispatchable resume brief:

```json
{
  "schema": "bundlebees.resume-brief/1",
  "kind": "resume_with_evidence",
  "dead_attempt_id": "att_0cdf5d9d",
  "dispatch": {"branch": "...", "lease_files": ["..."], "source_rev": "..."},
  "recovered_findings": {
    "label": "recovered-findings",
    "digest_sha256": "...",
    "root_cause": ["..."], "suspects": ["..."], "repro": ["..."], "lessons": ["..."]
  },
  "provenance": {
    "dead_attempt_id": "att_0cdf5d9d",
    "digest_source": "graceful/att_0cdf5d9d/digest.json",
    "digest_sha256": "...",
    "stream_source": "graceful/att_0cdf5d9d/stream.jsonl",
    "env_fault_code": "shell_transport_failed",
    "resumed_at": "..."
  }
}
```

* The **same lease/branch** is re-dispatched: scope comes from the explicit
  arguments, the dead attempt's brief, or — last — its verified lease record.
  No scope at all is refused (`resume_scope_missing`).
* The digest is embedded **explicitly labelled `recovered-findings`**.
* The resume **records provenance**: the dead attempt id and the digest
  source (plus stream source, digest hash and fault code).
* The brief is dispatched through the coordinator's normal dispatch path; the
  mechanism's job is to guarantee the brief carries the evidence, the scope
  and the provenance before it ever reaches that path.

Refusals — a blind retry cannot get through, from any direction:

| Code                     | When                                                            |
| ------------------------ | --------------------------------------------------------------- |
| `resume_without_evidence`| no durable digest, or a digest with no narration and no findings |
| `resume_not_env_fault`   | the recorded fault is not environmental (use the pinned protocol) |
| `resume_scope_missing`   | no lease scope to re-dispatch                                    |
| `resume_provenance_missing`| provenance incomplete or naming another attempt                 |
| `resume_evidence_mismatch`| `verify_resume_brief`: the embedded findings do not match the durable digest |
| `resume_invalid`         | wrong schema/kind                                                |

The gate lives in `validate_resume_brief`, not only in the composer, so a
hand-composed brief without a `recovered-findings` block is refused too.
`record_resume` persists the validated brief create-only
(`graceful/<attempt>/resume.json`); replaying the identical brief is
idempotent.

## 4. Claim release on death

The coordinator claim file is `coordinator/beat.claim.json`, beside the
scheduler queue (never inside it), mutated under `flock` on
`coordinator/beat.claim.lock`:

```json
{
  "schema": "bundlebees.coordinator-claim/1",
  "owner": "beat_manual",
  "pid": 4242,
  "generation": 3,
  "taken_at": "...", "renewed_at": "...", "expires_at": "...",
  "took_over_from": null
}
```

* `take_claim(owner)` — refused (`claim_held`) while a live claim of another
  owner exists. Taking your own claim again is the renewal path (extends the
  TTL, keeps identity and generation). A claim whose TTL expired or whose
  holder pid is dead is replaced, with `took_over_from` provenance.
* `release_claim(owner)` — cooperative; only the owner may.
* `release_claim_on_death(owner, death_evidence)` — the recovery-pass path.
  Once the holder's death is proven — the env-fault classification of the
  attempt that died, a dead holder pid, or an expired TTL — the claim file is
  released **immediately**, however young the claim is, and the release is
  appended to `coordinator/beat.claim.releases.jsonl`. A live claim without
  proof is never released (`claim_release_refused`).
* `takeover_claim(owner, threshold_s=CLAIM_STALE_THRESHOLD_S)` — the fallback
  for a holder that cannot be proven alive. Before the documented threshold
  (600 s = one heartbeat period) the takeover is refused
  (`claim_not_stale`); after it the claim is taken with
  `took_over_from.reason = "stale_threshold"`. The threshold bounds the
  poisoning class at one heartbeat period even in the worst case — today it
  was ~14 minutes with no bound at all.
* `claim_state` — read-only report (`exists`, `live`, `owner`, `age_s`,
  `stale`).

`recover_scheduler_claim(state_dir, work_id, dead_owner=..., death_evidence=...)`
is the queue-side twin: a beat that died left a *claimed work item* behind,
which is what actually poisoned the scheduler. It releases the item through
`work_scheduler`'s public, owner-checked API (keeping any durable checkpoint)
and optionally hands it to `new_owner` — only with the same proof of death,
or after the documented stale threshold measured from the item's claim stamp.

### The beat flow is not disturbed

The claim file is an additive record beside `coordinator/work.json`. The
reconciliation sweep and the gate-watcher duties keep reading and mutating the
durable queue through `work_scheduler`; a live beat claim does not block
`add_item`, `claim`, `ready_items` or `complete` (tested), and the recovery
pass touches the queue only through those same public APIs. Plugin tool
registrations are unchanged.

## Refusal codes

`GracefulRecoveryError` carries a stable `code`, a scrubbed `message` and a
`fix`. Codes: `attempt_invalid`, `owner_invalid`, `clock_invalid`,
`stream_record_invalid`, `stream_corrupt`, `stream_unreadable`,
`digest_missing`, `digest_invalid`, `digest_corrupt`, `digest_mismatch`,
`digest_unreadable`, `resume_invalid`, `resume_without_evidence`,
`resume_not_env_fault`, `resume_scope_missing`, `resume_provenance_missing`,
`resume_evidence_mismatch`, `claim_held`, `claim_not_stale`,
`claim_not_held`, `claim_release_refused`, `claim_corrupt`,
`claim_unreadable`, `release_log_unreadable`, `work_not_found`,
`record_exists`, `record_corrupt`, `record_unreadable`, `ttl_invalid`,
`threshold_invalid`, `locking_unavailable`.

## What this deliberately does not do

* It does not modify the pinned recovery protocol (`recovery.py`) or its
  sixteen-code vocabulary; non-environmental failures keep flowing through
  `recovery/<job>/` request/decision records.
* It does not dispatch. The resume brief is the dispatchable artefact; the
  coordinator's normal dispatch path consumes it.
* It does not widen the lease: a resume re-dispatches the dead attempt's
  scope, never a new one.
* It does not classify in favour of the never-blind-retry class: absent
  transport/workspace evidence, the verdict is `env_fault: false`.
