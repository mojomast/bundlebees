# The rizzstack doctor

`hermes bundlebees doctor` is the authoritative stack parity and health
diagnostic. It replaces the coordinator's manual pre-campaign investigation
with one read-only command: what revision each stack component is at, whether
the installed and loaded copies match what was reviewed, whether the
executables, sandbox, pinned sources and credentials a real campaign needs are
present, and what exact safe step fixes each gap. It never mutates the
environment and never prints a credential value.

## Running it

```text
hermes bundlebees doctor                  # human report, exit 0
hermes bundlebees doctor --json           # machine-readable report (same facts)
hermes bundlebees doctor --check-parity   # exit 1 when parity is broken
hermes bundlebees doctor --state-dir DIR  # plugin data directory override
```

`--json` and `--check-parity` combine. Without `--check-parity` the command is
pure reporting and exits 0 (the historical behavior); with it, the exit code
is the parity verdict described below.

## The six states

Every probe reports exactly one state; states are never folded and missing
evidence is never read as health:

| State | Meaning |
|---|---|
| `healthy` | detected and in parity with the reviewed revision |
| `stale` | detected and different from the reviewed revision |
| `unavailable` | not present here (not configured, or not applicable to this repo) |
| `inaccessible` | present but could not be read (permissions, transport failure) |
| `unknown` | present but the value could not be determined (best-effort probe exhausted) |
| `incompatible` | detected and violating a known contract (version floor, address policy) |

## Repo rows and aspects

Four stack repos are reported, each with up to five revision aspects. Aspects
that do not apply to a repo are reported as `unavailable` with a
`not applicable` detail, so the shape of the report is stable:

| Repo | checkout | origin main | installed package | gateway loaded | deployed runtime |
|---|---|---|---|---|---|
| `bundlebees` | the plugin checkout (parent of the loaded `bundlebees_plugin` package) | `origin/main` (then `origin/HEAD`, `origin/master`) | installed distribution version vs the version at reviewed main (`git show`) | the plugin the Hermes gateway loaded (read-only config probe, best effort) | not applicable (worker hosts are reported under `launcher`) |
| `workflow` | the `WORKFLOW_SRC` checkout vs the reviewed CI pin | origin main of that checkout | not applicable | not applicable | not applicable |
| `hyvv` | the `HYVV_SRC` checkout vs the reviewed CI pin | origin main of that checkout | not applicable | not applicable | not applicable |
| `launcher` | not applicable (a deployed release, no pinned checkout) | not applicable | not applicable | not applicable | the launcher version from its health endpoint vs the reviewed release pin |

The reviewed reference per repo follows the repository's own review model:
bundlebees is reviewed through `main` (so origin/main is the reference), the
protocol sources through their CI pins (see [pinned-sources.md](pinned-sources.md)
and [stack-manifest.md](stack-manifest.md)), and the launcher through its
pinned release. Gateway-loaded detection is best effort by design: when the
gateway configuration is not readable, the row says `unknown` and names why,
never a guess.

## Health checks

| Check | What it verifies |
|---|---|
| `credential` | PRESENCE of the launcher credential through Hermes secret scope. The value is never read into a printable form. |
| `sandbox` | a usable isolation backend (bwrap, firejail; unshare only with the explicit weak override). Weak or disabled isolation is `incompatible`, not healthy. |
| `schema_tokens` | the four tool schemas measured against the 900 o200k budget. |
| `gitleaks` | a resolvable, executable secret scanner (setting, `GITLEAKS`, legacy queue setting, then PATH). |
| `git_executable` | git on PATH. |
| `python_coordinator` | the coordinator interpreter. |
| `python_verify_runner` | the configured `verify_python` runner: absolute, existing, executable; empty means the coordinator interpreter. |
| `pinned_workflow` / `pinned_hyvv` | `WORKFLOW_SRC` / `HYVV_SRC` set, present, a git checkout, and at the reviewed pin. |
| `pinned_gitleaks` | the `GITLEAKS` export points at an executable scanner (or the scanner still resolves, reported as such). |
| `contract_enforcement` | whether the ACDD dispatch gate (`require_contract`) is on; off is `unavailable` with the opt-in fix, never folded into healthy. |
| `usage_accounting` | the append-only outcome log is readable and has recorded runs. |
| `observatory` | reachability of the endpoint named by the optional `observatory_url` plugin setting (one bounded GET, redirects refused, the endpoint value never displayed). Unset is `unavailable`; an endpoint the address policy refuses is `incompatible`. |
| `hermes_api_floor` | the installed Hermes version against the plugin manifest floor (`requires_hermes`, `api_version`). Below the floor is `incompatible` and actionable. |
| `launcher_identity` | the running launcher's exact build identity in three evidence tiers — receipt-backed, version-string-only (always flagged: a version string is not deployment evidence), unavailable — plus a read-only classification of `GET /v1/provision/capabilities` through the already-authenticated launcher client. A capabilities outcome never upgrades the identity tier. |

## The launcher identity check

The Nov 14–15 worker-update saga mistook version strings for deployment
evidence. This check names that drift class explicitly: what the launcher
claims in a version string is a claim, and only a deployment receipt is
evidence about what is actually running.

### Identity evidence tiers

| Tier | Evidence | Row state |
|---|---|---|
| receipt-backed | the health/admin payload carries a validated `deployment_receipt` | judged against the reviewed release pin: `healthy` on match, `stale` on mismatch |
| version-string-only | only a health `version` string, no usable receipt | always `unknown`, always flagged: "a version string is not deployment evidence", with the confirm-through-receipt fix |
| unavailable | no adapter configured, health unreachable, or no identity field in the answer | `unavailable` (nothing configured), `inaccessible` (no answer), or `unknown` (answered, no usable identity) |

A receipt counts as evidence only when it is an object carrying a non-empty
`deployed_at` and a validated build identity — a dotted `version`, or a
40/64-character hex `rev`, `revision` or `source_rev`. A receipt that names a
hex revision is not comparable to the dotted release pin and reports
`unknown`; a receipt whose build identity disagrees with the runtime's own
version string reports `unknown` (the evidence disagrees) rather than picking
a side.

### The capabilities probe

One read-only `GET /v1/provision/capabilities` (the path constant lives in
`provisioning.py`) through the already-authenticated launcher client — the
same channel every other launcher read uses, with the token read from Hermes
secret scope inside the client. Classified exactly one of:

| Classification | Meaning |
|---|---|
| `ok` | 200 with a parseable `rizzstack.provisioning/1` advertisement, provisioning enabled |
| `disabled` | the advertisement parses but says provisioning is disabled |
| `incompatible` | the advertisement violates the known contract (foreign schema, unusable profile list) |
| `not advertised` | the launcher answered 404 |
| `refused` | the launcher answered 401 or 403 to the authorized read |
| `answered N` | the launcher answered another HTTP status |
| `unreadable` | no authenticated client, no credential configured, or an unparseable response |

The classification is appended to the row's detail as report text and never
changes the identity tier: `ok` capabilities cannot make an unproven identity
healthy, and an `incompatible` advertisement cannot make receipt-backed
evidence stale.

### Read-path and no-unpause guarantees

- exactly two launcher reads, both GETs through the authenticated client (the
  health read and the capabilities GET); no request body, no subprocess, no
  curl-with-credentials in any argv, no secret value in any output — pinned
  by a test that records the exact request list and refuses any subprocess;
- the launcher is never restarted, edited or redeployed by this check, and
  dispatch pause state is never touched: the doctor has no write path to it.
  (Recorded gap: the no-unpause rule is not written anywhere in this repo —
  a RULES.md with the referenced section 4 does not exist here — so the rule
  is enforced structurally, by the absence of any write path, and pinned by
  the exact-request-list test.)

### Recorded gap (deliberately not fixed here)

`client.public_health` whitelists health-payload keys, so a real launcher's
deployment receipt is stripped before the doctor sees it; `client.py` is
outside this check's file scope. Until the client mapping learns the
`deployment_receipt` key, a real deployment reports version-string-only and is
flagged — which is the honest state, not a malfunction. A test in
`tests/test_doctor_launcher_identity.py` pins the stripping behavior so the
future client change flips that test consciously.

## Parity semantics

`--check-parity` exits nonzero exactly when an **installed**, **gateway
loaded** or **deployed runtime** revision is `stale` or `incompatible` — that
is, when a copy of the stack that actually executes diverges from what was
reviewed. A diverging *checkout* alone does not fail parity: the coordinator
works on feature branches by design, and the checkout row still reports the
divergence explicitly. `unknown`, `unavailable` and `inaccessible` rows never
flip the verdict — divergence must be proven, not assumed — but they are
always visible in the report.

## The legacy credential-state line

The first block of the human output preserves the historical doctor lines
byte for byte (header, credential state, adapter reachability, sandbox,
schema tokens). The credential line is composed, never declared as a literal:
a private label constant holds the bare word and a line-builder helper joins
the label, a colon, a space and the state suffix. Five forms exist, selected
by the probe outcome:

| Probe outcome | Emitted form |
|---|---|
| present | the legacy configured form |
| absent | the legacy not-configured form |
| probe failed | the presence-uncheckable form |
| configured, with an untrusted annotation | the redacted-configured form (annotation withheld, marker shown) |
| any other probe result | the redacted-marker form (the value is never printed) |

Tests assert through the imported helper and constants, so the format is
pinned without any test line carrying the literal shape.

## JSON report

`--json` emits one object with stable field names:

```json
{
  "schema": "bundlebees.doctor/1",
  "generated_at": "2026-11-16T12:00:00Z",
  "parity_ok": true,
  "repos": [
    {
      "repo": "bundlebees",
      "aspects": {
        "checkout": {"state": "healthy", "detail": "at the reviewed revision",
                     "fix": "", "rev": "1f8378f7a226c0fbdbd77fc5f48f46f3a4820993"}
      }
    }
  ],
  "checks": [
    {"check": "gitleaks", "state": "healthy", "detail": "resolved from PATH", "fix": "", "rev": null}
  ],
  "remediation": []
}
```

Each repo carries all five aspects (`checkout`, `origin_main`,
`installed_package`, `gateway_loaded`, `deployed_runtime`); each row has
`state`, `detail`, `fix` and `rev`. Revisions are validated (40-character hex
digests or dotted versions) and printed without scrubbing — a digest is
evidence, not a secret-shaped string. All other free text is scrubbed once at
creation. The legacy rendering lines are excluded from the JSON payload.

## Remediation

Every non-healthy row that names a fix appears once under `remediation`,
prefixed by its row label. The fixes are exact and safe: update through the
reviewed update process, restart the gateway at a quiet boundary, redeploy the
reviewed launcher release (never restart a busy launcher), run the
setup-pinned-sources script (which never resets or discards local changes),
install the missing executable. No remediation step deletes, force-pushes,
resets, or restarts anything by itself; the doctor performs none of them.

## Read-only guarantees

- git access is limited to `rev-parse` and `show` through a scrubbed,
  minimal child environment; nothing is fetched, checked out or written;
- installed-package metadata is read through `importlib.metadata`;
- the gateway probe reads the Hermes configuration through the documented
  read-only loader and, when it names a plugin path, reads that
  `plugin.yaml`;
- the outcome log is read with the existing metrics readers, which never
  create or rewrite state;
- the only network access is one bounded GET to an explicitly configured
  observatory endpoint, with redirects refused and ambient proxies stripped,
  plus the launcher-identity check's two authorized launcher reads (health and
  `GET /v1/provision/capabilities`) through the already-authenticated client —
  no subprocess, no request body, and no credential ever enters an argument
  vector or the report;
- credential checks answer presence only; no secret value is ever read into
  a printable form, and any unexpected probe result collapses to the
  redaction marker.

## Test injection

`doctor.default_checkers(...)` builds the real probe set; every seam (git
runner, metadata, PATH lookup, environment reader, gateway configuration
loader, observatory GET, Hermes version, metrics reader, clock) is an
optional keyword argument, and every probe is a replaceable field that takes
the run context and returns one `Finding`. A test context can inject a whole
probe set through the `bundlebees_doctor_checkers` attribute, the same way
`bundlebees_client` and the credential-presence injection work elsewhere in
the plugin. The launcher-identity probe takes its launcher reads from the
same `bundlebees_client` injection as the deployed-runtime probe, so fixture
launcher states are plain client fakes.
