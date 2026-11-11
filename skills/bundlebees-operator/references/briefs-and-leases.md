# Briefs, leases, and budgets

A brief is the whole contract between the coordinator and one bee. The tool
validates it in code against the bundled launcher schema before any create
request. Pass an object (preferred) or a JSON object string; the schema is
not embedded in the tool definition.

## Required and important fields

| Field | Type | Notes |
|---|---|---|
| `objective` | string | **Required.** One concrete outcome. No secrets or addresses. |
| `project` | string | Launcher-authorized project key. Must be in `bundlebees_status` `op=projects`. |
| `branch` | string | Feature branch only. Never `main`. |
| `source_rev` | string | Starting revision or tag for the slice. |
| `assigned_files` | string list | Exact files the bee may change. Required for code-work briefs. |
| `commands` | string list | Commands the bee may run, ideally the narrow test command. |
| `acceptance` | string list | Observable checks the user-run verify will look for. |
| `deliverables` | string list | Files or artifacts the bee must leave behind. |
| `deps` | string list | Sibling task ids or prerequisites. |
| `budget.timeout_s` | integer >= 1 | Wall-clock cap in seconds. |
| `budget.max_tool_calls` | integer >= 1 | Tool-call cap. |
| `kanban_task_id` | string | Optional Hermes card id; recorded and returned, not called. |

`additionalProperties` is allowed by the launcher schema, but keep the brief
small. Anything you add is still subject to scrubbing and review.

## What makes a brief good

- One slice, one branch, one narrow test. If the objective needs two
  independent areas, split it into two cards and two dispatch calls.
- The lease is the file list, not a directory wish. List each path the worker
  may create or edit. Do not use globs, parent directories, or `..`.
- Put the test command in `commands` and mirror it in `acceptance`. Do not ask
  for a full end-to-end suite unless the user explicitly wants it.
- State the deliverable, not the implementation. The worker decides how; the
  lease and tests decide whether it succeeded.
- Give a small `budget`. A cheap Flash bee should finish a narrow slice well
  inside it; a timeout is a signal to split the work, not to raise the cap.
- Never put a launcher URL, token, key, bind address, SSH host, or home path
  in any field. Those are preconfigured, not brief data.

## Lease rules

1. **Lease before dispatch.** `assigned_files` must be final before the
   attempt starts. A worker that needs another file needs a new brief.
2. **No overlap with a running attempt.** `bundlebees_dispatch` compares the
   new lease with live attempts on the same project and branch and returns
   `lease_overlap` on a shared path. This is **best-effort**: it sees only
   attempts the launcher currently reports, not work on other machines, not
   queued work that has not started, and not a stale result.
3. **Parallel bees get disjoint leases.** If two bees run at once, their file
   sets must not intersect. Branch alone is not enough when the same files can
   be edited twice.
4. **Verify enforces the lease.** The user-typed `hermes bundlebees verify`
   rejects a patch that touches anything outside `assigned_files` with
   `scope_violation`. A worker that ignores the lease fails verification even
   if its tests pass.
5. **Dependencies are ordered.** Use `deps` and the kanban card link so a
   dependent bee does not start until its parent is done and verified.
6. **No lease inheritance.** A card that follows another does not inherit its
   file list. Write a fresh lease for each attempt.

## Budgets and concurrency

- Default model is `deepseek-v4.1-flash`. Dispatch only models the launcher
  allow-lists; otherwise expect `model_not_allowed`.
- Keep the number of in-flight attempts at or below the launcher's
  `max_concurrent` and the user's own limit. Prefer two narrow bees over four
  broad ones.
- `budget.timeout_s` covers the worker run. A timeout does not authorize a
  cancel by itself; ask the user before cancelling a working bee.
- `budget.max_tool_calls` caps exploration. If a bee hits it repeatedly, the
  brief is too broad.

## Dispatch envelope

On success the handler returns a JSON string with `ok: true` and, when the
launcher reports them, `attempt_id`, `state`, `task_id`, and
`kanban_task_id`. Record those before the next action. On failure read
`code` and `fix`; the common codes are `brief_invalid`, `project_unauthorized`,
`lease_overlap`, `model_not_allowed`, `not_configured`, and
`launcher_unavailable`.

Dispatch only submits the brief. It does not apply a patch, run tests, push a
branch, or reach a host. Those are separate, user-run steps.
