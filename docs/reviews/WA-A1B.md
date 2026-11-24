VERDICT: APPROVE

# Review: WA-A1B - GREEN must run exactly the frozen collected-test ids (PR #272, head b521734)

Independent review of the squashed checkout at `b521734` (`411b15f base ...`).
I read the two production files in full, the approved test file in full, the
collection/freeze machinery the pin depends on (`acdd_cli._collect_manifest`,
`acdd_cli.verify_preflight`, `tdd.load_collection`/`collection_digest`,
`test_receipts`), and every caller of the changed helpers. The approved test
file contains exactly the 10 named cases and matches its brief; I could not
byte-diff it against RED `5640491874` because this checkout is one squashed
commit with no base (environment fact, not a defect). No tracked file outside
`docs/reviews/WA-A1B.md` was modified.

The leased change is correct and fails closed on every input the contract
names. `check_collected_ids` requires exact id equality after discarding
non-`call` stages, rejects missing/extra/duplicate ids, duplicate approved ids,
an empty approved list, and malformed cases (non-mapping, missing/empty/non-str
`id`). `check_green(..., approved_ids=...)` is additive and keyword-only, so all
existing callers keep the old behaviour. `verify_local` computes the pin once
(`verify_local.py:1091`), switches the named run to `sandbox.pytest_argv([])`
so per-case JUnit evidence exists (`:1092`), checks the GREEN cases before
accepting the exit code (`:1276-1281`), re-checks against the committed tree
(`:1326-1330`), and keys the SHA-256 receipt/identity material on the approved
set (`:560-561`, `:633-634`, `:803`, `:1146`, `:1508`) so a cached result cannot
cross approved sets. No gate, sandbox, check or test is weakened, skipped or
short-circuited; the legacy (no-pin) path behaves exactly as before, which I
exercised directly.

## Acceptance criteria (all met)

| AC | Approved test | Result |
| --- | --- | --- |
| AC-001 | `test_check_green_accepts_the_exact_run` | pass - `tdd.check_green(..., approved_ids=APPROVED)` accepts the exact GREEN record (tdd.py:1135-1136; reproduced) |
| AC-002 | `test_check_green_refuses_a_partial_run_when_approved_ids_are_given` | pass - refuses `tests_mismatch` (tdd.py:1135-1136, 1100-1104; reproduced) |
| AC-003 | `test_collection_stage_cases_do_not_count_as_run` | pass - non-`call` stages are dropped, so the id is missing (tdd.py:1074-1076; reproduced) |
| AC-004 | `test_duplicate_id_is_refused` | pass - `observed[id] != 1` (tdd.py:1082; reproduced) |
| AC-005 | `test_empty_approved_list_fails_closed` | pass - `if not approved_ids` (tdd.py:1085; reproduced) |
| AC-006 | `test_exact_match_passes` | pass - returns `None` (reproduced) |
| AC-007 | `test_extra_id_is_refused` | pass - observed set minus approved set (tdd.py:1081; reproduced; message names the id) |
| AC-008 | `test_failure_code_is_documented` | pass - `tests_mismatch` in `FAILURE_CODES` (tdd.py:69; reproduced) |
| AC-009 | `test_missing_approved_id_is_refused` | pass - approved set minus observed set (tdd.py:1080; reproduced; message names the id) |
| AC-010 | `test_order_does_not_matter` | pass - set/dict comparison, no ordering (reproduced) |

Additional checks: duplicate approved ids, malformed/non-mapping cases, an
`id` that is not a string, a pin that is not a mapping, a tampered manifest
digest, a pin/contract `source_rev` mismatch, a wrong `collected` count, an
empty collected set and an id without `::` all raise rather than pass (see the
commands below). The pin loader also re-loads the manifest through
`tdd.load_collection`, which enforces the HMAC signature and the
`contract_sha256` binding, so the ids cannot be swapped for another contract's.

## Findings

1. **major (coverage / follow-up; outside this lease, not a regression)** -
   the exact-id check is not on the `verify` path that certified this slice.
   `hermes bundlebees verify` (no `--commit-local`) runs `_cmd_verify`
   (`cli.py:2937`), which calls `acdd_cli.verify_preflight` (`cli.py:3002`) and
   then `acdd_cli.record_green` (`cli.py:3138`); `record_green` calls
   `tdd.check_green(contract, sha, red, record)` with no `approved_ids`
   (`acdd_cli.py:794`), and `integrate_preflight` does the same
   (`acdd_cli.py:857`). The recorded GREEN therefore still accepts a partial
   run, and `verify_local._check_frozen_green`'s checked record
   (`verify_local.py:105-140`) is discarded, never signed into the tdd store.
   The `verify_local` wiring is live on the lanes that call
   `verify_and_commit` (`verify_workers.py:628`, `beat_reconciliation.py:1140`,
   `job_cli.py:534`), but it has no test at all: grep finds no reference to
   `_frozen_approved_ids` or `_check_frozen_green` under `tests/`, and the
   approved file only covers `tdd.py`. Concrete fix (new slice, `acdd_cli.py`
   is not leased here): load the pinned ids in `record_green`/
   `integrate_preflight` (extract `_frozen_approved_ids` to a shared helper),
   pass `approved_ids=` to `check_green`, persist the checked GREEN record, and
   add a test that drives one pin through `verify_and_commit`. Until then the
   CHANGELOG claim should be read as "the commit-local verifier", not "all
   GREEN".

2. **minor (fail-open in the new gate; worth closing before pins are relied
   on)** - `check_collected_ids` keys only on `id` and `stage`
   (`tdd.py:1069-1077`); `outcome` is never inspected. A test that pytest
   reports as `skipped` has `stage="call"`, so it counts as an executed run,
   and `classify()` returns `passed` for an exit-0 skip-only result
   (`tdd.py:185-186`). I reproduced a fully skipped run of both approved ids
   being accepted by `check_collected_ids` and classified `passed`. That makes
   an approved test such as `@pytest.mark.skipif(not hasattr(mod, "new_api"))`
   a false GREEN when the patch removes the API. The function docstring says
   "exactly one executed case", so this contradicts its own contract even
   though it is outside the ten ACs. Fix: require `case.get("outcome") ==
   "passed"` (or explicitly refuse `skipped`/`xfailed`) for each call-stage id
   and add an approved case for it.

3. **minor (false refusal for large approved sets)** - the freeze allows
   `_MAX_COLLECTED_IDS = 20_000` (`acdd_cli.py:39`) but the JUnit parser that
   feeds `cases` stops at `limit=2000` (`tdd.py:263`, used from
   `sandbox.py:1273`). Any contract whose approved test files collect more than
   2,000 ids will now fail `tests_mismatch` (missing ids) even though GREEN
   passed. It fails closed, but it silently caps the size of a pinnable
   contract. Fix: align the two limits, or raise the parser limit to the
   collection bound and bound total bytes instead.

4. **minor (false refusal for statically-collected parametrized tests)** - when
   the approved file cannot be imported at `source_rev`, freeze falls back to
   AST ids `path::test_name` (`acdd_cli.py:181-204`) while a GREEN run reports
   `path::test_name[param]`; exact matching then refuses a legitimate run.
   A1b's file is not parametrized and imports cleanly, so this does not affect
   this slice. Fix: normalize/expand static ids through the same resolver at
   freeze time, or compare with `tdd.resolve_test_ref` semantics for
   statically-collected files only.

5. **minor (diagnostics only)** - `check_green` runs the approved-id check
   (`tdd.py:1135-1136`) before the classification check (`tdd.py:1137-1142`),
   so a GREEN that both fails and has an id mismatch reports `tests_mismatch`
   and hides `green_failed`. The run is still refused; only the operator's
   first error is less useful. Fix: move the classification check above the
   id check.

6. **minor** - `_check_frozen_green` coerces the JUnit counters with bare
   `int(...)` (`verify_local.py:119-122`). A non-sandbox/injected runner that
   returns a non-numeric `junit` value raises an uncaught `ValueError`/
   `TypeError` instead of a `VerifyError`; production `_junit_summary` returns
   ints, so this is test/diagnostic hardening only. Fix: coerce defensively or
   reuse the normalizer used by `_receipt_evidence`.

## Verification performed (command -> exit code)

Read-only inspection (all exit 0): `git log --oneline -5`; `git status`;
`git status --porcelain`; `git rev-parse HEAD`; `git show --stat --oneline
HEAD`; `ls`; `ls docs/reviews`; `wc -l bundlebees_plugin/tdd.py
bundlebees_plugin/verify_local.py tests/test_collected_ids_exact.py
CHANGELOG.md`; `sed -n`/`cat` over `bundlebees_plugin/tdd.py` (1-130,
560-680, 880-980, 1000-1180, 1128-1145, 1054-1106), `verify_local.py` (1-180,
180-360, 520-650, 720-760, 760-1000, 105-140, 1060-1200, 1200-1350,
1340-1530, 1530-1640), `tests/test_collected_ids_exact.py`,
`bundlebees_plugin/acdd_cli.py` (40-70, 100-125, 160-300, 355-425, 720-880),
`bundlebees_plugin/contract.py` (544-624, 690-740),
`bundlebees_plugin/sandbox.py` (930-1000, 1200-1300),
`bundlebees_plugin/test_receipts.py` (60-260, 380-440, 540-700),
`bundlebees_plugin/security.py` (203-223), `docs/ACDD.md` (140-175),
`docs/reviews/WA-A10.md`, `docs/reviews/WA-A11.md`; `grep -n`/`grep -rn` for
`check_green`, `check_collected_ids`, `approved_ids`, `test_collection`,
`FAILURE_CODES`, `_verification_identity`, `execution_identity`, `pytest_argv`,
`_MAX_COLLECTED_IDS`, `_junit_id`, `scan_patch`/`subversion_scan`.

Checks:

- `python3 -m py_compile bundlebees_plugin/tdd.py bundlebees_plugin/verify_local.py` -> 0
- `python3 -m py_compile tests/test_collected_ids_exact.py` -> 0
- `grep -c "^def test_" tests/test_collected_ids_exact.py` -> 0 (10)
- `python3 -c "import pytest, sys; print(...)"` -> 1 (`No module named pytest`; sandbox fact, not a defect)
- `python3 -c "import jsonschema; print(...)"` -> 1 (`No module named jsonschema`; the ad-hoc scripts below inject an in-memory stub module only, no file is written)
- ad-hoc script: replay of all 10 AC behaviours plus duplicate-approved, malformed case, non-str `id` -> 0 (every case PASS; "all_pass True")
- ad-hoc script: `check_green` wiring - partial refused `tests_mismatch` with `test_two` in the message, exact accepted, extra refused with `test_extra`, and `approved_ids=None` still accepts the partial run (legacy) -> 0 (all PASS)
- ad-hoc script: `_frozen_approved_ids` with a real HMAC-signed manifest written to a temp state dir - valid pin returns the ids; no pin returns `None`; wrong `manifest_sha256`, wrong pin `source_rev`, wrong `collected`, non-mapping pin all -> `evidence_tampered`; wrong contract sha and unknown contract -> `tests_not_collected` -> 0 (all PASS)
- ad-hoc script: empty `collected_ids` -> `tests_mismatch`; id without `::` -> `evidence_tampered` -> 0 (both PASS)
- ad-hoc script: `tdd.classify` on an all-`skipped` exit-0 result -> `passed`, and `check_collected_ids` accepts it -> 0 (this is finding 2)

No pytest was available, so the approved file was not executed by pytest; its
ten test bodies were replayed verbatim in the ad-hoc scripts above (same
fixtures, same assertions) and all pass. The coordinator's full-suite
comparison (3688/224 -> 3698/224, 0 new, 0 resolved) is consistent with these
10 cases being the only additions; I did not re-derive it.
