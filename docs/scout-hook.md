# Interim scout hook

`bundlebees factory-hook` is the repository-side bridge between the existing
2-minute rizz-scout and one ordinary factory-driver pass. The scout already
polls the launcher; when it observes an attempt reach a terminal state, the
hook lets it ask the factory driver to run *once* immediately instead of
waiting for the 30-minute heartbeat.

The hook is **repo code only**. It does not install itself, edit the live
scout script, create or change a cron entry, restart the gateway, or touch any
host configuration. Wiring it into the live 2-minute scout is an explicit owner
action (see below). The 30-minute heartbeat remains the backstop and is not
replaced by this change.

## What it does

- Reads the scout's attempt view (or the launcher's attempt list).
- Considers an attempt **completed** when its `state`/`status` is terminal:
  `succeeded`, `success`, `completed`, `complete`, `done`, `landed`, or a
  terminal failure such as `failed`, `cancelled`, `timed_out` or `rejected`.
  Running and queued attempts never trigger a pass.
- Runs at most one injected factory-driver pass for newly observed completed
  attempts.
- Records every completed attempt id in a durable watermark after the pass
  succeeds. Re-running over the same unchanged state is a no-op.
- Serialises overlapping invocations with a non-blocking `flock`. The loser
  reports `coalesced` and does not run a second pass.
- Leaves the watermark unadvanced when the pass raises, so the next scout tick
  retries the completion instead of losing it.

## State files

Both live under the plugin data directory (`--state-dir`):

| Path | Purpose |
| --- | --- |
| `factory/scout-hook-watermark.json` | Durable terminal attempt ids already handled. |
| `factory/scout-hook.lock` | `flock` used to coalesce overlapping scout runs. |

The watermark is written atomically (private temporary file, `fsync`,
`os.replace`). A corrupt or unreadable watermark fails closed rather than being
silently discarded.

## CLI

The command is registered as a subcommand of the existing user-run CLI:

```bash
hermes bundlebees factory-hook --help
```

With no completed attempt it prints a JSON report with `"status": "noop"` and
`"passes": 0`, and exits `0`. A completed attempt it has not handled before
prints `"status": "pass"` and `"passes": 1`, and exits `0`. An overlapping
invocation prints `"status": "coalesced"` and `"passes": 0`, and exits `0`.

A real hook failure (unreadable attempts, corrupt watermark, lock I/O failure,
or a factory pass that raises) exits non-zero. A pass failure exits `2` and
does not advance the watermark. "Nothing changed" is never an error.

### Attempt source

By default the command asks the configured launcher adapter for its attempts
list. If the live scout already saved the document it inspected, pass it
directly to avoid a second fetch:

```bash
hermes bundlebees factory-hook \
  --state-dir "$STATE" \
  --attempts-file "$SCOUT_ATTEMPTS_JSON"
```

The file may be a JSON array of attempt records, or an object with an
`attempts` (or `items`) list. A record needs an `attempt_id`/`id` and a
`state`/`status`.

### Factory pass

Without extra options the hook runs one pass through the same factory-driver
seams as `python -m bundlebees_plugin.factory_driver --once`: the durable
queue, landing/events and the configured tick. The common production options
are accepted directly:

```bash
hermes bundlebees factory-hook \
  --state-dir "$STATE" \
  --launcher-url "$BUNDLEBEES_LAUNCHER_URL" \
  --repo "$COORDINATOR_REPO" \
  --gitleaks "$GITLEAKS" \
  --provisioning --provisioning-host "$PROVISIONING_HOST" \
  --suite-command python -m pytest -q
```

`--queue`, `--launcher-outbox`, `--subscriber` (repeatable), `--tick
module:callable`, `--campaign-id` and `--lock-wait` are also available. The
pass is still exactly one pass; the hook never starts the resident loop.

## Wiring it into the live 2-minute scout (owner action)

The live scout script and cron entry are **not** part of this repository change
and have **not** been modified. To opt in, the owner should:

1. Open the live 2-minute scout script on the coordinator host. Locate the
   point after it has read the launcher attempts and determined that something
   changed (or simply call the hook every tick — the watermark makes that
   safe).
2. Add one invocation, using the same state directory and driver options the
   heartbeat already uses:

   ```bash
   hermes bundlebees factory-hook \
     --state-dir "$STATE" \
     --launcher-url "$BUNDLEBEES_LAUNCHER_URL" \
     --repo "$COORDINATOR_REPO" \
     --gitleaks "$GITLEAKS" \
     --suite-command python -m pytest -q
   ```

   If the scout has already written its attempts snapshot, add
   `--attempts-file "$SCOUT_ATTEMPTS_JSON"` so the hook reuses that exact view.
3. Leave the 30-minute heartbeat cron in place. It remains the restart-safe
   backstop and the multi-bee sweep.
4. Test the edited script on the host with one manual run, then let the
   existing 2-minute schedule pick it up.

Do not remove the heartbeat as part of this interim hook. Removing it is a
separate owner decision after the native completion path is deployed.

### Why duplicate triggers are safe

The scout may notice the same completion on several ticks, and two overlapping
scout processes may both reach the hook. The watermark makes repeats a no-op
and the lock makes a concurrent pair coalesce to one pass. A crash between the
pass and the watermark write can cause one extra pass on the next tick; the
factory driver's durable queue/claims are designed to make that pass
idempotent, and at-least-once is preferable to silently dropping a completion.

## Tests

`tests/test_scout_hook.py` records the RED-then-GREEN run in its module
docstring. The focused check is:

```bash
python -m pytest -q tests/test_scout_hook.py tests/test_factory_cli_contract.py
```

The tests inject the pass runner; they use no launcher, network, token or model
call.
