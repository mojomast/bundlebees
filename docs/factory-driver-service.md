# Factory driver service mode

`bundlebees_plugin.factory_driver` has a resident **service mode** that reacts
to a landing within seconds instead of waiting for the 30-minute heartbeat.
The unit shipped in this repository is a **template and documentation only. It
is NOT installed, enabled or started by this project.**

## What one service pass does

1. **Wakes on the inbox.** `FactoryService` watches
   `events/inbox/<subscriber>/` with inotify when the platform exposes it. If
   inotify is unavailable it falls back to a short bounded poll. An event that
   was written before the loop started is also picked up immediately.
2. **Drains the completion consumer.** `event_consumer.consume_completions` is
   called for the `coordinator-actions` subscriber, so a terminal landing
   enqueues its `verify` and `process-completion` action items.
3. **Dispatches only pre-approved work.** The service asks
   `work_scheduler.ready_items` for the next eligible item and dispatches it
   only when all of these are readable and allow it:
   - a durable dispatch pre-approval at
     `<state>/factory/dispatch-approvals/<work_id>.json` whose `approved` is
     exactly `true`;
   - a readable scope list covering every file the item leases;
   - the shared bee cap (`grants.GLOBAL_MAX_BEES`, 12);
   - the scheduler's planning-slot limits
     (`work_scheduler.planning_limits()`).
4. **Fails closed and wakes Hermes.** If the approval, scope, cap or limits
   are absent or unreadable, no dispatch happens. The service writes a
   durable judgment event and requests one coalesced wake through the existing
   `WakeCoalescer` + `hermes_adapter.notify_coordinator` path.

The generic `--interval` loop is unchanged. `FactoryDriver.run_pass()` also
keeps its existing behavior; service mode calls it with `advance_work=False`
and owns the dispatch gate itself. `--service` passes are flag-gated for the
ready-column observation described below; unset, the pass result and the
filesystem are unchanged.

## Triggers: events first, the heartbeat last

The 30-minute heartbeat is **the reconcile safety net only; it is no longer the
primary trigger**. In order:

1. **Landing event (seconds).** A bee landing writes a completion event into
   `events/inbox/<subscriber>/`; the driver service wakes on it (inotify, with
   a bounded poll fallback) and runs one pass. This is the primary path.
2. **Scout hook (about 2 minutes).** When the resident service is not running,
   the 2-minute scout can call the hook so a completed attempt triggers exactly
   one driver pass instead of waiting for the beat. The hook is a trigger, not
   a second driver.
3. **Heartbeat (30 minutes).** `beat.beat()` reconciles what the push triggers
   missed: the completed-attempt sweep, the merge stream, and the flag-gated
   ready-column observation. It repairs state; it is not what normally starts a
   landing's verification or dispatch.

An idle beat turn is therefore expected and healthy. A landing that only the
beat noticed is a defect in the event path, and the beat report shows the
attempt age that made it visible.

## Ready-column stocking (flag-gated)

`BUNDLEBEES_BOARD_STOCK=1` adds one observation-only duty to the beat and to
each `FactoryService` pass: it compares the number of cards the board reports
in `ready` with the free bee slots (shared cap minus claimed work items) and
raises a signal when ready work is short. It never creates, moves, approves or
dispatches a card; the only board write it can make is one comment when a low
observation changes, identical repeats are silent, and a recovery followed by a
new shortfall comments again. Default off: unset, no board read, no subprocess,
no file.
See `docs/board-stock.md`.

## Pre-approval record

A scheduler item is dispatchable in service mode only with a record like:

```json
{
  "schema": "bundlebees.factory-dispatch-approval/1",
  "work_id": "ws_verify_att_123",
  "approval_key": "<sanitized work_id>",
  "job_id": "job_a",
  "approved": true,
  "scope": ["src/**", "tests/**"],
  "approved_by": "owner",
  "approved_at": "2026-11-18T12:00:00Z"
}
```

The approval key is the sanitized ``work_id`` that names the record file; it is
a lookup name, never a secret, so an example may show it as a placeholder.

Write it with:

```python
from bundlebees_plugin import factory_driver

factory_driver.record_dispatch_approval(
    state_dir,
    "ws_verify_att_123",
    job_id="job_a",
    scope=["src/**", "tests/**"],
)
```

The driver never infers consent from a missing, malformed or false record.

## Required environment

The unit reads every value from `~/.config/bundlebees/factory-driver.env`
(mode 0600) through variable names only; no value belongs in the unit file.

| Variable | Required | Meaning |
|---|---|---|
| `BUNDLEBEES_LAUNCHER_URL` | yes for a real tick | Launcher base URL. Without it the driver only runs the mechanical observation tick. |
| `BUNDLEBEES_LAUNCHER_TOKEN` | yes when the URL is set | Bearer token the launcher accepts. Never put this value in the unit or a log. |
| `BUNDLEBEES_REPO` | yes for a real tick | Local repository checkout the job tick operates on. |
| `BUNDLEBEES_GITLEAKS` | yes for a real tick | gitleaks executable; a pass refuses before claiming anything without it. |
| `WORKFLOW_SRC` | **yes** | Checkout of `workflow` containing `tools/feature_packet.py`, `tools/recovery.py`, `tools/return_manifest.py` and the schemas. Landing validation, packet admission and recovery fail closed (for example `workflow_schema_unavailable`) when it is unset or unreadable. Point it at the pinned revision CI uses. |
| `HERMES_API_URL`, `HERMES_API_KEY` | no | Wake endpoint and bearer key; without them a judgment is recorded durably but no gateway wake is delivered. |

The unit starts with `--require-dispatch-config`: if the launcher URL, repo or
gitleaks value is unset it exits nonzero with the missing names instead of
silently degrading to observation-only. The `EnvironmentFile` directive has no
`-` prefix, so a missing file is also a startup failure.

## Running from the CLI

```bash
python -m bundlebees_plugin.factory_driver \
  --state "$BUNDLEBEES_STATE" \
  --queue "$BUNDLEBEES_QUEUE" \
  --launcher-url "$BUNDLEBEES_LAUNCHER_URL" \
  --repo "$BUNDLEBEES_REPO" \
  --gitleaks "$BUNDLEBEES_GITLEAKS" \
  --require-dispatch-config \
  --service \
  --service-interval 30
```

`--service-interval` is the upper bound between idle passes. Landing events
wake the loop immediately; the interval is not a fixed sleep. Without a
launcher URL, repo and gitleaks the driver can only run the mechanical
observation tick; `--require-dispatch-config` makes that a startup error
instead of a silent degradation.

## Unit file (not installed)

The user unit lives at:

```text
contrib/systemd/bundlebees-factory-driver.service
```

These are the manual steps; **this repository does not perform them**:

```bash
# 1. Review and copy the unit (values such as paths are placeholders).
mkdir -p ~/.config/systemd/user
cp contrib/systemd/bundlebees-factory-driver.service ~/.config/systemd/user/

# 2. Provide the values in a 0600 file (the unit references names only):
#    BUNDLEBEES_LAUNCHER_URL, BUNDLEBEES_LAUNCHER_TOKEN, BUNDLEBEES_REPO,
#    BUNDLEBEES_GITLEAKS, WORKFLOW_SRC, and optionally HERMES_API_URL and
#    HERMES_API_KEY.
install -m 0600 /dev/null ~/.config/bundlebees/factory-driver.env

# 3. Only after reviewing the unit and environment:
systemctl --user daemon-reload
systemctl --user enable --now bundlebees-factory-driver.service

# Optional, when a live user manager exists:
systemctl --user status bundlebees-factory-driver.service
journalctl --user -u bundlebees-factory-driver.service -f
```

To remove it:

```bash
systemctl --user disable --now bundlebees-factory-driver.service
rm ~/.config/systemd/user/bundlebees-factory-driver.service
```

## Sandbox tests

`tests/test_factory_service_mode.py` validates the shipped unit with strict
INI/structural checks (`[Unit]`, `[Service]`, `Type`, `ExecStart`). The
`systemd-analyze --user verify` check renders the shipped unit into a temporary
home with `ExecStart` pointing at an interpreter that exists on the runner, so
it runs on a CI runner without the deployment host's venv; a second case proves
the same check still rejects a missing interpreter. It skips only when
`systemd-analyze` itself is absent.

The service tests use temporary state, a fake tick and a spy for the gateway
wake, so they pass with no network, no systemd user session and no real
worker.
