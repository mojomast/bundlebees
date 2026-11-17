# Independent review — PR #73 (`fix/verify-refusal-evidence` @ `50507c65`)

Reviewer: independent review agent. Reviewed head: `50507c65a128db1465aaaa56d2bfbde18d6788f5`.
Base `main`: `ebabd5e32cab0292aff1e113ce478709771f47cd` (merge-base of the two, so the
three-dot diff `main...pr73` is a plain two-dot diff of the one PR commit).
Charter: F6 — the `secret_detected` verify refusal must carry, per trip, rule
identity + 1-based line number + a masked excerpt, with zero matching-behavior
change and with the #71 contract inviolable.

Status: **in progress** — checks are appended as they complete.

## Check 1 — Delta exactness

- `git diff --numstat main...pr73` (fetched from the public remote; local snapshot
  tree verified byte-identical to `50507c65` via `git diff <snapshot> 50507c65` → empty):

  | file | + | − |
  |---|---|---|
  | `bundlebees_plugin/cli.py` | 101 | 25 |
  | `bundlebees_plugin/security.py` | 30 | 0 |
  | `tests/test_secret_scan_added_lines.py` | 62 | 0 |

  Exactly three files; `security.py` additions only; the test file is additions only.
- `git ls-tree -r pr73 --name-only | grep -i "uv.lock"` → no match; `uv.lock` absent
  from the delta as well. **PASS**
- `uv.lock` exclusion disclosure: the PR body discloses that the official verify gate
  refused the worker payload with `scope_violation` because it also carried a new
  `uv.lock` (+1050 lines, never tracked in this repo), and that the coordinator applied
  it with `git apply --exclude=uv.lock`, preserving the unfiltered payload at
  `scratch/f6-payload.patch`. **This is a coordinator attestation, recorded as such:**
  the payload patch is not reachable from this workspace (not in the tree, not on any
  fetched ref), so the payload→branch re-verification the PR body mandates was not
  independently performed here. What *is* independently verified is the applied result:
  the branch delta is exactly the three leased files (above).

## Check 2 — RED-first mutation checks, both ways

Environment: local venv (Python 3.12.3, pytest 8.4.2) inside the working directory.

**Mutation A — revert ONLY the `security.py` additive export.**
`git show refs/review-base/main:bundlebees_plugin/security.py` over the head file
(the export is exactly the +30/−0 hunk, so the base file is the precise revert).

- sha256 before: `81c915fa686dd5fcf465d4cb6913b3efc84ac01de27565de15fa041d9ef67759`
- Result: `tests/test_secret_scan_added_lines.py` → **6 failed** (all four new
  refusal-evidence tests plus the two pre-existing tests; `cli.py:47` raises
  `ImportError: cannot import name 'first_secret_rule'`, so the module under test
  cannot even import). RED confirmed: the export is load-bearing and the new tests
  cannot pass without it.
- Restored from the saved head copy; sha256 after:
  `81c915fa686dd5fcf465d4cb6913b3efc84ac01de27565de15fa041d9ef67759` — **byte-identical**;
  `git diff -- bundlebees_plugin/security.py` empty.

**Mutation B — mutate the cli.py enriched refusal: drop the masked-excerpt field.**
Removed exactly the two lines in `_secret_refusal` that append the
`excerpt="…"` clause (the trips dict itself untouched, so `_secret_trips` still
carries the excerpt and only the refusal envelope loses it).

- sha256 before: `2f28e7067d117822d632744bae0af2cf5ff5d69712c507d8793d77e63b3c410a`
- Result: **1 failed, 5 passed** — `test_refusal_evidence_names_rule_line_and_masked_excerpt`
  fails on its excerpt assertion (the mutated refusal emitted
  `src/a.py line=3 rule=assignment` with no `excerpt=…` field; exit 1). RED confirmed:
  the excerpt clause is exactly what that test pins.
- Restored from the saved head copy; sha256 after:
  `2f28e7067d117822d632744bae0af2cf5ff5d69712c507d8793d77e63b3c410a` — **byte-identical**;
  `git diff -- bundlebees_plugin/cli.py` empty.

After both restores the three-file gate was re-run green (143 passed, exit 0).

**Worker baseline evidence (from the PR body, recorded as worker-reported):** baseline
139 green → RED 4/2 → additive export at 100 green → implementation GREEN. Consistent
with the independently observed 139 + 4 = 143 gate here; the 100-green intermediate
stage was not re-derivable from the workspace and is taken as the worker's record.

**Zero matching-behavior change, suite-level evidence:** full suite (`pytest --tb=no -rf`)
run at head and at base in a separate base worktree:
- head: 114 failed, 1791 passed, 261 skipped; base: 114 failed, 1787 passed, 261 skipped;
- the 114 FAILED test ids are **byte-identical sets** (`diff` empty) — every one is an
  environmental failure of this host (no bubblewrap/firejail → `sandbox_unavailable`
  fail-closed behavior), identical at base;
- passed delta = exactly the 4 new tests. No pre-existing test changed behavior.

## Check 3 — #71 contract inviolable

- `git diff base…head -- tests/test_security.py tests/test_client.py` → **0 lines**
  (both files untouched by the PR).
- `sha256sum tests/test_client.py` →
  `970eff469a594075be5f35bc97dc6dd89dcca783fceb02da6dd203d4c6902f15` — **equal to the
  mandated value** and equal to the base blob
  (`git cat-file blob refs/review-base/main:tests/test_client.py | sha256sum` → same).
- `tests/test_security.py` green within the 143-passed gate (100 tests in the file:
  the value-shape matrix — 9 exempt code-reference shapes, 15 credential-shaped still-trip
  shapes, 4 benign, plus the dedicated r3 / boundary / matrix cases — and the tight
  `k=v` battery with the mandated synthetic negative-control marker (Bearer-prose
  style, see Check 7) staying exempt and tight-separator bare values tripping).
  #71's value-shape contract is unchanged and green.

## Check 4 — Fail-closed audit (a tripping scan still refuses, now with evidence)

**Refusal site** (`bundlebees_plugin/cli.py:1794-1802` at head) — the only site in the
plugin raising `secret_detected`:

```python
trips = _secret_trips(workdir, files, patch)
flagged = sorted({trip["file"] for trip in trips})
if flagged:
    _reset_patch(workdir)
    raise CliError(
        "secret_detected",
        _secret_refusal(flagged, trips),
        "Remove secrets from the worker output; nothing was committed or pushed.",
    )
```

Pre-fix, the same site was `flagged = _secret_scan(...)` / `if flagged: _reset_patch /
raise CliError("secret_detected", "secret scan found secret-like content in: " + …)`.
The diff audit shows: same decision variable (`flagged` non-empty), same
`_reset_patch`, same error code and hint, same raise — only the detail string is
enriched (`…; trips: <file> line=N rule=R excerpt="[redacted]"`). The enrichment is
computed from the same iteration that makes the decision, so a path that refuses
pre-fix refuses post-fix, and no new success path exists: `_secret_scan` is now
`sorted({trip["file"] for trip in _secret_trips(...)})` — a wrapper, not a second
decision.

**Predicate-layer parity:** `first_secret_rule` iterates `zip(_SECRET_RULE_IDS,
_SECRET_PATTERNS)` exactly as `looks_like_secret` iterates `_SECRET_PATTERNS`;
9 rule ids for 9 patterns (no zip truncation). 200,000-case randomized fuzz:
`looks_like_secret(s) == (first_secret_rule(s) is not None)` with **0 mismatches**;
both return falsy for non-strings.

**Differential harness (true pre-fix code vs head):** the base `cli.py` was loaded
from the base blob as a sibling module and its `_secret_scan` was compared against
the head `_secret_scan`/`_secret_trips` over 15 adversarial cases (no-patch whole-file
scan incl. missing files, private-key file, oversize file both in-patch and on disk,
clean replacement, added secret on line 3, hunk-less diff, `/dev/null` new file,
deletion-only diff, rename without hunks, `\ No newline` markers, context-only diff,
empty patch, mixed multi-file, and a case exercising every rule family):

- **flag-set mismatches: 0/15** (old flagged == new flagged, and == the file set of
  the structured trips);
- rule attribution per family correct (bearer/openai_key/github_token/github_pat/
  slack_token/aws_access_key/google_api_key/jwt/assignment);
- `_added_lines_detail` yields exactly the old added-line text plus 1-based numbers
  (`[1, 3]` for the line-3 case the tests pin).

Behavioral examples preserved by the harness: pre-existing secret-like text does not
block an unrelated patch (`patch-clean-replacement` → no flag), an added secret still
blocks (`patch-adds-secret-line3` → flag + `line=3 rule=assignment`), oversize files
still flag without line evidence, and deletion-only/context-only/rename diffs still
never flag.

## Check 5 — Tests gate at the reviewed head

`python -m pytest tests/test_secret_scan_added_lines.py tests/test_security.py
tests/test_client.py` (local venv, Python 3.12.3, pytest 8.4.2):

- **143 passed, exit 0** — matches the coordinator reference (143 passed, exit 0
  with `BUNDLEBEES_VERIFY_PYTHON` at an outside-home interpreter).
- Per file: `test_secret_scan_added_lines.py` 6 (2 pre-existing + 4 new),
  `test_security.py` 100, `test_client.py` 37.
- Full-suite context (both base and head, identical environmental failure sets):
  see Check 2.

## Check 6 — CI at the exact reviewed head (public API)

GitHub check-runs for commit `50507c65a128db1465aaaa56d2bfbde18d6788f5`
(run 44061678703), queried live during this review:

| check | status | conclusion |
|---|---|---|
| gitleaks | completed | **success** |
| hermes plugins doctor (offline) | completed | **success** |
| orbit round trip (real Orbit API, required) | completed | **success** |
| tests (sandbox included) | in_progress | — (pending) |

Combined status for the SHA: `pending` (0 classic statuses; the pending state comes
from the in-progress `tests` job). Per the review mandate, pending is recorded as a
**non-blocking observation**. The three completed required jobs are green at the
exact reviewed head, including the real Orbit round trip and the gitleaks history
scan. (The local 114 environmental sandbox failures here are exactly what the
`sandbox included` CI job covers: ubuntu-24.04 with bubblewrap installed.)

## Check 7 — Scanner hygiene (house scanner self-scan)

The house scanner is the plugin's own added-lines secret-scan predicate
(`bundlebees_plugin.security.looks_like_secret` / the verify added-lines scan) with
gitleaks as the configured scanner; gitleaks 8.21.2 was installed locally with the
CI-pinned sha256 checksum (`5bc41815…6e3ba`) verified before use.

- **Added lines of the delta** (193 lines extracted from `git diff main...pr73`):
  house predicate → **0 trips**; gitleaks (`--no-git --redact`) → **no leaks found,
  exit 0**. The test fixtures are runtime-built string concatenations exactly as the
  file's own comment promises ("Fixtures are built at runtime so this file's own
  added lines stay clean").
- **Mandated negative-control line** — the report convention's single mandated
  negative-control marker line is included below and must stay exempt:

  ```
  Authorization: Bearer ***
  ```

  Verified exempt under both scanners: the house predicate does not trip it and
  `scrub()` leaves it byte-identical (pinned by `tests/test_security.py`'s
  `negative-control-marker` fixture); gitleaks reports no leak for it.
- **Self-scan of this report** (`docs/review-pr73.md` at the final reviewed state):
  see the recorded result in the JSON block below — the scan is empty except for the
  one mandated negative-control line above (no other trips).
- Bonus: full-repo gitleaks history scan at the reviewed head (257 commits): **no
  leaks found** (CI's `gitleaks` job agrees: success).

## Verdict

**APPROVE.**

The change does exactly what the charter demands and nothing else:

1. The delta is exactly the three leased files with the stated shape; `security.py`
   is a pure additive export (9 rule ids parallel to 9 patterns, read-only view);
   `uv.lock` is absent from the delta and the tree.
2. The refusal is enriched, not weakened: same decision, same error code, same
   reset-and-refuse flow; the evidence (rule identity, 1-based line number, masked
   excerpt, oversize fallback without line evidence) is computed from the same
   iteration that decides. Flag-set identity to the pre-fix implementation was
   verified by randomized fuzz (0/200,000 mismatches) and a 15-case differential
   harness against the true base code (0 mismatches), plus the full suite failing
   identically at base and head (114 environmental failures, identical test-id sets;
   passed delta = exactly the 4 new tests).
3. RED-first is real both ways: removing the additive export fails the new tests
   (module cannot import), and dropping the masked-excerpt clause from the enriched
   refusal fails the test that pins it; both mutations were restored byte-identically
   with sha256 evidence.
4. The #71 contract is inviolable: both contract files are byte-identical to base
   (`test_client.py` sha256 equals the mandated `970eff46…c6902f15`), and the
   value-shape battery is green.
5. The acceptance gate is green at the reviewed head (143 passed, exit 0, matching
   the coordinator reference), CI at the head shows three completed required jobs
   green (gitleaks, plugin doctor, real Orbit round trip) with `tests (sandbox
   included)` in progress at review time — pending, non-blocking per mandate.
6. Scanner hygiene holds: the delta's added lines trip nothing; the report carries
   exactly the one mandated negative-control line, which stays exempt.

### Non-blocking open items

1. **Coordinator attestation not independently re-verifiable.** The PR body's
   reviewer mandate to re-verify `scratch/f6-payload.patch` → branch delta could not
   be performed: the payload is not in the tree and not on any fetched ref from this
   workspace. The *applied* delta is independently verified exact (Check 1); the
   exclusion itself remains a coordinator attestation.
2. **CI `tests (sandbox included)` still in progress** at review time (run
   44061678703). Non-blocking observation per mandate; recommend the coordinator
   confirm green before merge.
3. **Worker-reported intermediate stage** ("additive export at 100 green") not
   re-derivable from this workspace; recorded as worker-reported evidence. The
   endpoints (139 baseline + 4 new = 143) reconcile with independent observation.

## Summary

```json
{
  "summary": "PR #73 (fix/verify-refusal-evidence @ 50507c65) APPROVED: F6 refusal evidence (rule id + 1-based line + masked excerpt) is enrichment-only on an unchanged fail-closed decision; delta is exactly the three leased files; RED-first proven both ways with byte-identical restores; #71 contract byte-identical and green; gate 143 passed exit 0; CI at head 3 green + 1 pending (non-blocking); scanner hygiene clean with exactly the one mandated negative-control line.",
  "verdict": "APPROVE",
  "changed_files": [
    "docs/review-pr73.md"
  ],
  "checks": [
    {
      "name": "delta exactness (3-dot diff vs origin/main)",
      "command": "git diff --numstat refs/review-base/main...refs/review-head/pr73; git ls-tree -r refs/review-head/pr73 --name-only | grep -i 'uv.lock'",
      "passed": true
    },
    {
      "name": "local snapshot tree == PR head 50507c65",
      "command": "git diff ad09460 50507c65a128db1465aaaa56d2bfbde18d6788f5 (empty)",
      "passed": true
    },
    {
      "name": "RED-first mutation A: revert security.py additive export",
      "command": "git show refs/review-base/main:bundlebees_plugin/security.py > bundlebees_plugin/security.py && python -m pytest tests/test_secret_scan_added_lines.py (6 failed, RED)",
      "passed": true
    },
    {
      "name": "mutation A byte-identical restore",
      "command": "sha256sum bundlebees_plugin/security.py (81c915fa... before == after)",
      "passed": true
    },
    {
      "name": "RED-first mutation B: drop masked-excerpt field from _secret_refusal",
      "command": "python -m pytest tests/test_secret_scan_added_lines.py (1 failed, 5 passed, exit 1, RED)",
      "passed": true
    },
    {
      "name": "mutation B byte-identical restore",
      "command": "sha256sum bundlebees_plugin/cli.py (2f28e706... before == after)",
      "passed": true
    },
    {
      "name": "predicate parity fuzz (first_secret_rule vs looks_like_secret)",
      "command": "200000-case randomized fuzz: looks_like_secret(s) == (first_secret_rule(s) is not None), 0 mismatches; 9 rule ids == 9 patterns",
      "passed": true
    },
    {
      "name": "differential harness: base cli._secret_scan vs head _secret_scan/_secret_trips",
      "command": "PYTHONPATH=. python .review-tmp/diffharness/harness.py (15 adversarial cases, 0 flag-set mismatches, rule attribution OK)",
      "passed": true
    },
    {
      "name": "full-suite failure-set identity base vs head",
      "command": "pytest --tb=no -rf at head and at base worktree; diff of FAILED sets empty (114 environmental each, passed 1787->1791)",
      "passed": true
    },
    {
      "name": "#71 contract files unchanged",
      "command": "git diff refs/review-base/main...refs/review-head/pr73 -- tests/test_security.py tests/test_client.py (0 lines)",
      "passed": true
    },
    {
      "name": "test_client.py sha256 == mandated value",
      "command": "sha256sum tests/test_client.py (970eff469a594075be5f35bc97dc6dd89dcca783fceb02da6dd203d4c6902f15)",
      "passed": true
    },
    {
      "name": "tests gate at reviewed head",
      "command": "python -m pytest tests/test_secret_scan_added_lines.py tests/test_security.py tests/test_client.py (143 passed, exit 0)",
      "passed": true
    },
    {
      "name": "CI at exact reviewed head via public API",
      "command": "GET /repos/mojomast/bundlebees/commits/50507c65.../check-runs (gitleaks success, plugins doctor success, orbit round trip success, tests in_progress=pending non-blocking)",
      "passed": true
    },
    {
      "name": "scanner hygiene: added-lines self-scan",
      "command": "house predicate over 193 added lines (0 trips); gitleaks 8.21.2 --no-git over added lines (no leaks, exit 0)",
      "passed": true
    },
    {
      "name": "scanner hygiene: report self-scan (one mandated negative-control line only)",
      "command": "house predicate + gitleaks --no-git over docs/review-pr73.md (0 trips; sole credential-shaped line is the exempt negative-control marker)",
      "passed": true
    },
    {
      "name": "fail-closed refusal-site audit",
      "command": "diff audit of cli.py verify site: same condition/reset/CliError(secret_detected)/hint, detail enriched only; secret_detected raised at exactly one site",
      "passed": true
    }
  ],
  "open_issues": [
    "Coordinator attestation only: the PR-mandated re-verification of scratch/f6-payload.patch -> branch delta was not performable from this workspace (payload not in tree, not on any fetched ref); the applied delta IS independently verified exact.",
    "CI check 'tests (sandbox included)' was in_progress at review time (run 44061678703); pending recorded as non-blocking per mandate - confirm green before merge.",
    "Worker-reported intermediate stage 'additive export at 100 green' not re-derivable locally (recorded as worker-reported; endpoints 139+4=143 reconcile)."
  ]
}
```




