VERDICT: APPROVE

# WA-A5B — accepted retry findings are attached to the dispatched brief

Independent review of bundlebees PR #299 at head
`58addc8660584e606388fdcec1330d872fa551a2` (the checkout is the single
squashed local commit `65a1900`, so there is no base to diff). I read
`bundlebees_plugin/tools.py` around `_gate`, `_gated_failure_id`,
`_attach_accepted_findings` and the `_dispatch` call site, plus the approved,
read-only `tests/test_retry_findings_attached.py` and the CHANGELOG bullet. The
approved tests were replayed, not modified.

The sandbox has no `pytest` and no `jsonschema`. `bundlebees_plugin.tools`
imports `jsonschema` transitively through `bundlebees_plugin.contract`, so I
prepended a temporary no-op `jsonschema` shim exposing
`Draft202012Validator` with a no-op `iter_errors` and replayed the five test
bodies directly. The shim is stated here so the replay result is not confused
with a real schema validation; the briefs used by the approved tests are
valid in the real schema.

## What the delta does

- `_gate` is unchanged and still calls `research_gate.require_findings`
  (`bundlebees_plugin/tools.py:732`). The attach helper performs no acceptance
  decision.
- `_dispatch` checks `state_dir` first, then calls
  `_attach_accepted_findings(brief, state_dir)` (`tools.py:827-830`) before the
  project authorization, lease-overlap and attribution checks and before
  `client.create` (`tools.py:946`).
- `_gated_failure_id` (`tools.py:747-761`) resolves the explicit
  `brief.failure_id` first (stripped), else the failure record named by
  `brief.retry_of`; it returns `None` when there is no resolvable gated id.
- `_attach_accepted_findings` (`tools.py:764-805`) returns the brief untouched
  when the caller supplied a `Mapping` findings block or when neither
  `failure_id` nor `retry_of` exists; otherwise it resolves the id, refuses
  with an error envelope when the state is unavailable, the artifact is
  unreadable/corrupt or missing, and only then returns a new `dict(brief)` with
  `findings` set to the artifact read from the state directory. It never edits
  the input mapping in place.
- The CHANGELOG bullet at `CHANGELOG.md:576` accurately describes the attach,
  caller-block preservation and the unchanged no-id path.

## Judge points

| Judge point | Implementation | Evidence |
| --- | --- | --- |
| A `failure_id` brief gets the accepted artifact | explicit id returned at `tools.py:750-752`; artifact read at `:793-805` | approved test `test_an_explicit_failure_id_also_carries_its_findings` (`tests/test_retry_findings_attached.py:85-96`) passes; client receives the exact `record_findings` return |
| A `retry_of` brief gets the accepted artifact | failure record resolved at `tools.py:753-761`; artifact read at `:793-805` | approved test `test_a_retry_brief_carries_the_accepted_findings_to_the_launcher` (`:73-83`) passes |
| A caller-supplied findings block is preserved | early return at `tools.py:774-775` | approved test `test_a_direct_findings_block_is_not_replaced_by_the_state_copy` (`:99-110`) passes; direct block with a different summary wins |
| A brief with neither field is untouched | `tools.py:776-778` | approved test `test_a_brief_without_a_gated_failure_carries_no_findings` (`:113-121`) passes; `findings` is absent from the created brief |
| The caller mapping is not mutated | `validate_brief` returns a fresh dict (`brief.py:183`); attach returns `dict(brief)` (`tools.py:803-804`) | approved test `test_the_caller_brief_is_not_mutated_by_the_attach` (`:124-132`) passes; direct probe also showed `out is not caller` and no key added to the caller |
| Missing/unreadable artifact refuses instead of dispatching | gate refuses first (`tools.py:732`); attach independently refuses missing (`:797-802`) and unreadable/corrupt (`:793-796`) | probes with a missing artifact, corrupt JSON and an unreadable failure record all returned `ok: false`, `code: research_required`, and `client.create` was called zero times |
| `require_findings` remains the only acceptance decision | `tools.py:732` unchanged; attach only reads and copies an already-accepted artifact; no call to `validate_findings` or bypass | code inspection plus the refusal probes; no second acceptance path was added |
| Fail closed / no synthesis or repair | attach assigns the exact artifact object returned by `research_gate.record_findings`/`_read_json`; no field is added or corrected | approved artifact-equality assertions pass |
| Scope, secrets, hosts, absolute paths, gate weakening | product delta is limited to `tools.py` (+66 lines); no new import, network call, secret, host or absolute path is introduced by the new code; new refusal text contains no path; the only paths in the approved test are synthetic fixture text | `git log`/file inspection; coordinator-supplied delta; `git status` before the review was clean |

## Findings

1. **minor — `bundlebees_plugin/tools.py:774`**: the helper short-circuits on
   any caller-supplied `Mapping` before resolving the gated id. If the supplied
   block's `failure_id` does not match the gated id, `require_findings` ignores
   it and validates the state copy instead (`research_gate.py:668-698`), but
   this early return then forwards the *unvalidated, mismatched* block to the
   launcher. I reproduced this with explicit `failure_id` A, a state artifact
   for A and a supplied block for `some-other-id`: the dispatch succeeded and
   `client.create` received the `some-other-id` block. This is not a blind
   retry (the gate did accept the retry against the state artifact) and it is
   pre-existing caller behaviour, so it is not a blocker; still, the launcher
   can render research for the wrong failure. Fix: resolve `fid` first and
   preserve the supplied mapping only when
   `str(findings.get("failure_id") or "") == fid` (or when there is no gated id
   to resolve); otherwise set the resolved artifact. The approved matching-id
   preservation test still passes with that rule.
2. **minor — `bundlebees_plugin/tools.py:758` and `:794`**: `_gated_failure_id`
   and `_attach_accepted_findings` call `research_gate._read_json`, a
   module-private helper, and re-implement the gate's id resolution. I judge
   this **acceptable, not a blocker** in this module: it is an intra-package
   call, there is no public equivalent today, both call sites convert
   `ResearchGateError` into an error envelope, and it cannot bypass the gate.
   The cost is coupling: a future change to the gate's evidence-read errors or
   id resolution can silently change the dispatch envelope. Fix (non-blocking):
   promote a public accessor in `research_gate` (for example
   `load_failure(state, id)` / `load_findings(state, fid)`, or one public
   `gated_failure_id(brief)` shared with `require_findings`) and keep
   `_read_json` private.
3. **minor (test coverage; non-blocking) — `tests/test_retry_findings_attached.py:73-132`**:
   the approved five tests cover the two attach paths, the supplied-block
   preservation, the no-id path and caller non-mutation, but they do not
   exercise the attach helper's own fail-closed branch (missing or corrupt
   artifact) or a direct findings block with no state copy. The product behaves
   correctly in the probes below, and `require_findings` refuses missing or
   corrupt findings before `client.create`, so this is a coverage gap rather
   than a defect. Fix: add focused tests in a follow-up (without changing this
   approved suite) for a missing artifact and a direct-only findings block.

## Residual observation (pre-existing, outside this delta)

A `retry_of` that names no recorded failure is not gated:
`research_gate.require_findings` returns `None` (`research_gate.py:641-656`),
and `_attach_accepted_findings` leaves the brief unchanged because there is no
gated failure id to resolve. A probe confirmed such a dispatch succeeds with no
findings and one `client.create` call. That is the existing gate policy, not
introduced or weakened by this PR, and there is no artifact to attach. If a
future change wants an unknown `retry_of` to refuse, it belongs in
`research_gate`, not in this attach helper.

## Commands run and exit codes

| Command | Exit | Result |
| --- | --- | --- |
| `git status --porcelain` (before writing this review) | 0 | clean; no changes |
| `git log --oneline -3` / `git rev-parse HEAD` | 0 | local squashed commit `65a1900a9e681a89dc1b8b93e79c0108351339d2`; no base commit |
| `python3 -c "import bundlebees_plugin.tools"` | 1 | `ModuleNotFoundError: No module named 'jsonschema'`; this is why the replay used a no-op shim |
| `python3` replay of the five approved test bodies (`importlib.util.spec_from_file_location`, module inserted into `sys.modules` before `exec_module`, `tempfile.mkdtemp` per test, `pathlib.Path` argument) | 0 | `SHIMMED jsonschema`, then 5/5 PASS: retry brief, explicit failure id, direct block preserved, no-id brief, caller not mutated |
| `python3` direct attach probes | 0 | missing explicit artifact -> refusal envelope; missing retry_of artifact -> refusal envelope; accepted artifact attached; caller mapping unmutated and a new dict returned; `_gated_failure_id` resolves explicit/retry_of and returns `None` for a no-id brief |
| `python3` full `_dispatch` probe with a mismatched supplied block | 0 | explicit `failure_id` A, state artifact A, supplied findings for `some-other-id`: dispatch returned `ok: true`, one `client.create` call, and the created brief carried the `some-other-id` block (basis for finding 1) |
| `python3` `_dispatch` refusal probes | 0 | missing artifact, corrupt JSON and unreadable failure record each returned `ok: false`, `code: research_required`, `client.create` calls 0 |
| `python3` unknown `retry_of` probe | 0 | dispatch succeeded (`ok: true`), no `findings` in the created brief, one `client.create` call (residual observation above) |
| `git status --porcelain` (after writing this review) | 0 | only `?? docs/reviews/WA-A5B.md`; the review is left uncommitted |
| `git diff --stat` (after writing this review) | 0 | no tracked-file diff; the only changed file is the untracked review |
