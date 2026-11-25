VERDICT: APPROVE

Independent review of the squashed PR head `a1470fa561f9453872f8a66812f5668f4a9f2d91`
(one local commit `b14c73a base a1470fa...`). The sandbox has no base object
(`git cat-file -t 47c9d5aee888` -> 128) and no pytest, so the approved test file
was read in the checkout and replayed offline with a minimal pytest shim. I
read every changed hunk in `bundlebees_plugin/tdd.py`,
`bundlebees_plugin/acdd_cli.py` and `bundlebees_plugin/verify_local.py`, the
pin/collection machinery they depend on (`_collection_pin`,
`_frozen_collection_ids`, `load_collection`, `collection_digest`,
`contract.freeze_contract`/`check_frozen`, `leases.evidence_key`), and every
caller of the changed helpers. The slice closes finding 4 of the WA-A1B review
(`docs/reviews/WA-A1B.md:100-107`) with the alternative that review recommended:
`resolve_test_ref` semantics for statically collected files only. No blocker,
major or functional minor defect was found; the two items below are a wording
nit and the mandatory residual disclosure.

**Residual (disclosed, by design; `bundlebees_plugin/tdd.py:1182-1197`,
AC-011):** a *partial* parametrization of a statically collected approved id is
accepted, because freeze time cannot know the parameter count. `check_collected_ids([S], cases(S_child), static_ids=[S])`
passes. This residual is pinned by
`test_a_partial_parametrization_is_the_documented_residual` (AC-011) and must not
be widened: the code still refuses an unrun approved id, an extra observed id,
a duplicate observed case, an unapproved/duplicated static id, and a skipped,
xfailed or collection-stage case. I verified each of those boundaries directly
(see checks below); none is loosened.

## Findings

1. **minor (wording only; `CHANGELOG.md:12`)** - the bullet says the static id
   covers "the exact id and its bracket-parametrized children", but the
   implementation delegates to `tdd.resolve_test_ref` (`tdd.py:889-902`), which
   also matches `::`-suffixed children (`item.startswith(text + "::")`). There
   is no behavioural gap in this slice because `_static_ids`
   (`acdd_cli.py:181-204`) emits leaf ids only, so a `::` match cannot occur
   for a statically collected id; the sentence is simply narrower than the
   code. Concrete fix (optional, not required for acceptance): drop the
   parenthetical or say "the ids `resolve_test_ref` maps to it".

2. **minor (plan deviation, coordinator-owned; `CHANGELOG.md:12`)** - the
   verbatim slice brief says "Do not edit ... CHANGELOG.md", yet coordinator
   commit `a1470fa` adds exactly one bullet. The supplied fact states
   t1634-check reports the lease ok because the slice touches the three leased
   modules plus `CHANGELOG.md`, and the review brief itself asks for the
   CHANGELOG wording to match the code, so I treat this as sanctioned
   coordinator bookkeeping rather than a defect. No code or gate is affected.
   Fix: none for this PR; if strict brief compliance is later required, record
   the deviation in the lease evidence rather than in code.

## Acceptance criteria

The checkout does not contain the frozen contract, so the AC numbers follow the
contract's named tests in file order; AC-011 line up with the brief's own
statement that the residual is "pinned by AC-011", which confirms the mapping.

| AC | Approved test (`tests/test_static_parametrized_ids.py`) | Result |
| --- | --- | --- |
| AC-001 | `test_collect_manifest_marks_a_parametrized_static_file` | pass - `_collect_manifest` sets `methods == {TEST_FILE: "static"}` and unexpanded `[PLAIN, SCALES]`; `resolved` unchanged |
| AC-002 | `test_frozen_selection_reports_the_static_ids` | pass - returns `([PLAIN, SCALES], [PLAIN, SCALES])`; `frozen_approved_ids` still returns only the ids; no pin -> `None` for both |
| AC-003 | `test_frozen_selection_of_a_pytest_collected_pin_has_no_static_ids` | pass - a `pytest`-collected file yields `([PLAIN, SCALES], [])` |
| AC-004 | `test_check_collected_ids_accepts_parametrized_children_of_a_static_id` | pass - `[S]` + `static_ids=[S]` accepts `S[2]`, `S[3]`; mixed plain+static accepted |
| AC-005 | `test_check_collected_ids_requires_a_case_for_every_approved_id` | pass - `tests_mismatch`, message names `PLAIN`; `[S]` with no cases refuses naming `S` |
| AC-006 | `test_check_collected_ids_refuses_children_when_the_id_is_not_static` | pass - without `static_ids`, `missing ids: S; extra ids: S[2]` (the exact pre-fix message) |
| AC-007 | `test_check_collected_ids_refuses_an_extra_parametrized_case` | pass - `extra ids: ...test_width_extra[2]` |
| AC-008 | `test_check_collected_ids_refuses_a_duplicate_parametrized_case` | pass - `duplicate run ids: S[2]` |
| AC-009 | `test_check_collected_ids_refuses_unexecuted_parametrized_cases` | pass - skipped, xfailed and collection-stage children each refuse naming the id |
| AC-010 | `test_check_collected_ids_refuses_an_unknown_static_id` | pass - `unknown static ids: S`, `tests_mismatch` |
| AC-011 | `test_a_partial_parametrization_is_the_documented_residual` | pass - one child of `[S]` is accepted; documented residual only (see above) |
| AC-012 | `test_check_green_expands_only_with_the_static_ids` | pass - wired GREEN accepted; same GREEN without `static_ids` refuses `missing ids: S` |
| AC-013 | `test_record_green_accepts_the_expanded_green` | pass - one GREEN persisted and re-accepted by `check_green` |
| AC-014 | `test_record_green_still_refuses_an_unrun_approved_id` | pass - `CliError("tests_mismatch")` naming `PLAIN`; no GREEN written |
| AC-015 | `test_integrate_preflight_accepts_the_expanded_green` | pass - returns the contract item and frozen sha |
| AC-016 | `test_cli_verify_records_the_expanded_green` | pass - `rc == 0`, stdout `green: valid`, persisted GREEN re-accepted |
| AC-017 | `test_verify_and_commit_persists_the_expanded_green` | pass - `ok is True`; persisted cases exactly `{CHILD_A, CHILD_B, PLAIN}` |
| AC-018 | `test_verify_and_commit_still_refuses_an_unrun_approved_id` | pass - `VerifyError("tests_mismatch")`; records stay `["red"]` |

## Other judged items

- **Exact-id rule not widened for non-static ids.** With `static_ids` empty or
  absent, `_static_coverage` contributes nothing and the problem list is
  byte-identical to the pre-fix function. A 20,000-shape differential between
  the reconstructed pre-refactor `_id_mismatch_problems` and the current
  three-helper version (empty `static_ids`) produced **0 mismatches**, including
  problem wording and order. Adversarial hand checks also refused an exact
  observed id plus a skipped child, a non-static child of another approved id,
  a duplicated static id, a malformed static id, and an extra unapproved child.
- **Manifest format and digest unchanged.** `_collect_manifest`
  (`acdd_cli.py:206-291`) still writes the same keys; the only value change is
  `methods[path] = tdd.STATIC_COLLECTION` (`:265`), where `STATIC_COLLECTION == "static"`
  (`tdd.py:45`). `collection_digest`, `load_collection`, `_frozen_collection_ids`
  (digest, `source_rev`, count, non-empty) are untouched. `cmd_contract` only
  swaps the literal `"static"` for the constant (`acdd_cli.py:415-419`), same
  output.
- **Pin semantics and refusal codes unchanged.** `frozen_approved_selection`
  reuses `_collection_pin`/`_frozen_manifest`/`_frozen_collection_ids` and the
  same `evidence_tampered` / `tests_not_collected` / `tests_mismatch` codes; the
  new `static_ids` dimension only adds `unknown static ids`, `duplicate static
  ids` and a `coverage.malformed` branch, all under `tests_mismatch`. No new
  error code or widened acceptance for approved non-static ids.
- **Wiring complete and scoped.** The selection is loaded once in
  `acdd_cli.record_green` (`:796-805`), `acdd_cli.integrate_preflight`
  (`:870-879`), and `verify_local.verify_and_commit` (`:1094-1095`), and passed
  to both `verify_local` checks (`:1282-1284` pre-commit and `:1334-1342`
  post-commit). `check_green`/`check_collected_ids` gained keyword-only
  parameters, so all existing callers (`return_report.py:766`,
  `tdd.check_refactor`, the code's own no-pin paths) keep the old behaviour;
  the related FUP1 suite still passes 25/25.
- **No weakening of gates or tests.** The checkout's approved file is untouched
  by the product diff (the pasted diff has no hunk for it); I could not
  byte-diff it against the RED rev because there is no base object, and I rely
  on the supplied t1634-check fact for the byte identity. The file contains
  exactly the 18 AC cases above. No sandbox, gate or check was made optional;
  the static path is still only entered after an import-type collection error
  (`acdd_cli.py:244-265`).
- **No secrets, hosts or absolute private paths** appear in the changed hunks.
  The only added CHANGELOG line is descriptive.

## Commands run (command -> result)

- `timeout 60 git status --porcelain`; `git log --oneline`; `git rev-list --all --count` -> clean tree, 1 squashed commit
- `timeout 60 git cat-file -t 47c9d5aee888` -> 128 (`Not a valid object name`; no base to diff)
- `timeout 120 python3 -m py_compile bundlebees_plugin/tdd.py bundlebees_plugin/acdd_cli.py bundlebees_plugin/verify_local.py` -> 0
- `timeout 900 python3 /tmp/opencode/review/run_tests.py` (offline shim: `jsonschema` no-op validator + `pytest.raises`/`tmp_path`/`monkeypatch`/`capsys`) -> 18/18 approved tests PASS
- `timeout 900 python3 /tmp/opencode/review/run_suite.py tests.test_collected_ids_exact tests.test_verify_frozen_pin` -> 25/25 PASS (exact-id and FUP1 regressions clean)
- `timeout 300 python3 /tmp/opencode/review/semantics.py` -> 21/21 explicit AC/edge assertions PASS; 20,000-shape old-vs-new differential -> 0 mismatches
- `timeout 300 python3 /tmp/opencode/review/selection.py` -> 12/12 (HMAC-signed manifest: static/pytest/mixed files, absent pin, non-mapping methods, tampered digest, wrong count, wrong `source_rev`, non-mapping pin, class child)
- `timeout 120 python3 /tmp/opencode/review/extra.py` -> all 8 adversarial shapes refused/`ACCEPT` exactly as specified
- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_static_parametrized_ids.py` was **not** run: `python3 -c "import pytest"` -> `ModuleNotFoundError` (environment fact, not a defect); the offline replay above substitutes for it
- `find . -name __pycache__ -type d -prune -exec rm -rf {} +`; `git status --porcelain --ignored` -> clean (only this review file will be untracked)
