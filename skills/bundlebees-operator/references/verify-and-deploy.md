# Verify and deploy (user-run CLI)

`hermes bundlebees verify`, `integrate`, `lease`, `metrics` and `deploy` are
registered CLI commands, **not model tools**. They apply worker output, run tests, push a
branch, or reach a machine, so a model turn must not invoke them. Ask the user
to type the command and paste the result back. The model may explain what the
command will do and read its output, but it does not run it.

## Verify

User types:

```text
hermes bundlebees verify <attempt_id>
hermes bundlebees verify <attempt_id> --workdir <path> --tests <name>
hermes bundlebees verify <attempt_id> --dry-run
hermes bundlebees verify <attempt_id> --kanban-comment <text>
```

Flags:

| Flag | Meaning |
|---|---|
| positional `attempt_id` | The attempt returned by `bundlebees_dispatch`. |
| `--workdir <path>` | Existing clean worktree to use. Confined; no path escape. |
| `--tests <name>` | Run only this named test. Repeat for more than one. |
| `--dry-run` | Apply the patch and run tests, then stop before any push. |
| `--kanban-comment <text>` | Opt-in: after a green run, suggest or post a comment through the public kanban CLI. |

What verify does, in order:

1. Load the coordinator lease `leases/<attempt_id>.json` from plugin data. It
   was written read-only and HMAC-signed at dispatch; a missing or edited
   lease fails closed (`lease_missing`, `lease_tampered`). File scope, branch,
   `source_rev` and tests come only from this record. Scope claims in the
   worker result are ignored and listed as `ignored_worker_claims`.
2. Fetch the attempt result and reduce it to the adapter result contract
   (patch, declared base, usage, worker-claimed checks). Missing or 409 maps
   to `no_result_yet`; wait and retry, do not invent a result.
3. Check out a clean copy of the **feature branch**. It refuses `main` and
   `master` as the push target.
4. Revision guarantees: the worker's declared base must equal the lease's
   `source_rev` (`base_undeclared`, `base_mismatch`); that commit must exist
   (`source_rev_missing`); the patch must apply to that exact commit, checked
   in a temporary index (`patch_not_at_source_rev`); and the feature branch
   must descend from it (`branch_diverged`).
5. Apply the patch to the branch. Any path outside the lease fails with
   `scope_violation`; nothing is committed or pushed. Then the secret scan.
6. Run the named tests in a **disposable sandbox**: a throwaway copy of the
   worktree (no `.git`), run as an unprivileged user under bubblewrap
   (or firejail) with no network and a scrubbed environment that has no
   coordinator credentials or home dotfiles. Only evidence returns: backend,
   exit code, junit counts, an output tail. `unshare` is never picked
   automatically (it leaves the coordinator's files readable); it needs
   `verify_isolation: unshare` plus the unsafe `verify_allow_weak_unshare`
   override. With no strong backend verify refuses (`sandbox_unavailable`)
   unless `verify_allow_unsafe_local` is set.
7. On green, commit and push the **feature branch only**. No merge, tag,
   force-push, or hook skip.
8. Print a summary with the SHA, tests, files and sandbox backend, and a note
   that the wave is not accepted until integrate passes.

Verify writes `verify-results/<attempt_id>.json` with `attempt_id`, `ok`,
`sha`, `branch`, `files`, `tests`, `at`, and `evidence` (`source_rev`,
`backend`, `exit_code`, `junit`, `diffstat`, `duration_s`). It also appends a
`verify` event to `metrics/<run_id>.jsonl`. No hosts, tokens, or paths outside
the repo.

## Integrate (wave acceptance)

```text
hermes bundlebees integrate --wave <id> --attempts <id> [<id> ...] [--branch <feat>]
```

Requires every attempt to have an `ok` verify record whose SHA is an ancestor
of the combined branch HEAD, then runs the **full** suite on that branch in
the same sandbox. It writes `waves/<id>.json`; only `accepted: true` counts,
and an accepted record is final. `bundlebees_attempt op=review accepted=true`
is refused with `integration_required` until then.

## Lease and metrics

- `hermes bundlebees lease <attempt_id> --brief <file>` records the lease for
  a brief dispatched outside `bundlebees_dispatch` (validated first, refused
  if a lease already exists).
- `hermes bundlebees metrics [--run <id>] [--json]` summarizes the
  append-only, hash-chained log: first-pass rate, accepted changes, tokens and
  cost per accepted change, human interventions, plan-to-accepted time,
  regressions, failed-worker recoveries. `--note <text>` records one human
  intervention.

If verify or integrate is red:

| Code | Meaning and next step |
|---|---|
| `no_result_yet` | Ask the user to wait and retry. |
| `lease_missing` / `lease_tampered` | No valid coordinator lease. Never edit lease files; record one from the dispatched brief or re-dispatch. |
| `base_undeclared` / `base_mismatch` / `source_rev_missing` / `patch_not_at_source_rev` / `branch_diverged` | Not built on the brief's exact revision. Re-dispatch with the right `source_rev` and `retry_of`. |
| `scope_violation` | The bee exceeded its lease. Stop; no repair dispatch until the user decides. |
| `sandbox_unavailable` | Install bubblewrap (or firejail). Do not suggest the unsafe local or weak unshare overrides on a shared or credentialed host. |
| `tests_failed` | Report the test names and output tail; ask whether to dispatch a fresh narrow brief. |
| `secret_detected` | Stop immediately. Do not echo the matched value. |
| `integration_required` | Run integrate for the wave first. |
| `accepted: no` (integrate) | Report the problems or failing suite; regressions are counted in metrics. |

## Deploy

User types:

```text
hermes bundlebees deploy
hermes bundlebees deploy --execute
```

Without `--execute`, deploy prints the setup steps or the generated host
prompt and refuses implicit SSH. Nothing leaves the machine.

With `--execute`, deploy may use a host alias that is already stored in plugin
config. The host value is never printed and is never accepted as an argument
pasted into chat. The child `ssh` or `scp` process gets a minimal exact-name
environment from `bundlebees_plugin.security.child_env`.

Deploy is always a user gesture. The model does not run it, does not pass a
host, and does not ask for a key or token in chat. A model-generated host
prompt is safe to show: it contains paths, environment variable names, and
phase instructions only.

## Doctor

```text
hermes bundlebees doctor
```

Doctor checks whether the launcher token is configured (yes or no, never the
value), whether the loopback adapter is reachable, and whether the four tool
schemas are within the token budget. It is also user-run in this skill.

## Exact handoff

When an attempt reaches a terminal state, send the user:

```text
Attempt: <attempt_id>
Card: <kanban_task_id or none>
State: <state>
Next, please run in your terminal:
  hermes bundlebees verify <attempt_id>
Then paste the summary back here.
```

After a green summary, the next user action is a pull request into
`feat/v0.1.0`. Never merge to `main`, tag, force-push, or deploy without a
fresh, explicit approval.
