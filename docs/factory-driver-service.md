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
and owns the dispatch gate itself.

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

## Running from the CLI

```bash
python -m bundlebees_plugin.factory_driver \
  --state "$BUNDLEBEES_STATE" \
  --queue "$BUNDLEBEES_QUEUE" \
  --service \
  --service-interval 30
```

`--service-interval` is the upper bound between idle passes. Landing events
wake the loop immediately; the interval is not a fixed sleep. Set the same
launcher, repo and gitleaks options as the ordinary driver when a real tick is
wanted.

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

# 2. Provide the non-secret configuration and, in a 0600 file, the launcher
#    token/URL and any Hermes wake endpoint/key.
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
INI/structural checks (`[Unit]`, `[Service]`, `Type`, `ExecStart`) and never
requires a systemd user manager. A `systemd-analyze --user verify` call is
attempted only when a live user manager is reachable; otherwise that check
skips cleanly.

The service tests use temporary state, a fake tick and a spy for the gateway
wake, so they pass with no network, no systemd user session and no real
worker.
