# Board → dispatcher bridge

The Kanban board is where approved work sits in `ready`; the factory driver is
what dispatches bees. Before this bridge nothing connected them: the board held
cards, the driver's `queue.json` and `factory/dispatch-approvals/` stayed empty,
and a bee was only ever started by hand.

`hermes bundlebees board-dispatch --once` is that connection, and it is the only
writer of a board-derived queue feature.

## What one pass does

1. **Reads the ready column** through the public CLI
   (`hermes kanban list --status ready --json`), never the SQLite file and never
   a card body as authority. A board or payload that cannot be read is one typed
   refusal (`board_unreadable`) and nothing is stocked.
2. **Loads the card's approval record** at
   `<state>/factory/board-approvals/<task_id>.json`. A card with no record is
   never stocked (`approval_absent`). Consent is never inferred from a title, a
   comment or the fact that a card is in `ready`.
3. **Validates the approved brief** with the strict dispatch validator: full
   40-hex `source_rev`, repository-relative non-empty `assigned_files`, and an
   acceptance block. A brief that would not reach a bee is refused here
   (`brief_invalid`).
4. **Requires the named job document** at `<state>/jobs/<job_id>.json`. A
   feature whose job does not exist can never dispatch — the queue stage would
   only mark it `needs_approval` — so the bridge refuses to stock it
   (`job_absent`) instead of leaving fake work in the queue.
5. **Writes, idempotently:**
   * one feature in `queue.json`, preserving the live docket envelope
     (`schema`, `generated_at`, `note`) and every existing feature verbatim;
   * one `factory/dispatch-approvals/<work_id>.json` pre-approval record whose
     scope is the brief's file lease.

The resident `bundlebees-factory-driver.service` reads `queue.json` on every
pass (an idle pass is bounded at 30 s), claims the feature and ticks its job,
which dispatches the bee. Stocking is the whole effect of this command: it does
not dispatch, claim, verify, merge, restart or approve anything.

## The approval record

```json
{
  "schema": "bundlebees.board-approval/1",
  "task_id": "t_example",
  "repo": "mojomast/workflow",
  "job_id": "job_example_1",
  "approved": true,
  "approved_by": "clanker01",
  "approved_at": "2026-11-18T21:00:00Z",
  "brief": {
    "objective": "One concrete outcome.",
    "project": "workflow",
    "branch": "feat/example",
    "source_rev": "40 lowercase hex characters",
    "assigned_files": ["path/one.py"],
    "acceptance": {"criteria": ["..."], "tests": ["tests/test_one.py"]}
  }
}
```

The record carries the brief on purpose: approval and brief cannot drift apart,
and the bridge validates exactly the brief it stocks. `job_id` must name an
existing job document; create it with `hermes bundlebees job create`, which
honours a human-enrolled repo grant for the repository.

Write the record only when a human has approved that work. The bridge never
creates, edits or repairs one.

## Fail-closed rules

| Refusal | Meaning |
|---|---|
| `board_unreadable` | The ready column could not be read; nothing is stocked. |
| `approval_absent` | No approval record for the card; nothing is stocked. |
| `approval_invalid` | `approved` is not `true`, or the record is incomplete. |
| `approval_unreadable` | The record is unreadable or names another card/schema. |
| `brief_invalid` | The approved brief fails the strict dispatch validator. |
| `job_absent` / `job_unreadable` | The job document is missing or names another job. |
| `feature_conflict` | A feature with this card id already names another job or repo. |
| `approval_conflict` | A pre-approval for this work id names another job or scope. |
| `queue_invalid` / `queue_unreadable` | `queue.json` is not one of the two supported shapes. |

A replay with the same inputs changes nothing: the feature identity is
`(feature_id, job_id, repo)` and the pre-approval identity is
`(work_id, job_id, scope)`. A differing identity is a conflict and is never
overwritten — a human decides.

## Command

```bash
hermes bundlebees board-dispatch --once --dry-run   # show intended writes
hermes bundlebees board-dispatch --once             # stock approved ready cards
hermes bundlebees board-dispatch --once --json      # the full per-card report
```

`--board <slug>` selects a board; `BUNDLEBEES_BOARD_DISPATCH_BOARD` sets the
default. `--state-dir` overrides the plugin data directory.

## What it deliberately does not do

* It never approves work, creates an approval record, or moves a card.
* It never creates a job or an authorization; a missing job is a refusal.
* It never writes a card body, comment or column.
* It never dispatches — the resident service does, from the stocked feature.
