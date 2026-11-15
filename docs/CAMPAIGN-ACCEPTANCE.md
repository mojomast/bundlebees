# Campaign acceptance

bundlebees verifies **tasks**. A campaign — several jobs, UI work, assets, browser
journeys and performance checks — needs a separate closeout. This note records
that boundary for the first Jackpot Syndicate campaign and points at the
workflow-owned report contract.

## Three levels, three claims

| Level | What bundlebees can prove | What it cannot prove |
|---|---|---|
| **Task** | The leased patch applies at the pinned revision, the approved tests run, the scope is intact, and the verifier records GREEN/REFACTOR/integration | That the feature is usable, visually correct or performant |
| **Project** | Every task in the project plan is `accepted`; a separate job review gate and RETURN determine whether the integrated job is reviewable | That every product acceptance criterion has been run; the pilot todo's `pilot/todo/acceptance.py` is an application test, not a runner |
| **Campaign** | Nothing by itself; the coordinator/reviewer assembles the closeout | A human receiver's decision |

`project_completion()` in `bundlebees_plugin/project.py` reads task states. It is
deliberately not an acceptance decision. The project plan's top-level
`acceptance` list tells the planner what the product should prove, and each
feature's `acceptance.tests` tells the worker which focused tests prove its
slice. Both are inputs to a review, not a substitute for one.

## Application acceptance

Use the pilot as the pattern, not as a finished product:

- `pilot/todo/acceptance.py` starts the **real** standard-library HTTP server on
  an ephemeral loopback port, exercises create/list/search/edit/complete,
  validation, 404s, the served UI and persistence, then shuts the server down.
- `pilot/todo/tests/test_ui.py` checks the served page without a browser; that
  proves the document and its references, not that the page is visible or usable.
- A browser journey, screenshot or recording belongs in the campaign closeout as
  `visual` evidence with the viewport and final head. A DOM assertion is not a
  visual acceptance.

Before dispatch, make each product criterion name its evidence, owner and stage.
The project plan's top-level `acceptance` entries accept optional `kind`,
`stage`, `owner` and `evidence` fields; `project.plan()` preserves them and
refuses an unknown kind/stage. A useful split is:

| Stage | Typical evidence |
|---|---|
| `contract` | the frozen criterion and its selector |
| `unit` | focused test command and result |
| `integration` | combined-tree command and result |
| `application` | real server/browser journey, persistence and cross-feature checks |
| `final` | full suite, performance, visual captures and asset verification on the final head |

Start with one **vertical slice** that exercises the real user journey and the
main integration risk before expanding the task graph. The Jackpot build learned
this late: the board and meta screens were functionally reachable while the
authored art was not yet visible.

For a campaign, run the application acceptance on the **combined, final** tree,
after the full package suite. Record the exact command, counts, exit code and
head SHA. A worker's `result.observed` or `state: succeeded` is not evidence.

## Closeout

The workflow repository owns `rizzstack.campaign-report/1`:

- schema: `schemas/campaign-report.schema.json`
- validator: `tools/campaign_report.py validate <report.json>`
- chapter: `docs/30-campaign-closeout.md`

The report keeps `implemented`, `integrated` and `accepted` counts separate,
requires evidence for a `PASS`, a reason for `NOT RUN`/`BLOCKED`/`UNVERIFIED`,
and refuses an agent-filled `receiver_decision`. bundlebees' RETURN remains the
per-job evidence; the campaign report is the cross-job review record. Neither
authorizes a merge, tag, release, deploy or publish.

## First-use evidence

At the Jackpot Syndicate closeout:

- **27/27 task rows implemented**; **226 tests passing** at the final head;
- **10 draft PRs** (#2–#11), no merge;
- Chromium and Firefox journey suites **10/10**, golden replay on Chromium,
  Firefox and WebKit **1/1**;
- **67/71 assets locked**, four placeholders retained and named;
- browser audit and coverage target **not run**;
- bundle **1.90 MB gzip** after reverting a change that broke the
  zero-runtime-network test.

The task graph was green before the second audit found save, scoring and UI
defects. That is the reason this closeout exists: **implementation is not
acceptance**, and a green task graph does not make a product ready.

## Checklist

- [ ] Final head is pushed; local and remote agree; working tree is clean.
- [ ] Full suite and application acceptance ran on that exact head; counts recorded.
- [ ] Every product AC has evidence or a named reason it did not run.
- [ ] Visual evidence names a viewport and a head SHA.
- [ ] Audit findings are fixed, rejected with a reason, or deferred with a reason.
- [ ] Placeholders and known limits are visible to the receiver.
- [ ] The campaign report validates and `receiver_decision` is `pending`.
- [ ] No merge, tag, release, deploy or publish was performed.
