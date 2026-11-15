# Owner approval card

The Bundlebees Desktop pane exposes an **Approvals** tab and a server-rendered
one-time card at:

```
/api/plugins/bundlebees/approval/<request_id>
```

The card is deliberately in the authenticated Hermes Dashboard plugin surface.
It follows Orbit's preview/approve pattern: the owner sees an exact, inert
preview; the decision posts the preview digest that was displayed; the trusted
application, not the agent, records a signed receipt; and the receipt is
one-use. The page contains no token, host, path, key or launcher credential.

## What the owner sees

The card names the job and statement, repository and base revision, authorized
paths, the exact task list and leases, worker project/profile, Bundlebees code
revision, budgets, coordinator stop/restart plan and expiry. It warns that
approval starts only this trial and does not approve a merge, release,
deployment, publication or final product acceptance. The only actions are
**Approve once** and **Decline**.

## Trust and refusal rules

- The route inherits the dashboard's owner authentication. The JSON decision
  route also requires the exact preview digest and derives the per-request
  owner capability inside the application from
  `BUNDLEBEES_APPROVAL_SECRET`; neither value is sent to the browser.
- A caller that can only run Python against the state directory is refused with
  `approval_owner_required`; it cannot mint a receipt.
- A changed digest, changed task set, changed worker/code revision, expired
  request, cross-job request, replayed receipt or conflicting decision is
  refused by `bundlebees_plugin.approval` before any authorization is written.
- Repeating the same decision and the same consumer is idempotent. A second
  different consumer is a replay and is refused.
- Approving the trial does not clear the existing `merge`, `release`, `deploy`
  or `publish` gate records. Those remain separate human decisions.
- The coordinator receipt path consumes one receipt into one sealed
  `authority: owner-approval` authorization; it never grants campaign
  acceptance.

## Test seam

The dashboard tests inject a fake approval module for route-level checks. The
real protocol is tested in `tests/test_approval.py`; the browser rehearsal uses
the actual `approval.create_request` and `approval.record_decision` in an
isolated temporary state directory with a test key.

## Installation requirement

The owner card is unavailable until the Bundlebees plugin installed in the
active Hermes profile includes `bundlebees_plugin/approval.py` and the
`/approvals` routes. The host must also provide a 32+ byte
`BUNDLEBEES_APPROVAL_SECRET` through the Hermes secret scope. Installing the
plugin or setting the secret is a separate host change and is not performed by
this code.
