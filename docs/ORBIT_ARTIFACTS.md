# Retrieving worker patches through Orbit (orbit-loopback adapter)

`hermes bundlebees verify` gets a finished worker's patch only through Orbit's coordinator-only artifact API
(`POST /api/local-workers {op: "artifact"}`, `orbit.worker-artifact/1`). It never reads launcher or Orbit
runtime files. The model-visible `bundlebees_attempt get` tool returns only the public result view, which
never carries a patch, whatever the client returns.

## What verify checks

1. `OrbitAdapterClient.artifact` checks the artifact version and that `attempt_id` matches. It checks that
   `sha256(patch) == patch_sha256` and that `source_rev` is a full hex commit id. Worker self-reports
   (`worker_claims_untrusted`) are dropped. Any failure is `artifact_invalid` and nothing is applied.
2. The artifact's `source_rev` (from Orbit's ledger) must equal the signed lease's `source_rev`, or verify
   stops with `stale_revision`.
3. The first patch hash read for an attempt is pinned in `<state>/artifact-pins/<attempt>.json`. A retry that
   returns a different patch stops with `artifact_changed`.
4. The rest of verify is unchanged: declared base, apply at source_rev, scope, secret scan, and tests run in
   the sandbox.

`verify-results/<attempt>.json` records `artifact`, `patch_sha256`, `result_sha256`, model, launcher, observed
tool calls and stop reason, and `usage`. Usage is either the launcher's counters (`source:
"launcher_reported"`) or `{"source": "unavailable"}`. Missing usage is never recorded as zero. The patch text is
never printed or stored by verify.

## Codes

| Orbit `code` | bundlebees code | retry? |
|---|---|---|
| `result_pending` | `no_result_yet` | yes |
| `worker_disconnected`, `launcher_unreachable` | `launcher_unavailable` | yes |
| `result_missing` | `result_missing` | re-dispatch |
| `no_result_for_state` | `no_result_for_state` | re-dispatch |
| `artifact_malformed`, `artifact_mismatch`, `artifact_changed`, `artifact_unsafe` | `artifact_invalid` | reject |
| `artifact_oversized` | `response_too_large` | split the brief |
| `not_owner`, `coordinator_required` | `forbidden` | check the `coordinator` setting |
| `unknown_attempt` | `not_found` | check the id |

The `coordinator` setting (default `MR.INDIA`, Orbit's default) is sent at dispatch and with every artifact
read. Orbit returns the patch only to the coordinator that dispatched the attempt.

## Real round trip test

`tests/test_orbit_roundtrip.py` runs the real Orbit route from an orbitdesktop checkout under node, in front
of a stand-in launcher. It covers dispatch with a signed lease, Orbit dispatch, artifact retrieval, a signed
GREEN in bubblewrap, refusal of a wrong key and of another coordinator, an Orbit restart with an identical
hash, and refusal of a substituted result. Run it with `ORBIT_SRC=/path/to/orbitdesktop python -m pytest
tests/test_orbit_roundtrip.py`. Without `ORBIT_SRC`, node or bwrap it is skipped with a reason, which means
not run. It does not count as passed.
