# Campaign heartbeat lanes

The campaign heartbeat started as one 30-minute agent beat that did everything: fold owner decisions, dispatch, verify, merge, deploy and record. That mixes a latency-sensitive duty (dispatch) with slow, evidence-heavy duties (verify, merge, deploy proofs). Two failures follow from the mix:

- a queued slice waits for the whole beat, so dispatch latency is bounded by how long verify, merge and deploy take (25-70 minutes in practice);
- an owner answer recorded while a beat is running is invisible to that beat, which read the plan at start, so the queue can idle while the record says the gate is answered.

The fix is two lanes over one queue, serialized by the existing coordinator claim. The lanes are a scheduling contract, not a second orchestrator: neither lane may invent work, and the claim file is the only lock.

## Serialization

Both lanes use `~/.hermes/cron/coordinator-claim` (epoch seconds).

- first action: write the claim; renew every 5 minutes while working;
- a claim younger than 20 minutes belongs to its owner: reply `[SILENT]` and exit without touching it;
- remove the claim before exit. A fast-lane run takes seconds to minutes; a slow-lane run takes 25-70 minutes.

## Fast lane (dispatch)

Cadence: every 5 minutes. Duties, in order:

0. Fold owner decisions: a newer `owner-decisions-*.json` sidecar or `## Owner decision` record section than `last_coordinator_touch` is folded into `plan.json` first (back up, move the answered gates off `blocked-on-owner`, record `state.owner_decisions_*`, set `next_action`). A redacted value is a missing answer, not a closed gate.
1. Dispatch queued slices with nothing blocking, after re-reading the plan and checking leases and attempts, so a slow-lane dispatch is never duplicated.
2. Append one short record entry only if it acted; otherwise write nothing.

It never verifies attempts, opens or merges PRs, deploys or restarts anything. Work that needs those goes into the record as one line for the slow lane.

## Slow lane (evidence)

Cadence: every 30 minutes. Reconciles terminal attempts, drives the queue as a fallback, runs the merge stream (green at the exact head plus independent approval), proves deployments, and writes the beat record. It re-checks owner decisions but does not own them.

## Lanes and caps

Complex, high-risk, cross-repo or failed-twice slices run on `k3.1-pro` (at most 6 running at once); docs, tests-only and low-risk slices run on the Nous lane (`nous:deepseek-v4.1-flash-1017`, then `nous:hy4-flash`); everything else uses `go:deepseek-v4.1-flash`. The launcher cap of 12 is shared with the rest of the stack; both lanes respect it and the run-level cap.

## Failure modes

- Duplicate dispatch: prevented by re-reading `plan.json` and checking leases before every dispatch. The claim does not replace that check, because the fast lane can start the moment the slow lane releases.
- Silent fires: when a claim is live both lanes return `[SILENT]`. That is healthy, not an incident.
- A missed decision: the fast lane folds it on its next 5-minute tick; the slow lane's duty 0 is the backstop.

## Wiring

Two Hermes cron jobs with the toolsets `terminal`, `files` and `bundlebees`, both `deliver: local`: fast every 5 minutes, slow every 30 minutes. Job ids are host-local and not part of this contract; the prompt duties above are. The mechanical reconcile sweep in `bundlebees_plugin.beat` remains a separate backstop and is not replaced by either lane.
