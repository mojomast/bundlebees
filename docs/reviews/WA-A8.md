VERDICT: APPROVE

No blocker or major findings. The five approved tests pin the contract and the
implementation satisfies all five: `GIT_TIMEOUT_S` is 300 s and bounded,
`_git` passes a numeric `timeout` to `subprocess.run` by default, a
keyword-only `timeout_s` overrides it (900 s is honoured), and
`subprocess.TimeoutExpired` is translated to `IntegrateError` with code
`git_timeout` before the `check` branch is reached, so a timeout is raised even
when `check=False` and is never degraded to an ordinary non-zero return code.
No test is skipped, xfailed or edited; no gate, sandbox check or other test is
weakened; no dependency, network use, secret, hostname, IP or absolute home
path is added; `timeout_s` is keyword-only with a `None` default, so existing
`_git` callers are unchanged; and the CHANGELOG "### Fixed" first bullet (300 s
bound, override, `git_timeout` even with `check=False`) matches the code.

1. minor — `bundlebees_plugin/integrate_job.py:440`: `_hold_tested_head`
   invokes git directly via `subprocess.Popen(["git", "update-ref", ...])` and
   its handshake (`stdin.write`/`flush`, two `stdout.readline()` calls) has no
   deadline, so it is the one git invocation in this module that `GIT_TIMEOUT_S`
   does not bound. This is pre-existing, not touched by the `_git` change, and
   not exercised by the approved tests, so it does not block this slice. Fix
   when that helper is next revised: bound only the setup handshake (deadline
   around the readline exchange, and a bounded close), leaving the deliberate
   hold across the `yield` open, so no `integrate_job` git invocation can
   outlive the module's timeout policy.

## Commands run (with exit codes)

- `git status --porcelain=v1` — 0; working tree clean before the review (after
  writing this file, only `docs/reviews/WA-A8.md` is untracked/added).
- `git rev-parse HEAD` — 0; `644cc47ba44a3fbd5dc0ea522322a877deffb1e3`, a single
  squashed "base 2830c4d7bb94916d8790d48bf54b4379d16babf7" commit, as expected
  (no usable history).
- `timeout 120 python3 -m py_compile bundlebees_plugin/integrate_job.py` — 0.
- `timeout 120 python3 -m pytest -q -p no:cacheprovider tests/test_integrate_git_timeout.py`
  — 1; `No module named pytest`. Environment limitation, not a finding; the
  coordinator's supplied 5 passed / 0 failed at head is accepted.
- Direct Python harness (stdin, no file written) that stubs only the
  unavailable `jsonschema` import and monkeypatches
  `integrate_job.subprocess.run`, reproducing the five approved tests plus
  compatibility checks — 0; all passed: T1 default timeout 300 numeric and
  `0 < t <= 600`; T2 constant 300 in range; T3 override 900; T4 timeout with
  `check=True` → `git_timeout` and message contains `fetch`; T5 timeout with
  `check=False` → `git_timeout`; B1 ordinary rc=1 with `check=False` still
  returns the `CompletedProcess`; B2 ordinary rc=1 with `check=True` →
  `git_error` (not `git_timeout`); R1 a real (unmonkeypatched)
  `subprocess.TimeoutExpired` also maps to `git_timeout`, not a leak.
- `grep -rn "GIT_TIMEOUT_S\|git_timeout" --include=*.py --include=*.md .` — 0;
  shows the constant, the override, the error code and the CHANGELOG bullet.
- `grep -rn "skip\|xfail" tests/test_integrate_git_timeout.py` — 1 (no matches);
  the approved file has no skip/xfail markers and exactly five tests.
- Full `tests` suite not runnable here (pytest absent); relying on the supplied
  measurement of 223 failing/erroring ids at source_rev and 222 at head, i.e. 0
  new failures. Not a finding.
