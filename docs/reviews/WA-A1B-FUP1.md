VERDICT: APPROVE

Independent review of the squashed PR head `aa7b4904450b48e8d7358560798e8de5c6d25275`
(one local commit, `375b258 base aa7b490...`). I read the approved specification
`tests/test_verify_frozen_pin.py` in full (15 real test functions; the other two
`def`s are inside `TEST_SOURCE`), the three leased product files, the pin/freeze
machinery they depend on (`contract.freeze_contract`/`check_frozen`,
`tdd.load_collection`/`collection_digest`, `leases.evidence_key`), and every
GREEN writer and consumer. The slice closes the exact gaps recorded in
`docs/reviews/WA-A1B.md` findings 1, 2, 3, 5 and 6. All 15 AC tests were
replayed offline and pass; the product code compiles. No blocker or major
defect found.

1. **minor (fail-open edge for an explicit null pin; `bundlebees_plugin/tdd.py:988-991`)** -
   `pin = review.get("test_collection"); if pin is None: return None` treats a
   present-but-null `review.test_collection` exactly like an absent pin, so the
   gate silently disappears. The contract schema does not define
   `review.test_collection` and does not set `additionalProperties: false`
   there, and `check_frozen` does not hash/validate `review`, so
   `{"review": {..., "test_collection": null}}` passes `check_frozen` and
   returns legacy `None`. The freeze API never writes null, so this is not
   reachable from `freeze_contract`, but it is the one malformed-pin shape the
   new loader does not refuse. Concrete fix: distinguish absence from a bad
   value, e.g. `if "test_collection" not in review: return None` followed by the
   existing `if not isinstance(pin, Mapping): raise TddError("evidence_tampered", ...)`.

2. **minor (residual coverage of the same class; `bundlebees_plugin/acdd_cli.py:496` and `:574`)** -
   `_cmd_refactor` and `_cmd_review` still call
   `tdd.check_green(contract, sha, red, green)` without `approved_ids`, so a
   partial GREEN already stored before this fix could still be used to record
   REFACTOR/REVIEW evidence. This is not a certification bypass: both GREEN
   writers are now pinned, and the final accept gate
   `integrate_preflight` refuses the stored partial GREEN (AC-005). Concrete
   fix: reuse the shared loader in those two handlers and pass
   `approved_ids=`, as `record_green`/`integrate_preflight` now do.

3. **minor (diagnostics on the other GREEN recorder; `bundlebees_plugin/acdd_cli.py:124-130`)** -
   `acdd_cli._result` still coerces JUnit counters with bare `int(...)`, so a
   non-numeric counter on the `hermes bundlebees verify` lane raises
   `ValueError` (surfaced as `internal_error`) before the pin check instead of a
   named refusal. AC-011 explicitly names the commit-local lane, where
   `_junit_counter` now raises `VerifyError("evidence_invalid")`; the CLI still
   refuses and pushes nothing. Concrete fix: reuse the defensive counter helper
   in `_result` so both recording surfaces report the same named error.

## Acceptance criteria

| AC | Approved test | Result |
| --- | --- | --- |
| AC-001 | `test_frozen_approved_ids_loads_the_pinned_selection` | pass - pinned contract returns `[T1, T2]`; an unpinned frozen contract returns `None` |
| AC-002 | `test_frozen_approved_ids_refuses_a_tampered_pin` | pass - re-signed subset manifest -> `evidence_tampered`; empty selection -> `tests_mismatch` |
| AC-003 | `test_record_green_refuses_a_partial_run_and_persists_nothing` | pass - `tests_mismatch` naming `T2`; record phases stay `["red"]` |
| AC-004 | `test_record_green_persists_the_exact_frozen_run` | pass - one GREEN persisted and accepted by `check_green(..., approved_ids=[T1, T2])` |
| AC-005 | `test_integrate_preflight_refuses_a_stored_partial_green` | pass - stored partial GREEN -> `tests_mismatch` naming `T2` |
| AC-006 | `test_integrate_preflight_accepts_a_stored_exact_green` | pass - returns the contract item with the frozen sha |
| AC-007 | `test_cli_verify_refuses_a_partial_green_for_a_pinned_contract` | pass - rc 1, stdout has `tests_mismatch`, only `red` evidence, `origin/feat/pin` still at base |
| AC-008 | `test_cli_verify_records_the_exact_frozen_run` | pass - dry-run records GREEN and `check_green(..., approved_ids=...)` accepts it |
| AC-009 | `test_verify_and_commit_persists_the_checked_green_record` | pass - persisted GREEN revision equals the returned local commit sha and its cases are exactly `T1`/`T2` |
| AC-010 | `test_verify_and_commit_refuses_a_partial_green` | pass - `VerifyError("tests_mismatch")`, record phases stay `["red"]` |
| AC-011 | `test_verify_and_commit_reports_a_non_numeric_junit_counter` | pass - `VerifyError("evidence_invalid")`, no `ValueError` |
| AC-012 | `test_check_collected_ids_refuses_a_skipped_case` | pass - `skipped` and `xfailed` call-stage cases -> `tests_mismatch` naming `T1` |
| AC-013 | `test_check_green_reports_green_failed_before_tests_mismatch` | pass - failing GREEN reports `green_failed`, not `tests_mismatch` |
| AC-014 | `test_parse_junit_cases_does_not_stop_at_two_thousand` | pass - 2,100 cases parsed with the default `MAX_JUNIT_CASES = 20_000` |
| AC-015 | `test_unwired_check_green_accepts_the_partial_run` | pass - `check_green` without `approved_ids` still returns the partial GREEN (legacy behaviour preserved) |

Other judged items: both GREEN writers (`acdd_cli.record_green` and
`verify_local._check_frozen_green`) now load the pin and pass `approved_ids=`
before signing; `verify_and_commit` persists exactly the record it checked
(revision is the local commit, cases are the measured cases); the legacy
no-pin path is unchanged; the CHANGELOG has exactly one new bullet under
`## Unreleased`; no host, IP, secret or private path is added. No approved test
was edited to my knowledge: I cannot byte-diff it against RED `144b9a0` because
this squashed checkout has no base object (`git cat-file` returns 128), so I
rely on the coordinator's `bin/t1634-check WA-A1B-FUP1` fact; the file contains
exactly the 15 test bodies mapped to AC-001..AC-015 and I found no
contradiction with that fact.

## Commands run (command -> exit code)

- `git log --oneline -3`; `git status --porcelain`; `git branch -a` -> 0 (single squashed commit, clean tree)
- `wc -l tests/test_verify_frozen_pin.py bundlebees_plugin/tdd.py bundlebees_plugin/acdd_cli.py bundlebees_plugin/verify_local.py` -> 0
- `grep -c "^def test_" tests/test_verify_frozen_pin.py` -> 0 (17; 15 real tests plus the 2 functions inside `TEST_SOURCE`)
- `git cat-file -t 144b9a036221356d5719b3f72cd4d10243b94f5f` -> 128 (object absent; no base to byte-diff)
- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_verify_frozen_pin.py` -> 1 (`No module named pytest`; environment fact, not a defect)
- `python3 -m py_compile bundlebees_plugin/tdd.py bundlebees_plugin/acdd_cli.py bundlebees_plugin/verify_local.py` -> 0
- ad-hoc script, shared pin loader with an HMAC-signed manifest: exact ids, unpinned `None`, re-signed digest, empty selection, non-mapping pin, missing key, wrong count, wrong manifest/pin `source_rev`, id without `::` -> 0 (all behaved as specified: `None` / `evidence_tampered` / `tests_mismatch`)
- ad-hoc script, `parse_junit_cases` on a 2,100-case JUnit -> 0 (2,100 cases, default bound 20,000); `check_collected_ids` with `skipped` and `xfailed` -> 0 (both `tests_mismatch` naming the id)
- ad-hoc script, `check_green`: unwired partial accepted, wired partial refused, exact accepted/returned, skipped refused, failing GREEN -> `green_failed`, setup-stage and duplicate ids refused -> 0
- ad-hoc script, `record_green` partial (no GREEN written, `tests_mismatch`) and exact (one GREEN persisted) plus `integrate_preflight` partial/exact -> 0
- ad-hoc script, real temporary git repo for `verify_and_commit`: AC-009 persisted GREEN matches the checked revision/cases, AC-010 `tests_mismatch` with only `red` left, AC-011 `evidence_invalid` -> 0
- ad-hoc harness replaying all 15 approved test bodies (in-memory `jsonschema` stub and `pytest.raises` shim; no pytest available) -> 0, 15/15 PASS
- `rm -rf __pycache__ bundlebees_plugin/__pycache__ tests/__pycache__ contrib/rizzstack_rails/__pycache__`; `git status --porcelain --ignored` -> 0 (generated caches removed; only this review file remains untracked)
