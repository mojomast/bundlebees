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

### Fixed

- Concurrent first dispatches on a fresh state directory could fail with
  `lease_invalid: the lease key is not 32 bytes`: one thread opened the key
  file after another created it but before the bytes were written (seen in
  the workflow cold-start acceptance on CI). The key is now written to a
  private temporary file and published with `os.link`, so readers only ever
  see a complete key. Regression test written first and seen failing.

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
