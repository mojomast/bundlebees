# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/mojomast/bundlebees/releases/tag/v0.1.0
