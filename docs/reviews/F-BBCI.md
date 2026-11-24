VERDICT: APPROVE

# F-BBCI — independent review of PR #268 (CI test sharding)

Scope reviewed: `.github/workflows/ci.yml`, `.github/scripts/shard_tests.py` (new),
`.github/test-weights.json` (new data), `tests/test_shard_tests.py` (new), `CHANGELOG.md`.
Head 6673e89981885401854fa4d42421b28d276fc8e0. Nothing in the diff weakens the sandbox
setup, gitleaks, the pinned checkouts or `ci-gate`, and the partition is exact at this head
(191 files, 54/45/46/46, 191 unique). **There are no blockers.** One major advisory and four
minor findings follow; none of them changes current behaviour.

## Findings

1. **major — the per-shard partition check is self-referential, so it cannot detect a
   collectable file that the enumerator misses.** `.github/scripts/shard_tests.py:22` builds
   the list with hardcoded `("test_*.py", "*_test.py")`; `--check` at
   `.github/scripts/shard_tests.py:39-46` compares `shards()`' output against the *same*
   `test_files()` (the greedy loop adds every enumerated file exactly once), and
   `tests/test_shard_tests.py:13-21` makes the same tautological comparison. Two independent
   calls happen, so a file appearing between them would be caught, but a file absent from
   `test_files()` for good (custom `python_files`, a dir matched by pytest's default
   `norecursedirs` such as a dot-dir, or a future change to pytest's defaults) is invisible:
   `--check` passes, every shard prints a partition of the smaller list, and the test is
   silently not run. `.github/workflows/ci.yml:86` runs the check before pytest as required,
   but the check proves the algorithm partitioned *its own* input, not pytest's collection.
   Current-head exposure is nil: `pyproject.toml:24-27` sets no `python_files`, all 191 files
   are direct `tests/test_*.py` children, no `*_test.py` file exists, and the coordinator
   verified set-identity with `pytest --collect-only`. Fix: give the check an independent
   oracle — derive `python_files`/`norecursedirs` from `pyproject.toml` with `tomllib`, and/or
   compare `test_files()` against `python -m pytest --collect-only -q -p no:cacheprovider`
   once (in the unit test, which runs in one shard) — or at minimum stop claiming the stronger
   guarantee (see finding 5).

2. **minor — the shard list's exit status is not propagated, only emptiness is checked.**
   `.github/workflows/ci.yml:87-88`: `mapfile -t files < <(python3 .github/scripts/shard_tests.py …)`
   discards the producer's status; `test "${#files[@]}" -gt 0` catches only a fully empty
   result. Today this fails closed because the script builds the whole list and prints once at
   the end (`.github/scripts/shard_tests.py:56`), so a crash yields no output and the job fails.
   A future change that streams output could run a partial shard and pass. Fix:
   `files_out=$(python3 .github/scripts/shard_tests.py "${{ matrix.shard }}" 4)` then
   `mapfile -t files <<<"$files_out"` (command substitution propagates the status under
   `set -e`), and optionally assert `len(files)` equals the count `--check` reports for that shard.

3. **minor — the shard total is written three times and one copy is untested.**
   `.github/workflows/ci.yml:30`, `:86`, `:87` each hardcode 4; `tests/test_shard_tests.py:26-30`
   asserts only the matrix literal and `--check 4`, not the trailing total on the print call.
   A single edit that changes the print total to 8 while leaving the matrix at 4 and
   `--check 4` passes every check and runs only 4 of 8 groups — half the suite silently
   dropped. (The opposite mismatch fails closed via the index guard, and the empty-shard guard
   covers total > files.) Fix: one job-level `SHARD_TOTAL: 4` env used as
   `${{ matrix.shard }} "$SHARD_TOTAL"` and `--check "$SHARD_TOTAL"`, plus a test that the
   matrix has exactly `SHARD_TOTAL` entries.

4. **minor — `check(total)` has no `total >= 1` guard.** `.github/scripts/shard_tests.py:39-46`
   calls `shards(0)`, which dies in `min()` on an empty load list: `--check 0` exits 1 with a
   traceback instead of a message. No CI path uses 0 (the print branch guards at `:54`), so
   this is cosmetic. Fix: `if total < 1: sys.exit("total must be >= 1")` at the top of `check`.

5. **minor — docs overstate the check's strength.** `.github/scripts/shard_tests.py:7-9`
   ("Every collectable test file lands in exactly one shard") and `CHANGELOG.md:12`
   ("fails if the shards are not an exact partition of every collectable test file") claim the
   stronger property that finding 1 shows the check cannot establish on its own. Fix either by
   implementing finding 1 or by rewording to "checks that the shard lists are a complete,
   duplicate-free partition of the test files it enumerates".

## Judgement on the coordinator flags

- **(a) `tests` check-run rename — safe within this repo.** A repo-wide search finds no
  consumer of the old display name: it is now only in `.github/workflows/ci.yml:24` and the
  CHANGELOG prose; no test, script, docs page or other workflow matches
  `tests (sandbox included)`, and nothing reads check-run names (`statusCheckRollup`,
  `required_checks`, `contexts` etc. have no call sites). The supplied ruleset requires only
  `ci-gate`, `gitleaks`, `slop-lint`, so the four new names
  `tests (sandbox included) (N/4)` cannot unset a required check. External consumers outside
  this checkout cannot be verified here; that is the only residual risk and it is not
  evidenced in the repository.
- **(b) 3 of 191 files without a weight — acceptable, not a balance finding.** The median
  fallback (`.github/scripts/shard_tests.py:30`) gives them 1.25 s each; measured loads are
  126.45 / 126.55 / 126.50 / 126.55 s against an ideal of 126.5125 s, i.e. a spread of 0.1 s.
  The nine `0.0` entries are legitimate sub-0.05 s measurements and only affect ordering among
  themselves. The table is a static snapshot with no freshness check, but the fallback is
  deliberate and documented.
- **(c) Duplicated pytest patterns — this is finding 1 (major).** Today the defaults match
  (`testpaths = ["tests"]` at `pyproject.toml:25`, no `python_files` override, no
  `*_test.py` files, no nested test dirs), so the exposure is limited to a future config or
  pytest-default change, but the failure mode is silent test loss, hence major rather than minor.

## Verified with no findings

- **No test is skipped (AC-001).** `--check 4` runs in every shard at
  `.github/workflows/ci.yml:86` before pytest; `set -euo pipefail` (`:85`), the non-empty guard
  (`:88`) and pytest as the last command (`:89`) propagate every failure. The diff adds no
  `-k`, `--ignore`, `--deselect`, marker, `collect_ignore` or conditional skip, and `-rs` is
  still passed, so skip reasons surface as before. pytest exit 5 (a shard listing only
  zero-test files) would also fail the shard. Shards are 54/45/46/46 = 191 unique files.
- **Required check names (AC-002).** `ci-gate` job id and name are unchanged
  (`.github/workflows/ci.yml:213-214`), `secret-scan` still names its check `gitleaks` (`:197`),
  `.github/workflows/slop-lint.yml` is untouched and still names its job `slop-lint`.
  `check_ci_gate.py` passes, which also enforces `ci-gate`'s name, `if: always()`,
  `toJSON(needs)` and its need of every job.
- **ci-gate still needs tests and fails with any shard (AC-002/AC-003).**
  `.github/workflows/ci.yml:217-222` has `if: always()` and `needs: [tests, …]`; a matrix job
  reports failure if any shard fails, so `needs.tests.result` is non-success. `:233-246` exits
  1 unless every result is `success`. Nothing adds `continue-on-error`, `if: always()` on the
  test step, or a swallowed exit code. `fail-fast: false` (`:28`) only makes the remaining
  shards keep reporting; it does not mask a failure.
- **AC-003.** Only the job display name, the `strategy`/`matrix` block, the pytest invocation
  and the `if: ${{ matrix.shard == 1 }}` on the schema measurement changed. Sandbox setup,
  the two bwrap sysctls, the in-job gitleaks install, the pinned workflow and hyvv checkouts and
  every other step are byte-identical to the reviewed base.
- **`.github/scripts/shard_tests.py` logic.** 1-based index with the `1 <= index <= total`
  guard (`:54-55`); empty-shard guard (`:44-45`); deterministic greedy LPT sorted by
  `(-weight, name)` with ties on `(load, shard-index)` (`:31-36`); median default (`:30`);
  each file appended exactly once; `ROOT = parents[2]` (`:16`), so it is cwd-independent; a
  missing/malformed weights file fails closed or falls back to uniform weight as documented.
  `test-weights.json` parses as 188 unique numeric keys, all under `tests/`, no zero-or-negative
  arithmetic hazard.
- **pytest invocation equivalence.** The explicit file args bypass `testpaths` only; rootdir
  still resolves to the repo root through `pyproject.toml:24`, so `pythonpath = ["."]`,
  `addopts = -q` and `tests/conftest.py` still apply. All 191 files are direct children of
  `tests/` and each shard list is sorted, so the relative execution order inside a shard is the
  same lexical order the old single run used. Shards run in separate processes on separate
  runners, so there is no shared session state; `tests/conftest.py` has only a function-scoped
  autouse fixture. Residual cross-file-ordering risk is therefore low, and the green sharded
  run at this head is consistent with that.
- **Schema measurement on shard 1.** `scripts/measure_schemas.py` only prints and fails above
  the token budget; nothing consumes its output (no artifact, no step summary). Its exit code
  is still gated through shard 1 -> `tests` -> `ci-gate`, and AC-003 explicitly allows the
  one-shard measurement. It is also skipped when shard 1's pytest fails, exactly as it was
  skipped after a failing suite before.
- **Hygiene.** No secrets, hosts, IPs or absolute paths in the added/changed content; keys in
  the weights file are repo-relative; scope is the five stated files.

## Commands run (exact, with exit codes)

Repository state: `git status --short` before writing this review printed nothing (clean).

1. `python3 .github/scripts/shard_tests.py --check 4` -> exit 0
   (`191 test files in 4 shards: 54, 45, 46, 46`)
2. `python3 .github/scripts/shard_tests.py 1 4` -> exit 0 (54 files);
   `… 2 4` -> exit 0 (45); `… 3 4` -> exit 0 (46); `… 4 4` -> exit 0 (46)
3. `cat $TMPDIR/shard*.txt | sort -u | wc -l` -> exit 0 (191) and
   `cat $TMPDIR/shard*.txt | wc -l` -> exit 0 (191); `… | sort | uniq -d` -> exit 0 (empty)
4. `python3 -m unittest discover -s tests -p 'test_shard_tests.py' -v` -> exit 0
   (`Ran 3 tests … OK`)
5. `python3 -m compileall -q .github/scripts/shard_tests.py` -> exit 0
6. `python3 .github/scripts/check_ci_gate.py .github/workflows/ci.yml` -> exit 0
   (`OK ci-gate needs every job …`)
7. `python3 .github/scripts/shard_tests.py --check 200` -> exit 1 (`a shard is empty`)
8. `python3 .github/scripts/shard_tests.py --check 0` -> exit 1 (traceback; finding 4)
9. `python3 .github/scripts/shard_tests.py 0 4` -> exit 1; `… 5 4` -> exit 1
   (`index must be in 1..total`); `… 4 4` -> exit 0
10. `cd $TMPDIR && python3 <repo>/.github/scripts/shard_tests.py --check 4` -> exit 0
    (same 54/45/46/46; confirms cwd independence)
11. `bash -c 'set -euo pipefail; … --check 4; mapfile -t files < <(… <shard> 4); test ${#files[@]} -gt 0; …'`
    for shard 1..4 -> exit 0 each; the same with index 9 -> exit 1 (guard fails closed)
12. `python3 - <<'PY'` monkeypatching `test_files()` to add a file on its second call ->
    exit nonzero, `shards do not partition the test files`
13. `python3 - <<'PY'` weights/partition analysis -> exit 0 (188 entries, 0 duplicate JSON
    keys, 3 missing, loads 126.45/126.55/126.50/126.55, ideal 126.5125)
14. `python3 -m pytest -q tests/test_shard_tests.py` -> not run: this image has no pytest
    (`python3 -m pytest --version` -> exit 1, "No module named pytest"); the unittest runner in
    step 4 covers the same file, per the brief, and this is not a finding.
15. `rg` sweeps for the old check name, `collect_ignore`/`-k`/`--ignore`/`--deselect`,
    absolute paths/IPs/URLs in the changed files, and workflow/status-check consumers -> exit 0
    for the sweeps, with the results reported above (no old-name consumer, no deselection added).

Review file is the only path this review added; it is left uncommitted and nothing was pushed.
