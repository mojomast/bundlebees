- **Delivery reconciler (rizzstack.delivery/1) + `hermes bundlebees deliver`**: verify now records a durable delivery intent *before* the push and the pushed sha after it (transactional-outbox style), opens the PR eagerly as part of the same run, and a level-triggered sweep converges anything left pending — an existing PR is the success condition, so repeated sweeps never duplicate. Closes the gap where three verified branches sat on origin with no PR because the last step was a human act.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

- **Static-collected ids cover their parametrized GREEN children (WA-A1B-FUP2):** `bundlebees_plugin/tdd.py` gains `STATIC_COLLECTION` and `frozen_approved_selection`, which returns the frozen ids plus the subset whose approved file was collected from its syntax tree at `source_rev`; `check_collected_ids` and `check_green` take `static_ids=` and let an id whose file was statically collected cover exactly the ids `resolve_test_ref` maps to it, while every id not statically collected keeps the exact-id rule. `acdd_cli.record_green`, `acdd_cli.integrate_preflight`, `verify_local._frozen_approved_selection`, `verify_local._check_frozen_green` and `verify_local.verify_and_commit` wire the selection through, so a parametrized approved file that cannot be imported at `source_rev` no longer refuses its own legitimate GREEN. The exact-id rule stays fail-closed everywhere else — an unapproved or duplicated static id, an unrun approved id, an extra observed id, a duplicate run id and a skipped, xfailed or collection-stage case all still refuse `tests_mismatch` — and the one disclosed residual is that freeze time cannot know the parameter count, so one executed child of a statically collected id is accepted.
- **Rails launcher transport uses an exact plain-HTTP peer allowlist:** `rails_remote_source.LauncherTransport` accepts IPv4 loopback `127.0.0.0/8` and IPv6 loopback `::1`; with `allow_private=True`, it also accepts exactly RFC 1918 `10.0.0.0/8`, `172.16.0.0/12` and `192.168.0.0/16`, plus RFC 6598 `100.64.0.0/10`. Even with `allow_private=True`, TEST-NET-1/2/3 (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`), benchmarking `198.18.0.0/15`, `240.0.0.0/4`, `0.0.0.0/8`, limited broadcast `255.255.255.255`, multicast, link-local, IPv6 ULA, global/public IP literals and DNS hostnames refuse `EnrollmentRefused` before the token read or network helper. The `https` arm and direct peer `Host` header are unchanged. The Host-rewriting `rails_host_proxy.py` loopback forwarder is a temporary, unsupported workaround, not part of the supported path.
- **Bundlebees Rails checkpoints use record identity:** new cursors are `r1.<dedupe_key>`, naming the last consumed record rather than an index and prefix hash. Resume finds that identity in the current projection and continues after it, so insertions, deletions and edits before the checkpoint no longer cause a positional-chain mismatch; an unknown identity or deletion of the checkpointed record itself still refuses `source_gap`. New identity cursors resume against the current snapshot head rather than pinning the head observed by an earlier page. Stored legacy four-part positional cursors are not converted: an exact chain match replays the same records, cursor bytes and `source_head`, while a chain mismatch still refuses `source_gap`. Recovery for a source already stuck on a legacy cursor is an explicit operator action after reconciling the snapshot and ledger: deliberately enroll a new source instance with a fresh cursor namespace, or deliberately reset that source's import state and start a new initial import. The adapter never falls back to a null cursor, silently resets, or automatically replays a stuck source. Identity resume does not replay insertions, deletions or content edits at or before the checkpoint: deleting or editing an already-imported non-checkpointed record can remain invisible, and editing the checkpointed record does not change its identity; only disappearance of that checkpointed identity is guaranteed to refuse. Rails/1 event shapes, event authority and ledger chain fields are unchanged.
- **Assignment secret-scan precision:** the predicate now trips only on credential-shaped literal values; request-header object literals that reference runtime code and angle-bracket placeholder fixtures are exempt, empty quoted values remain exempt, and runtime-built mixed-class credentials plus all existing bearer, prefix, and structured-token controls still trip.
- **Unwired modules moved to `contrib/` (owner decision, Kyle t1650u):** `trial_harness`, `trial_simulated_driver`, `risk_policy`, `rework_metrics`, `rails_owner_bridge`, `hive_docket` and `wave_compat` leave `bundlebees_plugin/` for `contrib/trial/` and `contrib/unwired/`; each moved module imports its old package absolutely, module-path constants and checkout-root calculations follow the move, `verify_workers` and `contract_gate` stay, and `risk_policy.py`'s body is byte-identical (its sha256 pin still holds). `slop-baseline.json` carries the same per-file allowances to the new paths (counts unchanged).
- **Every GREEN recorder enforces the frozen collected-test ids (WA-A1B-FUP1):** the frozen-pin loader moves into `bundlebees_plugin/tdd.py::frozen_approved_ids`, and `acdd_cli.record_green` / `acdd_cli.integrate_preflight` now load it and pass `approved_ids=` into `check_green` before anything is signed, so `hermes bundlebees verify` refuses a partial run with `tests_mismatch` and writes no GREEN — WA-A1B had wired the pin only into the commit-local `verify_and_commit` lane, and that lane now also persists the checked record through `write_record` instead of discarding it; `check_collected_ids` counts only a case whose outcome is `passed` (skipped/xfailed/error never counts), `check_green` raises `green_failed` before `tests_mismatch`, `parse_junit_cases` defaults to the new `MAX_JUNIT_CASES = 20_000` so a large contract is not falsely refused, and a non-numeric JUnit counter is a `VerifyError` (`evidence_invalid`) instead of a `ValueError`.
- Leases record a top-level `task_id` (brief `task_id`, else the coordinator `label`) and the brief's own `job_id`; `attempts` rows carry `task_id`.
- `route_sync`: record the launcher-reported model/route per leased attempt; `attempts` uses it when no verified attribution exists.
- `rails_ingest`: scheduled Rails ledger ingest from the bundlebees and outbox sources, with systemd units.
- `rails_ingest` gains a fourth `launcher` source for the worker's read-only `rizzstack.rails-export/1` pages, configured with explicit `--launcher-url`, `--launcher-chain`, `--rails-token-file` and `--allow-private` runner arguments, resuming from a per-origin-chain ledger cursor and refusing incomplete or malformed configuration before any ledger write.
- `rails_ingest` gains a fifth `launcher-rails` source: the same read-only export read strictly, staging every pending page and validating the whole range with the ledger's own origin validator before the first commit, so a page error, malformed record or schema mismatch exits non-zero with the ledger and per-chain cursor untouched and reports the failing cursor plus a scrubbed transport label; replay from the committed cursor admits each event once.
- The bundlebees Rails source skips a plain file under `evidence/` instead of refusing the whole source.
- **GREEN must collect exactly the approved test ids (WA-A1B):** `bundlebees_plugin/tdd.py` gains `check_collected_ids(approved, cases)` and `check_green` takes `approved_ids=`, so GREEN now fails with `tests_mismatch` when the run's per-case ids are missing one, add an extra one, repeat one, or when a collection-stage entry is counted as a run; an empty approved list fails closed. `bundlebees_plugin/verify_local.py` passes the frozen manifest ids from the signed collection into that check, so a partial or cached result cannot pass as GREEN.
- **Verify CLI subversion gate:** `bundlebees_plugin/cli.py` `_cmd_verify` now calls `verify_local.check_patch_subversion(patch)` as soon as the worker patch is read — before the ACDD preflight, worktree preparation, `git apply` and the sandboxed run — and reports its `test_subversion` refusal through `_as_cli_error`, so the main `verify` command (the path bees and the coordinator actually use) enforces the same subversion guarantee that `verify --commit-local` already had (WA-A1A review finding 1 on bundlebees PR #271).
- **Patch subversion scan hardening (WA-A1A-FUP2, review findings 2-8 on #271):** `bundlebees_plugin/subversion_scan.py` now emits a risky-path finding once per file at line 1 even when no added line carries it (an empty added file via the `diff --git` header, a pure rename via `rename to`, with `rename from` origins ignored), also matches `from os import _exit`, a bare `_exit(` call and an aliased `atexit` import, treats a `pytest11` entry point (`pyproject.toml`/`setup.cfg`) and a `-p`/`--plugin` token on an `addopts` continuation line as a plugin load, limits content and `pytest_plugins` matches to `.py`/`.pyi`/`.pth` and the four pytest config names (with `=(?!=)` for patched assertions) so prose and `==` comparisons are not findings, and strips control characters from quoted header paths; `verify_local.check_patch_subversion` now reports `len(findings)` and up to five `kind@path:line` entries. This stays a static, content-level filter, not a semantic guarantee.
- **Patch subversion scan edge hardening (WA-A1A-FUP4, PR #281 review minor 2-4):** `bundlebees_plugin/subversion_scan.py` strips U+2028 and U+2029 as well as the C0/C1 ranges from quoted header paths, so a finding `path` and the `test_subversion` refusal message cannot break a line in a terminal or log pipeline; matches the quoted, single-quoted and space-padded spellings of the `pytest11` key in the `project.entry-points` table header (`"pytest11"`, `'pytest11'`, optional whitespace around the dotted parts) as a `pytest_plugin` finding — a header whose other key segments are quoted or escaped is still out of scope, because the scan is a content-level filter; treats a `-p` token at end of an `addopts` line as a plugin load, the form whose plugin name pytest's shlex reads from the next line; and `scan_patch` documents that the `addopts` continuation resets at every hunk, a patch-only limitation. Every `-p` in `addopts`, including `-p no:cacheprovider`, stays a `pytest_plugin` finding.
- Model routing: Nous low-risk lane. Low-risk slices (docs, tests-only, verify) route to `nous:deepseek-v4.1-flash-1017`, slightly harder ones and small refactors to `nous:hy4-flash`; the launcher chains 1017 -> hy4 -> `go:deepseek-v4.1-flash` and paces all Nous models under 180 requests/min. Complex slices stay on Kimi.
- CI speed-up (owner yes t1659s8401): the `tests (sandbox included)` job in `.github/workflows/ci.yml` runs as four parallel shards. `.github/scripts/shard_tests.py` balances the test files by the seconds in `.github/test-weights.json` (per-file times from a CI run of the shards) (a new file gets the median weight) and fails if the shards are not an exact partition of every collectable test file; each shard re-checks this before it runs, and `--against-pytest` also compares it with pytest's own `--collect-only` file set (review finding on #268), so no test is skipped. Required check names (`ci-gate`, `gitleaks`, `slop-lint`) are unchanged, and `ci-gate` still needs `tests`, which fails if any shard fails. `tests/test_shard_tests.py` covers the partition.
- **Verify-workers shadow truth and a recorder that cannot raise (T1-VWSHADOW2 follow-up):** `bundlebees_plugin/verify_workers.py` now tells the truth in its body — production has exactly `in-beat` and `shadow`, mode `on` is refused as `mode_not_wired`, and the library keeps the `on` code path for its tests only — and `_FIX_MODE` names `in-beat or shadow`; `docs/verify-workers.md` no longer claims the `on` path is absent from the library, only that production does not wire it; and `verify_shadow.observe` moves the error record's timestamp inside its guarded block and wraps the `_record_error` call in `contextlib.suppress(Exception)`, so error recording can never raise out of `observe` and the in-beat verdict is always returned (the success path is unchanged).

- Wave E adapters ADR (`docs/adr/0001-adapters.md`): harness, model, sandbox, forge and coordinator seams; second harness recommendation is Claude Code over ACP.
- **Preflight honours `require_contract`:** `bundlebees_plugin/preflight.py` takes a keyword-only `require_contract=` and gains a `--require-contract` flag, both threaded into the same `tools._gate` the dispatcher uses, and the registered `preflight` command reads the `require_contract` setting through `config_flag(settings_from_ctx(ctx))` exactly like `tools._require_contract`, so preflight and dispatch return the same verdict when the setting is on.
- **GREEN-adjacent hardening (WA-A1B-FUP3, review-bb-pr282 minors):** `tdd.frozen_approved_ids` now refuses an explicit null or non-mapping `test_collection` pin as `evidence_tampered` instead of treating it as absent, `tdd refactor` and `tdd review` pass the frozen approved ids into `tdd.check_green`, and `acdd_cli._result` maps a non-numeric JUnit counter on the verify lane to a named `evidence_invalid` refusal.
- **Report-only `rizzstack.brief/1` drift in dispatch (Wave B8):** `bundlebees_plugin/contract_drift.py` checks every outgoing brief against the vendored frozen schema (`bundlebees_plugin/schemas/rizzstack-brief.schema.json`, byte-identical to workflow at the B3 head) through the stdlib-only `bundlebees_plugin/rizzstack_validators.py`, attaches a `brief_schema_drift` block (`mode: report_only`, count, brief sha256, problems) to the dispatch envelope only when there are findings, and appends one JSONL record per call, carrying that call's findings, under `<state_dir>/brief-schema-drift/report-only.jsonl`; recording is best effort and never refuses or aborts a dispatch, so enforcement stays a later slice (B6).
- **GREEN-adjacent residuals from the PR #290 review (WA-A1B-FUP4):** `tdd._collection_pin` keeps the legacy no-pin `None` only for a contract with no `review` key at all and refuses a present but non-mapping `review` block as `evidence_tampered`; `acdd_cli._result` and `verify_local._check_frozen_green` now share `verify_local._junit_counter_or_default`, which treats only `None` as absent, so a falsy malformed JUnit counter (`""`, `[]`, `{}`) is refused as `evidence_invalid` instead of silently becoming the case count or zero, while the case-count fallback and the zero default are unchanged for `None` and for an absent `junit` block.
- **The attach step keeps the findings the research gate actually validated (WA-A5B-FUP1, review finding 1 on #299):** `bundlebees_plugin/tools.py::_attach_accepted_findings` now resolves the gated failure id before it honours a caller-supplied `brief.findings` block, and the new `_supplied_findings` helper returns that block only when its `failure_id` equals the resolved id — a block naming any other failure is replaced by the accepted state artifact instead of being forwarded to the launcher, a matching block stays byte-identical, and a brief with no gated id is still returned before any state read; every fail-closed branch (unavailable state directory, unreadable or corrupt research record, missing artifact) keeps its error code. `tests/test_retry_findings_attach_scope.py` pins both id paths plus the direct-block, missing-artifact and corrupt-artifact branches.

## [0.2.0] - 2026-11-24

- Release prep (no tag): `pyproject.toml`, `dashboard/manifest.json` and `docs/stack-manifest.md` now say **0.2.0**, matching `plugin.yaml` and this changelog (the 0.1.1 / 0.2.0 split inside the repo is gone). `HYVV_PIN` is re-pinned to the hyvv 0.1.18 merge `ae8680a4c0289ce91dab52169eff1433cb694533` (hyvv #41) in every copy (pins.py, CI checkout, setup script, pinned-sources, stack manifest, README). `LAUNCHER_PIN` stays `0.1.17`: the pin must match a recorded real launcher envelope (`tests/test_verify_result_shape.py`), and no 0.1.18 launcher has been deployed or recorded yet. Tagging v0.2.0 still needs Kyle's typed yes.

Verifier and contract hardening from an external review of the workflow. The
unit of progress is an accepted artifact with evidence, and every guarantee
below lives in the coordinator, independent of which agent produced the work.
Built as six file-leased DeepSeek bee slices (two waves) and integrated in
one pull request.

- `verify` records now carry a `test_argv` only when it is the argv this process executed: when the evidence text is the exact trusted template for these tests (pytest with the fixed flags, or `node --test`) and equals the executed argv, it is recorded verbatim — including a path component that the scrubber would redact, because that argv is the CLI-built evidence; when the evidence text is not that template, the executed argv is recorded in its scrubbed form; and the key is omitted when there is no executed argv (an empty argv sequence counts as none), or when the evidence text is that template but the executed argv is not (review-bb-pr232 F1 / pr233 F1 / pr234 F1+F2 as a class, plus review-bb-pr236 F1/F2 and review-bb-pr237 F1-F5 / pr238 F1).

### Added (wave A, Nov 2026)

- **Versioned risk-floor policy (`bundlebees.risk-floor/1`):** the path floor now comes from `bundlebees_plugin/policy/risk-floor.json`, and test-harness files (`conftest.py`, `tests/fixtures/**`, `pytest.ini`, `tox.ini`, `setup.cfg`, `noxfile.py`, `sitecustomize.py`) are high; `derive_floor` takes an optional `policy_path` and fails closed to high when the policy is missing, unreadable or invalid, and the hardcoded `_SENSITIVE_MODULES` set is gone (WA-A7).

- **`metrics.weekly()`**: new trailing-window rollup across every run that returns `window_days`, `verified_patches` (distinct attempts with a passing `verify` event), `attempts`, `tokens_total`, `tokens_per_verified_patch`, `tokens_known_subtotal` and `coverage`. Token usage is cumulative per attempt so the last valid report wins; an unknown usage report makes `tokens_total` and the rate `None` (never zero) while `tokens_known_subtotal` carries the partial sum, and a genuine zero report is still zero.

### Changed (rizzstack cleanup, Nov 2026)

- **Tool-completion dedupe reader hardened:** the reader now opens `tool-completions` with a directory-level no-symlink guard and a `dir_fd`-relative record open, refuses multi-link records, and bounds a record at 65536 bytes, so a symlinked, hardlinked, or oversized planted record can no longer suppress the landing event (a planted record that is a real, single-link file of at most 65536 bytes is still trusted as the attempt's prior record).
- **Tool-completion write guard:** the completion-record writer opens `tool-completions` itself with a directory-level no-symlink guard and publishes create-only through that `dir_fd`, so a symlinked parent is refused before anything is read or written and completion evidence never leaves the state directory; FIFO/non-regular, multi-link or oversized records never block and are never treated as a prior record, an identical existing record is not rewritten, and a conflicting record is refused with its bytes untouched.
- **Contract gate, report-only in the live path:** `tools._dispatch_acdd` runs the provenance and acceptance-id checks and records would-refuse findings (`gate` block and `<state>/contract-gate/report-only.jsonl`, rotated at 1 MiB) without refusing. The switch is the constant `tools.CONTRACT_GATE_MODE`; enforcing needs a code change. The pinned workflow HEAD is cached per process (30 s) with a 5 s git timeout.
- **Workflow pin:** the `PIN-THE-WORKFLOW-REV` placeholder is gone; scaffolds seal `pins.WORKFLOW_PIN`, which the doctor also checks.
- **Reconciliation sweep backstop:** the factory driver runs the sweep classify-only through `LauncherAdapter`, at most every 1800 s, when started with `--sweep` (the shipped systemd unit passes it). Without a PR view it reports `pr_view: "unavailable"` and lists pushed-without-PR ledger records as `unverified` instead of gaps. git runs with `child_env()` and `GIT_TERMINAL_PROMPT=0`.
- **Removed:** `graceful_recovery` (never wired), the changelog-coverage and changelog-entry tests, and doc-text assertions in tests. `verify_workers` is marked NOT WIRED. The bundled TDD evidence schema is synced with workflow's (optional `sequence`, `prev_sha256`); bundlebees does not emit the hash chain (declined).
- **Landing-bridge non-blocking reads:** the landing bridge opens ack, source and cursor files non-blocking, so a FIFO or other non-regular file is refused with `source_invalid` instead of wedging the ingest pass, and an unusable acknowledgement is reported as pending rather than aborting the pass.
- **Landing-bridge residual hardening (tests and docs only):** the tests now pin a non-regular path at the move destination (`acked/<name>`, FIFO and directory) as bounded with no duplicate event and self-healing once removed, take the file-descriptor baseline before the first read, bound the fd test so a regression fails instead of hanging, and cover a non-directory outbox; the docs now say `pending` means not provably acknowledged and `failures` is authoritative (review-bb-pr223 R1/R2/R3/R5/R7; tests and docs only).
- **Landing-bridge destination truth (tests and docs only):** the tests now pin the regular-file `acked/<name>` conflict (`source_conflict`, source kept, destination bytes untouched) and the docs now name the destination paths the read refuses (a FIFO, a directory, a unix socket, or a symlink whose target resolves; a dangling symlink or link loop is replaced by the rename) and say a corrupt acknowledgement keeps its source pending until an operator repairs it (review-bb-pr224 F1/F2/F3, review-bb-pr225 B1/F1; tests and docs only).
- **Verify record argv literal (review-bb-pr226 F1, review-bb-pr228 F1):** the verify record's `evidence.test_argv` now keeps the exact CLI-built command for the single-runner verify lane, including the interpreter path that the long-key rule used to redact; a recorded value that does not match that template exactly — for example a test target containing a space, or the ACDD non-strict lane whose argv is built from the selected subset of the requested files — keeps the scrubbed form, and `{evidence}` remains the single runtime placeholder for the sandbox's evidence directory, so replaying a record needs only a writable directory substituted.

### Added

- **Complex-slice model routing:** `bundlebees_plugin.model_route.select_worker_model` (and `python -m bundlebees_plugin.model_route <brief.json>`) picks the worker model per brief: complex slices (effective risk high, a lease of 5+ files, cross-repo, a security or approval-gate path, Rails harness work, two failed attempts, or `complexity: complex`) get the launcher's Kimi route `k3.1-pro`; everything else stays on `go:deepseek-v4.1-flash`. Pure and offline; a complex model missing from the launcher allow-list falls back visibly (`fallback: true` with a reason). Contract: `docs/contracts/model-route.md`.
- **Verify workers wired in shadow mode (T1-VWSHADOW):** `BUNDLEBEES_VERIFY_WORKERS=shadow` runs the verify-worker lane beside the normal in-beat verify and records the comparison, while the in-beat verdict stays authoritative — `verify_shadow.observe` returns the exact verdict object it was given, never re-runs the in-beat verifier, and swallows each shadow failure as one JSON line under `<state>/verify-workers/shadow/errors.jsonl` (so a missing worker config is recorded, not raised). The mode is resolved once where the verifier closure is built, so a bad value fails closed before the tick; `verify_workers` is now marked `WIRED = "shadow"` and reached only through `verify_shadow`; mode `on` is refused with `mode_not_wired` (`bundlebees_plugin/verify_shadow.py`, AC-001..AC-009).
- **Test-only consumer adapters gated (WA-A10):** `agent_consumer` now exposes `allowed_adapters()`, `resolve_adapter()` and `crash_armed()`, so the `authorized` adapter and the `BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL` crash boundary are reachable only when `BUNDLEBEES_TEST_ADAPTERS` is exactly `1`; a work-item adapter hint cannot smuggle a test adapter into a production run (`adapter_test_only`), an unknown adapter keeps `adapter_unknown`, and the default stays `hermes`.
- **Patch subversion scan before the sandbox (WA-A1A):** `bundlebees_plugin/subversion_scan.py` adds `scan_patch()`, which flags an added `conftest.py`, an interpreter startup hook (`sitecustomize.py`/`usercustomize.py`), a `.pth` file, a pytest plugin load (`pytest_plugins`, or `addopts` with a `-p` plugin option), a forced exit, an `atexit` hook, a pytest hook, or a patched assertion; `verify_local.check_patch_subversion()` runs it before the sandboxed test run and any finding refuses with the `VerifyError` code `test_subversion`.
- **Board → dispatcher bridge:** `hermes bundlebees board-dispatch --once` connects the Kanban `ready` column to the factory driver. A ready card with a durable approval record (`factory/board-approvals/<task_id>.json`) that carries a strict-valid brief and an existing authorized job gets one `queue.json` feature (envelope preserved) plus one `factory/dispatch-approvals/<work_id>.json` record, idempotently, so the resident service picks it up on its next pass. Cards without an approval record are never stocked, a replay changes nothing, and a conflicting identity is refused rather than overwritten (`bundlebees_plugin/board_dispatch.py`, `docs/board-dispatch.md`).
- **Factory dispatch wiring:** the factory driver reads the live docket envelope (`bundlebees.queue/1`, a `features` list) as well as the legacy feature map, updates a feature in place and refuses any other shape with a typed `queue_invalid` instead of the attribute error the live queue stage hit. The shipped service unit passes the launcher URL, repo and gitleaks from a mandatory 0600 `EnvironmentFile` and starts with `--require-dispatch-config`, which exits with the missing variable names instead of silently running the observation tick; `WORKFLOW_SRC` is documented as required for landing validation and recovery.
- **Docs-sync companion (this PR):** registers #107 and #108 in the coverage registry with their Unreleased bullets, so a full-history run of the changelog guard stops reporting them as undocumented merges.
- **Research-before-retry gate (M12):** a retry of a recorded failure is now refused before any launcher call until accepted findings exist for that failure id (`bundlebees_plugin/research_gate.py`, `require_findings`). The failure signature is derived from the failing tests and the key error line, so a duplicate failure enqueues exactly one research item while a new attempt lineage gets its own; findings must carry sources, a rejected alternative and a verdict, and an environment-problem verdict goes to the decision queue with no retry. Fail closed: an unavailable queue or research worker leaves the retry blocked and surfaced as blocked.
- **Hermes kanban board sync (M13):** `hermes bundlebees board-sync` reflects bee/attempt and GitHub PR lifecycle events onto the existing kanban board through the public CLI instead of writing the SQLite file — cards are keyed by PR link or attempt id so replays change nothing, a reconcile pass repairs anything missed, a verified backup of the board is taken before the first mutation (refused on a bad copy), and the command fails soft when the board is unreachable (`bundlebees_plugin/board_sync.py`). It only reflects state; it never merges, dispatches, restarts or approves anything.

- **Coordinator action queue:** a durable per-item coordinator action queue over the existing state dir with atomic per-item claims, monotonically increasing durable fencing values (`fence`/`fence_seq`), precise stale refusals, idempotent replay by action key, stale-owner adoption, and per-repo serialization only for overlapping merge and deploy leases; an AST guard proves the queue only records, claims and fences and never executes anything.
- **rizzstack doctor:** `hermes bundlebees doctor` reports four-repo revision parity (checkout, origin main, installed package, gateway-loaded plugin, deployed runtime) with explicit per-component states (healthy, stale, unavailable, inaccessible, unknown, incompatible), launcher/gateway contract-incompatibility detection, executable / sandbox / pinned-source / credential-presence checks, `--json` output, and `--check-parity` exit semantics that exit nonzero on any divergence, all strictly read-only.
- **attempts and status CLI:** two read-only subcommands, `hermes bundlebees attempts` (live attempt list as JSON) and `hermes bundlebees status` (in-flight count, bee cap, and launcher health fields), backed by the plugin's existing attempt store. `status` performs exactly one secret-store read: the launcher-token presence probe — `client.token_configured()` calls Hermes's `agent.secret_scope.get_secret(BUNDLEBEES_LAUNCHER_TOKEN)` to fill `health.credential_configured`; it reads presence only, never a value, never emits one, and fails closed. No credential value is required or read for either view.
- **Binary payload verification:** result envelopes may carry binary worker payloads under the `bundlebees-binary-payload/1` marker; the adapter validates fail-closed (strict two-half sections, git base85, capped inflate, bounded delta instructions, blob-id re-derivation, path confinement) and applies them losslessly through the unchanged verify flow.
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

- **Launcher build-identity evidence tiers:** the doctor's launcher build-identity check reports the running launcher's identity in three evidence tiers — receipt-backed, version-string-only, unavailable — and classifies one read-only `GET /v1/provision/capabilities` through the already-authenticated client. A version string without a deployment receipt is always flagged as not deployment evidence (owner directive, 2026-11-14); the capabilities classification never upgrades the identity tier. See `docs/doctor.md`.
- **Lease payload staging for sandboxes:** the verify staging half copies the lease payload's pinned sources into the sandbox copy under `.bundlebees-payload/` with relative-path naming, serves them through the environment transport and checks byte identity end to end; the real-firejail masking assertion is measurement-gated (it skips with a named reason only when the nested jail downgrades to no mask) instead of being silently weakened. See `tests/test_sandbox_pinned_sources.py`.
- **Gate-watcher (planned to queued):** a backlog-driven transition watcher reads `plans/backlog.json` (`bundlebees.gate-backlog/1`), evaluates each planned slice's gates against live state (merged PRs, terminal attempts, dates) and records idempotent planned-to-queued transitions with gate evidence; the lane budget is clamped to 5-7 lanes / 10 bees, owner-gated slices are never auto-dispatched, and stale gate evidence blocks instead of dispatching. See `docs/gate-watcher.md`.
- **Attempt reconciliation sweep:** a reconciliation pass over terminal worker attempts (`rizzstack.attempt-reconciliation/1`): succeeded attempts are classified by stage (`full`/`pr`) or `not_succeeded`. Nothing ran it on a schedule until the rizzstack cleanup; it now runs from the factory driver's `--sweep` backstop, classify-only (no ledger writes, no claim, no processor), at most every 30 minutes. See `docs/factory-driver-service.md`.
- **Graceful recovery records:** removed by the rizzstack cleanup. The module was never wired into any production path; the durable coordinator claim it described was not live.
- **Parallel verify workers (shadow first):** built but NOT WIRED: no production path (beat, factory driver, tools, CLI) calls `verify_workers`; it is kept for the research gate's tests. See `docs/verify-workers.md`.
- **Require-contract dispatch gate:** `bundlebees_plugin/contract_gate.py` holds the fail-closed gate helpers and a dry-run CLI (`python3 -m bundlebees_plugin.contract_gate check BRIEF.json`); the live dispatch path does not call it. The live path (`tools._dispatch_acdd`) runs the provenance and acceptance-id checks report-only. See `docs/contract-enforcement.md`.
- **Lease payload served on every run:** the payload digest manifest of what the coordinator actually serves is recorded into the brief as ``brief["payload"]`` at dispatch and bound by verify byte for byte before any test runs; every runner-driven sandbox run now serves the payload, a set-but-unusable pin raises `PayloadError`, and `--commit-local` serves from the environment without binding a lease manifest. See `docs/pinned-sources.md`.
- **Contract-enforcement regression net:** the d8c2356 import-alias defect class (a fourth indented import variant aliasing the payload-server module as the gate) is now pinned by an import-identity test on `bundlebees_plugin.contract_gate` plus an import-variant/whitespace-tolerant sweep over the enforcement files; settings resolution lets an explicit `false` outvote a `default: true` while unparsable values keep their no-vote behavior; scaffold lease checks reject directory-style entries and an unusable `--out` returns the `scaffold_invalid` envelope with exit 1; docs/ACDD.md and docs/contract-enforcement.md truthed against the post-rename behavior. See `tests/test_contract_enforcement.py`.
- **Tool-path terminal-state write-back:** tool-path attempts now record real
  terminal states through `bundlebees_plugin/agent_consumer.py` instead of
  staying `unknown` in the local attempt store; the write-back is
  duplicate-safe and the completion record carries the real end state. See
  `tests/test_tool_path_write_back.py`.
- **Completion events wired into the terminal transitions:**
  `record_tool_completion` gained its production call sites in
  `bundlebees_plugin/tools.py`'s attempt-op terminal transitions — best-effort
  emission (a write failure never breaks completion), duplicate-terminal-safe,
  with launcher/coordinator/event-delivery/artifact-verification reconciled as
  separate facts, closing the emission gap that left tool-path dispatches
  permanently invisible (the F1/P2 failure class). See
  `tests/test_tool_path_write_back.py`.
- **rizzstack-rails usage-ledger contract rails:** new
  `contrib/rizzstack_rails` package (`contracts.py`, `ledger.py`,
  `privacy.py`) implementing the frozen usage-ledger contract rails:
  content-hash dedupe (`semantic_sha`/`candidate_sha`), hash vectors and
  tamper checks, backup/restore, and health/readonly/privacy gates, vendored
  reproducibly by `scripts/vendor_rails.py` against
  `contrib/rizzstack_rails/vendor-manifest.json`; acceptance lives in
  `tests/test_rails_ledger.py` (35 tests).
- **Factory-hook bridge (`hermes bundlebees factory-hook`):** the
  repository-side bridge turns an attempt the 2-minute scout observes reaching
  a terminal state into exactly one ordinary factory-driver pass instead of
  waiting for the beat. A durable watermark records the handled attempt ids so
  re-running over unchanged state is a no-op; a non-blocking `flock` coalesces
  overlapping invocations (the loser reports `coalesced` and runs nothing); a
  failed pass leaves the watermark unadvanced so the next tick retries; and
  the 30-minute heartbeat remains the heartbeat backstop. Wiring it into the
  live scout script or any cron entry is an explicit owner action — the hook
  installs nothing. See `docs/scout-hook.md`.
- **Durable completion event in the coordinator inbox:** a terminal landing now
  publishes one completion event into the coordinator inbox before the source acknowledgement,
  so a crash or a failed source ack cannot lose the
  completion; a write failure raises and leaves the landing unacked for a
  retry. Delivery is deduplicated by event id (a duplicate returns
  `duplicate: True` and never calls the handler), the model is at-least-once
  with one accepted decision, and a handler failure gets one bounded retry
  before the event is marked failed. See `docs/event-consumer.md`.
- **Factory driver service mode:** the resident service wakes on the inbox
  (inotify, with a bounded poll fallback) instead of polling for landings,
  drains the completion consumer, and dispatches an eligible scheduler item
  only when a durable pre-approval, a readable scope list covering every
  leased file, the shared bee cap and the scheduler's planning limits all
  allow it. Anything missing or unreadable means no dispatch plus a durable
  judgment and one coalesced wake through `WakeCoalescer` and
  `notify_coordinator`. The shipped unit is a template and documentation only
  — not installed, enabled or started. See `docs/factory-driver-service.md`.
- **Board-stock low signal comments on change, not once per episode:** while
  the ready column is low, the beat or the service pass posts one board comment
  whenever the observation changes — the state, ready count, free
  slots, shortfall or board — rather than once per low episode; identical repeats
  do not comment again, and a recovery followed by a new shortfall starts a new episode.
  The
  comment still states the numbers and that the factory only observes; it
  never creates, moves, approves or dispatches work. See `docs/board-stock.md`.
### Fixed

- **`docs/ACDD.md` status refreshed to match `main`.** The Status paragraph dated 2026-11-12 said ACDD/TDD was enforced only on the unmerged `feat/acdd-tdd` branch and that Orbit's evidence view was unmerged. It now records that enforcement at dispatch, verify and integrate is on `main` (bundlebees #5), that risk tiers are enforced at dispatch (#241/#242/#246/#247) with a frozen `acdd/1` contract required at the high tier, that box dispatchers run the side-effect-free preflight first (#252), and that Orbit's read-only evidence view is merged (orbitdesktop #26) while board wiring stays a separate plan; the same stale claims in §9 and the §10 table are corrected. The protocol text is unchanged.
- **Bounded git timeouts on the integration path:** `integrate_job._git` passes a bounded `GIT_TIMEOUT_S` (300 s) to every git call and takes a `timeout_s` override for longer operations such as a push, and a `subprocess.TimeoutExpired` becomes an `IntegrateError` with code `git_timeout` even when `check=False` — so a hung credential prompt, stuck remote or stale lock fails the integration step with a named error instead of holding it forever.
- **`verify --commit-local` and `--dry-run` are mutually exclusive.** The parser now puts both flags in one mutually exclusive group, and dispatch independently refuses a hand-built namespace that sets both with `invalid_argument` before any client fetch or verify-results write. Each flag alone keeps its existing behavior; the pair is refused rather than interpreted.
- **Dry runs no longer replace an attempt's official verify record.** A dry run writes its preview to `verify-results/dry-run/<attempt_id>.json` only, with `dry_run: true`, and never writes or replaces the canonical record, leaving `verify-results/<attempt_id>.json` untouched. Once its lease loads, a dry run also appends a `verify` event to `metrics/<run_id>.jsonl`. A non-dry-run verify writes `verify-results/<attempt_id>.json` whether it passes or refuses; a refusal records `ok: false` with a `code` and `error` and replaces any earlier canonical record for that attempt. `verify --commit-local` is the exception: it writes the canonical record only when it commits; a commit-local refusal writes no record and leaves any earlier one in place. A dry run never writes or replaces the canonical record.
- **Verify records refuse uncontained state paths and publish atomically.** A symlinked `verify-results/<attempt_id>.json` or `verify-results/dry-run/<attempt_id>.json` is now a typed `state_dir_uncontained` refusal naming the relative record path and the state directory root instead of a generic `internal_error`; a refusal that cannot write its record keeps the original refusal code and prints one `warning:` that the record was not written. Both record writes serialise the unchanged JSON bytes to a same-directory 0600 temporary file, fsync it, and `os.replace` it. A failed replace in `cli._write_verify_result` leaves the previous record byte-identical and removes the temporary file in a `finally` block. A crash can leave the temporary file behind: the previous record stays byte-identical until the atomic rename. That cleanup covers the canonical and dry-run writes that go through `cli._write_verify_result`, not the canonical write in `verify_local._write_record`, which has no `try`/`finally` cleanup.
- **A stale legacy wave record no longer blocks another job's wave 1.**
  `wave_reconciliation_required` fired whenever `waves/w1.json` existed,
  because every job's wave 1 carries that bare legacy alias — so a record left
  by an unrelated job or a standalone integration refused every later job's
  dispatch and integration. Recovery and integration now treat a legacy record
  as foreign when its sealed identity names another job or when its recorded
  attempts do not overlap this job's attempts: it is neither adopted nor
  allowed to block. Shared-attempt, empty and unreadable evidence still fails
  closed with the same refusal (`bundlebees_plugin/job.py`,
  `bundlebees_plugin/integrate_job.py`, `tests/test_legacy_wave_scope.py`).

- **A sandboxed run can record the lease for the payload it was served.**
  `dispatch.collect_payload` refused the served relative form
  (`./.bundlebees-payload/WORKFLOW_SRC`) that the sandbox child env carries, so
  a bee test that called `leases.record_lease` inside the verify sandbox died
  with `payload_source_invalid` before its own assertions ran. The
  absolute-path requirement is unchanged: the one accepted relative value is
  exactly the staged entry for that payload name, and it must resolve under the
  process's own `.bundlebees-payload/` directory — `..`, another name, a bare
  relative path and a symlink out of the copy all keep the refusal. See
  `docs/pinned-sources.md`.
- **The landing-event composition no longer collides with the coordinator
  action queue.** The composed consumer (`consume_completions`) wrote its own
  `rizzstack.coordinator-action/1` items into `coordinator/actions` — the
  directory the merged coordinator action queue owns — so a single composed
  item made `coordinator_actions.list_actions`/`enqueue` fail with
  `state_corrupt`, the merged queue's id grammar refused the `<kind>:<attempt>`
  keys, and the two stores' differing lock files and fence counters allowed a
  fenced bypass across stores. The composition's items now live in their own
  directory, `coordinator/completion-actions/`, behind the same API, and the
  explicit `coordinator_actions_enqueue` adapter at the `enqueue` seam
  publishes conformant `bundlebees.coordinator-actions/1` records through the
  merged queue (the idempotent key as `action_key`, a colon-free queue id, the
  completion facts in `payload`, `fence_seq` fencing, `status: "queued"`). A
  coexistence test proves both stores work in one state directory without
  corrupting each other. `complete_action`/`fail_action` now refuse an item
  that is not currently claimed (`not_claimed`), closing the gap where a stale
  owner could complete or fail an unclaimed, released or failed item; store
  I/O failures surface as `ActionQueueError` with code `queue_unavailable`
  instead of a bare `OSError`; and `docs/event-consumer.md` states the actual
  integration contract instead of claiming that any queue keeping the same
  keys can replace the bundled store without touching the consumer.

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
- **`attempts`/`status` count only live work as in-flight and report terminal
  tool completions.** A validated `rizzstack.tool-completion/1` record now
  makes an attempt `failed` when its state is `failed`, `cancelled` or
  `interrupted`, and `unverified` when it is `succeeded`; a verify record
  still outranks a completion. The terminal-completion branch precedes the
  expiry check, so a terminal completion is never called `expired`, and
  `updated_at` reports the completion's `recorded_at` when it is the latest
  stamp. A dispatched attempt with no recorded outcome is `expired` once the
  12-hour idle window passes, and a `verified` result is no longer in flight
  because the bee has already finished and the result awaits acceptance. On a
  real 414-lease store this took the reported `in_flight` count from 297 (with
  zero bees running) to 6, so the fleet view no longer reports finished and
  abandoned dispatches as active bees.
- **Verify records its durable delivery intent before the push.** `verify` now
  defines the lease's `source_rev` as the base revision before it writes the
  delivery outbox row, so the intent carries the verified base and the PR
  obligation is never lost to a `NameError`. The earlier
  `name 'base' is not defined` failure happened after the push and silently
  suppressed PR creation; the record now reaches `pushed` (and the eager relay
  opens the PR) or stays pending for `hermes bundlebees deliver`.
- **Verbose attempt results read up to a bounded 24 MiB.** One attempt's result
  document (and the attempt detail that can carry it) is read with
  `MAX_RESULT_BYTES`, matching Orbit's 8 MiB artifact cap plus JSON-escaping
  headroom, instead of being refused with `response_too_large` at the old 1 MiB
  client cap. Every ordinary response keeps the 1 MiB `MAX_RESPONSE_BYTES`
  limit, and a result over the new ceiling still refuses with
  `response_too_large`.
- **Verify output and its durable records keep their own evidence.** `verify`
  now prints its own validated branch, the verify-result path and the commit sha
  literally instead of `[redacted]`, and writes the same validated branch,
  the source revision and the result digests into the durable
  `verify-results/<attempt>.json` record; untrusted text, including
  worker-supplied values, stays scrubbed.

### Security
- **Attempts/status completion evidence reader hardening:** the reader now opens `tool-completions` itself with a directory-level no-symlink guard and opens each record relative to that descriptor, so a symlinked or non-directory `tool-completions` entry is refused without reading its target. A record with more than one hard link is also refused. The 65536-byte bound is pinned at the exact boundary, with both the pre-read size and post-read length race clauses tested; any refusal leaves the attempt reporting as `dispatched`.
- **Verify worktree integrity:** every git worktree-add site in `verify`
  (main verify, identity-for-commit, commit-contains-patch) now forces a
  **full checkout** (worktree-scoped `git sparse-checkout disable` with a
  shared-config fallback) and fails closed (`worktree_incomplete`) when
  `bundlebees_plugin/schemas/brief.schema.json` is missing from a tree that
  carries it, so a narrowed sparse checkout can never silently pass
  verification against an incomplete tree; the host checkout's sparse state
  is never mutated. A missing git context on the tests-gate failure path
  appends a **git-context hint** pointing at the doctor note, and the doctor
  sandbox row states the no-git-context constraint in every state. Pinned by
  RED-first probes with byte-identical restores, fail-closed audits at all
  three worktree-add sites, and the 48-test two-file gate including the
  newly collected `GitContextGap` class (its `__test__` marker and the
  GREEN-run stub fix landed in this PR's repair rounds).
- **Gate-watcher landed-delivery gate:** dependency and attempt-success gates
  in the planned-to-queued gate watcher now clear only on terminal attempt
  success AND an origin-verified landed delivery (branch on origin or merged
  PR), via a fail-closed `World.origin_landed` probe that answers `None` on
  every unprovable path (mapped to `waiting/origin_not_proven`) - a
  succeeded-but-zero-delivery attempt can no longer unlock dependents. Pinned
  by RED-first both ways (reverting to attempt-state-only clearing fails 7/8
  new tests with all 28 pre-existing passing; a truthy-default probe fails
  both dedicated fail-closed tests), byte-identical sha256 restores, and the
  36-test `tests/test_gate_watcher.py` gate.
- **Verify refusal evidence:** secret-scan refusals (`secret_detected`) raised
  by `verify` now carry refusal evidence — the matched rule id, its 1-based
  source line, and a masked excerpt of the matched span — while the fail-closed
  refusal decision, its single refusal site and its hint are unchanged
  (enrichment only). Pinned by a 200k-case predicate-parity fuzz, a 15-case
  differential harness against the pre-change scan, and full-suite
  failure-set identity (the 114 pre-existing environmental failures are
  unchanged) in `tests/test_security.py` and `tests/test_cli.py`;
  `tests/test_client.py` is byte-identical to its mandated value.

- **Scrub predicate value-shape repair:** the redaction predicate now catches
  secret values in tight `k=v` assignments and in the runtime-join construction
  the scrub battery itself uses (fragments concatenated at runtime), with
  prefix-family matching, bare-SHA safety and scrub parity across paths; the
  report convention's single mandated negative-control marker line stays
  exempt. Pinned by the 31-case edge battery plus 18 repair cases in
  `tests/test_security.py`; the targeted suite with `tests/test_client.py` is
  137 passed and `tests/test_client.py` is unmodified by the repair.
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
- **Every attempt row carries the dispatch `job_id`.** The fleet view now reads
  the job from the brief (falling back to the lease record) and adds `repo`
  only from what the brief or lease actually names — the project label is never
  substituted for a repository — so a row that previously rendered
  "Job unknown" can be traced back to its job and bound into a Rails timeline.
  On the real store 17 of 414 leases named a job; rows whose brief predates the
  field stay honestly null.
- **Secret scan reads tar archives instead of failing closed on size.** An
  oversize tar (`.tar`, `.tar.gz`, `.tgz`) is compared member by member against
  the base blob and only the added member lines are scanned, so an archive over
  the 2 MB file cap no longer refuses on every changed file. An unparseable
  archive, an unsafe member path, or a single member over the scan cap still
  fails closed with no evidence, and a 64 MiB decompression bound keeps a small
  gzip from exhausting coordinator memory.
- **Verify refusals record a machine code and a scrubbed reason.** When
  `verify` refuses, `verify-results/<attempt>.json` now carries the refusal's
  stable `code` and a bounded, scrubbed `error` reason with operator
  state/worktree/repo path prefixes replaced, instead of `ok: false` with
  all-null evidence and no diagnosable cause. The original CLI error is
  preserved even when the record write itself fails.
- **Verify selects a Node runner for Node test targets.** Existing
  `.mjs`/`.cjs`/`.js` test files run under `node --test` while pytest remains
  the default for Python targets; a mixed or unrecognised target set refuses
  with `mixed_test_targets` instead of silently dropping a target, a missing
  Node runtime refuses with `node_runner_unavailable`, and the test evidence
  records `evidence.runner/test_argv` so the exact command is auditable.
- **Changelog coverage guard:** `tests/test_changelog_coverage.py` reads the
  merge history and fails a full-history run when a merged campaign PR since #43
  has no registry row, or when a registered row's keywords are absent from
  this Unreleased section; a non-feature merge still needs an explicit
  exclusion reason, and a checkout without merge commits falls back to the
  pinned snapshot set.

### Changed

- Verify records now include `evidence` (`source_rev`, `backend`,
  `exit_code`, `junit`, `diffstat`, `duration_s`).
- Branch and tests for verify come from the user or the lease, never from
  worker metadata.
- A missing optional backend capability returns `unsupported_operation`.

Tool count unchanged (4). Schemas: 571 -> 573 o200k (+2, dispatch
description).

### Fixed

- **Retry findings reach the bee (WA-A5B):** `bundlebees_dispatch` now attaches the accepted research findings for a gated retry to `brief.findings` before `client.create`, so the launcher renders the research that `require_findings` already required; a findings block supplied by the caller is preserved, and a brief with no `failure_id` or `retry_of` is dispatched unchanged.

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
