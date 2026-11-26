VERDICT: APPROVE

# WA-A5B-FUP2 — independent review (PR #328, head c0009104313f20c03f6a3a4654b26d967e8a46f7)

## Scope reviewed

The single squashed tree at `5355f9c` (`base c0009104313f20c03f6a3a4654b26d967e8a46f7`).
I read:

- `bundlebees_plugin/tools.py`: `_gate` (675-751), `_gated_failure_id` (754-775),
  `_supplied_findings` (778-784), `_attach_accepted_findings` (787-800),
  `_attach_state_findings` (803-857), `_dispatch` (860-885) and the handler
  boundary (1501-1551).
- `bundlebees_plugin/research_gate.py`: `ResearchGateError`/`_refusal` (87-108),
  path helpers (116-136), `_read_json` (185-205), the whole of
  `require_findings` (649-796) and the new `_conflicting_id_refusal` (637-646).
- The whole approved file `tests/test_retry_findings_paths.py` (233 lines, six
  tests in AC order).
- The supplied full diff, reconstructed at `/tmp/opencode/pr328.patch` and
  checked against the tree (see commands).

The change does what the frozen contract asks. `_gated_failure_id` refuses a
truthy non-string explicit id with `ResearchGateError("research_required", ...)`
(`tools.py:759-767`); `_attach_state_findings` catches it and returns the typed
envelope (`tools.py:821-824` rather than propagating); `require_findings`
records the explicit and `retry_of`-resolved ids and refuses a mismatch before
the artifact loop (`research_gate.py:667-715`). The new refusal uses the same
`_refusal(code, error, fix, failure_id=..., artifact="")` shape as every other
refusal in that function. The retry-only, one-id and two-equal-id paths are
untouched: the inserted conflict check sits between id collection and the
existing loop, and nothing else in the function moved.

## Acceptance criteria

Each body was replayed directly (pytest is absent). At the head all six
passed. Against a reconstructed RED base (the same tree with the supplied diff
reverse-applied under `/tmp/opencode/baserepo`) exactly AC-001, AC-002, AC-004
and AC-005 failed and AC-003 and AC-006 passed, matching the supplied RED
record's 4 failed / 2 passed.

- **AC-001 — pass.** `_gated_failure_id({"failure_id": 5}, state)` raises
  `ResearchGateError` with `code == "research_required"` and a message naming
  `failure_id` and `string`. I also probed `True`, `5`, `1.5`, `["A"]` and
  `{"id": "A"}`; all five raise `research_required`.
- **AC-002 — pass.** With `ATTACH_FINDINGS=1`,
  `_attach_accepted_findings({"failure_id": 5, "findings": <block for
  "some-other-id">}, state)` returns a JSON string with `ok: false`,
  `code: "research_required"`, and no occurrence of `some-other-id`. The raise
  is converted to the envelope at `tools.py:821-824`; it does not escape the
  helper.
- **AC-003 — pass.** A supplied block naming the `retry_of`-resolved failure is
  returned by identity: `attached["findings"] is supplied`. `_supplied_findings`
  matches it (`tools.py:782`) and `_attach_state_findings` returns `brief`
  untouched (`tools.py:827-828`); no state copy replaces it.
- **AC-004 — pass.** A brief with explicit id A and `retry_of` resolving to B
  returns a non-`None` refusal with `code == "research_required"` whose `error`
  contains both ids. Ordering was probed independently by deleting both findings
  files: the refusal is still the conflict one naming both ids, so it happens
  before any artifact read/validation.
- **AC-005 — pass.** `_dispatch` on the disagreeing brief returns the typed
  refusal (`research_required`, both ids in `error`) and the fake launcher's
  `create` is never called (0 calls). `_gate` calls `require_findings`
  (`tools.py:739`) before `_dispatch` reaches the attach/launcher code
  (`tools.py:879-884`).
- **AC-006 — pass.** With explicit id A and `retry_of` resolving to A,
  `require_findings` returns `None` and the supplied block is preserved by
  identity. Whitespace around the explicit id is stripped before comparison, so
  `"  <A>  "` and A compare equal (probed).

## Findings

1. **minor — `bundlebees_plugin/tools.py:759-767`: a truthy *whitespace-only
   string* `failure_id` still resolves to the empty id, so a direct helper call
   keeps a foreign `findings` block.** The new branch only checks
   `isinstance(explicit, str)` and then returns `explicit.strip()`. For
   `failure_id = "   "`, `_gated_failure_id` returns `""`, and
   `_attach_state_findings` maps that to the "nothing resolved" early return
   (`tools.py:825-826`), returning the brief with the caller's block for another
   failure. This is the same symptom as the fixed defect, for a truthy string
   rather than a non-string. It is not reachable through `_dispatch`:
   `require_findings` refuses a whitespace-only explicit id at
   `research_gate.py:670-677` (`research_required`), which I confirmed. Severity
   minor because the production path is gated; AC-001 is explicitly scoped to
   non-strings. **Fix:** strip once and refuse the empty result with the same
   envelope, e.g. `text = explicit.strip(); if not text: raise
   rg.ResearchGateError("research_required", "failure_id must be a non-empty
   string", "Name the recorded failure this retry is for.")` and `return text`.
   While there, update the `_gated_failure_id` docstring (754-755) to mention
   that it now raises `ResearchGateError`. Add a direct-helper white-space test
   in a follow-up file; the approved file is read-only.

2. **minor — `bundlebees_plugin/tools.py:759-760` with
   `bundlebees_plugin/research_gate.py:661,669`: a present but falsy non-string
   `failure_id` (notably `0` and `False`) is treated as absent by both layers,
   so a foreign findings block is dispatched ungated.** Both sides use
   truthiness (`if explicit:`), so `{"failure_id": 0, "findings": <block for
   "some-other-id">}` is not gated (`require_findings` returns `None`), the
   attach early-returns the brief (`tools.py:816-818`, `825-826`), and `_dispatch`
   succeeds with the foreign block forwarded to `client.create`. I confirmed
   this end to end: `ok: true`, one `create` call, forwarded
   `findings.failure_id == "some-other-id"`. This is outside AC-001's literal
   "truthy non-string" wording, and it is equivalent to omitting the key (a
   caller-supplied block with no gated id is trusted by design), so it is not a
   blocker. **Fix (optional, follow-up):** distinguish "key absent / empty" from
   "present but invalid", e.g. `if "failure_id" in brief and explicit is not None
   and explicit != "":` before the type check in both `require_findings` and
   `_gated_failure_id`, so `0`/`False` get the same `research_required` refusal
   as other non-strings. If the project wants "falsy means absent", say so in
   the docstring instead and add the shape to a test.

3. **minor — `bundlebees_plugin/tools.py:816-828` with
   `bundlebees_plugin/research_gate.py:714-715`: the disagreement rule is
   enforced only in the gate, so a direct helper call on a disagreeing brief
   still attaches the explicit id's block while `retry_of` names the other
   failure.** Probed: `require_findings` returns `research_required`, but
   `_attach_accepted_findings({"failure_id": A, "retry_of": "att-b", "findings":
   <A's block>}, state)` returns a dict whose `findings` names A and whose
   `retry_of` is still `"att-b"`. Through `_dispatch` this cannot happen (the
   gate refuses first, 0 launcher calls), so this is defence-in-depth, not a
   reachable leak. **Fix:** give the rule one home — expose a single
   `research_gate.resolve_gated_id(brief, state)` (or equivalent) that resolves
   explicit/retry ids and returns/raises the same conflict refusal, and have
   both `require_findings` and `_gated_failure_id` call it. That also removes the
   duplicated explicit-first/`retry_of` logic that let this divergence exist.

No blocker and no major finding. Findings 1-3 are residuals/hardening around
input shapes the frozen contract does not cover, and none is reachable through
the single production dispatch entry point. AC-001 through AC-006 all follow
from the code and pass when their bodies are executed.

## Shape probes (beyond the six ACs)

No truthy non-string `failure_id` can resolve to an empty id on any path I
found: `_gated_failure_id` raises and `require_findings` refuses before the
attach step. The only empty resolution left is a whitespace-only *string*
(finding 1), and the only unrefused present-but-falsy ids are `0`/`False`
(finding 2), which both layers treat as "no id" and which AC-001 does not cover.

| Input shape | Observed |
| --- | --- |
| `failure_id` `True`, `5`, `1.5`, list, dict | `_gated_failure_id` raises `research_required` |
| `failure_id` `"   "` | helper returns `""`; gate `research_required`; direct helper keeps a foreign block (finding 1) |
| `failure_id` `0`, `False` | treated as absent, gate `None`, foreign block forwarded through `_dispatch` (finding 2) |
| `failure_id` `""`, `None` | treated as absent; gate `None`; brief returned untouched |
| `failure_id` A + `retry_of` unknown attempt | gate `None` when A has accepted findings (only a recorded failure gates); helper resolves A |
| `retry_of` only, resolves | gate `None`; the state artifact for that failure is attached |
| both ids equal, differing surrounding whitespace | accepted; gated id is the stripped explicit id |
| both ids disagree, artifacts deleted | conflict refusal naming both ids (proves it precedes artifact reads) |
| both ids equal, failure record verdict `environment-problem` | `environment_problem` refusal (branch unchanged) |
| explicit id only, artifact present / supplied block / missing artifact | accepted and state artifact attached / supplied block preserved by identity / `research_required` (all unchanged) |
| direct helper call with a disagreeing brief | gate `research_required`; helper alone attaches A's block (finding 3) |

## Verify-disclosure judgement: I accept the landing

I accept the coordinator's decision to land this as coordinator commits with no
`verify` record for this head, because I reproduced the false positive and the
clean git-backed result independently:

- The full supplied diff, reconstructed at `/tmp/opencode/pr328.patch`,
  reverse-applies to the head tree with `git apply --check -R` (exit 0) and
  fails forward (`--check`, exit 1, expected because it is already applied), so
  the patch is exactly the change in the tree.
- Both patched files parse with `ast.parse` (exit 0).
- `subversion_scan.scan_patch(patch)` reproduces exactly the two reported
  findings: `parse_error` at `research_gate.py:634` and `tools.py:773`
  (differential `v1=0, v2=2`). Inspecting the patch-only scanner's preimage
  document shows why. The rebuilt skeleton keeps only hunk lines at their old
  line numbers and fills the gaps with blanks: in `research_gate.py` its first
  hunk line is the `return` from inside `_artifact_label` at line 634, so in the
  skeleton it is a top-level `return`; in `tools.py` the skeleton literally ends
  with the hunk's last context line `if failure is None:` — a block header with
  no body. So the parse errors are an artifact of rebuilding each hunk as a
  standalone file, not a defect in the change.
- I reconstructed the RED base in `/tmp/opencode/baserepo` by reverse-applying
  the same patch to a copy of the tree and committing it, then ran
  `subversion_scan.scan_for_verify(patch, repo, rev)` — the git-backed path the
  coordinator cited — and it returned `[]` with differential `match: True`.

I could not run `hermes bundlebees verify` itself (the `hermes` executable is
not installed on this host), so my reproduction uses the same scanner function
verify calls, not the CLI wrapper. On that evidence the `test_subversion`
refusal is a scanner false positive and landing without a verify record is
justified.

## Other checks

- **Scope:** the diff has exactly three `diff --git` sections: `CHANGELOG.md`,
  `bundlebees_plugin/research_gate.py`, `bundlebees_plugin/tools.py`. No
  `tests/` hunk, so the approved file and `tests/test_retry_findings_attach_scope.py`
  are untouched by the change. The approved file contains exactly six
  `def test_` entries in AC order. I cannot independently prove byte-identity to
  the RED commit without base history (supplied fact), but nothing in the tree
  contradicts it and the working tree was clean before and after my review.
- **CHANGELOG:** the new bullet claims only what the code does (the raise and
  code, the both-ids-differ refusal naming both ids, the unchanged
  one-id/equal-id/retry-only paths, and the six pins). "pins all six acceptance
  ids" is loose wording (tests are named by scenario, not `AC-00N`), not an
  overclaim.
- **Refusal shape:** `_conflicting_id_refusal` returns
  `ok/code/error/fix/failure_id/artifact`, identical in shape to the non-string
  refusal at `research_gate.py:671-677`, and both `_gate` and the attach step
  flatten it to `_err(code, error, fix)`.
- **Secrets / hosts / private paths:** none added. A scan of the diff for URLs,
  e-mail/host-shaped strings, home-directory paths and key prefixes found only
  the pre-existing Semantic Versioning URL in a context line. The synthetic
  absolute-path strings under a generic home directory live only in the
  read-only approved test fixture, as before.
- **Unchanged paths:** the environment-problem branch
  (`research_gate.py:717-726`), the supplied-block/artifact read (`727-758`),
  `validate_findings` and its refusal (`759-767`), the verdict branches
  (`768-784`) and the trailing `environment-problem` pass (`786-795`) are
  byte-for-byte the pre-change code; the inserted conflict check is the only
  control-flow edge added. I probed the environment-problem branch with two
  equal ids and got `environment_problem`, and the explicit-only path still
  accepts an accepted artifact, preserves a matching supplied block by identity
  and refuses a missing artifact with `research_required`.
- **Import placement:** the `research_gate` import moved to the top of
  `_gated_failure_id`; it remains function-local, so there is no new module
  import cycle (`tools` already imports `research_gate` lazily in `_gate` and
  `_attach_state_findings`).
- **Lint/type:** new lines are all under 100 columns; I did not run the
  project's linter (see below).

## Commands run

All commands ran from the repository root. The only temporary artefacts are
under `/tmp/opencode`. `pytest` is not installed, so the pytest invocation is an
honest failed attempt, not a test run; the six AC bodies were replayed directly
instead.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `timeout 60 git log --oneline -5` | 0 | one squashed commit `5355f9c base c000910...` |
| 2 | `timeout 60 git status --short` (before and after) | 0 | clean; only this review file added afterward |
| 3 | `python3 --version` | 0 | Python 3.12.3 |
| 4 | `timeout 60 python3 -c "import bundlebees_plugin.tools"` (no shim) | 1 | `ModuleNotFoundError: jsonschema` |
| 5 | `python3 -c "import pytest"` | 1 | `No module named 'pytest'` |
| 6 | `python3 -c "import jsonschema"` | 1 | `No module named 'jsonschema'` |
| 7 | `python3 -c "import yaml"` | 1 | `No module named 'yaml'` |
| 8 | `timeout 600 python3 -m pytest -q tests/test_retry_findings_paths.py` | 1 | `No module named pytest` (not a test run) |
| 9 | `timeout 120 env PYTHONPATH=".:/tmp/opencode/shim" python3 /tmp/opencode/replay.py` | 0 | AC-001..AC-006: 6 passed, 0 failed |
| 10 | `timeout 180 env PYTHONPATH=".:/tmp/opencode/shim" python3 /tmp/opencode/probe.py` | 0 | ten edge-shape probes (results in the table above) |
| 11 | `timeout 120 env PYTHONPATH=".:/tmp/opencode/shim" python3 -` (dispatch probes: `failure_id` 0 vs 5) | 0 | 0: `ok:true`, 1 call, foreign block forwarded; 5: `research_required`, 0 calls |
| 12 | `timeout 120 env PYTHONPATH=".:/tmp/opencode/shim" python3 -` (explicit-only, supplied-block, missing-artifact probes) | 0 | gate `None` / preserved by identity / `research_required` |
| 13 | `timeout 60 python3 -c 'ast.parse both files'` | 0 | both parse |
| 14 | `awk 'length>100 {print FILENAME":"FNR":"length}'` over both files | 0 | no new over-long lines in the changed ranges |
| 15 | `timeout 60 git apply --check -R /tmp/opencode/pr328.patch` | 0 | the diff matches the head tree |
| 16 | `timeout 60 git apply --check /tmp/opencode/pr328.patch` | 1 | expected: already applied |
| 17 | `timeout 120 env PYTHONPATH=... python3 -c 'scan_patch(patch)'` | 0 | two `parse_error` findings at `research_gate.py:634`, `tools.py:773` |
| 18 | `git init` + copy + `git apply -R` + commit in `/tmp/opencode/baserepo` | 0 | reconstructed RED base `7f88880` |
| 19 | `timeout 120 env PYTHONPATH=... python3 -c 'scan_for_verify(patch, baserepo, rev)'` | 0 | `[]`, differential match `True` |
| 20 | `timeout 60 env PYTHONPATH=... python3 -c '_preimages tails'` | 0 | `tools.py` skeleton ends at `if failure is None:`; `research_gate.py` skeleton resumes mid-function |
| 21 | `command -v hermes` | 1 | not installed |
| 22 | `command -v ruff` | 1 | not installed |
| 23 | `command -v slop-lint` | 1 | not installed (only `scripts/vendor_slop_lint.py`) |
| 24 | `timeout 60 python3 scripts/vendor_slop_lint.py --help` | 0 | vendoring helper only, not the linter |
| 25 | `grep -nE` over the patch for URLs, e-mail/host shapes, home-directory paths, `sk-`/`AKIA` prefixes and private-key headers | 0 | only the pre-existing semver URL |
| 26 | `grep -c "^def test_" tests/test_retry_findings_paths.py` | 0 | 6 |
| 27 | `grep`/`sed` read-only inspections of the gate, flow checks and caller search | 0 | `_gated_failure_id` has one caller (`tools.py:822`); `require_findings` one caller (`tools.py:739`) |
| 28 | `timeout 180 env PYTHONPATH="/tmp/opencode/baserepo:/tmp/opencode/shim" python3 /tmp/opencode/replay.py` | 1 | reconstructed RED base: AC-001/002/004/005 fail, AC-003/006 pass (expected RED, 2 passed / 4 failed) |

**Could not run, and why:** the six-test `pytest` run (pytest absent — the six
bodies were replayed directly instead), the full-suite junit node-id comparison
(no pytest), `hermes bundlebees verify` (hermes absent; reproduced with
`subversion_scan` directly), and the project slop-lint (not installed; only the
vendoring script exists). I did not re-run the RED record with pytest either; I
replayed the same bodies against a reconstructed base (row 28) and got the same
4 failed / 2 passed. I am not claiming any of the runs I could not make.

### Disclosed shim

`jsonschema` is absent and `bundlebees_plugin/contract.py` imports it at module
load, so importing `bundlebees_plugin.tools` requires a stand-in. I used one
no-op module at `/tmp/opencode/shim/jsonschema/__init__.py` with exactly this
content (validation is a no-op; the six AC bodies never exercise ACDD schema
validation):

```python
"""No-op jsonschema shim for the WA-A5B-FUP2 review harness."""


class ValidationError(Exception):
    pass


class Draft202012Validator:
    def __init__(self, schema=None):
        self.schema = schema

    def iter_errors(self, document):
        return iter(())


def validate(document, schema=None):
    return None
```

No `yaml` shim was needed: no imported module in this path uses PyYAML.
