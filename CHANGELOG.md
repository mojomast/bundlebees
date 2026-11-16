# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

Verifier and contract hardening from an external review of the workflow. The
unit of progress is an accepted artifact with evidence, and every guarantee
below lives in the coordinator, independent of which agent produced the work.
Built as six file-leased DeepSeek bee slices (two waves) and integrated in
one pull request.

### Added

- **Coordinator action queue:** a durable per-item coordinator action queue over the existing state dir with atomic per-item claims, monotonically increasing durable fencing values (`fence`/`fence_seq`), precise stale refusals, idempotent replay by action key, stale-owner adoption, and per-repo serialization only for overlapping merge and deploy leases; an AST guard proves the queue only records, claims and fences and never executes anything.
- **rizzstack doctor:** `hermes bundlebees doctor` reports four-repo revision parity (checkout, origin main, installed package, gateway-loaded plugin, deployed runtime) with explicit per-component states (healthy, stale, unavailable, inaccessible, unknown, incompatible), launcher/gateway contract-incompatibility detection, executable / sandbox / pinned-source / credential-presence checks, `--json` output, and `--check-parity` exit semantics that exit nonzero on any divergence, all strictly read-only.
- **attempts and status CLI:** two read-only subcommands, `hermes bundlebees attempts` (live attempt list as JSON) and `hermes bundlebees status` (in-flight count, bee cap, and launcher health fields), backed by the plugin's existing attempt store. `status` performs exactly one secret-store read: the launcher-token presence probe — `client.token_configured()` calls Hermes's `agent.secret_scope.get_secret(BUNDLEBEES_LAUNCHER_TOKEN)` to fill `health.credential_configured`; it reads presence only, never a value, never emits one, and fails closed. No credential value is required or read for either view.
- **integrated_not_pr_merged detection:** the read-only Git/PR observer now
  classifies a PR whose exact head is already an ancestor of authoritative main
  while the PR metadata still reports open, and the reconciliation report stops
  the wait and bans re-integration instead of executing it.
- **Owner approval card:** the authenticated Bundlebees Desktop pane now lists
  pending one-time job approvals and opens a plain-language card bound to the
  exact job, repository/base revision, task leases, worker project/profile,
  code revision, budgets and coordinator stop/restart plan. The owner clicks
  **Approve once** or **Decline**; the decision route derives an in-process
  owner capability, so an agent with filesystem access cannot mint a receipt.
  The receipt authorizes only the named trial run; merge/release/deploy/publish
  gates and final product acceptance remain separate. See
  `docs/OWNER-APPROVAL-CARD.md`.

- **Standing repository grants:** `hermes bundlebees repo grant`, `repo list`
  and `repo revoke`. A human can enroll one TTY-only grant per repository;
  `job create` then derives a narrow, sealed job authorization and records the
  grant id, statement and approval time in its evidence. Grants fail closed on
  missing, expired, revoked, malformed or tampered state, while merges,
  protected pushes, releases, deploys, worker changes and secret access remain
  explicit human gates. See `docs/repo-grants.md`.
- **One-time owner approval:** `bundlebees_plugin/approval.py` and
  `job authorize --approval-receipt`. The owner reviews a plain-language
  preview in the trusted application and clicks once; the application signs an
  HMAC-SHA256 receipt with `BUNDLEBEES_APPROVAL_SECRET` from Hermes
  `agent.secret_scope.get_secret`. The receipt binds the exact job id,
  repository/base revision, statement, scope, normalised task specs, budgets,
  worker project/profile, Bundlebees code revision, planned coordinator
  stop/restart, gates and recovery ceiling, and expires (900 s default). A
  non-TTY authorization accepts it only after signature, expiry,
  one-time-consumption and exact-binding checks; the sealed authorization
  records `authority: owner-approval` and the receipt identity/digest. A
  decision without the per-request owner capability fails closed with
  `approval_owner_required`. The bridge authorises one trial run only: no
  acceptance, merge, release or deploy. See `docs/job-approval.md`.
- **ACDD contracts and TDD evidence** ([docs/ACDD.md](docs/ACDD.md), the
  protocol from workflow chapter 16). `bundlebees_plugin/contract.py` validates
  `acdd/1` contracts (the bundled schema plus the eight code checks), hashes
  them canonically and freezes them. `bundlebees_plugin/tdd.py` classifies test
  runs per case, writes HMAC-signed, append-only `tdd-evidence/1` records and
  checks RED, GREEN and REFACTOR. `bundlebees_dispatch` enforces a contract
  in `brief.contract` and returns an `acdd` block. Briefs without one run in
  legacy mode ("no ACDD contract; TDD evidence not required"). The new
  `require_contract` setting makes a contract mandatory.
- **CLI:** `contract validate|freeze`, `tdd red|refactor|review|report`. With a contract,
  `verify` refuses edits to approved tests, requires a valid RED and records a
  signed GREEN. `integrate` requires GREEN and records an `integration` record.
  Sandboxed runs now return per-case results (`cases`).
- The combined tool schema grows from 573 to 585 o200k (dispatch description).
- Built test-first: the approved tests were committed before any
  implementation, and DeepSeek bees implemented `contract.py`/`tools.py` and
  `tdd.py`/`sandbox.py` against them. The coordinator implemented the CLI
  wiring (also test-first) while bee dispatch was paused.
- `tdd refactor --rev <sha>` reruns the read-only approved tests on a
  descendant of GREEN in the sandbox and records signed REFACTOR evidence
  (`refactor_failed`, `tests_tampered`, `scope_violation`, `stale_revision`);
  `tdd refactor --none --note <text>` records "no refactor needed". Built
  test-first by a DeepSeek bee against coordinator-approved tests.
- `tdd review --reviewer <name> [--role reviewer|human] (--approve|--reject)
  --note <text>` records a signed REVIEW for the GREEN (or required
  REFACTOR) revision; review and manual criteria take its outcome, and a
  rejection exits with `review_failed`. Workers can never record a review.
- `integrate` now also requires a valid REFACTOR record (a real one or an
  explicit `--none` skip) when the contract lists `refactor`.
- The shared evidence schema allows `classification: "failed"`, for review
  records only (a rejected review).
- **Route preview:** `bundlebees_route_preview` previews the model and
  credential route a dispatch would take, starting no attempt (the fifth
  registered tool).
- **Completion events drive the coordinator action queue.** The landing event
  consumer now composes the durable bee-landed terminal signal with the
  coordinator action queue: `consume_completions` subscribes a
  `coordinator-actions` subscriber, classifies each delivered event, and
  translates every terminal completion (`landed`, `succeeded`, `failed`,
  `cancelled`) into create-only action items keyed by the idempotent keys
  `verify:<attempt>` (for a result-carrying landing) and
  `process-completion:<attempt>`, recording one accepted decision per
  `(attempt_id, state)` in a completion ledger. Exactly once is layered:
  per-event acknowledgements, create-only idempotent keys and the ledger give
  zero duplicate actions across duplicate delivery, explicit replay and
  coordinator restart; action claims are fenced, and a stale consumer's
  fencing token is refused (`stale_fence`). Failure modes are bounded: an
  unavailable queue releases the event into its durable inbox record with a
  30 s backoff and `COMPLETION_MAX_ATTEMPTS` claims (an exhausted delivery is
  recoverable through the explicit `requeue_failed`), and malformed or
  non-terminal events are skipped with a counted reason instead of retrying.
  The consumer is additive: the mechanical tick, the Hermes wake path and the
  10-minute heartbeat remain the backstop, so a worker completion advances
  the pipeline without polling and without Hermes being awake. Item shapes
  and the queue seam are documented in `docs/event-consumer.md`. Built
  test-first (`tests/test_event_consumer_actions.py`).

### Fixed

- **`attempts`/`status`: a secret-shaped attempt id keeps its scrubbed form.** The read-only views re-inserted a validated attempt id after scrubbing, so an id that itself looked like a credential (e.g. an `sk-`-prefixed string that passed the attempt-id grammar) was scrubbed to `[redacted]` and then put back raw — a narrow scrub bypass. The re-insertion now requires the id not to look like a secret, so the scrub result always wins for a secret-shaped id; ordinary validated attempt ids are unchanged. Found by an external reviewer and closed with a regression test written first against the unfixed tree.
- `return_report.pending_approvals` treated the mere presence of
  `approvals/<job>.<gate>.json` as an approval. A hand-written `{}` cleared a
  gate, and so did a recorded `refused` verdict. Gate decisions are now loaded
  through `job.load_approval()`/`job.gate_decision()`, which verify the sealed
  envelope, the job id, the gate name and the `granted` verdict. Only a valid
  granted record clears a gate; a forged, copied, refused or unreadable record
  stays pending and is reported as a limitation. Existing terminal approval
  behavior is unchanged.
- **Verify can start pytest on a host whose Hermes interpreter lives under `$HOME`.**
  `firejail --private=<copy>` masks the coordinator home, so an interpreter at
  `~/.hermes/tools/python-*/bin/python3` (or a `uv run --with` shim under
  `~/.cache/uv/builds-v0/`) does not exist inside the jail; the wrapper reported only
  `no suitable <path> executable found` and the run failed with `tests_failed`. Reachability is
  now **probed through the chosen backend** before the worker's command starts (a path heuristic
  is wrong: this host masks `~/.hermes` while a separate mount under `~/.cache/uv` stays
  visible), a hidden interpreter refuses with `sandbox_interpreter_hidden` and a fix, and the new
  `verify_python` setting (or `BUNDLEBEES_VERIFY_PYTHON`) names an interpreter the jail can exec.
  `auto` still prefers bubblewrap, where the coordinator interpreter is already visible.
- Concurrent first dispatches on a fresh state directory could fail with
  `lease_invalid: the lease key is not 32 bytes`: one thread opened the key
  file after another created it but before the bytes were written (seen in
  the workflow cold-start acceptance on CI). The key is now written to a
  private temporary file and published with `os.link`, so readers only ever
  see a complete key. Regression test written first and seen failing.
- **Provision reconcile recovery: one idempotent re-POST on a
  host-confirmed `receipt_unknown`.** When the read-only receipt lookup
  itself answers `receipt_unknown` while the `attempts <= 3` guard holds,
  `provision_bind.advance` re-POSTs the identical request exactly once,
  relying on host idempotency; a resulting `ProvisionUncertain` keeps the
  read-only lookup and never re-POSTs blindly. A definitive re-POST refusal
  is terminal `denied` with the host error recorded
  (`re-POST refused: <code>: <message>`), and later passes never re-POST
  it. A first-POST refusal detail is persisted to the job before the error
  is re-raised, so a restart cannot lose the refusal.
- **Verifier/session reliability: one configured runner over the existing sandbox.**
  `job tick`, queue ticks, the programmatic factory adapter and integration
  run local verification through one configured runner
  (`execution.SandboxRunner`); test receipt identity now covers the full
  execution policy: interpreter executable bytes, dependency manifests
  (`pyvenv.cfg`, `*.dist-info/RECORD`) and tiktoken cache bytes. A bare
  Python name is resolved through `PATH`. Scanner discovery follows the
  declared setting first (`gitleaks_path`, then environment, then `PATH`),
  and a scanner-less `job tick` stops before launcher access. The
  programmatic factory adapter still requires scanning. Repeated `--tests`
  flags are appended. Definite provisioning refusals keep their structured CLI errors.
  The approver's sealed recovery ceiling is exposed through interactive
  `job authorize` CLI flags.
- **Queue runs report typed provisioning refusals instead of crashing.** A tick
  that raises `ProvisionError` or `ProvisionBindError` — on an already-claimed
  card or on a card the pass just admitted — no longer escapes
  `queue.run_once`: the run returns a `blocked` result carrying the typed code
  and detail, the card is set to `blocked` with the refusal (code, message,
  fix) recorded as its evidence, and nothing else is admitted behind the
  refusal, the same short-circuit the job CLI already applied. The client now
  accepts the host's typed refusal code from either the launcher contract's
  `code` field or the provisioning host's 409 body's `error` field (only a
  bare lowercase identifier counts as a code; a human-readable string stays a
  message), so a full profile's `quota_exhausted` denial survives the
  authenticated transport as a typed `ProvisionDenied` instead of degrading
  into a generic conflict — pinned end to end: a host 409
  `{"error": "quota_exhausted"}` travels the real HTTP path and the provisioner
  refuses with that exact code before a journal entry is written.

### Security

- **No automatic weak sandbox.** `verify_isolation=auto` now picks only a
  strong backend (bubblewrap, then firejail). `unshare`, which isolates
  network and PIDs but leaves the coordinator's filesystem readable, is never
  auto-selected: it runs only when `verify_isolation=unshare` is set *and* the
  new, documented-unsafe `verify_allow_weak_unshare` setting (or
  `BUNDLEBEES_ALLOW_WEAK_UNSHARE=1`) is on. With no strong backend, verify and
  integrate fail closed with `sandbox_unavailable` and a message naming
  bubblewrap/firejail. `doctor` reports a refused or overridden unshare.
- **Sandboxed verification.** `hermes bundlebees verify` no longer runs pytest
  in the coordinator worktree. Tests run on a disposable copy (no `.git`) under
  bubblewrap (unprivileged uid, all namespaces unshared, no network, only
  `/usr` and the Python install mounted), firejail (`--net=none --private`) or
  unshare (`--user --net --pid`, weakest), with an environment rebuilt from
  scratch (no coordinator credentials, no SSH agent, empty `HOME`). Only
  evidence returns: backend, exit code, junit counts, output tail, diff stats.
  With no backend, verify and integrate fail closed with `sandbox_unavailable`
  unless the new, clearly labelled `verify_allow_unsafe_local` setting is on.
- **Immutable lease authority.** `bundlebees_dispatch` writes
  `leases/<attempt_id>.json` under plugin data at dispatch: `O_EXCL`, mode
  `0400`, HMAC-SHA256 signed with a `0600` key. Verify trusts only this record
  (`lease_missing`, `lease_tampered`) and ignores, then reports, scope claimed
  in the worker result.
- **Revision guarantees.** Verify requires the worker's declared base to equal
  the brief's `source_rev`, the patch to apply to that exact commit (checked in
  a temporary index), and the feature branch to descend from it
  (`base_undeclared`, `base_mismatch`, `source_rev_missing`,
  `patch_not_at_source_rev`, `branch_diverged`).

### Added

- **Strict brief contract.** `brief.schema.json` requires `objective`, a full
  40/64-hex `source_rev`, non-empty repository-relative `assigned_files`, and
  `acceptance` (criteria list, or `{criteria, commands, tests}`). Validated in
  `bundlebees_plugin/brief.py` before any launcher call. Optional `run_id`,
  `wave`, `retry_of`, `tests`.
- **Wave integration gate.** `hermes bundlebees integrate --wave <id>
  --attempts ...` checks every slice's verify record against the combined
  branch, then runs the full suite there in the sandbox and writes
  `waves/<id>.json`. `bundlebees_attempt op=review accepted=true` returns
  `integration_required` until the attempt is in an accepted wave.
- **Outcome metrics.** Append-only, hash-chained `metrics/<run_id>.jsonl`
  (dispatch, verify, integrate, intervention, recovery events) and
  `hermes bundlebees metrics [--run] [--json] [--note]`: first-pass rate,
  accepted changes, tokens and cost per accepted change, human interventions,
  plan-to-accepted time, regressions found, failed-worker recoveries.
- **`hermes bundlebees lease <attempt_id> --brief <file>`** records a lease for
  a brief dispatched outside the tool.
- **Agent-agnostic worker backends.** `bundlebees_plugin/adapters.py`:
  `WorkerBackend` (required: `create`, `result`), `WorkerAdapter` (optional
  capabilities), `normalize_result` (patch, declared base, usage,
  worker-claimed checks; everything else dropped), `orbit-loopback` default,
  `launcher`, and documented `opencode` / `codex` stubs. See
  `docs/ADAPTERS.md`. Core modules never name an agent or launcher (tested).
- Settings `verify_isolation`, `verify_allow_unsafe_local`, `verify_timeout_s`;
  `doctor` reports the sandbox backend.
- **Read-only Git/PR drift observer (workflow-F3).**
  `reconcile.campaign_git_drift` compares a campaign checkpoint's recorded
  base revision, branch and pull request against the world as it actually is —
  real git refs in a worktree and open PR heads reported by `gh` — and returns
  the difference as drift with the exact expected-vs-actual values:
  `branch_moved`, `branch_missing`, `pr_merged`, `pr_closed`, `pr_missing`,
  `pr_head_branch_changed`, `unexpected_pr_head`. Strictly report-only: only
  allowlisted read-only git subcommands (`rev-parse`, `show-ref`,
  `merge-base`, `rev-list`, `cat-file`, `symbolic-ref`) and read-only
  `gh pr view`/`gh pr list` queries ever run; nothing is reset, pushed,
  merged, closed or rewritten, and no checkpoint is modified — drift is
  returned for a caller to act on, never applied. Subprocesses run under
  `child_env()` (no ambient credential is forwarded, and `gh` is never handed
  a token on argv), free text is redacted through `scrub` (a validated commit
  digest prints verbatim, exactly like the coordinator prints digests), and
  the report never carries a URL. An unobservable world raises
  `GitObserverError` with a stable code: an observation failure is never
  reported as drift.
- **Causal regression attribution: name the attempt behind a failing test or
  integration.** When a wave is rejected, the failing test selectors the
  combined suite reported are correlated with the attempts whose leases cover
  the failing files, and exactly one attempt covering every failing file is
  named as the offending attempt. Ambiguous evidence — two attempts' leases
  covering one file, no attempt's lease covering it, or a lease that could not
  be verified — names no attempt and says why instead of guessing, and
  attribution is lease-based, so it survives supersession: the named attempt
  is the one whose lease covered the failing file. The integration result
  carries the structured attribution and a recovery-ready failure record
  (`wave_rejected` or `combined_suite_failed`, with `failed_tests`); the suite
  evidence keeps the failing test ids from per-case sandbox results (and,
  failing that, from the output tail — a tail naming no selector means no
  attribution, never a guess); and a recovery request built from an
  integration failure is recorded against the offending attempt's task, with
  the cause named in the rationale and the outstanding question.
- **Design document: a native, durable bee-completion signal.**
  `docs/w6-native-completion-design.md` (docs only) designs the fix for the
  fragile continuation path: today the coordinator learns a bee finished only
  through a cron rule on the Hermes host, and one recorded one-shot wakeup
  cron whose fire time fell inside a gateway-restart window was silently
  dropped, leaving a finished bee unprocessed until a human nudged. The
  design gives the launcher a blocking wait operation, exposes it as
  `bundlebees_attempt op=await`, and makes the coordinator's own tool call the
  continuation — a level-triggered wait on a durable completion fact, with no
  schedule artifact left to drop — while the existing 10-minute heartbeat
  stays as the backstop and the multi-bee sweep, not the primary wait.
  Status: design document only; no code changes.

### Changed

- Verify records now include `evidence` (`source_rev`, `backend`,
  `exit_code`, `junit`, `diffstat`, `duration_s`).
- Branch and tests for verify come from the user or the lease, never from
  worker metadata.
- A missing optional backend capability returns `unsupported_operation`.

Tool count unchanged (4). Schemas: 571 -> 573 o200k (+2, dispatch
description).

## [0.1.1] - 2026-11-12

Fixes from the Hermes catalog review of 0.1.0 (NousResearch/hermes-agent#153888). Tested against real Hermes 0.23.0.

### Fixed

- The launcher token is read with `agent.secret_scope.get_secret("BUNDLEBEES_LAUNCHER_TOKEN")` directly. 0.1.0 expected an `agent` on the plugin context or handler kwargs, which Hermes never passes, so the token was never found.
- Settings are read per key with `ctx.get_config(key)` (tools and CLI) and with `hermes_cli.config.load_config_readonly()` in the dashboard API. 0.1.0 read a `ctx.config` attribute that does not exist.
- `desktop/plugin.js` now default-exports `{ id: "bundlebees", name, register(ctx) }` and contributes the pane with `ctx.register({ id, area: "panes", render })`, as the Desktop runtime loader requires. React comes from `import * as React from "react"` (the SDK does not export React).
- `dashboard/manifest.json` declares `"api": "plugin_api.py"`, so Hermes mounts the routes at `/api/plugins/bundlebees/`. The backend is built lazily per request from the current settings; nothing has to call `configure_backend`.
- `hermes bundlebees doctor` printed `token: [redacted] configured` because its own scrubber masked the word after `token:`. The fixed status line now reads `token: configured` or `token: not configured`.
- CLI and dashboard state live in `plugins.plugin_storage.plugin_data_dir("bundlebees")`. The XDG fallback is gone; with no Hermes host the CLI asks for `--state-dir`.

### Security

- **No automatic weak sandbox.** `verify_isolation=auto` now picks only a
  strong backend (bubblewrap, then firejail). `unshare`, which isolates
  network and PIDs but leaves the coordinator's filesystem readable, is never
  auto-selected: it runs only when `verify_isolation=unshare` is set *and* the
  new, documented-unsafe `verify_allow_weak_unshare` setting (or
  `BUNDLEBEES_ALLOW_WEAK_UNSHARE=1`) is on. With no strong backend, verify and
  integrate fail closed with `sandbox_unavailable` and a message naming
  bubblewrap/firejail. `doctor` reports a refused or overridden unshare.
- Cross-origin launcher redirects (any change of scheme, host, or port) are refused with `unsafe_redirect` before a request is sent, and `Authorization` is stripped from any non-origin URL as defence in depth.
- `bundlebees_attempt` with `op` cancel or review now goes through a `pre_tool_call` approval (`provides_hooks: [pre_tool_call]`), not only the model-set `confirm=true` argument.
- `child_env()` keeps the user's `HOME`, `PATH`, `USER`, `LOGNAME`, `LANG`, `TZ`, and `TMPDIR` (exact names) so a user-run `git push` finds its config and credential helper. Nothing else is inherited.

Tool schemas are unchanged: 4 tools, 571 o200k tokens.

## [0.1.0] - 2026-11-11

First public release of the bundlebees Hermes plugin.

### Added

- Four tools (`bundlebees_status`, `bundlebees_dispatch`, `bundlebees_attempt`, `bundlebees_host_prompt`) whose combined schemas measure 571 tokens on `o200k_base` (under a 900 budget).
- User-run CLI: `hermes bundlebees verify`, `deploy`, and `doctor`. The model does not apply patches, run the suite, push a branch, or SSH.
- Desktop pane on Hermes 0.23 or newer, talking only through `ctx.rest`.
- Bundled skill `bundlebees:bundlebees-operator`.
- Catalog-bar controls: secrets only through `get_secret`, exact-name `child_env`, path confinement, public-address / loopback URL policy, and honest disclosure in `plugin.yaml`.

### Security

- **No automatic weak sandbox.** `verify_isolation=auto` now picks only a
  strong backend (bubblewrap, then firejail). `unshare`, which isolates
  network and PIDs but leaves the coordinator's filesystem readable, is never
  auto-selected: it runs only when `verify_isolation=unshare` is set *and* the
  new, documented-unsafe `verify_allow_weak_unshare` setting (or
  `BUNDLEBEES_ALLOW_WEAK_UNSHARE=1`) is on. With no strong backend, verify and
  integrate fail closed with `sandbox_unavailable` and a message naming
  bubblewrap/firejail. `doctor` reports a refused or overridden unshare.
- Connections to a user-configured launcher are pinned to a pre-resolved, policy-checked address so a later DNS lookup cannot steer the socket. Redirects are re-validated and re-pinned before the next hop.
- Private or LAN launcher URLs require `allow_private=true` in plugin settings (default `false`). The default `orbit-loopback` adapter stays loopback-only.
- The Desktop data-directory fallback uses `plugin_data_dir` / `get_hermes_home()` and does not read `HERMES_HOME` from the process environment.
- Declared `config_schema` covers the settings the client and CLI honor (`launcher_url`, `allow_private`, `orbit_url`, `worktree`, `branch`, `deploy_host_alias`).

[0.1.1]: https://github.com/mojomast/bundlebees/releases/tag/v0.1.1
[0.1.0]: https://github.com/mojomast/bundlebees/releases/tag/v0.1.0
