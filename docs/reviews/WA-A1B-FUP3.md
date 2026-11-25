VERDICT: APPROVE
BLOCKING: none

Independent review of bundlebees PR #290, squashed local commit `9893b6e` whose
message records the upstream head `e03d93c17d127032966cbdc3ac3fe2bdf683b733`
(this checkout has exactly one commit, so there is no base to diff). I read
`tests/test_green_path_hardening.py` in full (372 lines; 9 real tests plus the
two functions inside `TEST_SOURCE`) and all four hunks. The three FUP1 minors
are closed exactly as specified: a present null or non-mapping pin is refused
with `evidence_tampered`, `tdd refactor` and `tdd review` load the frozen ids
and pass them into `check_green`, and the verify-lane JUnit counters go through
`verify_local._junit_counter` and surface as `evidence_invalid`. No approved
test, gate, sandbox, check or legacy path was weakened or short-circuited, and
the no-pin path still behaves exactly as before. All nine approved scenarios
pass when replayed against the real product functions. Three minor,
non-blocking findings follow.

## Findings

1. **minor — `CHANGELOG.md:38`**: the new WA-A1B-FUP3 bullet sits inside the
   dated `## [0.2.0] - 2026-11-24` section (heading at line 27), not under
   `## Unreleased` (line 10), which contradicts the coordinator's "one bullet
   under Unreleased" fact. Docs-only and coordinator-owned, so no product
   impact. Fix: move the bullet into the `## Unreleased` list above line 27.

2. **minor — `bundlebees_plugin/tdd.py:985`**: `_collection_pin` maps a present
   but non-mapping `review` block to `{}`, so it returns `None` (the legacy
   unpinned path) instead of refusing. This is the same absence-vs-malformed
   ambiguity the hunk removes one level down. It is not reachable through the
   shipped CLI (the schema types `review` as an object and `check_frozen`
   requires a `Mapping`; I confirmed `check_frozen` rejects `"review": "x"`),
   but a direct caller that skips validation gets the permissive branch. Fix:
   keep `None` only for an absent `review`/`test_collection`, and raise
   `TddError("evidence_tampered", ...)` when `review` is present but is not a
   `Mapping`.

3. **minor — `bundlebees_plugin/acdd_cli.py:130-133`**: the `or` fallbacks
   still treat falsy malformed counters (`""`, `[]`, `{}`) as absent, so
   `_result` returns the case count / 0 instead of `evidence_invalid`. Truthy
   non-numeric counters are refused (AC-007, verified), and
   `verify_local._check_frozen_green` has the identical `or` fallback, so this
   is parity rather than a regression, and the approved test pins only
   `"many"`. Fix: treat only `None` as absent and pass every other present
   value through `_junit_counter` (this keeps the case-count fallback and the
   `0` default).

## Acceptance criteria (all replayed against the real functions)

| AC | Approved test | Result |
| --- | --- | --- |
| AC-001 | `test_null_pin_is_refused_not_treated_as_absent` | pass - a null and a string pin both still pass `check_frozen` and then raise `TddError("evidence_tampered")` in `frozen_approved_ids`; neither falls back |
| AC-002 | `test_absent_pin_still_returns_none` | pass - a frozen contract with no `test_collection` key returns `None` |
| AC-003 | `test_refactor_refuses_a_pinned_partial_green` | pass - rc 1, stdout has `tests_mismatch` and `T2`, persisted phases stay `["red", "green"]` |
| AC-004 | `test_refactor_records_the_exact_pinned_green` | pass - rc 0, `refactor: valid`, phases `["red", "green", "refactor"]` |
| AC-005 | `test_review_refuses_a_pinned_partial_green` | pass - rc 1, stdout has `tests_mismatch` and `T2`, persisted phases stay `["red", "green"]` |
| AC-006 | `test_review_records_the_exact_pinned_green` | pass - rc 0, `review: valid`, phases `["red", "green", "review"]` |
| AC-007 | `test_result_reports_a_non_numeric_junit_counter` | pass - each of `tests`/`failures`/`errors`/`skipped` set to `"many"` raises `cli.CliError("evidence_invalid")` naming that counter, never `ValueError` |
| AC-008 | `test_result_keeps_integer_counters` | pass - `(tests, failures, errors, skipped) == (3, 1, 0, 2)`, all `int`, and the two-case evidence still yields `tests == 2` |
| AC-009 | `test_record_green_reports_a_non_numeric_junit_counter` | pass - `record_green` raises `cli.CliError("evidence_invalid")` and the persisted phases stay `["red"]` |

Other judged items:

- `_cmd_refactor` (`acdd_cli.py:500-503`) and `_cmd_review` (`:580-583`) build
  `preflight = {"contract", "sha", "key"}`, load `frozen_approved_ids`, and
  pass `approved_ids=` inside the existing `except tdd.TddError` guard, so the
  refusals surface as the named `CliError` codes (`tests_mismatch`,
  `evidence_tampered`), not a traceback or `internal_error`.
- Extra probe: a null-pin contract run through both CLI handlers is refused
  with `evidence_tampered` and writes no REFACTOR/REVIEW.
- Extra probe: pin shapes `[]`, `0`, `""`, `["x"]`, a wrong 64-hex digest and
  `{}` are all refused with `evidence_tampered`; only a genuine absence
  returns `None`.
- Extra probe: the legacy no-pin path is unchanged - a partial GREEN still
  records REFACTOR and REVIEW when no pin key exists, while the exact pinned
  GREEN records in both cases.
- `_result` uses a function-local `from . import verify_local`, so there is no
  import cycle (the call succeeds before any module re-entry). `check_green`
  only invokes `check_collected_ids` when `approved_ids is not None`, so the
  legacy path is not tightened.
- The only docs/product lines touched are within the three leased files plus
  the coordinator's CHANGELOG bullet; I found no secret, hostname, private path
  or network call in the hunks.

## Commands run (command -> exit code)

- `git status --porcelain`; `git log --oneline -3`; `git rev-list --count HEAD`
  -> 0 (clean tree; one commit, message "base e03d93c...")
- `wc -l tests/test_green_path_hardening.py bundlebees_plugin/tdd.py bundlebees_plugin/acdd_cli.py CHANGELOG.md`
  -> 0 (372 / 1469 / 932 / 625)
- `grep -n "^def test_" tests/test_green_path_hardening.py` -> 0 (9 real tests
  plus `test_first_value` / `test_second_value` inside `TEST_SOURCE`)
- `sha256sum tests/test_green_path_hardening.py` -> 0
  (`acf487fd3e7af454466904aacf1e0df57b5bee4024b01d0580685e0a3586d977`,
  equal to the coordinator's RED value)
- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_green_path_hardening.py`
  -> 1 (`No module named pytest`; environment fact)
- `python3 -m py_compile bundlebees_plugin/acdd_cli.py bundlebees_plugin/tdd.py`
  -> 0
- `PYTHONPATH=. python3 -c "import bundlebees_plugin.tdd"` -> 1
  (`No module named jsonschema`; environment fact)
- `PYTHONPATH=/tmp/opencode/shim:. python3 /tmp/opencode/probe_fup3.py` -> 0
  (replays all nine approved test bodies: 9/9 PASS)
- `PYTHONPATH=/tmp/opencode/shim:. python3 /tmp/opencode/probe_extra.py` -> 0
  (legacy refactor/review, null-pin CLI refusals, pin-shape matrix,
  falsy-counter matrix: 6/6 PASS with the falsy-counter notes above)
- `git cat-file -t 2256ac48fac6cfdd40f22edfd7f35693e7e2f896` -> 128 (RED object
  absent in this squashed checkout)
- `ls bin` -> 2 (`bin/t1634-check` is not present in this checkout)
- `git diff --stat` -> 0 (empty before this review file)
- `rm -rf __pycache__ bundlebees_plugin/__pycache__ tests/__pycache__
  contrib/rizzstack_rails/__pycache__`; `git status --porcelain --ignored` -> 0
  (only this review file remains as a change)

DISCLOSURES: this image has neither `pytest` nor `jsonschema`; I replayed every approved test body in a plain-Python harness (a `try/except` in place of `pytest.raises`) and imported the plugin through a no-op `jsonschema.Draft202012Validator` shim in `/tmp`, so `contract.py`'s bundled `_schema_problem` (which implements every keyword the bundled schema uses) makes the accept/refuse decisions; no product file was modified for the probes, and the temporary git repos and state dirs live under `/tmp`. I did not write this code and have no conflict of interest; no network was used.

UNPROVEN: a byte-diff of the approved test against RED `2256ac48` (the object is absent, `git cat-file` exit 128; I verified the file's sha256 equals the coordinator's RED fingerprint and read all 9 bodies), the real frozen contract `f103...`, GREEN record `wave-a-A1b-fup3/0002-green.json`, the 9-AC/9-id GREEN run, the `bin/t1634-check` lease result (script absent here), and the 3787/3796-id suite comparison are coordinator-supplied facts not reproducible in this checkout; whole-tree scope cannot be diffed without a base, so I rely on the lease fact and reviewed the four described hunks plus their consumers. Merge, tag, release and deploy remain human gates.
