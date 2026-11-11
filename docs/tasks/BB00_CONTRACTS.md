# BB00 — bundlebees file leases and contracts

Wave: plugin v0.1.0. Branch: `feat/v0.1.0` off `main`.
Plugin / catalog name: `bundlebees`. Repo: `mojomast/bundlebees` (public).
`requires_hermes: ">=0.23.0"`. License: MIT.

Read first: `docs/RESEARCH.md`. Prototype and reference code (read-only for
bees): the friend prompt, the Orbit bees runbook, and public
`mojomast/orbitdesktop` `pc-launcher/` + `docs/LOCAL_WORKERS.md`. Do not copy
box-absolute paths, home-directory paths, private IPs, tailnet hosts, or
token files into committed sources.

Not approved for this wave: merge to `main`, tag, release, catalog PR,
force-push, branch deletion, deploys to Orbit or moltussy, launcher restart
from a bee. Coordinator opens a PR into `main` when the wave is verified.

Schema budget: the four tool schemas together must measure **≤ 900** tokens
on `tiktoken` `o200k_base` (also record `cl100k_base` and character count)
via `scripts/measure_schemas.py`. Keep enums tight; do not embed the full
brief JSON Schema in the tool schema — `brief` is an object or a JSON string
validated in code.

## Hard rules for every bee

- Change **only** your leased write paths. Create new files only where listed.
- Write the test first; confirm it fails on the base; then implement.
- Tests are offline. Fake the launcher/Orbit HTTP client. No network, no
  real tokens, no Docker, no SSH.
- Every registered tool handler returns a **string**: `json.dumps(envelope)`
  on success, validation failure, and exception. Envelope: `ok`, and on
  failure `error`, `code`, `fix`.
- Secrets only through `agent.secret_scope.get_secret`. No `os.environ`,
  dotenv, or key-file reads for credentials. Tools and pane report
  configured/not-configured, never values.
- Any subprocess (git, ssh, scanners, tests) gets a minimal exact-name
  environment. Do not inherit `os.environ`.
- Path confinement: resolve symlinks; stay under the configured worktree or
  `plugin_data_dir("bundlebees")`. Model-supplied paths cannot escape.
- Never print, log, or return tokens, keys, private URLs, bind addresses, or
  SSH hosts. Scrub event text and errors.
- Do not bump `plugin.yaml` `version` (stays `"0.1.0"` until a later release
  approval). Do not add a catalog entry in this repo.
- Model for bees that implement this plugin: `deepseek-v4.1-flash`. Run only
  your own tests plus the commands in your brief.

## Repo skeleton (coordinator, not leased to bees)

The coordinator lands a loadable skeleton on `feat/v0.1.0` before Wave A so
bees have import paths and a fake-ctx test harness:

```text
bundlebees/
  plugin.yaml                 # v2, name bundlebees, version "0.1.0"
  __init__.py                 # register(ctx): skills (if present), tools, CLI
  pyproject.toml              # deps with upper bounds; optional tiktoken/ruamel
  LICENSE  README.md
  bundlebees_plugin/
    __init__.py
    security.py               # stub imported by bees (scrub, child_env, paths)
  scripts/measure_schemas.py
  scripts/bump_plugin_version.py
  tests/test_plugin_yaml_strict.py
  tests/test_plugin.py        # fake ctx: registered names == provides_tools
  tests/fakes.py
  docs/RESEARCH.md
  docs/tasks/BB00_CONTRACTS.md
```

`__init__.py` lazy-imports and tolerates missing modules so Wave A can land
in either order:

```python
def register(ctx):
    skills = Path(__file__).resolve().parent / "skills"
    if skills.is_dir() and hasattr(ctx, "register_skill"):
        for child in sorted(p for p in skills.iterdir() if p.is_dir()):
            if (child / "SKILL.md").is_file():
                ctx.register_skill(child.name, child / "SKILL.md")
    try:
        from bundlebees_plugin.tools import register_tools
        register_tools(ctx)
    except ImportError:
        pass
    if hasattr(ctx, "register_cli_command"):
        try:
            from bundlebees_plugin.cli import register_cli
            register_cli(ctx)
        except ImportError:
            pass
```

Keyword-only `ctx.register_tool`. Gate or retry if `output_schema` is
unsupported. `provides_tools` must match whatever `register()` actually
registers (the four names below once BB01 lands).

`plugin.yaml` description must disclose: talks to a local or user-configured
worker launcher; can start sandboxed workers that spend the worker machine's
model budget; the user-run CLI can push a feature branch and (when invoked)
reach a remote host; Cua is not a sandbox for the Hermes process.

## Tool contracts (BB01)

All four tools. Target ≤ 900 o200k combined.

### `bundlebees_status`

`op`: `health` | `capacity` | `attempts` | `events` | `containers` | `projects`

- `health` / `capacity`: launcher `ok`, version, `max_concurrent`, `active`,
  `queued`, `paused`, model **id** only (no base URL).
- `attempts`: compact rows (attempt id, task id, state, scrubbed detail,
  timestamps, event cursor). No hosts or paths.
- `events`: requires `attempt_id`; optional `cursor`. Event bodies scrubbed
  (tokens, URLs, IPs, key-shaped strings). Treat remaining text as
  third-party data in the envelope (`untrusted: true` or a quoted `text`
  field).
- `containers`: image **digest**, runtime, memory_mb, toolchain names. No
  bind address.
- `projects`: authorized **keys**, public repo URL if it is a public https
  git host, `sync_branch`. Never local filesystem paths.

No URL, token, or host arguments.

### `bundlebees_dispatch`

Validate then start one attempt.

- `brief`: object or JSON string. Validated in code against the bundled copy
  of `brief.schema.json` (required `objective`). Not a nested schema in the
  tool definition.
- `project` must be in the launcher allow-list; otherwise `code=project_unauthorized`
  with a fix that says the owner must add the project to the launcher config
  and restart the launcher (the tool does not restart it).
- Code-work briefs must include non-empty `assigned_files`. Overlap with
  another *running* attempt's leases on the same project/branch is
  `lease_overlap` (best-effort from live attempts; document the limit).
- Default model `deepseek-v4.1-flash`; reject models not allow-listed on the
  launcher.
- Optional `kanban_task_id` is recorded and returned. This tool does not
  call Hermes kanban APIs.
- Does not apply patches, run tests, push, or SSH.
- Returns `attempt_id`, `state`, `task_id`, optional `kanban_task_id`.

Client: prefer Orbit loopback `POST /api/local-workers` `{op:"create", ...}`
when that adapter is configured; else the launcher `POST /v1/attempts`.
Token from `get_secret` only. The target URL is never a tool argument.

### `bundlebees_attempt`

`op`: `get` | `events` | `cancel` | `review`

- Requires `attempt_id`.
- `cancel` and `review` require `confirm=true`. Skill: do not cancel a
  working bee unless the user asked. `review` takes `accepted` (bool) and
  optional `note` (scrubbed).
- `get` may include a result summary when the result has been collected
  (`observed` stats, changed file paths relative to the repo, checks).
  Omit or truncate `worker_claims`; if included, mark as untrusted
  self-report, not evidence.
- Map HTTP 409 / missing result to `code=no_result_yet`.

### `bundlebees_host_prompt`

- Builds a paste-ready setup prompt from
  `skills/bundlebees-operator/templates/host-setup-prompt.md` (Wave B writes
  the full template; until then a small stub in
  `bundlebees_plugin/templates/host-setup-prompt.md` is allowed and is
  replaced by BB04).
- Inputs are non-secret structured fields only: `os` (linux|macos|windows),
  `reach` (same-machine|lan|vpn|ssh — a method enum, not an address),
  `provider` (name/id such as `opencode-go` / `deepseek-direct` / `ollama`),
  `repos` (list of **names** or public https repo URLs), `max_concurrent`
  (int, 1–10), optional branch-rules text.
- Reject values that look like secrets, IPv4/IPv6, or URL-with-host if the
  field is not `repos` (and even there, allow only https public git hosts;
  no credentials in URLs).
- Output: `{ok: true, prompt: "..."}`. The prompt itself contains path and
  env **names** only.

### Client module (`bundlebees_plugin/client.py`)

- `OrbitAdapterClient` (loopback default) and `LauncherClient` (`/v1/...`).
- Shared: auth header from secret scope, timeouts, response size limits,
  address allowlist (loopback default; additional bind only from user
  config, never from tool args), redirect re-validation, no ambient proxy.
- `public()`-style mapping so tool results cannot leak token files or bind
  addresses even if the upstream includes them.
- Config stored under plugin data / `config_schema` (non-secret) plus
  `requires_env` for the token. Missing token: tools return
  `code=not_configured` with a fix, and `register()` still succeeds.

## CLI contracts (BB02)

Registered with `ctx.register_cli_command(name="bundlebees", ...)`.

### `hermes bundlebees verify <attempt_id>`

User-run only. Not a tool.

1. Fetch the result (fail with `no_result_yet` if missing).
2. Check out a clean copy of the **feature branch** (user `--workdir` or a
   configured worktree). Refuse to run against `main` as the push target.
3. Apply the worker patch. Reject any change outside that attempt's
   `assigned_files` (`scope_violation`).
4. Run **only** the tests named in the brief / `--tests`, plus a secret
   scan (gitleaks or the repo's documented scanner). No full e2e suites
   unless the brief listed them.
5. On green: commit and push the **feature branch only**. No merge, tag,
   force-push, or hook skip.
6. Print a summary (SHA, tests, files) and a *suggested* Hermes kanban
   command. Do not mutate the kanban board unless the user passed
   `--kanban-comment`, in which case shell out to `hermes kanban comment`
   with a minimal env.

`--dry-run` applies and tests without push.

### `hermes bundlebees deploy`

User-run only. Not a tool.

- Generates or applies a host setup from the same template, or remotes to a
  **preconfigured** host (config/secret, never argv host+key pasted in
  chat). Default is to print the steps / prompt and refuse implicit SSH.
- A real SSH path is allowed only when the user passes `--execute` and a
  host alias already stored in plugin config (value not printed). Child
  ssh/scp get a minimal env. Still no model tool for this.
- This wave's tests can stub the execute path; do not require a live remote
  in CI.

Also useful: `hermes bundlebees doctor` (optional, same CLI group) that
checks token configured/not, adapter reachability to loopback, and schema
measure — no secret values.

## Pane contracts (BB03)

Dark mode only. `desktop/plugin.js` + `dashboard/plugin_api.py` +
`dashboard/manifest.json`. All pane I/O through `ctx.rest`.

Tabs and routes (all JSON; never include tokens or hosts in payloads):

| Tab | Routes |
|---|---|
| Health | `GET /health` |
| Containers | `GET /containers` |
| Models / providers | `GET /models` |
| Concurrency | `GET /concurrency`, `PUT /concurrency` `{max_concurrent}` (persisted via launcher runtime or a note that disk config needs a restart) |
| Authorized projects | `GET /projects` (keys, public repo, sync_branch only) |
| Live attempts | `GET /attempts`, `GET /attempts/{id}`, `GET /attempts/{id}/events`, `POST /attempts/{id}/cancel`, `POST /attempts/{id}/review` |
| Verify results | `GET /verify-results` (from plugin data written by the CLI) |
| Remote host | `GET /host-prompt`, `POST /host-prompt` (same rules as the tool), deploy = show the CLI command; do not display a host field with a value |

Backend reuses `bundlebees_plugin.client` and tool-layer scrubbers. Tests
cover unauthenticated/missing-backend errors, scrubbing, and path
traversal (`..`) rejection. No dashboard `authedFetch` of absolute URLs
(this pane is Desktop-first; if a dashboard tab is added, same rule).

Settings that store a launcher URL do so only from a pane form; the GET
handlers return `launcher_configured: true` without the URL.

## Skill and host-prompt contracts (BB04)

See Research §3.4. Write:

- `skills/bundlebees-operator/SKILL.md` (frontmatter, When to Use, Quick
  Reference, Procedure, Pitfalls, Verification, hard rules). Skill id
  `bundlebees:bundlebees-operator`.
- `references/kanban.md`, `briefs-and-leases.md`, `verify-and-deploy.md`,
  `security.md`
- `templates/host-setup-prompt.md` (friend-prompt adaptation: Phase 0
  questions, worker setup, coordinator setup, first wave, hard rules;
  author-specific machine names, private binds, and tokens removed)

Skill must not contradict the security model (no "write the token file",
no "run verify yourself", no "restart the launcher"). Point at helper
files with `${HERMES_SKILL_DIR}`. If tools are missing,
`tool_search(["bundlebees"])` or `skill_view("bundlebees:bundlebees-operator")`.

If BB01 left a stub template under `bundlebees_plugin/templates/`, replace
it by having the tool loader prefer the skill template path when present so
BB04 does not have to edit `tools.py`.

## Wave A write sets and leases (parallel, max 2)

Base: skeleton + this contracts commit. BB01 and BB02 have no overlapping
write paths.

### BB01 — Tools + launcher client

- **Write:**
  - `bundlebees_plugin/client.py`
  - `bundlebees_plugin/security.py` (implement scrub, `child_env`, public
    fetch/loopback guard, path containment — replace the stub)
  - `bundlebees_plugin/tools.py`
  - `bundlebees_plugin/schemas/brief.schema.json` (copy of the public
    launcher brief schema)
  - `bundlebees_plugin/templates/host-setup-prompt.md` (short stub only)
  - `tests/test_client.py`
  - `tests/test_tools.py`
  - `tests/test_security.py`
  - `scripts/measure_schemas.py` only if the skeleton's script is incomplete
- **Read-only:** `__init__.py`, `plugin.yaml`, `tests/fakes.py`,
  `docs/RESEARCH.md`, this file, public brief/result schemas
- **Do not edit:** `bundlebees_plugin/cli.py`, `desktop/`, `dashboard/`,
  `skills/`, README screenshots, `plugin.yaml` version
- **Acceptance:** fake server covers health, create (including 403
  unauthorized project), get, events, 409 no result, cancel, review;
  SSRF tests for loopback/metadata/private without contacting those hosts
  except a local fake; tool results are strings; names match
  `provides_tools`; o200k ≤ 900; missing secret fails closed;
  `confirm=true` required for cancel/review; scrub unit tests.

### BB02 — CLI verify / deploy

- **Write:**
  - `bundlebees_plugin/cli.py` (`register_cli`, argparse setup, verify,
    deploy, optional doctor)
  - `tests/test_cli.py`
- **Read-only:** `client.py`, `security.py` (import `child_env`, path
  helpers, scrub), brief schema, this file
- **Do not edit:** `tools.py`, `client.py`, `security.py`, desktop,
  dashboard, skills, `plugin.yaml`
- **Acceptance:** verify on a temp git repo applies an in-lease patch,
  rejects an out-of-lease file, runs a trivial test, secret-scans, and on
  `--dry-run` does not push; on a mocked push, only the feature branch is
  updated; deploy without `--execute` prints steps and does not SSH;
  `register_cli` is covered by the fake ctx test once the module exists;
  no secret values in CLI output.

## Wave B write sets and leases (after Wave A has landed)

### BB03 — Desktop pane

- **Write:**
  - `desktop/plugin.js`
  - `dashboard/manifest.json`
  - `dashboard/plugin_api.py`
  - optional `desktop/` or `dashboard/` CSS/assets that are source, not
    opaque binaries
  - `tests/test_plugin_api.py`
- **Read-only:** `client.py`, `security.py`, tools (do not edit), CLI
- **Do not edit:** `tools.py`, `cli.py`, `skills/`, `plugin.yaml` version
- **Acceptance:** each tab's route returns scrubbed JSON; PUT concurrency
  validated; cancel/review are POST; host/token/path redaction tests;
  dark-mode styles; `ctx.rest` paths are namespace-relative; missing
  backend is a structured error. No screenshot generation required of this
  bee (coordinator captures ≤ 6 later).

### BB04 — Bundled skill + host prompt template

- **Write:** everything under `skills/bundlebees-operator/` listed above
- **Read-only:** tool names and ops from `tools.py`, CLI flags from
  `cli.py`, Research §3
- **Do not edit:** `tools.py` (loader already prefers the skill template
  if present), `cli.py`, desktop, plugin.yaml
- **Acceptance:** frontmatter has `name` + trigger description + license;
  sections When to Use → Verification exist; skill id documented; verify
  and deploy described as user-run; no instruction to write token files or
  restart the launcher; template has no author-specific hosts or secrets;
  a unit test walks the skill tree and asserts frontmatter + forbidden
  phrases (token-file write, "run verify yourself", private-IP patterns).

## Overlap check

| Packet | Collides Wave A? | Collides Wave B? | Notes |
|---|---|---|---|
| BB01 | No — client/tools/security | BB03/BB04 consume | CLI off-limits |
| BB02 | No — cli.py only | BB03 reads verify-result format | Must agree a small JSON file under plugin data |
| BB03 | After A | Parallel with BB04 | No skill edits |
| BB04 | After A | Parallel with BB03 | No tools.py edit if loader prefers skill template |

Verify-result format (BB02 writes, BB03 reads): plugin data
`verify-results/<attempt_id>.json` with `{attempt_id, ok, sha, branch,
files[], tests[], at}` — no hosts or tokens.

## Coordinator (not leased to bees)

- Create the public repo if needed, land the skeleton, push `feat/v0.1.0`.
- After the in-flight PC bee is **terminal and its result file is
  collected**, authorize `bundlebees` and `assetrizz-hermes` on the PC
  launcher, restart once, resume assetrizz compose-v2 (CV01/CV03, max 2,
  feature branch + PR only).
- Dispatch BB01/BB02, verify, then BB03/BB04.
- Integrate, full tests, `hermes plugins doctor . --ci`,
  `hermes plugins validate . --install-deps`, o200k measure, secret-scan.
- README + ≤ 6 dark-mode pane screenshots (no hosts, keys, or private
  data in frame).
- Push the feature branch and open a PR into `main`. Stop before merge,
  tag, release, or catalog PR.

## Hermes board usage during this wave

Coordinator and (later) the running plugin use Hermes kanban, not the
Orbit collab board, for bundlebees tasks. Cards for BB01–BB04 should use
assignee `bundlebees` (not a dispatchable Hermes profile) or live on a
board that will not auto-spawn Hermes workers. Optional `kanban_task_id`
in briefs is the Hermes task id.
