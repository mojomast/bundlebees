# Dispatch attribution identity (E9)

Pre-dispatch worker attribution records **which route** a dispatch took *before* the POST:
endpoint, worker, ledger chain, job, project, contract and revision. The launch gate refuses a
canary dispatch it cannot attribute, and refuses to trust an attempt that no signed intent claims.

## The problem this convention answers

The launcher's public health endpoint (`GET /v1/health`) advertises `version`, `max_concurrent`,
`active`, `queued`, `paused` and the model list. It advertises **no worker identity and no ledger
chain**. The coordinator cannot invent those facts, and it must not treat "the only worker I know
about" as proof of identity.

So the operator supplies them, once, as plugin settings:

| Setting | Meaning | This deployment |
|---|---|---|
| `attribution_canary_projects` | projects where attribution is enforced | `rizzstack-m2-canary` |
| `attribution_worker_id` | the worker host that executes canary work | `brrrrrrrr` |
| `attribution_ledger_chain` | the worker's evidence chain identity | `brrrrrrrr:attempt-journal` |

## What the convention means, precisely

- `attribution_worker_id` is the **launcher host alias** — the same name used for the admin
  channel and the deploy target. It identifies the machine, not a process.
- `attribution_ledger_chain` names the **attempt journal** on that host: the append-only record the
  launcher itself counts active and queued bees from. It is the chain a coordinator can later
  reconcile a bound attempt against.
- Both are **coordinator-recorded operator facts**, not launcher-attested proofs. They are treated
  as configuration, and the audit says so: a `verify_attribution` PASS means "this attempt matched
  the intent recorded for this configured route", not "the launcher cryptographically vouched for
  its identity".
- When the launcher eventually advertises worker/ledger facts in its authenticated health, the
  configured values become a cross-check instead of the only source, and a mismatch between the two
  is a refusal (not a preference for either).

## Fail-closed behaviour that depends on this

- canary dispatch with no worker/ledger configured → `attribution_incomplete`, **nothing is POSTed**
- an attempt with no matching intent → `adopted_attempt`
- two intents bound to one attempt → `ambiguous_route`
- endpoint, worker, chain, job or project disagreeing with the intent → `attribution_mismatch`,
  naming the fields

## Known gap, stated rather than hidden

`settings_from_ctx()` only returns keys declared in `settings.DEFAULTS`. A key declared solely in
`plugin.yaml` is never delivered to the gate — which is exactly how this gate spent its first hour
inert while its unit tests passed. Any new attribution key must be added to **both** `DEFAULTS`
and `plugin.yaml`, and the settings test asserts delivery rather than injection.
