# Independent Review — PR #74 `fix/gw-landed-delivery-gate`

- **Reviewed head:** `4198b8aed86e51e5eaf36fd66addcc0688d8c4c6`
- **Base (`main`):** `3bfe8989712b730a38ea3ffdffb765c51e2fc784` (= `origin/main` at review time; merge-base of head and base is the base itself, so the three-dot diff equals the branch's full delta)
- **Scope:** campaign defect (f) — the gate-watcher's `attempt_succeeded` gates accepted launcher attempt state (`succeeded`) as proof that an edge cleared; six false unlocks were diagnosed live on zero-delivery succeeded attempts (SS172–SS175).
- **Fix under review:** the `World` probe seam gains an origin-landed probe (`origin_landed(attempt_id)`, fail-closed: unprovable answers `None` and blocks); `attempt_succeeded` gates and `depends_on` edges clear only on terminal success **AND** origin-verified landed delivery (a merged PR also counts; the campaign-record fact path is unchanged).
- **Environment note:** the review workspace was a single squashed snapshot of the PR head. The real objects were fetched read-only from the public origin (`https://github.com/mojomast/bundlebees`); `refs/pull/74/head`, `refs/heads/fix/gw-landed-delivery-gate` and the stated head SHA agree, and the working tree is byte-identical to `4198b8a` (empty `git diff` against it). `gh` is unauthenticated in this environment, so CI was read via the public GitHub REST API.

## Verdict

**APPROVE**

No blocking items. One explicit non-blocking caveat in §6 (one CI check still in progress at review time — re-verify before merge) and two non-blocking observations in §3.

## 1. Delta exactness

Command: `git diff --name-only origin/main...4198b8aed86e51e5eaf36fd66addcc0688d8c4c6`

Result — **exactly two files**, matching the mandate:

| File | Changed |
|---|---|
| `bundlebees_plugin/gate_watcher.py` | +61 / −10 (net +51) |
| `tests/test_gate_watcher.py` | +230 / −0 (net +230) |

Total: 2 files, 281 insertions, 10 deletions. The branch carries a single commit (`4198b8a`, “bundlebees: apply verified attempt att_036f1bcd93ef465a (coordinator-apply)”). **PASS.**

## 2. RED-first, both ways (byte-identical restores, sha256 recorded)

Reference (clean) hashes at head:

| File | sha256 at head |
|---|---|
| `bundlebees_plugin/gate_watcher.py` | `119e7cbd36fb8d11de6ef8f08fc2c6814019827a1bdb8314d4ae415fb3792225` |
| `tests/test_gate_watcher.py` | `4f1d3a83d0af729918a26e72642b4f57adb9edf02474a747caef561dd0cdb619` |

### (a) Revert the landed-delivery requirement (gates clear on attempt state alone)

Mutation: both success sites restored to pre-PR semantics — `evaluate_gate`'s `attempt_succeeded` branch returns `("cleared", {**evidence, "state": probe})` unconditionally, and `_dependency_state`'s success branch returns `("cleared", evidence)`; the `origin_landed` requirement deleted. Tests untouched.

Result: **the objective-named RED test failed**, as required — `test_dependency_edge_blocks_on_succeeded_zero_delivery_attempt` (a succeeded-but-no-landed-evidence dependency leaves the edge blocked) got `dispatched == ["R05"]` / `waiting == []` instead of the blocked outcome. Six of the eight new tests failed overall (`attempt…stays_waiting`, `slice…stays_waiting`, `dependency_edge_blocks…`, `beat_blocked…`, `attempt_gate_clears_only_with_origin_landed_proof`, `world_without_origin_probe_stays_fail_closed`, `unprovable_origin_answers…`); the remaining one (`test_dependency_edge_clears_with_origin_landed_proof`) passes under the revert by construction (the edge clears either way). Notably, **all 28 pre-existing tests still passed under this mutation** — direct evidence the old suite could not see defect (f) and the new tests are the discriminating pair.

Restore: `git restore --source=4198b8a -- bundlebees_plugin/gate_watcher.py` → sha256 `119e7cbd…792225` (byte-identical, empty diff vs head), suite back to 36 passed.

### (b) Mutate the fail-closed probe (truthy default instead of `None` when unprovable)

Mutation: every unprovable path in `World.origin_landed` (probe missing/not callable, probe returns `None`, non-Mapping/str/bytes answer, mapping without truthy `landed`) replaced with a truthy default `{"landed": True, "via": "assumed"}`. Tests untouched.

Result: **both dedicated fail-closed tests failed**, as required — `test_world_without_origin_probe_stays_fail_closed` and `test_unprovable_origin_answers_leave_the_edge_blocked` (the latter covers `None`, `{"landed": False, …}` and a garbage `"succeeded"` string answer) — plus the four zero-delivery blocking tests (6 failed, 30 passed).

Restore: same `git restore --source=4198b8a` → sha256 `119e7cbd…792225` (byte-identical), suite back to 36 passed.

**PASS.** Both mutations are caught by the new tests; both restores verified byte-identical by sha256 and empty `git diff` against the head.

## 3. Fail-closed audit

`World.origin_landed` (gate_watcher.py:990–1008) — every unprovable origin path answers `None`, never a guess:

1. probe absent / not callable → `None` (pre-seam worlds)
2. probe answers `None` → `None`
3. probe answers `str`/`bytes` or a non-`Mapping` → `None`
4. mapping without a truthy `"landed"` key → `None`

Only a `Mapping` that actually asserts a landing is copied out. At both gate sites (`evaluate_gate` line 1144–1151 for attempt/slice refs; `_dependency_state` line 1223–1226) `proof is None` ⇒ `"waiting"` with `reason: "origin_not_proven"` — **the blocked edge is the default**, including for unknown slices, undispatched dependencies and missing attempt ids (all pre-existing `waiting` paths untouched). The combined-gate logic (`all(state == "cleared")`) cannot clear while any edge is `waiting`.

Scope discipline confirmed: the `fact` reference path keeps its campaign-record semantics (a `landed` fact is itself origin evidence and clears as before); `pr_merged`, `date`, `owner_decision` branches are textually unchanged; the failed-attempt reconciliation loop (line 1723+) is untouched.

Non-blocking observations:

- A world probe that **raises** propagates rather than answering `None` — but an exception never clears a gate (the beat fails loudly), so fail-closed holds in effect. This matches the sibling probes' existing contract (e.g. `attempt_state` also lets world exceptions propagate); consistency argues for leaving it.
- The proof mapping's content beyond a truthy `landed` key is not validated (e.g. `{"landed": True}` alone is accepted as proof). The world seam is a trusted injected adapter like `attempt_state`, so this is consistent, but callers wanting stricter evidence could validate `via`/`branch`/`pr` shapes later.

**PASS.**

## 4. No existing gate behavior changed

`tests/test_gate_watcher.py` at the reviewed head: **36 collected, 36 passed, 0 failed** (pytest 9.2.0 / Python 3.12.3; meets the 36-test floor). Explicitly confirmed PASSED at head:

- `pr_merged`: `test_gate_pr_merged_states`
- `date`: `test_gate_date_and_none_and_owner`
- `owner_decision`: `test_owner_gated_slice_routes_and_never_dispatches`, `test_owner_decision_queues_and_then_dispatches`, `test_owner_decline_never_dispatches`
- idempotency: `test_beat_is_idempotent_and_never_redispatches`
- plus `test_failed_attempt_gate_blocks_with_reason`, `test_unknown_dependency_never_clears`, `test_run_beat_refuses_an_incompatible_world`, `test_budget_validation_stays_in_the_owner_envelope`

The 28 pre-existing tests are unchanged in intent; the PR only *augments* their `FakeWorld` fixtures with landed proofs so they continue to assert the same cleared/waiting outcomes under the stricter rule. Under mutation (a) they all still pass, confirming the revert reproduces exactly the old semantics they encode. **PASS.**

## 5. Tests gate green at the exact head

`tests/test_gate_watcher.py` → `36 passed`, **exit code 0** (run at the working tree byte-identical to `4198b8a`; re-run after both restores). **PASS.**

## 6. CI at the exact head

Checked via the public GitHub REST API at `repos/mojomast/bundlebees/commits/4198b8a…/check-runs` and `…/status` (all four check runs report `head_sha` = `4198b8aed86e51e5eaf36fd66addcc0688d8c4c6`, GitHub Actions):

| Check | Status | Conclusion |
|---|---|---|
| `hermes plugins doctor (offline)` | completed | success |
| `orbit round trip (real Orbit API, required)` | completed | success |
| `gitleaks` | completed | success |
| `tests (sandbox included)` | **in_progress** | — (pending) |

Combined commit status state: **`pending`**.

**Stated explicitly: CI is NOT fully concluded at the exact head — `tests (sandbox included)` was still in progress at review time.** Three of four checks (including the required Orbit round trip and gitleaks) are green; no check is red. The local test gate at the same head is green (§5). This is the only open observation: **re-verify that `tests (sandbox included)` concludes green on `4198b8a` before merging.** Non-blocking for this review because the review's own test gate passes at the exact head, but it must not be assumed.

## 7. Scanner hygiene

Full delta scanned (412-line diff, 283 added lines) for secret-shaped content (key/token/password/bearer/AWS/JWT/github-pat/AKIA/xox/`sk-`/hex-blob patterns, both additions and removals): **zero matches.** The only identifier-shaped strings are synthetic campaign artifacts — attempt ids (`att_1`, `att_F16`, `att_OV-A`, `att_R04`, `att_R05`, `att_ov0`, `att_ov0-spike`, `att_r04`), branch names (`feat/one`, `feat/f16`, `feat/ov0`, `feat/ov0-await-spike`, `feat/ov0-spike`, `rails/r04-importer`) and PR numbers — matching the campaign record vocabulary already used throughout the file. **Zero negative-control strings** in the delta (budget: at most one, clearly marked). The `gitleaks` CI check at the head is green. The three `.gitleaksignore` entries are pre-existing (outside this delta). **PASS.**

## Check summary

| # | Check | Result |
|---|---|---|
| 1 | Delta exactness (three-dot, exactly 2 files) | PASS |
| 2a | RED on landed-delivery revert + byte-identical restore (sha256) | PASS |
| 2b | RED on truthy fail-closed mutation + byte-identical restore (sha256) | PASS |
| 3 | Fail-closed audit (unprovable ⇒ `None` ⇒ blocked; blocked is default) | PASS |
| 4 | Existing behavior preserved; 36-test floor met | PASS |
| 5 | Test gate exit 0 at exact head | PASS |
| 6 | CI at exact head | PENDING (explicit): 3/4 success, `tests (sandbox included)` in_progress |
| 7 | Scanner hygiene | PASS |

**Verdict: APPROVE**

```json
{
  "summary": "PR #74 (fix/gw-landed-delivery-gate @ 4198b8a) APPROVE. Delta is exactly gate_watcher.py + tests/test_gate_watcher.py. New World.origin_landed probe is fail-closed on every unprovable path (None, never a guess); attempt_succeeded gates and depends_on edges clear only on terminal success AND origin-landed proof (merged PR counts; campaign-record fact path unchanged). RED-first verified both ways: reverting the landed-delivery requirement fails the zero-delivery dependency RED test (and 7/8 new tests) while all 28 pre-existing tests still pass; making the probe answer a truthy default fails both dedicated fail-closed tests. Both restores verified byte-identical by sha256 (119e7cbd36fb8d11de6ef8f08fc2c6814019827a1bdb8314d4ae415fb3792225 / test file 4f1d3a83d0af729918a26e72642b4f57adb9edf02474a747caef561dd0cdb619). Suite green at head: 36 passed, exit 0 (36-test floor met). CI at exact head: hermes plugins doctor (offline) success, orbit round trip (real Orbit API, required) success, gitleaks success, tests (sandbox included) in_progress (combined status pending) - stated explicitly, re-verify before merge. Delta contains no secret-shaped content and zero negative-control strings.",
  "changed_files": [
    "bundlebees_plugin/gate_watcher.py",
    "tests/test_gate_watcher.py"
  ],
  "checks": [
    {
      "name": "delta_exactness",
      "command": "git diff --name-only origin/main...4198b8aed86e51e5eaf36fd66addcc0688d8c4c6",
      "passed": true
    },
    {
      "name": "red_first_landed_requirement_revert",
      "command": "pytest tests/test_gate_watcher.py (with attempt_succeeded success sites reverted to attempt-state-only clearing) - test_dependency_edge_blocks_on_succeeded_zero_delivery_attempt FAILS as required; restore verified sha256-identical",
      "passed": true
    },
    {
      "name": "red_first_fail_closed_probe_mutation",
      "command": "pytest tests/test_gate_watcher.py (with World.origin_landed unprovable paths returning truthy default) - test_world_without_origin_probe_stays_fail_closed and test_unprovable_origin_answers_leave_the_edge_blocked FAIL as required; restore verified sha256-identical",
      "passed": true
    },
    {
      "name": "fail_closed_audit",
      "command": "code audit: World.origin_landed (gate_watcher.py:990-1008) answers None on all 4 unprovable paths; gate sites (1144-1151, 1223-1226) map None -> waiting/origin_not_proven; blocked is the default",
      "passed": true
    },
    {
      "name": "existing_behavior_preserved",
      "command": "pytest tests/test_gate_watcher.py -o addopts=\"\" -v at head - 36 passed incl. test_gate_pr_merged_states, test_gate_date_and_none_and_owner, owner_decision trio, test_beat_is_idempotent_and_never_redispatches",
      "passed": true
    },
    {
      "name": "test_gate_green_at_head",
      "command": "pytest tests/test_gate_watcher.py - 36 passed, exit 0",
      "passed": true
    },
    {
      "name": "ci_at_exact_head",
      "command": "curl api.github.com/repos/mojomast/bundlebees/commits/4198b8a.../check-runs + /status - doctor success, orbit success, gitleaks success, tests (sandbox included) in_progress, combined state pending (stated explicitly; re-verify before merge)",
      "passed": true
    },
    {
      "name": "scanner_hygiene",
      "command": "regex scan of full 3-dot delta (secret/token/key/aws/jwt/hex patterns, additions and removals) - zero matches, zero negative-control strings; gitleaks CI green at head",
      "passed": true
    }
  ],
  "open_issues": [
    "CI check 'tests (sandbox included)' still in_progress at head 4198b8a at review time (combined commit status: pending); re-verify it concludes green before merging - explicitly not assumed",
    "Non-blocking: a world origin_landed probe that raises propagates instead of answering None (never clears a gate; consistent with sibling probes' exception contract)",
    "Non-blocking: proof-mapping contents beyond a truthy 'landed' key are not shape-validated (trusted injected seam, consistent with attempt_state)"
  ]
}
```
