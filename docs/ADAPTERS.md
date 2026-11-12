# Worker adapters

bundlebees keeps its acceptance and authorization contracts independent of any
one agent runtime. Hermes makes the dispatch; a worker runtime executes the
brief; bundlebees decides whether the result is trustworthy.

## Three layers

| Layer | Owns | Does not own |
|---|---|---|
| **Workflow (the protocol)** | The brief: objective, project, branch, `assigned_files`, tests, acceptance, and the handoff to a human-run verify. | Execution details, credentials, or how a runtime starts an agent. |
| **bundlebees (execution)** | Brief validation, the authorization allow-list, the immutable file lease, dispatching to an isolated worker, revision checks, secret scan and sandboxed test verification, wave integration, and outcome metrics. | The runtime's internals or the shape of its replies. |
| **Orbit / launcher (coordination interface)** | Reaching a local-worker adapter or a launcher endpoint and translating its replies into coordinator view models. | Leases, scope, acceptance, or who may push. |

The adapter layer is the seam between the middle and bottom rows. Changing the
runtime must not change the middle row.

## The worker-backend interface

Any agent can be a bundlebees worker. The core (brief contract, signed lease,
revision checks, sandboxed verify, wave integration, metrics) never imports or
names a specific agent, model, or launcher; `tests/test_adapters.py` fails if
one of those core modules does. DeepSeek bees through the Orbit/Hermes
launcher are simply the default adapter.

**Input: the brief, frozen as a lease.** The coordinator validates the brief
(`objective`, full 40/64-hex `source_rev`, non-empty repository-relative
`assigned_files`, `acceptance`; optional `branch`, `tests`, `run_id`, `wave`,
`retry_of`) and hands it to `create`. The same fields are written to the
coordinator's read-only, HMAC-signed lease at dispatch. The worker should
start from exactly `source_rev`, change only `assigned_files`, and meet
`acceptance`.

**Output: a patch plus evidence.** `result` returns whatever the runtime
produced; `normalize_result` reduces it to the result contract below: a
unified diff against `source_rev`, the base the worker declares, usage, and
the checks the worker says it ran. Everything else is dropped.

The required minimum is two calls:

```python
class WorkerBackend(Protocol):
    name: str
    def create(self, brief: dict, *, model: str,
               kanban_task_id: str | None = None) -> dict: ...   # -> {"attempt_id", "state"}
    def result(self, attempt_id: str) -> dict: ...               # raw output, normalized by the coordinator
```

`WorkerAdapter` extends it with optional capabilities the Hermes tools use
when present: `health`, `projects`, `attempts`, `get`, `events`, `cancel`,
`review`. A backend that lacks one gets a clear `unsupported_operation` error
from that tool op, never a fabricated success.

`bundlebees_plugin/adapters.py` exposes:

- `ADAPTERS`, the factory table keyed by `orbit-loopback`, `launcher`,
  `opencode`, and `codex`.
- `adapter_from_config(config, token_provider=...)`, which reads
  `config["adapter"]` (default `orbit-loopback`). The aliases `direct`,
  `http`, and `https` select `launcher`.
- `available_adapters()`, a stable list of
  `{"name", "default", "implemented"}` records.
- `UnsupportedAdapter`, whose `code` is always `unsupported_adapter`.
- `normalize_result(raw)`, the result-contract enforcement point.

The `orbit-loopback` and `launcher` entries delegate to
`client.client_from_config`, so existing behaviour is unchanged. `opencode`
and `codex` are documented stubs: every operation raises
`UnsupportedAdapter` and points here.

## The result contract

Worker replies are untrusted data. Every adapter result is reduced to exactly:

```json
{
  "attempt_id": "string or null",
  "patch": "unified diff string or null",
  "base_rev": "declared base revision or null",
  "usage": {"tokens": "integer or null", "cost_usd": "number or null"},
  "evidence": {"checks": [{"name": "string", "command": "string", "passed": true}]},
  "state": "string or null",
  "untrusted": true
}
```

Rules:

- Anything else the worker sent is dropped. That includes `assigned_files`,
  `lease`, `brief`, `scope`, `worker_claims`, tokens, hosts, and private paths.
- `base_rev` is the base the worker **declares** (full hex only). It is not
  proof; the coordinator requires it to equal the lease's `source_rev`, then
  checks that the patch applies to that exact commit and that the feature
  branch descends from it.
- `evidence.checks` is what the worker **claims** it ran (scrubbed, at most 50
  entries). It is informational: the coordinator regenerates trusted evidence
  by running the tests itself in a disposable sandbox.
- Runtime-specific shapes (for example the Orbit launcher's
  `source.repo_rev`, `patch.diff`, and `observed.usage`) are mapped inside
  `normalize_result` and nowhere else.
- `usage` is advisory accounting. Missing or malformed values become `null`.
- `state` is display text. It never authorizes an action on its own.
- `untrusted` is always `true`.
- `patch` is preserved byte-for-byte because it is handed to isolated
  verification, not interpreted as prose. It is applied only after lease and
  path checks, and only by the user-run verify path.

Use `normalize_result` in every adapter before returning a result.

## What stays in the trusted coordinator

No adapter, however implemented, may take over these:

- **Brief validation.** Objective, project, branch, and file lease are checked
  before dispatch.
- **Authorization.** The project allow-list and file lease come from plugin
  configuration and the brief, never from a worker reply.
- **Isolation.** The worker runs in a disposable, isolated environment. A
  runtime cannot ask bundlebees to run on the coordinator host.
- **Revision checks.** The coordinator compares the declared base against the
  expected revision and refuses a mismatched patch.
- **Verification.** Only the user-run `hermes bundlebees verify` applies a
  patch, enforces the signed lease, checks the revision, runs a secret scan,
  runs the named tests in a disposable no-network sandbox, and pushes a
  feature branch. `main` and `master` are refused.
- **Integration.** A wave counts as accepted only after
  `hermes bundlebees integrate` passes the full suite on the combined branch.
- **Credentials.** Adapters never receive the launcher token or any
  coordinator secret. Credentials stay in Hermes's secret scope.
- **Change control.** No adapter pushes, merges, tags, force-pushes, releases,
  or edits a catalog.
- **Envelopes.** Adapter error text is scrubbed before it reaches a log or a
  model-visible response.

If an adapter needs a capability that would weaken any of these, that
capability belongs in a new coordinator contract, not in the adapter.

## Implementing OpenCode or Codex

Both stubs follow the same steps. Start from the contract, not from the
runtime's API.

1. **Create a branch factory.** Accept the coordinator's resolved
   `source_rev`, create a disposable checkout, and create plus check out a
   feature branch. Refuse `main`, `master`, and any name the coordinator did
   not authorize. Never check out on the coordinator's own worktree.
2. **Start the runtime.** Pass the validated brief and the model id as a
   non-interactive run. Give the runtime only the checkout and the brief. Do
   not pass a launcher token, a host address, an SSH alias, or any coordinator
   credential.
3. **Confine writes.** Treat `assigned_files` as the writable scope. Detect any
   change outside it before returning a diff and fail closed with an
   `UnsupportedAdapter`-style envelope or a mapped client error. Do not ask the
   coordinator to accept the extra files.
4. **Collect the diff.** Run `git diff <source_rev>` in the checkout and return
   the unified diff as `patch`. If the runtime produced no changes, return
   `patch: null`.
5. **Report the base and usage.** Return `base_rev` set to the `source_rev` the
   run was started from, plus `usage.tokens` and `usage.cost_usd` when the
   runtime reports them. Leave missing values as `None`.
6. **Normalize.** Pass the runtime's reply through `normalize_result`. Never
   forward `lease`, `assigned_files`, `scope`, `worker_claims`, or credentials.
7. **Map the other operations.** `health` reports runtime availability without
   secrets or endpoints. `projects` lists only coordinator-authorized projects.
   `attempts`, `get`, `events`, `cancel`, and `review` return coordinator view
   models with scrubbed text. `events` respects the cursor and keeps
   `untrusted: true`.
8. **Fail closed.** Unknown ids, missing results, rejected projects, and
   unsupported operations raise a typed error with a stable code and a
   remediation. Do not fall back to another runtime.
9. **Never push.** The adapter ends at a diff and a declared base. Commit,
   push, and PR creation remain user-run coordinator actions.
10. **Test offline.** Unit-test with a fake runtime, a temporary git repository,
    and a fake token provider. No network, no Docker, no SSH, no real tokens.
11. **Register.** Add the class beside `OpenCodeAdapter` or `CodexAdapter` in
    `bundlebees_plugin/adapters.py` and update that adapter's entry in
    `ADAPTERS`. Keep `available_adapters()` honest: `implemented` becomes
    `true` only when every method works.

Until those steps are complete, selecting `opencode` or `codex` raises
`unsupported_adapter`. That is deliberate: a stub must never silently run a
different backend or report a fabricated result.
