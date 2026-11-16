# Session audit: execution wiring and provisioning refusals

The 2026-11-15/16 acceptance session exposed two independent problems: the
durable job path did not use the standalone verifier's configured interpreter
and cache, and a definite provisioning refusal could be saved as `preparing`
and become an indefinite receipt lookup on the next tick.

## Execution settings

`verify --commit-local`, job ticks, queue ticks and the production factory driver
now use a shared runner over the existing sandbox. It carries `verify_python`,
`sandbox_tiktoken_cache`, `verify_isolation`, `verify_timeout_s` and the existing
explicit weak/unsafe overrides into verification **and integration**. Strong
isolation remains the default. Test receipt identity includes the configured
policy, executable bytes, dependency manifests and encoding-cache bytes; a cached
result still checks sandbox availability.

Configure `plugins.entries.bundlebees.settings.gitleaks_path` for a scanner that
is not on PATH. Resolution is `--gitleaks`, the declared setting, the legacy
queue setting, `GITLEAKS`, then PATH. A missing scanner stops the job CLI before
launcher access. The programmatic factory tick never disables required scanning
because a scanner argument was omitted.

Bare Python commands are resolved through PATH before checking whether they name
the coordinator interpreter. A different interpreter is not silently replaced.
The default job suite uses the coordinator's absolute executable. Changing a
task's `acceptance.commands` does not change the job verifier's suite command.
The factory's `--suite-command` remains the explicit suite override.

Repeated `--tests` arguments append targets rather than dropping earlier ones:

```text
hermes bundlebees verify ATTEMPT --tests tests/a.py --tests tests/b.py
```

## Recovery ceilings

The interactive `job authorize` command exposes the existing sealed recovery
envelope through:

- `--max-attempts-per-task N`
- `--contract-corrections-allowed 0|1`
- `--corrected-test-allowed SELECTOR` (repeatable)
- `--recovery-file-scope PATH` and `--recovery-test-scope PATH` (repeatable)
- `--recovery-gate NAME` (repeatable; cannot remove an existing gate)

No flags means the existing empty ceiling. These flags do not change the TTY
requirement, create authority for an agent, amend old sealed authorizations, or
permit worker retries for environment/infrastructure failures. A correction must
name its replacement tests and permitted scopes; the existing envelope validator
rejects incomplete or widened permissions.

## Provisioning states

A received `ProvisionError` is persisted as `denied` with its original code and
detail. Further ticks report that refusal instead of looking for a nonexistent
receipt or replaying the denied request. The job CLI returns structured error
JSON and a nonzero exit code for provisioning and binding refusals.

A lost acknowledgement or crash remains uncertain and is reconciled read-only.
This change does not automatically reset historical `preparing`/`uncertain`
intents. Inspect the original intent and host journal before taking a recovery
action. Likewise, a denied request requires inspection and an explicit recovery
decision; changing host policy does not silently replay it.

`receipt_unknown` alone does not establish a disabled host. Receipt lookup checks
capabilities first; a disabled advertisement yields `provisioning_disabled`.
No config-preservation change is justified without the actual update wrapper
and before/after host evidence.

## Remaining acceptance

These fixes do not activate or restart a service. Verify runtime revision parity
separately. Run the focused tests plus the repository's normal Linux CI, then
exercise the actual coordinator's configured Firejail/bubblewrap environment.
The full pre-dispatch environment fingerprint, bounded re-verification of an
existing artifact, historical-intent repair, campaign-wide churn caps and live
Phoenix delivery remain distinct work. A worker-reported passing check is not
independent artifact verification.
