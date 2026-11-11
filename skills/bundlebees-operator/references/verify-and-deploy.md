# Verify and deploy (user-run CLI)

`hermes bundlebees verify` and `hermes bundlebees deploy` are registered CLI
commands, **not model tools**. They apply worker output, run tests, push a
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

1. Fetch the attempt result. Missing or 409 maps to `no_result_yet`; wait and
   retry, do not invent a result.
2. Check out a clean copy of the **feature branch** in the user's worktree or
   a configured worktree. It refuses to use `main` as the push target.
3. Apply the worker patch. Any path outside the attempt's `assigned_files`
   fails with `scope_violation`; the patch is not committed and nothing is
   pushed.
4. Run only the tests named in the brief or passed with `--tests`, plus the
   repository's documented secret scan. No full end-to-end suite unless the
   brief listed it.
5. On green, commit and push the **feature branch only**. No merge, tag,
   force-push, or hook skip.
6. Print a summary with the SHA, tests, and changed files, plus a suggested
   `hermes kanban request-review <id>` line. It does not mutate the board
   unless the user passed `--kanban-comment`.

Verify writes a small record under plugin data
`verify-results/<attempt_id>.json` with `attempt_id`, `ok`, `sha`, `branch`,
`files`, `tests`, and `at`. That record has no hosts, paths outside the repo,
or tokens.

If verify is red:

- `no_result_yet` - ask the user to wait and retry.
- `scope_violation` - the bee exceeded its lease. Stop; do not dispatch a
  repair until the user decides.
- A failing named test - report the test name and output, then ask whether to
  dispatch a fresh narrow brief.
- A secret-scan hit - stop immediately. Do not echo the matched value.

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
