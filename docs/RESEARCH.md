# bundlebees research (S0)

Written 11 Nov 2026, America/Toronto. This is the design record for
`mojomast/bundlebees`, a native Hermes plugin that teaches a Hermes agent to
set up and orchestrate CUA bees (worker launcher + sandboxes, briefs, leases,
verify, feature-branch-only pushes) and ships a dark-mode Desktop pane for
settings and live attempts.

Nothing in this file is a secret, a private host, a tailnet address, or a
token. Refer to credentials and endpoints by **env name or file path** only.

## 1. Sources (what was actually read)

| Source | What it supplied | Verified |
|---|---|---|
| Official skill authoring: `website/docs/developer-guide/creating-skills.md` in a local `hermes-agent` tree at `1ee91dfe070` (v0.23.0 + later canary) | Frontmatter, When to Use, progressive disclosure, helper files, `${HERMES_SKILL_DIR}`, secrets vs config | 11 Nov 2026 |
| Desktop SDK: `website/docs/developer-guide/desktop-plugin-sdk.md` (same tree) | `ctx.rest` / `ctx.socket`, `dashboard/plugin_api.py` mount, `requires_hermes: ">=0.23.0"`, broadcast events, no grant scraping | 11 Nov 2026 |
| Kanban: `website/docs/user-guide/features/kanban.md`, `tools/kanban_tools.py`, `tools/kanban_tools_schemas.py`, `hermes_cli/kanban.py` | Agent tools, CLI, dashboard REST under `/api/plugins/kanban/`, dispatcher behavior | 11 Nov 2026 |
| `hermes-plugin-development` skill + `references/catalog-review-lessons.md` | Catalog bars, Desktop `ctx.rest`, skill registration, CLI commands | loaded this turn |
| Bundled skills of reviewed plugins: `hermes-plugin-dev` (skill + `references/`), `assetrizz-composer`, Hermagotchi, stitchslop, Eye Of Hermes / godseye-analyst | Length, structure, failure codes, security wording | local checkouts |
| teknium1 reviews on catalog PRs [#152699](https://github.com/NousResearch/hermes-agent/pull/152699), [#152771](https://github.com/NousResearch/hermes-agent/pull/152771), [#149691](https://github.com/NousResearch/hermes-agent/pull/149691), [#150358](https://github.com/NousResearch/hermes-agent/pull/150358), [#151946](https://github.com/NousResearch/hermes-agent/pull/151946) | Recurring admission bars, including one skill-contradiction finding | review bodies, 8–11 Nov 2026 |
| Friend prompt: `/workspace/cua-bees-prompt/BEES-SETUP-PROMPT.md` | Agent-agnostic setup phases the host-prompt tool must generate | 11 Nov 2026 |
| `/workspace/emurizz-spec/ORBIT-BEES-RUNBOOK.md`, public `mojomast/orbitdesktop` `pc-launcher/` (`launcher.py` 0.1.9, `brief.schema.json`, `result.schema.json`, `scripts/*.ps1`) and `docs/LOCAL_WORKERS.md` | Launcher HTTP API, brief/result contracts, authorize-then-restart, verify loop | 11 Nov 2026 |

Canonical upstream plugin docs still win if they disagree with this note:
`website/docs/developer-guide/plugins/index.md`, `plugin-catalog/README.md`,
`website/docs/developer-guide/plugins/catalog-submission.md`.

## 2. Approved product shape

- Plugin id / catalog name: `bundlebees`. Repo: `mojomast/bundlebees` (public).
- `requires_hermes: ">=0.23.0"`. Dark mode only for the pane and any screenshots.
- Four model-facing tools, combined schema target **≤ 900 o200k tokens**:
  - `bundlebees_status` — launcher health, capacity, live attempts and events
  - `bundlebees_dispatch` — validate a brief, enforce the authorized-project
    allow-list and file leases, start a bee
  - `bundlebees_attempt` — get, events, cancel, or review one attempt
  - `bundlebees_host_prompt` — paste-ready setup prompt for an agent on a
    remote host (adapted from the friend prompt)
- User-run CLI only (not model tools): `hermes bundlebees verify` and
  `hermes bundlebees deploy`. These apply a patch / run tests / push a feature
  branch, or deploy to a host — they execute worker output or reach a machine,
  so they must not be model-callable (teknium #152699).
- Board: **Hermes' own kanban**, not the Orbit collab board.
- Desktop pane via `ctx.rest`: Health, Containers, Models/providers,
  Concurrency, Authorized projects, Live attempts, Verify results, Remote host
  (prompt generator + deploy). Secrets only via `get_secret`. The pane never
  shows tokens or host addresses.
- Catalog bars: no unscannable blobs, minimal child envs, path confinement,
  public-address (or explicit loopback) pinned fetches, honest disclosure,
  YAML-only catalog PR pinned to a release when Kyle later says yes. At most
  six dark-mode screenshots. o200k measured.

Not approved in this wave: merge, tag, release, catalog PR, force-push, branch
deletion, or deploys to Orbit / moltussy.

## 3. Hermes skill best practice (for this plugin)

Kyle asked that the bundled skill follow current best practice, researched in
the context of bundlebees — not a generic essay.

### 3.1 Official authoring rules

From `creating-skills.md`:

- **Frontmatter is required.** At minimum `name` and a trigger-oriented
  `description` (this is what skill search and the system prompt see). Add
  `license: MIT`. Optional `metadata.hermes.tags` and `related_skills`. Do
  **not** set `requires_tools` to the four plugin tools: Tool Search may defer
  them, and a hard require would hide the skill in the session that most needs
  it to call `tool_search` / `skill_view`.
- **Standard sections, in this order:** When to Use, Quick Reference,
  Procedure, Pitfalls, Verification. That is the official skeleton; use it.
- **Progressive disclosure.** Common workflow first (status → slice on the
  Hermes board → dispatch two leased bees → wait → ask the user to run
  verify → open a PR). Edge cases (stuck bee, unauthorized project, host
  setup, deploy) go further down or in `references/`.
- **Helper files, not a novel in SKILL.md.** Official guidance: put parsers
  and long reference material in `scripts/` and `references/`, and point at
  them with `${HERMES_SKILL_DIR}`. The activation message also prints
  `[Skill directory: /abs/path]`. Keep the main file short enough that loading
  it is cheap; this plugin should land around **800–1,500 words** in
  `SKILL.md`, with depth in helpers (the pattern `hermes-plugin-development`
  uses for a field guide, and that `assetrizz-composer` shows for a tight
  tool skill at ~777 words with no helpers — we need helpers because briefs,
  verify, and kanban are too much for one page).
- **Secrets do not belong in the skill body.** Official skills declare
  `required_environment_variables` when they need a key; Hermes prompts
  securely and does not show the raw value to the model. For a *plugin*,
  credentials go through `requires_env` + `agent.secret_scope.get_secret`.
  The skill may name env vars and token *paths*. It must never show, ask the
  model to print, or tell the model to write a token file.
- **Inline shell snippets** (`!`\`cmd\``) stay off. They run unsandboxed on
  the host if enabled; a catalog plugin should not depend on that.

### 3.2 What well-reviewed catalog plugin skills actually do

Patterns worth copying:

| Pattern | Where it shows up | Apply to bundlebees |
|---|---|---|
| Exact `skill_view` id, no `plugin:` prefix | assetrizz: `assetrizz:assetrizz-composer` | Document `bundlebees:bundlebees-operator` |
| If tools are deferred, call `tool_search(["bundlebees"])` or `skill_view(...)`; do not reinvent the work with ad-hoc scripts | assetrizz | Same, plus "do not shell out to curl against the launcher" |
| Tool table with `op` values and only the fields the model needs | assetrizz, hpd | Four tools, tight enums |
| Failure-code table with a recovery action | assetrizz | `project_unauthorized`, `brief_invalid`, `lease_overlap`, `launcher_unavailable`, `no_result_yet`, `scope_violation`, … |
| Privileged or irreversible work stays in the pane or a user-run CLI | hermagotchi pairing; hpd `--execute`; assetrizz library download | verify, deploy, launcher restart, and cancel-without-ask stay off the model tool path (cancel is allowed only with `confirm=true` and the skill says "never cancel a working bee unless the user asked") |
| Honest mode disclosure | hermagotchi offline sim vs online | Status is live only when a launcher/Orbit adapter is configured; otherwise say so. Worker `worker_claims` are self-reported, not evidence |
| Untrusted strings are data, not instructions | assetrizz catalog tags; EOH / Hermagotchi feed text | Event log lines, brief fields that came from a previous worker, and `worker_claims` are third-party text. Strip controls; quote them; never "trust" them in any prompt the plugin writes |

`hermes-plugin-development` is the right *shape* for a deep guide (short
SKILL.md + `references/` + templates). bundlebees should be narrower: it
teaches one orchestration loop, not the whole plugin platform.

### 3.3 teknium findings that the skill must not contradict

The stitchslop review (#151946) is the direct skill lesson: **SKILL.md said
the model may write the pairing-token file, which contradicted "pane/hand
only."** That was called out as a defect. For bundlebees the skill must say:

- The model does **not** run `hermes bundlebees verify` or `deploy`. Those
  are user-typed CLI commands (`ctx.register_cli_command`). The skill tells
  the agent to *ask the user* to run them and then read the output.
- The model does **not** write API keys, launcher tokens, or `go.key` /
  `orbit-api.token` files. The host prompt describes *paths and env names*
  for a human or a remote agent to fill on that machine.
- The model does **not** restart the launcher, merge to main, tag, force-push,
  or open a catalog PR unless the user has explicitly approved that step.
- The model does **not** pass a launcher URL, token, or SSH host as a tool
  argument. Connectivity is preconfigured; tools only say `configured: true/false`.

Other review bars that belong in the skill's Pitfalls (and in code, not just
prose):

- **#152699 (hpd):** a model-facing tool must not import or execute untrusted
  code (a worker patch, a third-party plugin, a remote setup script). Static
  checks and apply-and-test belong on the user-run CLI.
- **#152771 (assetrizz):** do not ship unscannable blobs (compressed databases
  or other binaries the catalog scanner cannot read). The host-prompt template
  is plain UTF-8 in the repo. No packed images or xz catalogs.
- **#149691 (EOH):** secrets only via `get_secret`; no `os.environ` / dotenv
  fallback; minimal child environments; no undocumented host internals;
  profile-aware `plugin_data_dir` / `get_hermes_home()`; untrusted feed text
  is not "trusted" in a system prompt.
- **#150358 (Hermagotchi):** tools return `json.dumps(...)` strings; Desktop
  uses `ctx.rest` (floor 0.23.0); never hand an absolute `http(s):` URL to
  dashboard `authedFetch`; privileged account or machine actions stay a human
  gesture (here: deploy, review-accept if we treat it as merge-adjacent,
  launcher restart).
- **#151946 (stitchslop):** any loopback relay is token-gated on every
  sensitive route, bound to `127.0.0.1` by default, with Host checks; we
  should not add a viewer/relay at all if we can talk to Orbit on loopback
  or to a preconfigured launcher. Skills must match that.

### 3.4 Skill outline we will ship

Path: `skills/bundlebees-operator/SKILL.md`

```text
skills/bundlebees-operator/
  SKILL.md                         # when-to-use, 4 tools, 2 CLIs, loop, hard rules
  references/kanban.md             # Hermes board only; do not assign dispatchable profiles
  references/briefs-and-leases.md  # brief.schema fields, lease rules, budgets
  references/verify-and-deploy.md  # exact CLI the user types; what verify checks
  references/security.md           # secrets, hosts, untrusted worker text, no launcher restart
  templates/host-setup-prompt.md   # source for bundlebees_host_prompt
```

Frontmatter description (draft):

> Set up and orchestrate CUA bees with the bundlebees plugin: check launcher
> health, slice work into leased Hermes kanban tasks, dispatch briefs,
> watch attempts, and ask the user to run verify/deploy. Use when the user
> wants cheap parallel coding workers in disposable sandboxes, a worker
> launcher on a host, or a paste-ready bees setup prompt.

## 4. Hermes kanban (use this, not the Orbit collab board)

Orbit's `/api/collab-board` and `~/bin/collab-board` are the old coordinator
path in the Emurizz runbook. **bundlebees must not create or update Orbit
board cards.** Tracking lives on Hermes' built-in kanban.

Agent-facing tools (toolset `kanban`, already in Hermes — do not reimplement):

`kanban_create`, `kanban_list`, `kanban_show`, `kanban_complete`,
`kanban_block`, `kanban_schedule`, `kanban_request_review`,
`kanban_request_changes`, `kanban_heartbeat`, `kanban_comment`,
`kanban_attach`, `kanban_attach_url`, `kanban_attachments`,
`kanban_unblock`, `kanban_link`.

CLI: `hermes kanban init|create|list|show|...` and `hermes kanban boards ...`.
Dashboard REST (Hermes' own kanban plugin, not ours) is under
`/api/plugins/kanban/`.

**Do not import `hermes_cli.kanban_db` or other undocumented core modules**
(EOH review: drop `tools.desktop_ui`-style internals). The orchestrating
agent uses the public `kanban_*` tools. bundlebees tools may accept an
optional `kanban_task_id` and return it on dispatch/status so the agent can
comment or complete the card itself.

**Dispatcher collision (critical):** `kanban_create` is also how Hermes
spawns *Hermes profile workers*. A card assigned to a real installed
profile will be picked up by the gateway dispatcher. Bees would then race
that worker. Contract:

- Use a dedicated board (e.g. `hermes kanban boards create bundlebees`)
  and a non-profile assignee such as `bundlebees` that is **not** an
  installed Hermes profile, **or** leave auto-dispatch off for that board.
- The skill must say this up front. Do not assign `coder` / `researcher`
  unless the user explicitly wants the Hermes dispatcher instead of bees.
- `parents` / `kanban_link` express dependencies the way file-lease waves
  do (`todo` until parents are `done`, then `ready`).

Verify CLI, by default, does not mutate the board. It may print a suggested
`hermes kanban request-review <id>` line. An opt-in `--kanban-comment` that
the *user* passes is allowed; it must use a public CLI (`hermes kanban
comment`) as a subprocess with a minimal env, not a private import.

## 5. Desktop pane and `ctx.rest`

- Ship `desktop/plugin.js` (bundled, SDK-only: `@hermes/plugin-sdk`). No
  prototype patching, no `eval`, no `window.hermesDesktop` grant scraping, no
  CDN React, no absolute `http(s):` URLs to `authedFetch`.
- Backend: `dashboard/manifest.json` + `dashboard/plugin_api.py` exporting
  `router = APIRouter()`. Routes mount at `/api/plugins/bundlebees/`. The
  pane calls `ctx.rest('/health')`, `ctx.rest('/action', { method: 'POST',
  body })`. Path traversal is rejected; the namespace is plugin-scoped.
- The Python backend is imported only when the plugin is in
  `plugins.enabled` (separate from the Desktop enable toggle). The pane must
  degrade when the backend is off (`ctx.rest` errors, not a crash).
- Prefer `broadcast_plugin_event("bundlebees", "attempts.updated", ...)` plus
  a polling fallback. `ctx.socket` is an accelerator and is a no-op on some
  remotes.
- Load any images through JSON/`ctx.rest`, blob URLs, revoke when done.
- Dark mode only. Never render token values, launcher bind addresses, VPN
  hosts, SSH hosts, or home-directory paths. Show project *keys*, public
  repo URLs, `sync_branch`, model *ids*, image digests, memory, runtime,
  concurrency, attempt ids and states. A setting can be `configured: yes/no`.
- `requires_hermes: ">=0.23.0"` in `plugin.yaml` (and later in the catalog
  entry, when authorized).

## 6. Launcher and coordinator APIs (reference implementation)

Public code: `mojomast/orbitdesktop` `pc-launcher/`. Live launcher version
observed in this tree: **0.1.9**.

HTTP (all authenticated; token from a user-only file on the worker machine):

| Method | Path | Role |
|---|---|---|
| GET | `/v1/health` | `{ok, version, max_concurrent, active, queued, model}` |
| GET | `/v1/attempts/:id` | Public attempt record |
| GET | `/v1/attempts/:id/events?cursor=` | Events since cursor |
| GET | `/v1/attempts/:id/result` | 200 result, or 409 `no result yet` |
| POST | `/v1/attempts` | Create; 403 if `brief.project` is not in `config.projects` |
| POST | `/v1/attempts/:id/cancel` | Cancel |
| POST | `/v1/config` | Runtime `max_concurrent` only; **reverts on restart**; disk config is the source of truth |

`config.json` is read **at startup**. Authorizing a new project or changing
images / default model requires `stop.ps1` then `start.ps1`, and only with
the owner's OK, never while a wanted attempt is still running. Runtime
`max_concurrent` is the exception and is not persisted.

Brief (`brief.schema.json`): required `objective`; important fields
`project`, `branch`, `source_rev`, `assigned_files`, `commands`,
`acceptance`, `deliverables`, `budget.timeout_s` / `max_tool_calls`,
`deps`. Result (`result.schema.json`): `observed` is evidence (paths,
sha256, in-scope flags, usage); `worker_claims` is not.

On the Orbit box, the same token-gated API is proxied as
`POST http://127.0.0.1:4318/api/local-workers` with
`op=create|list|get|cancel|review|tick|archive`. That loopback adapter is
the preferred client target when Hermes is running on the same host as
Orbit. The plugin must not print the launcher URL or the token. Direct
launcher access, if configured, uses a URL the *user* set in the pane or
config — never a model-supplied URL — and is subject to the SSRF /
loopback-or-explicit-private-bind guard.

Worker model keys stay on the worker machine (env name
`ORBIT_WORKER_GO_KEY` or a user-only key file). Hermes only holds the
adapter/launcher API token, via `get_secret`.

Default model id for this project: `deepseek-v4.1-flash`.

## 7. Catalog security applied to this design

- **No model-executed untrusted code.** verify (apply patch, run tests,
  git push) and deploy (SSH/remote) are CLI. `bundlebees_dispatch` only
  submits a JSON brief.
- **No unscannable blobs.** Prompt template and schemas are UTF-8.
  Desktop JS is committed source + lockfile if bundled.
- **Secrets:** declare token (and any deploy key) in `requires_env`;
  `get_secret` only; tools and pane report configured/not; scrub secrets
  from errors and event text.
- **Child env:** git, ssh, scanners, and tests launched by the CLI get an
  exact-name allowlist, not a copied `os.environ`.
- **Path confinement:** verify only writes inside a user-configured
  worktree; reject assigned_files escapes and symlink escapes. State in
  `plugin_data_dir("bundlebees")`.
- **Fetches:** any HTTP client (launcher URL, pinned reference fetches)
  resolves addresses, refuses unexpected private/metadata/link-local
  targets, pins the connection, and re-validates redirects. Loopback to
  Orbit on `127.0.0.1` is the documented default. A user-configured
  private bind is explicit pane/config, not a tool argument.
- **Tool results:** every registered handler returns `json.dumps(envelope)`.
- **Honest disclosure** in `plugin.yaml` description and README: the
  plugin talks to a local or user-configured worker launcher, can start
  paid-model sandboxes on that machine, and the CLI can push a feature
  branch or (when the user runs deploy) reach a remote host. Cua is not
  an OS sandbox for the Hermes host process.

## 8. Implications for tools, CLI, and pane

See `docs/tasks/BB00_CONTRACTS.md` for file leases and acceptance tests.
Summary:

- Prefer the Orbit loopback adapter when available; fall back to a
  user-configured launcher client. One `bundlebees_plugin/client.py`.
- Four composable tools with `op` enums; no URL/token/host parameters.
- `bundlebees_host_prompt` fills `templates/host-setup-prompt.md` (from
  the friend prompt, with author-specific hosts removed) from structured
  non-secret answers (OS, reach *method* as an enum, provider *name*,
  repo names, concurrency, branch rules). Reject inputs that look like
  secrets or addresses.
- Pane tabs map 1:1 to REST routes under `/api/plugins/bundlebees/`.
  Deploy in the pane is a user gesture that either copies the CLI command
  or calls the same code path as the CLI — never a model tool.

## 9. Open points already decided

- Wait for in-flight PC bees (including result collection) before any
  launcher restart or new dispatch. This research does not depend on that.
- DeepSeek Flash bees, two waves of two, then coordinator integration.
- Catalog PR and release wait for a later explicit yes.
