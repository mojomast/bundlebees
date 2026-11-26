VERDICT: APPROVE

# WA-A5B-FUP1 — independent review (PR #311, head d9089051)

## Scope reviewed

The single squashed tree at `d90890511370c8de814de5f30cdf4c9131f9170c`. I read
`bundlebees_plugin/tools.py` around `_gated_failure_id`, `_supplied_findings`,
`_attach_accepted_findings` and `_dispatch`; the whole approved test file
`tests/test_retry_findings_attach_scope.py`; the gate side in
`bundlebees_plugin/research_gate.py` (`require_findings`, `validate_findings`,
`_read_json`, `findings_path`, `failure_path`); `bundlebees_plugin/brief.py`
(`validate_brief`) and the new CHANGELOG bullet at `CHANGELOG.md:35`.

The change is small and in the right place. `_attach_accepted_findings` no
longer trusts any `Mapping` under `brief.findings` before the gated id is known:
it resolves `fid` first (`tools.py:788-790`), returns early only when no id
resolves (`tools.py:792-793`), and honours the caller block only when
`_supplied_findings` matches that resolved id (`tools.py:764-770`, `794-795`).
Otherwise it reads the state artifact exactly as before (`tools.py:802-814`).
The fail-closed branches and their codes are unchanged.

The core safety argument holds: the attach-side match predicate
(`str(supplied.get("failure_id") or "") == fid`) is the same predicate the gate
uses to decide the supplied block is the artifact to validate
(`research_gate.py:668-669` and `validate_findings` at `research_gate.py:541-546`).
The id resolution is also the same on both sides: explicit `failure_id.strip()`
first, else the failure record's `failure_id` read through the same
`failure_path`/`_read_json`, with `_gated_failure_id` treating no state dir or a
missing/blank `retry_of` as "no id". Therefore, on the single-id paths, an
accepted supplied block is forwarded only if the gate validated that same block,
and otherwise the launcher receives the state artifact the gate validated.

## Findings

1. **minor — `bundlebees_plugin/tools.py:750-752` with `tools.py:792-793`: a
   truthy non-string `failure_id` is treated as "no id", so the helper in
   isolation returns the brief with an arbitrary supplied findings block
   untouched.** `_gated_failure_id` returns `""` for any non-string explicit
   `failure_id`, and `_attach_accepted_findings` maps an empty `fid` to the
   "nothing resolved" early return. A direct call such as
   `_attach_accepted_findings({"failure_id": 5, "findings": findings("some-other-id")}, state)`
   returns a dict carrying `some-other-id`, which violates the helper's own
   docstring invariant. This is **not reachable through `_dispatch`**: `_gate`
   calls `require_findings` first, and `research_gate.py:614-622` refuses a
   non-string `failure_id` with `research_required` before the attach step; I
   confirmed that path dispatches nothing (`research_required`, zero
   `client.create` calls). Severity is minor because the only production caller
   gates first, but the fail-open shape is unnecessary defence-in-depth risk.
   **Fix:** in `_gated_failure_id`, raise
   `rg.ResearchGateError("research_required", "failure_id must be a non-empty string", ...)`
   for a truthy non-string id (or distinguish "no id" from "unresolvable id" and
   make `_attach_accepted_findings` refuse the latter instead of returning the
   brief). Add a direct-helper regression test in a new test file (the approved
   file is read-only for this PR).

2. **minor — `tests/test_retry_findings_attach_scope.py:106-117`: AC-003 is
   pinned only on the explicit `failure_id` path, not on the `retry_of` path.**
   The code is correct on both paths — I verified with an ad-hoc probe that a
   supplied block matching a `retry_of`-resolved failure id is returned by
   identity from both `_attach_accepted_findings` and `_dispatch`, and that a
   matching-but-invalid block is still refused by the gate with
   `research_required` — but the six approved tests do not lock the symmetric
   `retry_of` case. **Fix:** none in this PR (the approved file must not change);
   a follow-up test file can cover "supplied block matching the `retry_of`
   failure is preserved unchanged" so the two id paths stay pinned together.

3. **minor — `bundlebees_plugin/tools.py:747-761` with `tools.py:794-795`: in a
   contradictory brief carrying both `failure_id` and a disagreeing `retry_of`,
   the explicit id wins, so a supplied block matching `failure_id` (but not the
   `retry_of` failure) still reaches the launcher while the `retry_of` failure's
   artifact does not.** Verified with a probe: `failure_id=A`,
   `retry_of=B` (A != B), `findings.failure_id=A` -> dispatch succeeded and
   `client.create` received A's block; when the block matched B it was replaced
   by A's state artifact. The gate had validated both A and B, so this is not
   the unvalidated-forward defect and it is not introduced here (explicit-first
   resolution predates the patch; before it, any mapping was forwarded), but
   read literally AC-002 — "a caller-supplied findings block whose `failure_id`
   differs from the failure named by `retry_of` never reaches the launcher, and
   the accepted state artifact does" — is false for this shape, and a worker
   retrying B is rendered A's research. AC-001 and AC-002 cannot both hold for a
   contradictory brief with a single `brief.findings` slot, so the fix is a
   disambiguation rule, not a different attachment choice. **Fix:** refuse a
   brief whose explicit `failure_id` and resolved `retry_of` failure id differ
   (fail closed in `require_findings`/`_gate` with `research_required` or
   `brief_invalid`), or require the supplied block to match every resolved gated
   id and refuse otherwise. Add a both-id-disagree test in a follow-up file; the
   approved file stays untouched.

No blocker or major finding. Findings 1-3 are minor and non-blocking: 1 is
unreachable through `_dispatch`, 2 is test coverage, and 3 is a pre-existing
contradictory-brief disambiguation gap rather than a reintroduction of the
unvalidated forward. AC-001, AC-002, AC-004, AC-005 and AC-006 pass on the input
shapes they are written and tested against, and AC-003's behaviour follows from
the code on both id paths despite the test-coverage note above.

## Acceptance criteria

- **AC-001 (explicit id vs. a different supplied block):** pass.
  `_supplied_findings` returns `None` on mismatch (`tools.py:768-769`), the
  helper falls through to the state read (`tools.py:802-814`), and the launcher
  receives the accepted artifact. The approved test asserts both the call
  payload and its `failure_id`; it passed in my direct execution.
- **AC-002 (`retry_of` id vs. a different supplied block):** pass on the
  `retry_of`-only path it is written and tested against. With no explicit id,
  `_gated_failure_id` resolves the failure record's id (`tools.py:753-761`); the
  same mismatch rule applies, and the state artifact is attached. The approved
  test passed. The contradictory both-id shape is finding 3.
- **AC-003 (matching block preserved):** pass. `_supplied_findings` returns the
  same object and `_attach_accepted_findings` returns the brief unchanged at
  `tools.py:794-795`; no state copy replaces it. Verified by the approved test
  and by an identity probe.
- **AC-004 (missing artifact):** pass. `_read_json` returns `None` and the helper
  emits `research_required` (`tools.py:806-811`). The approved test passed.
- **AC-005 (corrupt artifact):** pass. `_read_json` raises
  `ResearchGateError("research_corrupt")`, which is caught at
  `tools.py:804-805` and returned with its code. The approved test passed.
- **AC-006 (no gated id):** pass. Both `failure_id` and `retry_of` are falsy, so
  the helper returns the brief at `tools.py:783-785` before importing or reading
  research state. I additionally monkeypatched `rg._read_json` to raise and
  dispatched an ungated brief with a direct block: dispatch succeeded, the exact
  block object reached `client.create`, and no research read occurred.

## Entry-path and shape checks

| Input shape | Result |
| --- | --- |
| explicit matching block, artifact present | forwarded by identity, not replaced |
| explicit mismatching block, artifact present | replaced by the accepted state artifact |
| `retry_of` only, mismatching block | replaced by the accepted state artifact |
| `retry_of` only, matching block | forwarded by identity (probe; not in the approved tests) |
| both ids present and equal | normal matching behaviour |
| both ids present and disagreeing | gate requires accepted findings for both; attach resolves explicit first. A supplied block matching the explicit id (but not the `retry_of` failure) is forwarded and the `retry_of` failure's artifact does not reach; a block matching only the `retry_of` failure is replaced by the explicit artifact. Both artifacts were gate-validated, so no *unvalidated* artifact is rendered, but this literal AC-002 counterexample is finding 3 |
| supplied block with no `failure_id` key | mismatch (`""` vs. a non-empty id), replaced by the state artifact |
| non-string truthy `failure_id` | `_dispatch` refuses `research_required` in the gate before attach; direct helper call is the minor finding above |
| `dict` subclass under `findings` | `isinstance(..., Mapping)` is true; matching subclass object is preserved by identity |
| no state directory and matching supplied block | returned unchanged (no read needed) |
| no state directory and mismatching supplied block | `not_configured` |
| missing / corrupt / unreadable artifact | `research_required` / `research_corrupt` / `research_unreadable` |
| unsafe id with a *supplied* matching block | forwarded unchanged by the helper (same as the pre-fix early return); the gate also accepts a direct matching block without a path lookup, so this is not a new path introduced here |
| brief with no gated id | returned before any research read |

Additional verification: for each resolved id,
`research_gate.require_findings` validates a caller-supplied block only when its
`failure_id` equals that id and otherwise reads and validates the state copy
(`research_gate.py:666-699`), so the attach step now agrees with the gate it
sits behind on the single-id paths. Multi-id briefs are finding 3.

## Non-findings / notes

- **No new unvalidated path to `client.create` found.** The gate and the attach
  step use the same match predicate and the same id resolution; the only
  divergence (non-string explicit id) is refused by the gate first. The state
  artifact is re-read after the gate, exactly as before this PR; any
  between-read change is a pre-existing TOCTOU property, not introduced here.
- **Both-ids-disagree behaviour is not a regression, but is the finding-3
  contract gap.** It is a contradictory brief; `_gated_failure_id` documents
  "explicit first", the gate validates both artifacts, and the launcher renders
  only the explicit one. This predates the fix and does not let an unvalidated
  block through, but it does mean the `retry_of` failure's research is not what
  the worker sees, and it is the one shape where AC-002's literal "never"
  fails.
- **CHANGELOG:** `CHANGELOG.md:35` claims no more than the code does. It
  correctly describes the new ordering, the matching-only authority, the
  preservation of a matching block, the no-state-read path for a brief with no
  gated id, and the retained fail-closed codes. "Keeps its error code" is true
  of the helper; note that through `_dispatch` the gate may already have turned a
  corrupt *failure* record into `research_required`
  (`research_gate.py:641-650`), so the helper-level wording is the accurate
  one.
- **Scope:** only `bundlebees_plugin/tools.py`, `CHANGELOG.md` and the new
  approved test file are in the PR delta per the supplied coordinator facts. I
  could not diff against a base (the checkout is one squashed tree), but the
  reviewed helper is the only behavioural change I found and I saw no unrelated
  edits in the files I read. Working tree was clean before and after my review.
- **Secrets / hosts / identifiers:** none in the changed code or the CHANGELOG
  bullet. The test fixture at `tests/test_retry_findings_attach_scope.py:29`
  contains a synthetic absolute-path failure message under a generic home
  directory; it is not a real user path or host, is the kind of absolute path
  `research_gate._normalise_text` rewrites, and the file is the approved
  read-only fixture.
- **Test file read-only condition:** the approved file was not modified by me.
  I cannot independently prove byte-identity to the RED commit without a base
  history; the supplied fact is that it is identical, and nothing in the tree
  contradicts that.

## Commands run

All commands ran from the repository root. `pytest` is not installed on this
host, so the two pytest invocations below are honest failed attempts, not test
runs.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `timeout 60 git status` | 0 | clean worktree (only this review file untracked afterward) |
| 2 | `timeout 60 git log --oneline -5` | 0 | one squashed commit, `77dde39 base d908905...` |
| 3 | `timeout 60 python3 --version` | 0 | Python 3.12.3 |
| 4 | `timeout 120 python3 -c "import pytest; print(pytest.__version__)"` | 1 | `ModuleNotFoundError: No module named 'pytest'` |
| 5 | `timeout 30 python3 -c "import jsonschema"` | 1 | `ModuleNotFoundError` |
| 6 | `timeout 30 python3 -c "import yaml"` | 1 | `ModuleNotFoundError` |
| 7 | `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_retry_findings_attach_scope.py` | 1 | `/usr/bin/python3: No module named pytest` |
| 8 | `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests` | 1 | `/usr/bin/python3: No module named pytest` |
| 9 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 python3 /tmp/opencode/run_attach_tests.py` | 0 | 6 passed, 0 failed (all six approved test bodies, direct execution) |
| 10 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 python3 /tmp/opencode/probe_attach.py` | 0 | entry-path/shape probe (explicit, `retry_of`, both ids, non-string id, missing key, dict subclass, ungated no-read, fail-closed codes) |
| 11 | `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. timeout 300 python3 /tmp/opencode/probe_retry_match.py` | 0 | `retry_of` matching preservation and matching-but-invalid refusal probe |
| 12 | `timeout 60 env PYTHONDONTWRITEBYTECODE=1 python3 -c "import ast; [ast.parse(...) for ...]"` | 0 | both files parse |
| 13 | `timeout 60 grep -n "_gated_failure_id\|_supplied_findings\|_attach_accepted_findings" bundlebees_plugin/tools.py` (plus read-only `grep`/`sed` source inspection) | 0 | source read |
| 14 | `timeout 60 rm -rf tests/__pycache__ bundlebees_plugin/__pycache__` then `timeout 60 git status --short --ignored` | 0 | removed bytecode created by my early imports; only the review file remains |

## Could not run

- **The real pytest suites** (approved file and full `tests/`) — pytest is not
  installed and there is no network or install step available. Both allowed
  pytest commands were attempted and failed with `No module named pytest`; I do
  not claim a pytest result.
- **The coordinator's RED/GREEN records, 4043-case junit comparison and
  slop-lint** — accepted as supplied facts; not re-derived here.
- **A base-commit diff / byte-identity check of the approved test file** — the
  checkout has one squashed commit and no usable base, as stated in the brief.

## Shim disclosure

Test bodies were executed directly (command 9) with a small runner that supplies
only the pytest `tmp_path` fixture and stubs the absent `jsonschema`. Its exact
content is:

```python
import importlib.util, sys, tempfile, traceback, types
from pathlib import Path

# jsonschema is not installed on this host. contract.py imports it at module
# scope and uses it only in _jsonschema_problem, which these six tests never
# reach (none supplies brief.contract). Fail loudly if it is ever used.
stub = types.ModuleType("jsonschema")
class Draft202012Validator:
    def __init__(self, *a, **k):
        raise RuntimeError("jsonschema shim used: real jsonschema was needed")
stub.Draft202012Validator = Draft202012Validator
sys.modules["jsonschema"] = stub

spec = importlib.util.spec_from_file_location(
    "test_retry_findings_attach_scope",
    "tests/test_retry_findings_attach_scope.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failed = 0
tests = [n for n in dir(mod) if n.startswith("test_")]
for name in tests:
    fn = getattr(mod, name)
    kwargs = {"tmp_path": Path(tempfile.mkdtemp())} if fn.__code__.co_argcount else {}
    try:
        fn(**kwargs)
        print(f"PASS {name}")
    except Exception:
        failed += 1
        print(f"FAIL {name}")
        traceback.print_exc()
print(f"{len(tests)-failed} passed, {failed} failed, {len(tests)} total")
sys.exit(1 if failed else 0)
```

Each test gets a fresh temporary directory on the host filesystem; nothing in
the repository is written. `PYTHONDONTWRITEBYTECODE=1` prevents `.pyc` writes.
The `jsonschema` stub is never exercised by these six tests (no `brief.contract`
is supplied), and it raises if it ever is, so a silent validation bypass is not
possible.
