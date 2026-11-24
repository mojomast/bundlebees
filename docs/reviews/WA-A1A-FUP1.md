VERDICT: APPROVE

# Review: WA-A1A-FUP1 main `verify` subversion gate (PR #280, head cf33aba)

Reviewed every changed line of `bundlebees_plugin/cli.py`, the approved
read-only test file `tests/test_verify_cli_subversion.py`, the reused scan
(`bundlebees_plugin/verify_local.py:106-115`,
`bundlebees_plugin/subversion_scan.py:117-129`), the CLI error boundary
(`cli.py:2589-2594`, `cli.py:2655-2693`) and the record writer
(`cli.py:2696-2760`). I did not write this code.

The slice adds `from . import verify_local` at `cli.py:2938` and, at
`cli.py:3001-3004`, calls `verify_local.check_patch_subversion(patch)` right
after the existing no-patch refusal (`cli.py:2994-3000`) and before the ACDD
preflight (`cli.py:3006-3010`), branch selection, `_prepare_worktree`
(`cli.py:3025`), revision checks, `git apply` and the sandboxed run. That is
the exact position the brief requires, and it is before every operation the
refusal must prevent.

The reused function is the real one: `check_patch_subversion` calls
`subversion_scan.scan_patch` and raises `VerifyError("test_subversion", ...)`
on the first finding (`verify_local.py:106-115`). No scan logic was copied,
softened or reordered, and `verify_local.py` is untouched. The existing scan in
`verify_local.verify_and_commit` (`verify_local.py:954`) is still present, so
`verify --commit-local` keeps the same guarantee.

The refusal keeps the code and the finding text: `_as_cli_error`
(`cli.py:2589-2594`) reads `exc.code` -> `test_subversion`, `exc.fix`, and the
message through `str(exc)` (the `exc.error` attribute is absent on
`VerifyError`, so the `getattr(..., exc)` fallback yields the exception whose
`str()` is the message recorded by `VerifyError.__init__`). The outer
`except CliError` writes `code` and a clipped/scoped `error` into
`verify-results/dry-run/<attempt>.json` and re-raises, so `_dispatch`
(`cli.py:766-785`) prints the error and returns non-zero. Nothing swallows or
rewrites the `test_subversion` code.

AC mapping against the approved tests:

- AC-001, `tests/test_verify_cli_subversion.py:113`
  (`test_cli_verify_refuses_a_subverting_patch`): non-zero rc, code
  `test_subversion`, `ok:false`, `forced_exit` and `src/a.py` in the record
  error, and no sandbox call. Met by `cli.py:3001-3004`.
- AC-002, `tests/test_verify_cli_subversion.py:129`
  (`test_cli_verify_refusal_precedes_worktree_sandbox_commit_and_push`): the
  repo stays on `main`, `src/a.py` still holds its pre-patch bytes, and no
  sandbox/commit/push step ran. The branch/bytes assertions do prove ordering
  because `_prepare_worktree` switches the worktree to the feature branch in
  place (`cli.py:1894-1910`) and would fail on the clean `main` worktree.
- AC-003, `tests/test_verify_cli_subversion.py:145`
  (`test_cli_verify_accepts_a_clean_patch`): a clean patch still reaches the
  sandbox once and records `ok:true` with no code. Met; the new call is a no-op
  for a patch with no findings.
- AC-004, `tests/test_verify_cli_subversion.py:159`
  (`test_the_scan_alone_refuses_the_patch`): with only
  `verify_local.check_patch_subversion` monkeypatched to return `[]`, the same
  `SUBVERSION_PATCH` verifies (`rc == 0`, `ok:true`, no code). Because the CLI
  imports and calls the module attribute (`cli.py:2938`, `cli.py:3002`), the
  monkeypatch reaches the code under test; every other gate therefore clears
  this patch and the scan alone is responsible for the refusal.

Approved test file: read it in full; it contains the four described cases and
no `skip`, `xfail`, `try`/`except`, or weakened assertion. I found no edit to it
(supplied fact 2 also records the bee did not touch it). No gate, sandbox,
check or test is weakened, skipped or made optional; the slice is fail-closed
(a `VerifyError` becomes a `CliError` refusal, and any unexpected scan failure
still aborts through the CLI boundary rather than applying the patch). The
change adds no dependency, network call, secret, hostname or absolute path, and
`CHANGELOG.md` has only the coordinator bullet (I did not edit it); that bullet
matches the code, including "as soon as the worker patch is read", the ordering
list, and the `_as_cli_error` / `test_subversion` wording. Scope is the one
leased file.

## Findings

1. **minor (informational, no change required)** - `bundlebees_plugin/cli.py:2589-2594`
   relies on the `getattr(exc, "error", exc)` fallback plus `str(exc)` to carry
   a `VerifyError` message, because `VerifyError` exposes `.message`, not
   `.error`. It works today and the approved test pins the finding text, so the
   new call is correct. Concrete fix (only if this boundary is ever hardened
   outside this lease): map `getattr(exc, "error", None) or getattr(exc,
   "message", None) or exc` explicitly. Do not change it in this PR; the brief
   requires going through `_as_cli_error` as-is.

2. **minor (informational, approved read-only test)** -
   `tests/test_verify_cli_subversion.py:132-151` drives `verify --dry-run`, so
   `_commit_changes`/`_push_branch` would not run even without the fix and the
   `doubles.commits == []` / `doubles.pushes == []` assertions are vacuous.
   Ordering is still proven by the meaningful assertions (branch still `main`,
   `src/a.py` bytes unchanged, empty sandbox list). Because the file is
   approved and read-only, no change is requested here; noting it so a future
   review does not mistake those two lines for stronger evidence than they are.

No blocker or major findings.

## Commands run

- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_verify_cli_subversion.py`
  -> exit 1: `/usr/bin/python3: No module named pytest`. This image has no
  pytest (no network, and no pytest binary or virtualenv on PATH), so the
  targeted suite could not be executed here. Per the supplied facts this is not
  a slice failure; supplied coordinator evidence is RED at bf8df74 (the two
  approved tests failed) and GREEN at e0cdeb8 (valid, lease ok, only
  `bundlebees_plugin/cli.py` in the bee commit).
- `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests`
  -> exit 1: `/usr/bin/python3: No module named pytest` (same reason; full
  suite not executable in this image). Supplied full-suite junit node-id
  compare on this host: 226 failures/errors at bf8df74, 224 at PR head, 0 new,
  2 resolved, and the two resolved ids are exactly the approved RED tests.
- `python3 -m py_compile bundlebees_plugin/cli.py bundlebees_plugin/verify_local.py bundlebees_plugin/subversion_scan.py tests/test_verify_cli_subversion.py`
  -> exit 0: all four files parse (static syntax check only; generated caches
  removed afterwards).
- Static read of the call order and error path (`cli.py:2937-3004`,
  `cli.py:2589-2594`, `cli.py:2655-2693`, `cli.py:2696-2760`,
  `cli.py:766-785`); this is the basis for the AC assessment above in the
  absence of a runnable pytest.

Working tree at the end contains only this review file; nothing else was
changed.
