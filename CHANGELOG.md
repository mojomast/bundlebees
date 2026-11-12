# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- Connections to a user-configured launcher are pinned to a pre-resolved, policy-checked address so a later DNS lookup cannot steer the socket. Redirects are re-validated and re-pinned before the next hop.
- Private or LAN launcher URLs require `allow_private=true` in plugin settings (default `false`). The default `orbit-loopback` adapter stays loopback-only.
- The Desktop data-directory fallback uses `plugin_data_dir` / `get_hermes_home()` and does not read `HERMES_HOME` from the process environment.
- Declared `config_schema` covers the settings the client and CLI honor (`launcher_url`, `allow_private`, `orbit_url`, `worktree`, `branch`, `deploy_host_alias`).

[0.1.1]: https://github.com/mojomast/bundlebees/releases/tag/v0.1.1
[0.1.0]: https://github.com/mojomast/bundlebees/releases/tag/v0.1.0
