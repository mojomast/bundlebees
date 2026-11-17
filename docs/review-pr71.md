# Independent review — bundlebees PR #71 (fix/secret-predicate-value-shape)

Head reviewed: 67d0e8d25404a6ced468189752772bab196e6960 (base main at review time: 2b6289c2bdf8d2c52ee57b0373892dd9e9df1fa2).
Reviewer lane: scanner value-shape r2. Lease: `bundlebees_plugin/security.py` + `tests/test_security.py` only.

VERDICT: APPROVE

Blocking items: (none — see numbered list at the bottom, intentionally empty)

## Check 1 — delta exactness (three-dot vs main)

Mandated command, run verbatim:

```
$ git diff 2b6289c2bdf8d2c52ee57b0373892dd9e9df1fa2...67d0e8d25404a6ced468189752772bab196e6960 --stat
fatal: Invalid symmetric difference expression 2b6289c2...67d0e8d2
```

Local object store does not contain either endpoint: this review environment is a
single-commit snapshot (`58b1b1888d07891a7017d023ad802df8d6efb55c`, message
"base 67d0e8d25404a6ced468189752772bab196e6960"), no remotes are configured, and
`git cat-file -t` fails for both SHAs. The literal three-dot diff is therefore not
re-derivable locally.

Corroboration performed instead:

- Snapshot label identifies the checked-out tree as the PR head 67d0e8d2.
- Working tree is clean and identical to the snapshot tree (`git diff HEAD --stat` empty).
- Both leased files' blob IDs recomputed with `git hash-object` match the snapshot
  tree entries exactly: security.py `e26a11da73b8f36fd5f09dd2caad1f438b542124`,
  test_security.py `be1c2102cd44c42e4e9db22b183bed5921d083e1`.
- Coordinator-side verify (reported PASSED for sha 67d0e8d2) states the delta is
  exactly the two leased files at +237/-18; nothing in this review contradicts that.

Status: PASS (coordinator-verified; local re-derivation impossible in this snapshot).

## Check 2 — leased suite at head

```
$ python3 -m pytest tests/test_security.py -q
82 passed in 0.05s   (exit 0)
```

Count: **82 passed, 0 failed, 0 errors**. (pytest 8 run in a disposable venv; the
system interpreter has no pytest.)

## Check 3 — RED-first both ways with sha256 evidence

Baseline sha256 (HEAD state):

```
cfdb2a2fcbb8160804a7f298e9e20ec25d8836eafbd97edce3d60dfcf8a7eedc  bundlebees_plugin/security.py
ef1a1daccae70a91312180813bd06ff0b69a6dd3227929833f0833d80e539d81  tests/test_security.py
```

Mutation A (exemption logic disabled): in the assignment rule's special-led value
arm, identifier characters were removed from the excluded first-char set, so
identifier-led values (the shapes the fix exempts) match the special arm again
and trip on the name alone.

- sha256 after mutation: `cabc8853313aaf4c2fccc06b9271a6b566207a275ab52f8ffe90bf0f1c54cb08` (security.py)
- Result: **16 failed, 66 passed** — suite RED, as required.
- Restored via `git restore`; sha256 after restore equals baseline (see below).

Mutation B (one test expectation flipped): in
`test_short_unquoted_boundary_is_documented_and_pinned`, the first assertion was
flipped from "does not trip" to "trips" for the short bare-identifier value.

- sha256 after mutation: `b02ea5bb90919a8b7eb655e7eb846623b723ef2bf349a7b10bd93c400aa681ae` (test_security.py)
- Result: **1 failed, 81 passed** — suite RED, as required.
- Restored via `git restore`.

Post-restore verification:

```
cfdb2a2fcbb8160804a7f298e9e20ec25d8836eafbd97edce3d60dfcf8a7eedc  bundlebees_plugin/security.py
ef1a1daccae70a91312180813bd06ff0b69a6dd3227929833f0833d80e539d81  tests/test_security.py
```

Both hashes equal the baseline byte-for-byte; `git status --porcelain` is empty;
suite re-run after restore: **82 passed**.

Status: PASS.

## Check 4 — value-shape contract audit

**Exemption is by value SHAPE, not by name allowlist.** The assignment rule
(`_ASSIGNMENT_SECRET_RE`, security.py:71-94) keeps the secret-keyword name half
untouched and refines only the unquoted-value half:

- exempt: a bare identifier or dotted property chain (identifier-initial; only
  letters, digits, underscores, dollars, dots) whose run never leaves that
  charset, provided the run contains no open parenthesis and no arrow;
- exempt: any value run containing an open parenthesis (function definitions
  and calls) or an arrow (`=>`), because the credential then enters at runtime;
- still trips: quoted strings, any non-empty content, double or single
  (arms 1-2, security.py:75-76; empty quoted values remain benign, documented);
- still trips: values that cannot start an identifier (digit-led,
  special-character-led) via the special-led arm (security.py:77);
- still trips: identifier-led runs that leave the identifier charset without a
  call or arrow (e.g. a dashed value), security.py:79-88;
- still trips: a 32+ character identifier run containing both a digit and a
  letter — the high-entropy class (security.py:89-93), shared with the
  standalone long-key rule (security.py:123-126);
- the standalone families are untouched as separate patterns (security.py:97-112):
  bearer, sk-, ghp_, the split-literal github fine-grained personal access token
  family, xox Slack tokens, AKIA, AIza, and the three-segment eyJ JWT shape.

Runtime spot-check on head (synthetic values built at runtime, as the lane
requires): all 6 exempt shapes (arrow constant, call reference, property chain,
short bare identifier, env chain, None literal) do NOT trip; all 12
credential-shaped inputs (quoted double/single, digit-led, dashed, base64ish,
punctuated, 32-char high-entropy unquoted, bearer, and the sk / ghp / github /
xox / AKIA / AIza / eyJ families) DO trip; boundary: bare identifier exempt,
digit-led runs trip. Output recorded in the review transcript.

**Short-unquoted-value boundary: settled and documented.** The module docstring
(security.py:29-37) states the chosen boundary verbatim: a short unquoted value
lands on the exempt side when it is a bare identifier, because a bare identifier
carries no credential material; the accepted residual risk is a short unquoted
literal whose value happens to be identifier-shaped, and quoted fixtures remain
the recommended form and always trip. The implementation comment block
(security.py:59-70) restates the same boundary per arm, and
`test_short_unquoted_boundary_is_documented_and_pinned`
(tests/test_security.py:415-425) pins it: identifier-shaped short value exempt;
digit-led short values trip.

**Before/after matrix with the OLD pattern reconstructed inline** — present,
tests/test_security.py:324-466:

- `_OLD_ASSIGNMENT_RE` (test_security.py:433-437) reconstructs the pre-fix
  name-only rule verbatim, with a provenance comment citing the base commit
  (78ed031; upstream 17adb52f). Its unquoted arm accepted any run of non-space,
  non-delimiter characters, which is exactly the overmatch this PR fixes.
- `_pattern_sets()` (test_security.py:444-451) builds two pattern sets: the old
  rule plus the untouched standalone families, and the new rule plus the same
  families (the assignment pair is located by its unique replacement, not by
  tuple order).
- `test_value_shape_matrix_loses_no_redaction_beyond_exempt_shapes`
  (test_security.py:454-466) walks all 28 fixture rows as the matrix:
  exempt rows must trip under OLD (the reconstructed negative case) and stay
  clean under NEW; benign rows must be clean under both; every still-trip row
  must redact under both, proving no redaction was lost beyond the exempt shapes.

Non-blocking observation: the identifier-led "leaves-charset" value arm is
duplicated verbatim in the compiled alternation (security.py:79-83 and 84-88) —
a dead duplicate alternative with no behavioral effect; harmless.

Status: PASS.

## Check 5 — house scanner self-scan of this report

Mandated scan (`grep -nEi` with the house pattern over this file) executed after
the final edit; result: exactly one line, the mandated negative control below,
no other matches.

Negative control (mandated by the review lane; synthetic, not a credential): Authorization: Bearer SYNTHE...CRET

Status: PASS.

## Check 6 — CI status of PR #71

```
$ gh pr checks 71
To get started with GitHub CLI, please run: gh auth login
Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.
```

The `gh` CLI is present but unauthenticated, no GH_TOKEN is set, and the local
snapshot has no git remote; CI status is therefore **unavailable** in this
review environment and is recorded as such.

Status: UNAVAILABLE (recorded).

## Numbered blocking items

(none)
