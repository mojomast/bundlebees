# Disposable restart trial harness

`contrib.trial.trial_harness` coordinates the host-side half of a coordinator
restart trial. It exists to answer one question with durable evidence:

> After the coordinator is stopped mid-flight and started again, did the
> campaign adopt the completed worker attempt and finish **exactly once** —
> one dispatch, one verification, one accepted wave — while campaign closeout
> and the owner's acceptance stayed pending?

It is not a scheduler, a second orchestrator or an approval system. It is
scoped to a **single-task job**: one task, one attempt, one verification and one
accepted wave are what "exactly once" means here.

## What it does and does not do

It does:

1. start the real coordinator as a direct child process (`Popen` of an explicit
   argv), recording that PID;
2. wait until the durable job or the launcher records show the trial is ready;
3. stop **only that PID** (SIGTERM, bounded by the deadline, then SIGKILL),
   confirming `returncode is not None` before continuing;
4. wait for the worker to complete while the coordinator is down;
5. start the coordinator again, adopting the same job and attempt;
6. read the job, intent, verification and wave records and refuse any duplicate;
7. reconcile the campaign and assert `job_complete` with
   `requires_campaign_closeout: true` and `requires_human: false`;
8. read (never write) the RETURN manifest and assert the receiver decision is
   still `pending`.

It never:

- starts, stops or restarts the launcher or a worker;
- dispatches work itself (the coordinator run 1 does that);
- uses `shell=True`, a pipeline, `tee`, `pkill`, `killpg` or a process group;
- writes to the launcher (`create`/`cancel`/`review` are refused by a read-only
  launcher view);
- merges, tags, releases, deploys or publishes.

A trial also refuses to reuse a prior trial's ready/report artifacts, refuses an
arbitrary `*.py` script in place of the Bundlebees factory driver, and on any
failed wait stops the exact coordinator PID it recorded. If the job already
records one attempt, the worker probe must report that same `attempt_id`.

## Two timeouts, two owners

| Timeout | Owner | What it stops |
|---|---|---|
| The trial **overall deadline** | this harness | the coordinator driver it started, and its own waits |
| The **launcher per-attempt worker timeout** | the launcher | one worker attempt |

The deadline starts once, before run 1, and never resets. Every wait and every
start decision uses the remaining time; when it expires, no further coordinator
run is started and the harness fails closed. It never cancels a worker. A worker
that outlives the deadline is recorded as a named limitation
(`worker_completion_unproven`), not inferred and not killed.

Every report repeats this table under `timeouts`, so a reader cannot mistake the
harness deadline for a worker cancellation.

## Three Python contexts

| Context | Interpreter | Used for |
|---|---|---|
| `coordinator` | the project venv | `python -m bundlebees_plugin.factory_driver` |
| `sandbox_suite` | the system python (`/usr/bin/python3`) | the smoke suite, inside the verifier's sandbox |
| `worker` | `fake` (local rehearsal) or `real` (launcher) | the worker attempt; the harness never runs a real one |

The system python on this host has **no pytest**, and the project venv cannot be
executed inside its firejail, so the suite is an explicit direct-test-runner
command, never `python -m pytest` and never the venv:

```text
/usr/bin/python3 -c "import sys; sys.path.insert(0, '.'); import tests.test_smoke as t; t.test_answer_is_42()"
```

The suite command is a required configuration value (`TrialPlan.suite_command`,
CLI `--suite-command`); the harness refuses a coordinator argv that names
anything else. `verify_local` appends the named test target as `argv[1]`, which
the `-c` program ignores.

## Pinned checkout

The plan carries an explicit `source_root`. The harness:

- refuses a root without `bundlebees_plugin/__init__.py`;
- runs a child probe (`import bundlebees_plugin; print(__file__)`) under the
  coordinator interpreter and refuses a path outside the root
  (`checkout_mismatch`);
- prepends the root as the **only** `PYTHONPATH` entry for children (an ambient
  entry can shadow the checkout) and sets `PYTHONNOUSERSITE`/`PYTHONDONTWRITEBYTECODE`.

## Evidence semantics

Counts come from execution records, never file counts or prose:

| Claim | Record |
|---|---|
| one dispatch | `jobs/<job>/intents/<task>-<n>.json` with an attempt id, plus `tasks[*].attempts` |
| one verification | the single `->verified` job event and `verify-results/<attempt>.json` |
| one accepted wave | `integration.accepted_waves` (one entry) and the create-only `waves/<wave_id>.json` |
| worker running at the stop | a launcher observation in a running state, stamped with the harness's monotonic clock, no older than the staleness limit, with no terminal observation before the stop |
| worker completed during downtime | a successful terminal observation after the driver exit and before run 2 started |

A duplicate dispatch, a second attempt, a second `->verified` transition, a
second verification record or a second accepted wave is a named refusal
(`duplicate_dispatch`, `duplicate_attempt`, `duplicate_verification`,
`duplicate_accepted_wave`). A missing record is `missing_*`; damaged evidence is
`*_corrupt`/`*_unreadable`, never "absent". If the worker proof is unavailable the
harness raises `TrialIncomplete` with the named limitation — it never infers.

## Readiness without a foreign writer

`--ready-on dispatch` (a task has a recorded attempt) and
`--ready-on worker-running` (a task row is `running`/`verifying`) read the durable
job document. `--restart-on job-complete` reads `campaign.resume_plan` and
requires the phase `job_complete`. `--ready-on file` / `--restart-on file` remain
for tests and for a driver that writes its own marker.

## Approval binding

The harness calls `bundlebees_plugin.approval.verify_receipt` then
`consume_receipt` when that lane is present; it implements no approval of its
own. When the module is absent, a live trial refuses with
`approval_unavailable`. A receipt whose `code_rev` differs from the checkout's
git HEAD (or `--code-rev`) is refused without widening or consuming it. Receipts
are consumed before run 1: one receipt admits one trial.

## Commands

```bash
# Offline rehearsal: simulated worker and driver, no launcher, no worker.
python -m contrib.trial.trial_harness rehearse \
  --source-root "$PWD" --deadline-seconds 60

# Create the inert one-time owner approval request from the plan. The owner
# then reviews the card in the Bundlebees pane; no terminal authorization.
python -m contrib.trial.trial_harness prepare \
  --state "$BUNDLEBEES_STATE" --plan trial/restart-trial-20261115.json \
  --source-root "$PWD" --code-rev "$MERGED_BUNDLEBEES_REV"

# A live trial (only after the owner approved it and the job is queued).
BUNDLEBEES_TRIAL_LIVE=1 python -m contrib.trial.trial_harness run \
  --state "$BUNDLEBEES_STATE" --job job_x --campaign camp_x \
  --source-root "$PWD" --deadline-seconds 900 \
  --ready-on dispatch --restart-on job-complete \
  --suite-python /usr/bin/python3 \
  --driver-argv '["/path/.venv/bin/python","-m","bundlebees_plugin.factory_driver","--state","...","--interval","2","--suite-command","/usr/bin/python3","-c","..."]' \
  --restart-argv '["/path/.venv/bin/python","-m","bundlebees_plugin.factory_driver","--state","...","--once"]' \
  --suite-command /usr/bin/python3 -c "import sys; sys.path.insert(0, '.'); import tests.test_smoke as t; t.test_answer_is_42()" \
  --receipt receipt.json --report trial-report.json
```

## Limits

- The rehearsal uses a simulated worker and verification/integration records; it
  proves the harness's process, deadline, proof and record paths, not that a real
  worker or a real systemd unit behaves the same way.
- A live trial needs a launcher URL/token for attempt observations; without one
  the worker proof fails closed.
- The harness reads the bundlebees state and the RETURN manifest. It does not
  query Git or a GitHub PR view; campaign closeout and owner acceptance are
  explicitly left pending.
