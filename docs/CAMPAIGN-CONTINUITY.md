# Campaign continuity

The Jackpot Syndicate campaign exposed a continuity failure, not a verification
failure: the coordinator repeatedly finished a status turn and waited for a
human `what's next`, even while workers were still running. A durable job alone
does not make a campaign continue; something has to wake the coordinator and
something has to reconcile the job, Git and PR state after a restart.

This note describes the smallest bundlebees layer that closes that gap. It does
not add a second authority system: the job's sealed authorization remains the
only permission. It reconciles durable job/worker state; a separate read-only
Git/PR observer is still required for full external reconciliation.

## What is durable

`bundlebees_plugin.campaign` writes one checkpoint per campaign under
`<state>/campaigns/<campaign_id>.json` (`rizzstack.campaign-state/1`):

| Field | Meaning |
|---|---|
| `objective` | The reviewable outcome, in the coordinator's words |
| `job_id` | The one authorized job that executes it; the job is authoritative |
| `authorization` | A snapshot of the sealed auth id, statement, repo, base revision, scope, budget and human gates; never a new grant |
| `limits` | The same budget and gates in a compact form for reporting |
| `phase` | `authorized`, `ready_to_advance`, `running`, `job_complete`, `ready_for_review` or `blocked` |
| `job_accepted` / `job_acceptance_reasons` | The queue/job completion check: verified attempts, gitleaks, suite and an accepted integration wave, not merely task state strings |
| `active_workers` | Task ids, job states and attempt ids currently in flight |
| `dependencies` | Each task's declared dependencies |
| `last_completed_action` | The latest durable job event |
| `next_action` | What the coordinator should do next, such as `dispatch:T2`, `wait_for_worker` or `prepare_campaign_closeout` |
| `wait_condition` | Why the next action is waiting |
| `blocker` | The named failure or dependency problem, when blocked |
| `task_counts` | total, accepted, active, ready and failed counts |
| `revision` | Incremented only when the reconciled view changes |

The record is written atomically and mode `0600`. Repeating a reconcile against
unchanged job state returns the same record and does not rewrite it. A restart
therefore adopts the checkpoint, not a remembered plan. A `verified` task is
waiting for integration, not for a worker; a terminal integration failure is a
named blocker.

## What reconcile does and does not do

`campaign.reconcile(state, campaign_id)`:

1. loads the checkpoint and the durable job;
2. refuses a job whose repository or base revision no longer matches the
   campaign's authorization snapshot;
3. derives active, accepted, failed, ready and dependency-blocked tasks;
4. records the reason for the next action and the current wait/blocker;
5. writes only if the derived view changed.

It never dispatches, verifies, integrates, recovers, merges, tags, releases,
deploys or publishes. Those stay in `job_cli.tick`, the queue and the existing
recovery path. That is what prevents the continuity layer from becoming a second
orchestrator or from widening authority to keep a run moving.

## Commands

Create the checkpoint after the human authorization and job exist:

```bash
hermes bundlebees campaign init \
  --campaign-id jackpot-2026-11 \
  --job-id jackpot-2026-11 \
  --objective "Ship the playable Jackpot Syndicate campaign for review" \
  --branch feat/b3-integration \
  --pull-request 8
```

Inspect or resume:

```bash
hermes bundlebees campaign status     --campaign-id jackpot-2026-11
hermes bundlebees campaign reconcile  --campaign-id jackpot-2026-11
hermes bundlebees campaign resume-plan --campaign-id jackpot-2026-11
```

`resume-plan` sets `should_continue: true` only for `authorized`,
`ready_to_advance` or `running`. At `job_complete` the job evidence is accepted but campaign
product evidence and audit triage are not verified, so it returns
`requires_campaign_closeout: true` and does not claim ready-for-review. At
`ready_for_review` it returns `requires_human: true`; at `blocked` it returns the
named blocker and does not retry.

The queue card's `ready_for_review` status is a **job-level** status emitted when
the job document is accepted. It is not campaign review: the campaign checkpoint
stays at `job_complete` until the closeout report and its evidence are reviewed.

## Scheduler

`factory_driver --interval` is the implemented resident loop. It can reconcile a
campaign on every pass, but it advances work through the feature queue: the
authorized job must already have a queue item. A campaign checkpoint never
enqueues, dispatches or claims work on its own.

```bash
python -m bundlebees_plugin.factory_driver \
  --state "$BUNDLEBEES_STATE" \
  --queue "$BUNDLEBEES_QUEUE" \
  --campaign-id jackpot-2026-11 \
  --launcher-url "$BUNDLEBEES_LAUNCHER_URL" \
  --repo "$REPO_WORKTREE" \
  --gitleaks "$GITLEAKS" \
  --interval 5
```

A prompt instruction is not a scheduler. The process must be started by the
host. The supported shape is a user service with restart-on-failure (values are
placeholders; this repository does not install it):

Name the template `bundlebees-campaign@.service`; `%i` is the campaign id.
`bundlebees-campaign@jackpot-2026-11.service` starts one instance.

```ini
[Unit]
Description=bundlebees campaign driver (%i)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/rizzstack/bundlebees
EnvironmentFile=%h/.config/bundlebees/driver.env
ExecStart=%h/rizzstack/.venv/bin/python -m bundlebees_plugin.factory_driver \
  --state %h/.hermes/plugin-data/bundlebees \
  --queue %h/.hermes/plugin-data/bundlebees/queue.json \
  --campaign-id %i \
  --repo %h/rizzstack/work \
  --gitleaks %h/.local/bin/gitleaks \
  --launcher-url ${BUNDLEBEES_LAUNCHER_URL} \
  --interval 5
Restart=on-failure
RestartSec=10
Nice=10

[Install]
WantedBy=default.target
```

`BUNDLEBEES_LAUNCHER_TOKEN` and `BUNDLEBEES_LAUNCHER_URL` belong in the
mode-`0600` environment file. Do not launch this unit without `--repo`,
`--gitleaks` and a launcher URL for a dispatch-capable campaign: without them
the driver silently falls back to `mechanical_tick`, claims queue cards and
cannot dispatch or verify. That fallback is useful only for observation and is
not a work-conserving scheduler.

The unit and its environment file are host changes. Installing, enabling or
restarting them needs the approver's yes and is **not** done by this change. If
the coordinator host is down, the campaign is idle; on restart the process
reloads the checkpoint and reconciles from the durable job (including its
accepted-wave recovery) and the launcher/worker state. It does not independently
query Git or a GitHub PR view; those remain a read-only observer to add before
claiming full Git/PR reconciliation. It does not redispatch work just because the
process started again.

The existing `job queue run --once` and `job tick` commands remain single
passes. They are safe from cron, but cron is a host choice; no cron entry is
created here.

## How a restart avoids duplicate work

The campaign layer is observational. Duplicate protection stays where it already
lives: the job's dispatch intent, idempotency key, signed lease and the queue's
`O_EXCL` claim. A restarted driver:

- loads the same queue/campaign/job state;
- reconciles an accepted wave and restores its durable ref;
- adopts a terminal attempt instead of dispatching another;
- records a campaign revision only if the observed view changed.

A campaign restart that produces a second dispatch or a second accepted wave is a
failure of the job/queue seam, not something the checkpoint should hide.

## Deterministic restart trial

`tests/test_campaign_continuity.py` runs a bounded deterministic trial:

- a two-task job is authorized and checkpointed;
- a real `JsonQueue` + `QueueStore` and the production `FactoryDriver` are used,
  with a deterministic tick that advances the job through verification and
  integration;
- the checkpoint is reconciled, the file is read fresh, and the driver is
  reconstructed as a restart;
- the test asserts one queue claim, no duplicate dispatch, the queue card reaches
  its job-level `ready_for_review` status, and the campaign reaches
  `job_complete` only when the job's own acceptance evidence passes;
- task rows marked `accepted` without verification/integration evidence are
  `blocked` at `repair_job_evidence`;
- a `ready_for_review` campaign phase is reserved for a future verified closeout
  gate and is never set solely by task acceptance.

`tests/test_factory_cli_e2e.py` adds the CI-only production-path trial: an
authorized job in the real queue, the real `job_cli.tick`, and a fresh
`FactoryDriver` per restart pass. It uses a simulated worker patch, not a real
worker host.

This is **not** a live-host trial. It does not prove that a real systemd unit
restarts, that the launcher survives a reboot, or that a real model continues
without a nudge. Those remain `NOT RUN` until an authorized host experiment.

## First Jackpot result

The Jackpot build did **not** use this campaign layer or the factory driver. It
was a chat-coordinated build with background watchers and subagent lanes. That is
why the pauses in the transcript were mostly "the turn ended and no scheduler
woke it", not "the job was stuck". The layer above is the path for the next
authorized campaign; it does not retroactively claim the Jackpot pauses were
handled automatically.
