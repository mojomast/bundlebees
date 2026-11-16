# Pinned sources

The runtime runs a fixed protocol revision, not whatever is on a branch tip
(see [Protocol pinning](../README.md#protocol-pinning) and the
[stack manifest](stack-manifest.md)). Three environment variables point the
suites and the driver at that revision; when any of them is missing, the
M4/M4.3 end-to-end suites fail closed rather than pretend to pass:

| Variable | What it is for | Required by |
|---|---|---|
| `WORKFLOW_SRC` | checkout of `mojomast/workflow` holding the `rizzstack.recovery/1` validator (`tools/recovery.py`), its schema (`schemas/recovery.schema.json`), the feature-packet and RETURN validators and the landing schemas. It **is** the pin: recovery records the exact commit the checkout is at. | packet admission, recovery, bee events, the M4 dogfood suite |
| `HYVV_SRC` | checkout of `mojomast/hyvv` holding the governed provisioning contract (`provisioning.py`), which the real `Provisioner` is loaded from. | provisioning, the queue/factory e2e suites, M4/M4.3 |
| `GITLEAKS` | the secret-scanner executable a real tick must run against attempts. | `factory_driver` ticks, the M4.3 wake-up suite (fails, never skips, when unset) |

`REQUIRE_WORKFLOW=1` and `REQUIRE_HYVV=1` turn a missing or unreadable source
into a test/operation failure instead of a skip; CI sets both.

## One-command setup

```bash
sh scripts/setup-pinned-sources.sh
```

The script clones/updates both pinned checkouts under `~/.rizzstack/pinned/`,
verifies the marker files exist (`workflow`: `tools/recovery.py` and
`schemas/recovery.schema.json`; `hyvv`: `provisioning.py`), and makes gitleaks
available. stdout carries exactly the three export lines; everything else goes
to stderr:

```console
$ sh scripts/setup-pinned-sources.sh
export WORKFLOW_SRC="/home/you/.rizzstack/pinned/workflow"
export HYVV_SRC="/home/you/.rizzstack/pinned/hyvv"
export GITLEAKS="/home/you/.rizzstack/pinned/bin/gitleaks"
```

Wire them straight into the current shell:

```bash
eval "$(sh scripts/setup-pinned-sources.sh)"
REQUIRE_WORKFLOW=1 REQUIRE_HYVV=1 python -m pytest -rs
```

Set `RIZZSTACK_PINNED_DIR` to place the checkouts somewhere else (it must not
contain `"`, `` ` `` or `$`; the script refuses such paths because it could not
emit a safe export line).

What the script does and does not do:

- It clones at the revisions CI pins (`workflow` `9c69d3a987ea9544a77cdd9b5a259904eb36f006`
  on `feat/bee-landed`, `hyvv` `8ffaf73000974e0ba273c72fca42e07b79c38106` on
  `feat/bee-landed-events`) and verifies each checkout's HEAD against that pin
  and the marker files. A checkout already at the pin is left untouched and no
  network round trip is made.
- It never deletes, resets, cleans or force-pushes anything. When it cannot
  move an existing checkout to the pinned revision (for example because of
  conflicting local changes), it exits non-zero and tells you what to do
  yourself; it never discards your state.
- It never prints secret material: remote URLs are echoed only with any
  userinfo redacted, and nothing from the environment is dumped.
- If gitleaks is already exported, on `PATH`, or previously installed under
  `pinned/bin/`, it is recorded and reused. Otherwise the script downloads the
  v8.28.0 release CI uses (linux x64/arm64, darwin x64/arm64) into
  `pinned/bin/gitleaks` and verifies the tarball against a pinned sha256
  before trusting it. If nothing can be provided, it exits non-zero with the
  install instructions — printing a `GITLEAKS` line that points at nothing
  would recreate the blocked state it exists to prevent.
- Any clone failure exits non-zero with the sanitized git error and a fix.
  Re-running the script is always safe.

## Making the exports persistent

Append the three lines to your shell profile (`~/.profile`, `~/.bashrc`,
`~/.zshrc`) or let direnv manage them per checkout:

```bash
# ~/.profile
eval "$(sh /path/to/bundlebees/scripts/setup-pinned-sources.sh)"
```

The script is idempotent and re-resolves on every login, so a moved pin is
picked up at the next shell; keeping the checkout itself pinned is a
deliberate, reviewed act (below). For long-running operators (the
`systemd` unit pattern in [CAMPAIGN-CONTINUITY.md](CAMPAIGN-CONTINUITY.md)),
add `Environment=` lines with the same three values plus
`REQUIRE_WORKFLOW=1 REQUIRE_HYVV=1`.

## How CI provides them

`.github/workflows/ci.yml` does what the script automates, on a disposable
runner:

1. **gitleaks** — downloads the v8.28.0 release into `$RUNNER_TEMP/bin`, adds
   it to `GITHUB_PATH` and sets `GITLEAKS`.
2. **workflow** — `git clone`s `mojomast/workflow`, fetches
   `refs/heads/feat/bee-landed`, checks out
   `9c69d3a987ea9544a77cdd9b5a259904eb36f006`, asserts
   `git rev-parse HEAD` equals the pin, then sets `WORKFLOW_SRC` and
   `REQUIRE_WORKFLOW=1`.
3. **hyvv** — the same for `mojomast/hyvv` (`feat/bee-landed-events`,
   `8ffaf73000974e0ba273c72fca42e07b79c38106`), setting `HYVV_SRC` and
   `REQUIRE_HYVV=1`.

The script mirrors those exact steps so a local run and CI exercise the same
revisions; [stack-manifest.md](stack-manifest.md) records the pins. Verify a
checkout the same way CI does:

```bash
git -C "$WORKFLOW_SRC" rev-parse HEAD
git -C "$HYVV_SRC" rev-parse HEAD
```

## Changing a pin

A pin moves only through review. Update the SHAs in `.github/workflows/ci.yml`,
`docs/stack-manifest.md`, the top of `scripts/setup-pinned-sources.sh`, and the
"Protocol pinning" section of the README together, in one change.

## The lease payload: dispatch records it, verify binds it, every run serves it

The coordinator's pinned checkouts live in the coordinator home, which every
strong sandbox wrapper hides (`firejail --private=<copy>` mounts the disposable
copy over the home and `--private-tmp` replaces `/tmp` and `/var/tmp`). A
payload addressed at its absolute host path is ENOENT inside the jail, so
serving is *staging*, never env transport: `sandbox.stage_payload` copies each
source into the sandbox copy and the child env names it by the relative path
`./.bundlebees-payload/<name>`. The host checkout path never appears in the
child env, and on a masking wrapper it does not even exist inside the jail.

The wiring has three halves, all behind one chokepoint per half:

1. **Dispatch records it** (`leases.record_lease`, the chokepoint all three
   dispatch paths share: `bundlebees_dispatch`, the launcher reconciler's
   lease binder, and `hermes bundlebees lease`). The digest manifest of what
   the coordinator actually serves — `dispatch.payload_digest(
   dispatch.collect_payload())` over `WORKFLOW_SRC`/`HYVV_SRC` — is recorded
   into the record's brief, inside the HMAC. With the pins unset the key is
   absent (never an empty manifest), so leases stay byte-identical to the
   previous format on a coordinator that serves nothing. A pin that is set
   but unusable (missing directory, relative path, NUL) fails dispatch closed
   with the named `dispatch.PayloadError` (`payload_source_missing`,
   `payload_source_invalid`) and no lease is written.
2. **Verify binds it** (`cli._cmd_verify`). Before any test runs, the lease's
   recorded manifest is checked against the sources this coordinator serves
   now via `dispatch.lease_payload(record)`. A mismatch — the stale-pin
   consequence: a checkout that moved, changed, or was never exported — is a
   `payload_lease_mismatch` refusal, a stated disclosure, not a softened
   fallback: re-dispatch so the lease records the payload the coordinator
   actually serves. A lease whose brief records no manifest binds nothing.
3. **Every runner-driven sandbox run serves it**
   (`execution.sandbox_options` / `execution.SandboxRunner`). The payload
   collected from the environment is part of the sandbox options, so the
   verify CLI, `verify --commit-local`, durable-job verification and
   integration, and the factory driver all stage and serve it through the
   same options. Receipt identity carries an additive optional
   payload-digest field when something is served and stays byte-identical to
   the base when nothing is, so a changed checkout re-earns cached receipts.

**Disclosed gaps.** `verify --commit-local` serves the payload from the
environment without binding a lease manifest — by design, because that path
has no lease to bind; the payload served is whatever the coordinator's
environment names at that moment. `acdd_cli.py` and `integrate_job.py` call
`sandbox.run_in_sandbox` directly on their runner-less fallbacks, so those
runs are not proven to serve the payload this lane; when a runner
(`execution.SandboxRunner`) is injected, as `job_cli` and `factory_driver` do,
serving is inherited through the runner. Both gaps are unproven, not closed,
here.

## Troubleshooting

| Symptom | Meaning and fix |
|---|---|
| `git clone failed: …` | No network access to the remote, or no disk space. Fix the cause and re-run; nothing existing was touched. |
| `exists but is not a git checkout` / `tracks a different origin` | The directory under `RIZZSTACK_PINNED_DIR` was not created by this script. Move or remove it yourself — the script never deletes — and re-run. |
| `cannot check out the pinned revision` | The checkout carries local changes that conflict with the pin. Commit or discard them yourself, then re-run; the script never resets. |
| `the pinned revision is gone from the remote` wording / unknown-revision checkout failure | The pin was rebased or GC'd away. Ask the owner to re-pin ("Changing a pin"). |
| `the pinned checkout is incomplete: …` | The pin no longer contains a marker file. Report it; do not relax the check locally. |
| `no pre-verified download exists for <os>/<arch>` | gitleaks is not installed and the script has no checksum-pinned asset for this platform. Install gitleaks yourself and export `GITLEAKS=/path/to/gitleaks`. |
| `failed its checksum verification` | The download was truncated or tampered with. Do not use it; re-run later or install gitleaks yourself. |
| The suites still skip/fail on `WORKFLOW_SRC`/`HYVV_SRC`/`GITLEAKS` | The exports are not set in the shell running pytest. Re-run the eval line in that shell, or persist them as above. |
| `payload_source_missing` / `payload_source_invalid` at dispatch | A pin is exported but does not name an existing absolute directory (moved, deleted, or mistyped). Repair the checkout and re-dispatch; dispatch fails closed and writes no lease. |
| `payload_lease_mismatch` at verify | The lease pinned source bytes the coordinator no longer serves (stale pin). Point the variable at the tree the lease pinned, or re-dispatch so the lease records what the coordinator actually serves. |
