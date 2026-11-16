# One-time owner approval for a job

`bundlebees job authorize` normally requires the approver to type the
authorization at a TTY. That is the right default, and it stays. It is not
usable by an owner who is away from the keyboard, and it is deliberately not
usable by a coordinator that has no TTY.

This note describes the additive bridge: the owner reviews a plain-language
preview in the trusted application, clicks **Approve** once, and the
application writes a signed receipt. An agent may then run a CLI command that
turns the receipt into the same sealed authorization — but the agent cannot
create the decision, and it cannot widen what the owner approved.

The bridge authorizes **one trial run**. It does not accept a job or campaign,
does not call integration acceptance, and grants no merge, push, release,
deploy or publication authority. Those remain the existing human gates.

## Installation requirement

The decision is signed with an owner-held secret read only through Hermes
`agent.secret_scope.get_secret("BUNDLEBEES_APPROVAL_SECRET")`. The secret must
be at least 32 bytes of random material. Configure it in the secret scope of
the trusted application that renders the preview:

```text
BUNDLEBEES_APPROVAL_SECRET=<32+ random bytes>
```

The plugin never reads this value from the environment, a dotenv file, plugin
settings, the coordinator state directory or argv. If the scope has no secret,
every decision and verification fails closed with
`approval_key_unavailable`, and the fix names this installation step.

The owner capability is derived from the secret and the request id. The
trusted route keeps it in memory for the click; it is never written into a
request, a receipt, a page or a log. A process that can only read the state
directory cannot produce it.

## Flow

1. **The agent proposes.** `approval.create_request(...)` writes one pending
   request under `<state>/job-approvals/requests/<request_id>.json` (mode
   `0600`, directory `0700`). The request is inert: it carries the binding and
   its SHA-256 digest, and nothing else. Repeating an identical request
   returns the same one; a different request for the same job is refused while
   an undecided one exists.

2. **The owner reviews.** The trusted application renders
   `approval.preview(request)`. The preview is presentation only; the decision
   binds `request_digest`, not the wording.

3. **The owner clicks once.** The trusted route obtains the secret through
   `get_secret`, computes `approval.owner_capability(request_id)`, and calls
   `approval.record_decision(..., owner_capability=<capability>,
   expected_digest=<digest the owner saw>, decision="approved"|"declined",
   owner=<name>, auth_method=<route>)`. The result is an HMAC-SHA256 receipt
   under `<state>/job-approvals/receipts/<request_id>.json`. A conflicting
   second decision is refused. A call without the capability is refused with
   `approval_owner_required` and writes nothing, even when the secret is
   configured.

4. **The agent settles the receipt.** After the click, the agent may run:

   ```bash
   hermes bundlebees job authorize \
     --job-id job_a \
     --approval-receipt /path/to/receipt.json \
     --tasks tasks.json
   ```

   `--repo`, `--base-rev`, `--scope`, `--statement`, `--bees` and
   `--wall-clock` default to the receipt's values. If any is passed
   explicitly, it must match exactly. `--tasks` is optional and gives an early
   refusal if the task list is not the approved one; `job create` checks the
   exact normalised task digest again.

5. **Create the job as usual.** `job create` refuses a task set whose
   normalised digest differs from the owner-approved one, even when every
   lease still fits the scope and the bee budget.

## What the receipt binds

Every field is explicit and immutable; none is inferred later.

| Field | Why it is bound |
|---|---|
| `job_id` | A receipt never carries to another job |
| `repo`, `base_rev` | The exact repository and base commit |
| `statement` | The owner's own words, verbatim |
| `scope` | The authorised path globs |
| `tasks`, `tasks_digest` | Task ids and the exact normalised task specs |
| `budgets` | Bees, wall clock and any extra budget the owner saw |
| `worker.project`, `worker.profile` | Which worker project/profile will run |
| `code_rev` | The Bundlebees revision the owner approved |
| `coordinator.stop`, `coordinator.restart` | The planned stop/restart |
| `gates` | The human gates that remain in force |
| `recovery`, `recovery_digest` | The recovery ceiling, never widened after approval |
| `expires_at` | The decision expires (default 900 s, maximum 24 h) |

## Refusals

| Code | Meaning |
|---|---|
| `approval_owner_required` | No owner capability was presented; no receipt is written |
| `approval_owner_capability_invalid` | The capability belongs to another request |
| `approval_key_unavailable` | The Hermes secret scope has no `BUNDLEBEES_APPROVAL_SECRET` |
| `approval_key_weak` | The configured secret is shorter than 32 bytes |
| `approval_request_pending` | Another undecided request exists for this job |
| `approval_request_tampered` | The request's binding does not match its digest |
| `approval_request_ambiguous` | More than one pending request exists; the caller must not guess |
| `approval_scope_changed` | The authorization fields or task set differ from the approved binding |
| `approval_tasks_mismatch` | `--tasks` is not the approved task list |
| `approval_code_rev_mismatch` | The running Bundlebees revision is not the approved one |
| `approval_code_rev_unverifiable` | The checkout cannot name its revision |
| `approval_expired` | The request or receipt is past `expires_at` |
| `approval_signature_invalid` | The receipt does not verify with the owner key |
| `approval_declined` | The owner declined; nothing is authorised |
| `approval_conflict` | A different decision already exists for this request |
| `approval_already_consumed` | The receipt was already spent on another authorization |
| `authorization_receipt_requires_nontty` | A receipt was offered with `require_tty=True` |
| `authorization_not_interactive` | The ordinary non-TTY refusal, unchanged |

## Consumption and replay

`approval.consume_receipt(...)` writes a one-time marker under
`<state>/job-approvals/consumed/`. The marker names a stable consumer identity
(a hash of the authorization fields). Repeating the same consumer is
idempotent; a different consumer is refused with
`approval_already_consumed`. The marker is written before the authorization is
created, so a crash between the two is repaired by replaying the same
authorization, not by a second approval.

Re-sending the same `job authorize --approval-receipt` is idempotent: it
returns the existing sealed authorization when every identity field matches,
and refuses otherwise. The terminal path is unchanged: without a receipt and
without a TTY it still raises `authorization_not_interactive`.

## The receipt in the sealed authorization

The authorization records `authority: "owner-approval"`, `by:
"owner:<name>"`, and an `approval` block with the `request_id`, `receipt_id`,
`receipt_digest`, `request_digest`, owner, authentication method, issue and
expiry times, code revision, task digest, worker and coordinator facts, and
budgets. The job document pins the same block. Nothing here is accepted by
`return_report` as product acceptance; it is provenance for the trial run.

## Gate decisions are verified, not merely present

The bridge does not record or clear a gate decision. Separately, this change
fixes an existing flaw in `return_report.pending_approvals`: it used to treat
any file at `approvals/<job>.<gate>.json` as a recorded approval. A
hand-written `{}` cleared the gate, and so did `record_approval(...,
decision="refused")`. `return_report` now calls `job.gate_decision()`, which
loads and verifies the sealed envelope, the job id, the gate name and the
verdict. Only a valid `granted` record clears a gate; a forged, copied,
refused or unreadable record stays pending and is reported as a limitation.
Existing terminal approval behavior is unchanged.
