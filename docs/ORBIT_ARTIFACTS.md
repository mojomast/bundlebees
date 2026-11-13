# Retrieving worker patches through Orbit (orbit-loopback adapter)

`hermes bundlebees verify` gets a finished worker's patch only through Orbit's capability-gated artifact API
(`POST /api/local-workers {op: "artifact"}`, `orbit.worker-artifact/1`). It never reads hyvv (formerly the
PC launcher) or Orbit runtime files. The model-visible `bundlebees_attempt get` tool returns only the public result view, which
never carries a patch, whatever the client returns.

Orbit returns the per-attempt `artifact_capability` once, from `create`. The client stores it privately as
`<state>/artifact-caps/<attempt_id>.cap` (directory `0700`, file `0600`) and sends it, with the attempt id,
on every artifact read. The capability is never returned, logged or shown; a client without a stored
capability fails locally with `capability_missing` before making any request. The `coordinator` setting is an
optional label with no default: when set it is validated and sent on dispatch and artifact reads, when empty
it is not sent at all.

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
| `artifact_malformed`, `artifact_mismatch`, `artifact_changed`, `artifact_unsafe`, `revision_missing` | `artifact_invalid` | reject |
| `artifact_oversized` | `response_too_large` | split the brief |
| `not_owner`, `coordinator_required`, `capability_required` | `forbidden` | use the capability stored at dispatch and re-dispatch from this coordinator |
| `unknown_attempt` | `not_found` | check the id |

Ownership is the per-attempt capability Orbit returns from dispatch, not a coordinator name. Re-dispatch from
this coordinator if the capability is missing (`capability_missing`) or Orbit refuses it (`capability_required`);
the `coordinator` setting is only an optional label.

## hyvv

hyvv (formerly the PC launcher) owns the `/v1` worker API. It plans to add `GET /v1/attempts/{id}/artifact`
with the same codes, advertised as `artifact_fetch`. Orbit can then become a thin proxy for this op. The
coordinator-facing artifact and codes stay the same, so this client needs no change.

## Real round trip test

`tests/test_orbit_roundtrip.py` runs the real Orbit route from an orbitdesktop checkout under node, in front
of a stand-in for hyvv's `/v1` API. It covers dispatch with a signed lease, Orbit dispatch, artifact retrieval, a signed
GREEN in bubblewrap, refusal of a wrong key and of a forged capability, an Orbit restart with an identical
hash, and refusal of a substituted or oversized result while nothing is applied. Run it with `ORBIT_SRC=/path/to/orbitdesktop python -m pytest
tests/test_orbit_roundtrip.py`. Without `ORBIT_SRC`, node or bwrap it is skipped with a reason, which means
not run. It does not count as passed.
