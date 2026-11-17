# Independent review r3 — bundlebees PR #71 (fix/secret-predicate-value-shape) at c965df0

Reviewer: independent r3 review of the repair attempt att_9b81c201, applied unmodified by the
coordinator and recorded as head c965df0757622b00780f5f3292f7fec7ac4fccd3. Date: 2026-11-17.
Prior reviews: r1 and r2 (approved, with the value-shape exemption contract). Prior CI RED:
the pre-repair head 67d0e8d2 failed tests/test_client.py::test_get_is_sanitized (assert at
line 450) and tests/test_client.py::test_events_are_scrubbed (assert at line 466), both on
`assert 'leakedvalue' not in text`.

Hard constraint honored throughout: tests/test_client.py was never modified; every restore in
this review was verified byte-identical by sha256.

Revision record: branch review/pr71-r3; early commit 945c3c1 (full report with all measured
results); this final commit appends the revision record only. The scanner self-scan of this
file was re-run before each commit; final state: exactly one hit, the mandated control line
in section 6.2.

## 0. Environment and provenance

This checkout is a single-commit snapshot: `git log --oneline --all` shows exactly one commit,
`8c1c40b`, with the message `base c965df0757622b00780f5f3292f7fec7ac4fccd3` — the working tree
IS the PR head state, but the upstream history (origin/main, the merge-base, and the pre-repair
head 67d0e8d2) is not present as git objects. Consequently every check below was run either
directly at head, or against surgically reconstructed predicate states that are labeled as
reconstructions, and where a mandated git operation could not run, the exact command and error
are recorded under that check. Nothing was skipped silently.

Hygiene: no network was used beyond this checkout itself, plus one-time installation of the
project's own declared dev dependencies (pytest, jsonschema, pyyaml, ruamel.yaml, tiktoken,
fastapi, httpx — the set named in pyproject.toml `[project.optional-dependencies].dev`) into a
local untracked venv `.venv-review/`, which was required to run the mandated suite at all
(neither pytest nor jsonschema existed in the base image). No subagents were started. No
secrets or environment variables are printed in this report.

Transcription convention used in this report: fixture literals from terminal output are
reproduced in the runtime-join convention the tests themselves use (the batteries in
tests/test_security.py assemble every credential-shaped string by concatenating string
fragments at runtime), so this report carries no credential-shaped assignment line other
than the single mandated control line in section 6. `<joined-fixture>` denotes the tight
assignment shape written `tok" + "en=leaked" + "value`; `<joined auth-header prose>` denotes
`auth" + "orization: Bearer ***`; `<kw>` denotes the bare keyword name of a fixture split
across literals.

## 1. Delta audit

### 1.1 Literal three-dot diff vs origin/main — cannot run in this checkout (recorded, not silent)

```
$ git rev-parse origin/main
fatal: ambiguous argument 'origin/main': unknown revision or path not in the working tree.
$ git rev-parse origin/main...HEAD
fatal: ambiguous argument 'origin/main...HEAD': unknown revision or path not in the working tree.
$ git merge-base origin/main HEAD
fatal: Not a valid object name origin/main
$ git remote -v
(no output — no remote configured)
$ git cat-file -t c965df0757622b00780f5f3292f7fec7ac4fccd3
fatal: git cat-file: could not get object info
```

`git cat-file --batch-all-objects --batch-check` confirms the object store contains exactly one
commit (the snapshot). Therefore the literal three-dot changed-file enumeration against
origin/main is unresolvable in this checkout. This is an environment limitation, not a property
of the PR; it is recorded as open issue O-1 and the audit continues with the strongest
substitute evidence available in-tree.

### 1.2 Delta-scope evidence (what the tree itself establishes)

Every PR-specific marker in the tree was located by searching the whole working tree for the
repair-iteration vocabulary (tight-separator, value-shape, the CI-regression reference, PR #71):

| file | PR-specific content found |
| --- | --- |
| bundlebees_plugin/security.py | module docstring value-shape boundary (lines 9-47), assignment-rule comment block (lines 69-86), the tight-separator literal arm (lines 110-120), the tight-separator docstring paragraph (lines 26-31, 46-47) |
| tests/test_security.py | the r2 defect-lane battery section (lines 324-466), the tight-separator repair section headed with the PR #71 reference (lines 469-557) |

No other file in the tree carries any of those markers: the predicate's consumers
(bundlebees_plugin/tools.py, bundlebees_plugin/cli.py, bundlebees_plugin/bee_events.py) import
`looks_like_secret` unchanged and need no edits for a predicate-internal refinement; no other
test file, doc, or the CHANGELOG carries a tight-separator or PR-#71 marker. The evidenced PR
delta is therefore confined to exactly the predicate-fix scope:

1. `bundlebees_plugin/security.py`
2. `tests/test_security.py`

"Files the PR legitimately touched earlier": the r1/r2 iterations visible in the tree (the
value-shape refinement and its r2 battery, plus the r3 approval-surface pin
`test_r3_approval_surface_line_is_not_flagged`) are all inside these same two files; no third
file is evidenced as PR-touched. Whether the PR also touched e.g. the CHANGELOG cannot be
resolved without the merge-base and is folded into open issue O-1.

### 1.3 tests/test_client.py unmodified vs the merge-base

A byte diff against the merge-base object is impossible here (O-1); the following four-point
congruence package is the recorded substitute:

1. No PR-iteration marker of any kind appears in tests/test_client.py (no tight-separator
   vocabulary, no repair/PR-#71 reference anywhere in the file).
2. The file sits at exactly the CI-recorded line numbers: `test_get_is_sanitized` is defined at
   tests/test_client.py:445 with its fixture assertion loop at line 450, and
   `test_events_are_scrubbed` is defined at line 461 with its assertion loop at line 466 — the
   exact test/line pair recorded RED at 67d0e8d2.
3. The RED-first reproduction in section 2 lands on exactly those tests at exactly those lines
   with the recorded failure signature; if the test file had been edited to accommodate the
   fix, reverting only the predicate could not reproduce the recorded RED at the same lines.
4. The fixtures feeding the two tests (the tight logfmt event line at line 245/371 carrying the
   bare low-entropy assigned value, and the get() detail at line 117) contain the literal
   pre-existing fixture strings; they are pre-existing content of the unmodifiable file (their
   presence at 67d0e8d2 is proven by the CI-red record itself, which failed on those values).

sha256 recorded for the audit trail:

```
a61b2baec83371a3d412a7be53823bf7ced28ee72134de06947d1d74e8b9743c  bundlebees_plugin/security.py   (head c965df0)
970eff469a594075be5f35bc97dc6dd89dcca783fceb02da6dd203d4c6902f15  tests/test_client.py            (head c965df0)
c53a99aedaf75ea02e142094c48ed782e399b67d24a6d743b1f87a7351a94311  tests/test_security.py          (head c965df0)
```

## 2. RED-first, both ways (byte-identical restores)

Targeted run at head first:

```
$ .venv-review/bin/python -m pytest tests/test_client.py::test_get_is_sanitized tests/test_client.py::test_events_are_scrubbed
2 passed in 1.04s
```

### 2.1 RED reproduction — revert to the pre-repair predicate (the 67d0e8d2 state)

The pre-repair predicate was reconstructed surgically by removing exactly the tight-separator
literal arm (security.py lines 110-120: the comment plus the five regex literals ending in the
trailing group close on line 121), leaving every other byte untouched. The reconstruction was
validated before installation: the trimmed regex compiles, still trips on the tight dashed and
digit-led shapes, and no longer trips on the tight bare shape or the code-style spaced shape.
(Docstring-only differences between this reconstruction and the actual 67d0e8d2 text do not
affect behavior.)

Result with the pre-repair predicate installed:

```
FAILED tests/test_client.py::test_get_is_sanitized - assert 'leakedvalue' not...
FAILED tests/test_client.py::test_events_are_scrubbed - assert 'leakedvalue' ...
2 failed in 1.04s
```

with the recorded assert detail `tests/test_client.py:466: assert 'leakedvalue' not in ...`
and the surviving event text (transcribed per the section-0 convention):
`<joined-fixture> connected to [redacted-url]`, where the joined fixture is
`tok" + "en=leaked" + "value` — i.e. the scrub path left the bare low-entropy assigned value in
place while still redacting the URL. This is the CI-red reproduction at 67d0e8d2, at the same
tests and the same lines.

Additional validation of the reconstruction: with the pre-repair predicate installed, the
entire r2-era battery still passes —

```
$ .venv-review/bin/python -m pytest tests/test_security.py -k "not tight and not repaired and not client_event_line and not spaced_bare"
82 passed, 18 deselected in 0.06s
```

— which pins the reconstruction to the 67d0e8d2 behavior on the whole r2 contract, and the
18 deselected tests are precisely the repair's new cases (section 3).

### 2.2 Merge-base direction — the pre-PR predicate (base 78ed031 shape)

The merge-base assignment rule was reconstructed verbatim from the `_OLD_ASSIGNMENT_RE` literal
carried in tests/test_security.py (lines 433-437, documented there as the source of
bundlebees_plugin/security.py at base commit 78ed031): its unquoted value arm is a bare run of
non-space, non-delimiter characters, so any identifier-shaped value tripped on the name alone.
With that predicate installed:

```
$ .venv-review/bin/python -m pytest tests/test_client.py::test_get_is_sanitized tests/test_client.py::test_events_are_scrubbed
2 passed in 1.04s
```

Interpretation: at the merge-base the two named tests passed (the old rule scrubbed any value),
so the CI-red regression is delta-caused — introduced by the r2 value-shape exemption, which
stopped the client scrub path from removing bare low-entropy assigned values — and the repair
at head restores merge-base scrub behavior for the tight shape while keeping the r2-contract
exemptions. This is exactly the regression provenance the coordinator recorded.

### 2.3 Byte-identical restores

After each temporary installation the head file was restored from a pre-made copy and verified:

```
before: a61b2baec83371a3d412a7be53823bf7ced28ee72134de06947d1d74e8b9743c  bundlebees_plugin/security.py
after  (restore 1, post RED reproduction): a61b2baec83371a3d412a7be53823bf7ced28ee72134de06947d1d74e8b9743c
after  (restore 2, post merge-base run):   a61b2baec83371a3d412a7be53823bf7ced28ee72134de06947d1d74e8b9743c
after  (restore 3, post r2-battery run):   a61b2baec83371a3d412a7be53823bf7ced28ee72134de06947d1d74e8b9743c
git status --short after all restores: (empty)
```

## 3. r2 contract intact

### 3.1 The 31-case battery

The r2 battery in tests/test_security.py (section headed "value-shape boundary (defect lane
r2)", lines 324-466) comprises:

- 9 exempt rows (`arrow-constant`, `arrow-method`, `function-keyword`, `call-reference`,
  `property-chain`, `env-chain`, `bare-identifier`, `short-identifier`, `none-literal`),
  each asserted twice (does not trip; scrub leaves the line byte-identical);
- 15 still-trip rows (`quoted-double`, `quoted-single`, `digit-led`, `dashed`, `base64ish`,
  `punctuated`, the 32-character letters+digits unquoted row, and the seven literal families:
  bearer-shaped, openai-style, github-classic, github fine-grained, slack, aws, google, plus
  the JWT three-segment row), each asserted to trip;
- 4 benign rows (`plain-text`, `unrelated-assignment`, `keyword-not-at-name-end`,
  `quoted-empty`), each asserted not to trip;

28 parametrized rows, plus the 3 boundary pins inside
`test_short_unquoted_boundary_is_documented_and_pinned` (spaced bare identifier exempt;
digit-led tight and spaced trip; all-digits trip) = 31 cases, plus two further r2-era checks
(`test_r3_approval_surface_line_is_not_flagged`, and
`test_value_shape_matrix_loses_no_redaction_beyond_exempt_shapes`, which reconstructs the
pre-fix rule at runtime and asserts old-trips/new-exempt for every exempt row and no
redaction loss for every still-trip row). All green at head (137 passed, section 4).

### 3.2 The mandated negative-control exemption

The mandated synthetic marker row `negative-control-marker` (tests/test_security.py:501,
labeled "not a credential" in the same comment block at lines 499-500) is asserted exempt by
`test_tight_code_references_and_marker_stay_exempt` and was additionally verified at runtime:

```
marker = "auth" + "orization: Bearer ***"
looks_like_secret(marker) -> False
scrub(marker) == marker -> True
```

The marker stays exempt because its separator is code-style (whitespace after the colon) and
its placeholder run is not a bearer-class character run.

### 3.3 The tight k=v overmatch contract

`_TIGHT_TRIP_LINES` (5 rows) trip and are scrubbed (`test_tight_separator_bare_values_trip`,
`test_scrub_redacts_tight_separator_bare_values`), while the code-reference exemptions survive
under tight spacing (`tight-call-reference`, `tight-property-chain`, `tight-env-chain`,
`tight-arrow` all stay exempt — dotted chains and call/arrow shapes keep their exemption under
both spacings), and the spaced bare literal residual risk is pinned explicitly
(`test_spaced_bare_literal_residual_is_pinned`). Runtime spot-check:
`looks_like_secret("tok" + "en=leaked" + "value")` is True and `scrub` rewrites it to
`tok` + `en=[redacted]`.

### 3.4 The repair's new battery cases — 18, present and meaningful

Exactly 18 new test invocations beyond the r2 battery (confirmed both by the deselect count in
section 2.1 and by collection):

| test | invocations | what it proves |
| --- | --- | --- |
| test_tight_separator_bare_values_trip | 5 | five distinct tight shapes trip (bare word, digit-mixed bare, colon separator, dashed, digit-led) |
| test_scrub_redacts_tight_separator_bare_values | 5 | the same five shapes are rewritten through the scrub path, output contains the redaction marker |
| test_tight_code_references_and_marker_stay_exempt | 5 | call reference, property chain, env chain, arrow, and the mandated marker stay exempt under tight spacing |
| test_client_event_line_shape_is_scrubbed | 1 | the exact CI-regression shape leaves the scrubbed text entirely (value absent, IP absent, rewritten name present) |
| test_spaced_bare_literal_residual_is_pinned | 1 | the documented residual (spaced bare literal stays exempt) cannot silently widen |
| test_repaired_boundary_matrix_loses_no_redaction_beyond_code_style | 1 | all 11 tight rows cross-checked against the reconstructed pre-fix rule: no redaction lost, no new over-trip |

None is a tautology: the trip rows assert the live predicate positively, the scrub rows assert
a concrete rewritten output, the exempt rows assert byte-identical scrub output, the client
shape test asserts three independent observable properties, and both matrix tests make
differential assertions against a reconstructed second predicate rather than restating the
same call's result.

## 4. Suite at head

Targeted (the coordinator's measurement, reproduced):

```
$ .venv-review/bin/python -m pytest tests/test_client.py tests/test_security.py
137 passed in 17.25s
```

(37 in tests/test_client.py + 100 in tests/test_security.py = 137.)

Full suite (junit-recorded for exact counts):

```
$ .venv-review/bin/python -m pytest tests --junitxml=...
tests=2122  failures=114  errors=0  skipped=261  (passed = 1747)
```

The 114 failures are all environmental, concentrated in suites that hard-require pinned
production inputs and fail (by design, never skip) without them:
`RecoveryError: WORKFLOW_SRC is not set, so the pinned recovery validator cannot be loaded`;
`missing WORKFLOW_SRC='' (a pinned workflow checkout); HYVV_SRC='' ...; GITLEAKS=...`;
`VerifyError: no strong sandbox backend (bubblewrap or firejail) is usable on this host`.
Files affected: test_crash_durability.py (15), test_factory_cli_e2e.py (2),
test_factory_return_e2e.py (1), test_git_observer_integration_states.py (4), test_job_cli.py
(7), test_m3_closeout.py (9), test_m3_corrections.py (3), test_m43_wakeup_e2e.py (10),
test_m4_dogfood_e2e.py (9), test_reconcile.py (8), test_reconcile_crash.py (7),
test_reconcile_crash_process.py (4), test_reconcile_hardening.py (35).

Zero NEW failures attributable to the PR delta, proven directly: the identical 13 failing
files were re-run with the pre-repair predicate installed and the failure set is byte-identical
(`cmp` of the two sorted FAILED lists: identical; 114 = 114). A literal merge-base full-suite
run is impossible in this checkout (O-1); the failure-set-invariance experiment plus the
environmental cause analysis is the recorded substitute, and the failures are recorded
separately from the PR verdict exactly as the mandate requires. The repo's own
added-lines secret-scan acceptance test (tests/test_secret_scan_added_lines.py) passed inside
the full suite.

## 5. CI at the exact head

gh is installed (v2.101.0) but cannot be used from this checkout:

```
$ gh run list
To get started with GitHub CLI, please run:  gh auth login
Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
$ gh auth status
You are not logged into any GitHub hosts. To log in, run: gh auth login
```

additionally `git remote -v` shows no remote, so no check-run target exists to query. Per the
mandate's fallback, the recorded evidence for CI at the exact head is the local suite above
(targeted 137 passed = the coordinator's own verification at c965df0; full suite with the
delta-independence proof in section 4), and the prior RED at 67d0e8d2 is referenced through
the section-2 RED-first reproduction, which reproduces the recorded CI failure at the same two
tests and the same two assert lines. In-progress checks: none observable (no remote access);
recorded as open issue O-2, non-blocking per the mandate.

## 6. Scanner self-scan

### 6.1 The PR delta's lines

The coordinator gate regex was applied to the full current content of both PR-touched files
(a superset of any added line the delta could contain):

```
$ grep -nEi '<coordinator gate pattern, elided here because its github fine-grained literal arm self-trips any file containing it>' bundlebees_plugin/security.py tests/test_security.py
bundlebees_plugin/security.py:29:  ... a tight ``<joined-fixture>`` in an event or log line ...     <- red-record docstring prose
tests/test_security.py:477: # remove: ``<joined-fixture>`` in an event line. ...                    <- red-record comment
tests/test_security.py:478: # negative-control marker (``<joined auth-header prose>``-style) stays    <- mandated marker prose
tests/test_security.py:497:     "tight-env-chain": "signing_" + <kw>=process.env.SIGNING,            <- battery fixture row
```

All four hits are exactly the coordinator's recorded "4 trips ... all the mandated
scrub-battery/negative-control class": two red-record documentation lines (the module docstring
line and the repair-section comment, both backticked prose), the mandated negative-control
marker prose line, and the tight-env-chain battery fixture whose runtime join leaves the bare
keyword name adjacent to the equals sign (its value is a code reference, not a credential).
Every value in all four lines is a synthetic placeholder; there is zero credential material.
The remaining batteries are clean because every credential-shaped string is assembled by
runtime concatenation. Residual (non-blocking, R-1): the line-497 row could be re-joined with
the keyword itself split (`"token" + "=..."`) to be fully gate-clean in a future iteration;
as delivered it is inside the coordinator-accepted mandated class.

Confinement and no-runtime-echo verification (by reading the scrub path):

- The battery's secret-shaped strings exist only in tests/test_security.py fixtures/comments
  and the single security.py docstring documentation line; a recursive grep over
  bundlebees_plugin/ runtime code finds no fixture value outside that docstring line.
- The scrub path (security.py `_scrub_str`, lines 161-170) substitutes only fixed literals
  (`[redacted]`, `[redacted-url]`, `[redacted-host]`, `[redacted-ip]`) and regex group
  backreferences; it cannot echo any fixture value.
- The client scrub path applies exactly that function: bundlebees_plugin/client.py imports
  `scrub` from `.security` (line 34) and applies it to error strings (line 67), response data
  values (line 275), event text (line 297), and detail strings (line 371).
- tests/test_client.py's literal fixture strings are pre-existing content of the unmodifiable
  file (not added lines of this delta), consistent with the added-lines-only scan semantics
  the repo itself tests in tests/test_secret_scan_added_lines.py.

### 6.2 This report file

Self-scan before every commit of docs/review-pr71-r3.md with the mandated command. Final
state: exactly one hit, the single mandated control line below, labeled in the same line:

Authorization: Bearer ***  <!-- mandated synthetic negative-control marker; placeholder prose, NOT a secret; the only verbatim control line permitted in this report -->

## 7. Hygiene

- Worked only inside the working directory; no subagents; no secrets or environment variables
  printed.
- No network beyond this checkout and the one-time dev-dependency install recorded in
  section 0.
- Temporary predicate installations were restored byte-identical (section 2.3); working tree
  clean apart from this report and the untracked local venv.
- Bees do not push and do not open PRs: the branch review/pr71-r3 carries only local commits;
  the coordinator runs verify (apply + tests + secret scan) and pushes.

## 8. Open issues (none blocking)

- O-1 (environment, non-blocking): the literal three-dot enumeration vs origin/main and the
  byte-level merge-base diff cannot run in this single-commit snapshot (exact errors in
  section 1.1). Substitute evidence recorded in sections 1.2, 1.3 and 2.2 establishes the
  same conclusions: delta confined to the predicate-fix scope, tests/test_client.py congruent
  to the CI-red record, regression delta-caused.
- O-2 (environment, non-blocking): no GitHub check-run state could be queried (gh
  unauthenticated, no remote configured — exact outputs in section 5). Local suite recorded
  as the evidence per the mandate's fallback; the 67d0e8d2 RED is referenced via the section-2
  reproduction.
- O-3 (observation, non-blocking): 114 full-suite failures are environmental (missing pinned
  workflow/hyvv checkouts, gitleaks binary, sandbox backends) and proven delta-independent by
  the failure-set-invariance experiment in section 4; they belong to the environment, not the
  PR.
- R-1 (residual, non-blocking): the tight-env-chain fixture row (tests/test_security.py:497)
  trips a naive re-scan of the whole file; it is inside the coordinator-accepted mandated
  class, but a future re-join of the keyword fragment would make the file fully gate-clean.

## Verdict

APPROVE. All runnable checks pass; the repair reproduces the coordinator's 137-pass
measurement, the RED-first chain is demonstrated in both directions with byte-identical
restores, the r2 contract holds with the mandated negative-control exemption and the tight
k=v overmatch, the 18 new battery cases are present and meaningful, and no blocking finding
was identified. Open issues are environment limitations and non-blocking residuals only.

## JSON summary

```json
{
  "summary": "Independent r3 review of bundlebees PR #71 (fix/secret-predicate-value-shape) at c965df0: delta evidenced confined to bundlebees_plugin/security.py + tests/test_security.py with tests/test_client.py congruent to the CI-red record; RED-first demonstrated both ways (pre-repair predicate reproduces the recorded CI RED at tests/test_client.py:450/:466; pre-PR merge-base predicate passes, proving the regression delta-caused) with sha256-verified byte-identical restores; r2 contract intact (28-row battery + 3 boundary pins = 31 cases, mandated negative-control exemption, tight k=v overmatch) plus 18 meaningful new repair cases; targeted suite 137 passed; full suite 2122 run / 114 environmental failures proven delta-independent / 0 errors / 261 skipped; scanner self-scan of the delta shows only the coordinator-accepted mandated class (4 synthetic placeholder lines) and the report itself carries exactly one mandated control line; verdict APPROVE with non-blocking open issues only.",
  "changed_files": [
    "bundlebees_plugin/security.py",
    "tests/test_security.py",
    "docs/review-pr71-r3.md"
  ],
  "checks": [
    {"name": "delta-audit-literal-three-dot-diff-vs-origin-main", "command": "git rev-parse origin/main...HEAD", "passed": false},
    {"name": "delta-audit-scope-confinement-substitute-evidence", "command": "grep -rn for PR-iteration markers across the whole tree; consumer import audit", "passed": true},
    {"name": "delta-audit-test-client-unmodified-proof", "command": "marker grep + CI line congruence (445/450, 461/466) + RED reproduction lands on the same tests/lines + sha256 970eff469a594075be5f35bc97dc6dd89dcca783fceb02da6dd203d4c6902f15", "passed": true},
    {"name": "red-first-at-head", "command": ".venv-review/bin/python -m pytest tests/test_client.py::test_get_is_sanitized tests/test_client.py::test_events_are_scrubbed", "passed": true},
    {"name": "red-first-revert-pre-repair-predicate-CI-red-reproduction", "command": "install pre-repair predicate (tight arm surgically removed); same pytest command", "passed": true},
    {"name": "red-first-merge-base-direction", "command": "install pre-PR merge-base predicate (old assignment rule per _OLD_ASSIGNMENT_RE); same pytest command", "passed": true},
    {"name": "byte-identical-restores", "command": "sha256sum bundlebees_plugin/security.py before/after each temporary install; git status --short", "passed": true},
    {"name": "r2-contract-31-case-battery", "command": "dict census + full battery green inside the 137-pass run", "passed": true},
    {"name": "r2-contract-negative-control-marker-exempt", "command": "parametrized test + runtime probe (looks_like_secret False, scrub byte-identical)", "passed": true},
    {"name": "r2-contract-tight-kv-overmatch", "command": "tight trip/scrub/exempt/residual/matrix tests green at head", "passed": true},
    {"name": "repair-18-new-battery-cases", "command": "deselect count 18 in the pre-repair run + collection census", "passed": true},
    {"name": "targeted-suite-137", "command": ".venv-review/bin/python -m pytest tests/test_client.py tests/test_security.py", "passed": true},
    {"name": "full-suite-at-head", "command": ".venv-review/bin/python -m pytest tests --junitxml (2122 tests, 114 failures all environmental, 0 errors, 261 skipped)", "passed": true},
    {"name": "full-suite-zero-new-failures-vs-merge-base", "command": "failure-set invariance: same 13 files re-run under pre-repair predicate, sorted FAILED lists identical (cmp)", "passed": true},
    {"name": "ci-check-runs-at-head", "command": "gh run list (unauthenticated, no remote - local suite recorded as evidence per mandate fallback)", "passed": false},
    {"name": "scanner-self-scan-pr-delta-lines", "command": "coordinator gate regex over both PR files: 4 hits, all the coordinator-accepted mandated class, zero credential material", "passed": true},
    {"name": "scanner-self-scan-report-file", "command": "grep -nEi '<gate pattern>' docs/review-pr71-r3.md - exactly one hit: the mandated control line", "passed": true},
    {"name": "scrub-path-no-fixture-echo", "command": "read of security.py _scrub_str and client.py scrub call sites (67/275/297/371)", "passed": true}
  ],
  "open_issues": [
    "O-1 (environment, non-blocking): literal three-dot file list vs origin/main and byte-level merge-base diff unresolvable in this single-commit snapshot; exact git errors in section 1.1; substitute evidence in 1.2/1.3/2.2",
    "O-2 (environment, non-blocking): GitHub check-run states not queryable (gh unauthenticated, no remote configured); local suite recorded as evidence per the mandate fallback",
    "O-3 (environment, non-blocking): 114 full-suite failures are environmental (missing pinned WORKFLOW_SRC/HYVV_SRC checkouts, gitleaks binary, sandbox backends) and proven delta-independent by failure-set invariance",
    "R-1 (residual, non-blocking): tests/test_security.py:497 tight-env-chain fixture row would trip a naive whole-file re-scan; inside the coordinator-accepted mandated class; a future re-join of the keyword fragment would make it fully gate-clean"
  ]
}
```
