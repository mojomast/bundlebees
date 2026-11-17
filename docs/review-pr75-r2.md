# Independent Re-Review — Round 2 — bundlebees PR #75

- **PR:** mojomast/bundlebees#75 — "verify: force full checkouts in prepared worktrees + git-context hint on gate failures (defect a + git gap)"
- **Head reviewed:** `93496915c4c1853ee2dc1b0b0df1f32140f89547` (branch `fix/verify-full-checkout-sparse`) — exact
- **Base:** `3bfe8989712b730a38ea3ffdffb765c51e2fc784` (`main`) — exact
- **Round-1 verdict:** changes_requested, one blocking item (GitContextGap not collected — missing `__test__ = True`)
- **Repair commits under review:** `d26cd11bf3b62e62fe63cd2a48faad22e11b5a3c` (round 1) and `93496915c4c1…` (round 2, the PR head itself)
- **Review date:** 2026-11-17
- **Method:** this workspace is a single-commit snapshot (`3e07a35`, tree = head `9349691`). Delta exactness was computed against the *fetched upstream base tree* `3bfe898` (whole-tree `diff -rq`) and cross-checked against the GitHub compare API; the audited files' git blob hashes were verified identical to the upstream head tree, so the snapshot is a faithful stand-in for the PR head.

---

## Check 1 — Delta exactness vs base main

Whole-tree diff of the working tree against the fetched base tree (`3bfe898`, tarball from codeload, `diff -rq --exclude=.git`) differs in **exactly four files**:

| file | +adds | −dels | local numstat | GitHub compare | match |
|---|---|---|---|---|---|
| `bundlebees_plugin/verify_local.py` | 62 | 4 | +62 −4 | +62 −4 | exact |
| `bundlebees_plugin/doctor.py` | 21 | 1 | +21 −1 | +21 −1 | exact |
| `tests/test_verify_local.py` | 377 | 1 | +377 −1 | +377 −1 | exact |
| `tests/test_doctor.py` | 11 | 0 | +11 −0 | +11 −0 | exact |

- **`bundlebees_plugin/sandbox.py`: byte-identical to base (diff EMPTY)** — confirmed by whole-tree diff and per-file `cmp`. Out of scope per the review charter; no change requested.
- No fifth file differs anywhere in the tree (full recursive diff clean).
- Branch commits base→head: `27f4ad5` (feature: the four files above, +374/−1 on the verify test file), `d26cd11` (repair r1), `9349691` (repair r2).
- **Repair commit `d26cd11`: touches ONLY `tests/test_verify_local.py` (+1 line).** GitHub commit API + `.patch`: one added line, `__test__ = True` on `class GitContextGap` (now at `tests/test_verify_local.py:497`).
- **Repair commit `9349691`: touches ONLY `tests/test_verify_local.py` (+3 −1).** The full patch is a single hunk inside `test_a_gitless_suite_failure_points_at_the_doctor_note`: `- if len(calls) > 1:` → `+ if len(calls) == 2: return {…, "output_tail": "green run"}` + `+ if len(calls) > 2:`. Nothing else in the commit.

**PASS.**

## Check 2 — Round-1 blocking item resolved

- `class GitContextGap` at `tests/test_verify_local.py:495` now carries `__test__ = True` at `:497` (the exact `d26cd11` hunk), mirroring every sibling class in the file.
- Collection evidence at this head: `pytest tests/test_verify_local.py --collect-only` = **20 tests**, including all three:
  - `GitContextGap::test_a_gitless_green_failure_points_at_the_doctor_note`
  - `GitContextGap::test_a_plain_green_failure_keeps_the_plain_guidance`
  - `GitContextGap::test_a_gitless_suite_failure_points_at_the_doctor_note`
- Run evidence: `-k GitContextGap` → **3 passed** (17 deselected). Two-file gate: 48 collected, 47 passed, 1 skipped, 0 failed (Check 5).

**PASS.**

## Check 3 — Stub remedy minimal and correct; asserted path reachable

**Minimality.** The round-2 repair delta is exactly the single hunk shown in Check 1 — no other line of `tests/test_verify_local.py` changed in `9349691`, and the class's other content is untouched vs the feature commit.

**The stub now reads** (`tests/test_verify_local.py:564-574`):

```python
def runner(workdir, argv, *args, **kwargs):
    calls.append(1)
    if len(calls) == 2:
        return {"exit_code": 0, "timed_out": False, "output_tail": "green run"}
    if len(calls) > 2:
        return {"exit_code": 1, "timed_out": False,
                "output_tail": "fatal: not a git repository: .git"}
    return {"exit_code": 1, "timed_out": False, "output_tail": "red run"}
```

- GREEN-run arm: `exit_code 0`, tail `"green run"` — contains **no gitless text**. ✔
- Gitless arm (`len(calls) > 2`) still covers **every call beyond the suite run** (calls 3, 4, 5, … — any hypothetical retry/future gate stays gitless-failing). ✔
- Fallback arm (call 1) keeps a plain red failure, satisfying the RED gate.

**Reachability against product code** (`bundlebees_plugin/verify_local.py`), runner-call accounting in `verify_and_commit`/`_verify_and_commit_locked`:

1. **RED run = runner call 1** (`_run_tests` → `_run_test_command` → `_execute_test_command` calls `runner(...)` exactly once; gate at `verify_local.py:1044-1059` accepts exit ≠ 0). Stub else-arm returns exit 1 "red run" → RED passes.
2. **GREEN run = runner call 2** (`verify_local.py:1104-1115`, gate `:1122-1127` raises `tests_failed` on exit ≠ 0). Stub returns exit 0 → GREEN passes; the previously-fatal `tests_failed` path is now avoided.
3. **Suite run = runner call 3** (`verify_local.py:1130-1140`, gate raises `suite_failed` at `:1147-1152`; the `"suite_failed"` literal sits at `:1149`). Stub's `len(calls) > 2` arm fires → `suite_failed`, and `_git_context_hint` (`:115-120`) sees "not a git repository" in the tail, so `fix` carries `GIT_CONTEXT_NOTE` (`:107-109`) — satisfying all three assertions (`code == "suite_failed"`, `"doctor" in fix`, `"no git repository context" in fix`, asserted at `tests/test_verify_local.py:590-592`).

No intervening runner calls exist: `_verification_identity` (`:456-505`) only computes digests/policies (`_environment_policy`/`_sandbox_policy` inspect the runner, never call it); `_assert_sandbox_usable` (`:557-579`) calls `runner.check_available` only when the runner exposes it — the stub is a plain function; the receipt cache in `_run_test_command` (`:637-720`) misses on a fresh `tmp_path` state, so each gate executes the runner exactly once. The RED-first probe (Check 4) demonstrates the exact pre-fix mechanism: with call 2 failing, `tests_failed` is raised at `:1122-1127` before the suite run is ever reached.

**PASS.**

## Check 4 — RED-first revert probe (byte-identical restore)

- `sha256sum tests/test_verify_local.py` **before** probe:
  `c6657ee447bde665672592b24913d9483de8c7bbd397aef2d304d4b3bd1da0eb`
- Locally reverted the stub hunk to the pre-fix form (`if len(calls) > 1:` with the gitless dict; GREEN arm removed).
- Re-ran `-k GitContextGap`. The named test failed with the recorded round-1/round-2 signature, verbatim:

  ```
  FAILED tests/test_verify_local.py::GitContextGap::test_a_gitless_suite_failure_points_at_the_doctor_note
  E       AssertionError: assert 'tests_failed' == 'suite_failed'
  E         - suite_failed
  E         + tests_failed
  tests/test_verify_local.py:588: AssertionError
  1 failed, 2 passed, 17 deselected in 0.25s
  ```

  This is exactly the pre-fix signature on record (`assert 'tests_failed' == 'suite_failed'` reported at `tests/test_verify_local.py:588`), reproducing the round-1/round-2 diagnosis: call 2 (GREEN) failed → `tests_failed` at `verify_local.py:1122-1127` before the suite run could produce `suite_failed`.
- Restored with `git checkout -- tests/test_verify_local.py`; `git status --porcelain` clean.
- `sha256sum` **after** restore: `c6657ee447bde665672592b24913d9483de8c7bbd397aef2d304d4b3bd1da0eb` — **identical** (byte-identical restore). Post-restore two-file gate re-run: 47 passed, 1 skipped, 0 failed.

**PASS.**

## Check 5 — Tests gate at the exact head

Interpreter disclosure (per charter): this host's only system python (`/usr/bin/python3`, 3.12.3) has no pytest and PEP 668 blocks pip; a venv was created **outside the home directory** at **`/opt/pytest-venv`** (`sudo python3 -m venv /opt/pytest-venv`) with `pytest 9.2.0` plus `jsonschema`/`pyyaml`/`ruamel.yaml` (the plugin's runtime dependency and test-only extras). All runs used:

- `BUNDLEBEES_VERIFY_PYTHON=/opt/pytest-venv/bin/python`
- `/opt/pytest-venv/bin/python -m pytest tests/test_verify_local.py tests/test_doctor.py`

Result (twice — pre-probe and post-restore):

```
47 passed, 1 skipped in 3.82s
```

- **Zero failed.**
- **Exactly one environment-dependent skip**, disclosed: `SKIPPED [1] tests/fakes.py:146: no strong sandbox backend is usable on this host` — the `require_sandbox_interpreter()` guard in `test_failing_tests_after_the_patch_are_refused` (`tests/test_verify_local.py:170`). This host has no `bwrap`/`firejail`, and `unshare --user …` fails with "Operation not permitted" in this container, so a real jail cannot be exercised here. The `sandboxed()` runner used by the remaining tests opts into the documented `allow_unsafe_local` path and runs with the sandbox's scrubbed environment against the disclosed outside-home interpreter.
- `-k GitContextGap`: 3 passed.

**PASS.**

## Check 6 — CI at the exact head `9349691`

Queried live from the GitHub API (Checks API, scoped to the exact head sha; unauthenticated, public repo). **All four PR check runs are `completed` with conclusion `success`:**

| check run | status | conclusion |
|---|---|---|
| hermes plugins doctor (offline) | completed | success |
| tests (sandbox included) | completed | success |
| gitleaks | completed | success |
| orbit round trip (real Orbit API, required) | completed | success |

Legacy Status API for the same sha: `total_count 0` (the `state: "pending"` there is the no-status default; the Checks API governs). Not assumed — fetched at review time. Nothing pending.

**PASS.**

## Check 7 — Scanner hygiene

- All 471 added lines across the four delta files scanned for secret-shaped content (AWS `AKIA…`, `ghp_`/`github_pat_`, Slack `xox…`, `sk-`/`gsk_` API keys, PEM private-key blocks, `Bearer …`, `password=`/`secret=` literals, and ≥40-char high-entropy literals outside code identifiers): **no matches**.
- **Zero** synthetic/negative-control strings in the delta (searched for fake/canary/placeholder/`example.com`/`not-a-real` markers) — within the "at most one, clearly marked" allowance.
- `.gitleaksignore` is unchanged by this delta (its entries reference base-history commits, not this PR). Consistent with the repair commit's own "0 secret trips" claim and the CI `gitleaks` success at this head.

**PASS.**

## Notes

- Line-number mapping between the round-1 record and this head (the `d26cd11` line shifts later content by one): round-1 `verify_local.py:1106` ≙ the GREEN `_run_tests` block starting at `:1104-1105`; `:1120-1125` ≙ the `tests_failed` raise at `:1122-1127`; `:1149` is still the `suite_failed` literal; round-1 `tests/test_verify_local.py:588` ≙ the suite-failure assertions now written at `:590-592` (pytest still attributes the pre-fix failure to `:588`, matching the recorded signature exactly).
- The branch's first commit `27f4ad5` ("bundlebees: apply verified attempt att_17f6b893171d4d07 (coordinator-apply)") is the feature-carrying commit; the two repair commits modify nothing but the test file, as required.
- `sandbox.py` is deliberately out of scope; its diff is empty and no change is requested.

## Verdict

**APPROVE** — no blocking items. Both repair commits are minimal and confined to `tests/test_verify_local.py`; the round-1 blocking item is resolved; the round-2 stub remedy is minimal, correct, and demonstrably reachable against product code (RED-first probe reproduces the pre-fix signature; post-fix the asserted `suite_failed` path executes); the two-file gate is green with zero failures and exactly one disclosed environment-dependent skip; CI at the exact head is 4/4 success; the delta touches exactly the four declared files with `sandbox.py` byte-identical to base.

```json
{
  "summary": "Round-2 independent re-review of PR #75 (head 9349691, base 3bfe898): delta is exactly the four declared files (sandbox.py byte-identical); repair commits d26cd11 (+1 __test__ line) and 9349691 (+3/-1 stub hunk) touch only tests/test_verify_local.py; GitContextGap is collected and its 3 tests pass; the stub remedy is minimal and the asserted suite_failed path is confirmed reachable against verify_local.py call order (RED=call1, GREEN=call2 exit 0, suite=call3 gitless fail -> suite_failed+doctor hint); RED-first revert probe reproduces the recorded tests_failed-vs-suite_failed AssertionError at :588 and restores byte-identical (sha256 c6657ee4... before and after); two-file gate 47 passed / 1 disclosed env-dependent skip / 0 failed with BUNDLEBEES_VERIFY_PYTHON=/opt/pytest-venv/bin/python (outside home); CI at the exact head: 4/4 check runs success (doctor, tests, gitleaks, orbit round trip); scanner hygiene clean (471 added lines, no secret-shaped content, zero negative-control strings).",
  "changed_files": ["docs/review-pr75-r2.md"],
  "checks": [
    {"name": "delta_exactness", "command": "diff -rq fetched-base-tree(3bfe898) worktree --exclude=.git; git diff --no-index --numstat per file; compare API cross-check", "passed": true},
    {"name": "sandbox_py_diff_empty", "command": "cmp base-tree/bundlebees_plugin/sandbox.py bundlebees_plugin/sandbox.py", "passed": true},
    {"name": "repair_commits_scope", "command": "GitHub commits API + .patch for d26cd11 and 9349691", "passed": true},
    {"name": "round1_blocking_resolved", "command": "grep __test__ tests/test_verify_local.py; pytest --collect-only; pytest -k GitContextGap -v", "passed": true},
    {"name": "stub_reachability", "command": "product-code call-order audit verify_local.py:1044-1059/1104-1127/1130-1152/456-505/557-579/637-720", "passed": true},
    {"name": "red_first_revert_probe", "command": "sha256sum before c6657ee4...; revert stub hunk; pytest -k GitContextGap -> 1 failed (tests_failed vs suite_failed at :588); git checkout --; sha256sum after identical", "passed": true},
    {"name": "tests_gate", "command": "BUNDLEBEES_VERIFY_PYTHON=/opt/pytest-venv/bin/python /opt/pytest-venv/bin/python -m pytest tests/test_verify_local.py tests/test_doctor.py -q", "passed": true},
    {"name": "ci_at_head", "command": "curl api.github.com/repos/mojomast/bundlebees/commits/93496915.../check-runs (4/4 success)", "passed": true},
    {"name": "scanner_hygiene", "command": "secret-pattern + high-entropy + negative-control scan over 471 added delta lines", "passed": true}
  ],
  "open_issues": []
}
```
