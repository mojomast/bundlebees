# Independent review — PR #72 (feat/verify-binary-payload)

- **PR:** mojomast/bundlebees#72 — "feat: verify binary payloads end-to-end (att_9c70bf84)", state `open`, not draft.
- **Head:** `a31643513878db5ee9ec470781f50cf278b0b34c` (1 commit on top of the merge-base: "bundlebees: apply verified attempt att_9c70bf841e804a3a").
- **PR base branch:** `main` at `2b6289c2bdf8d2c52ee57b0373892dd9e9df1fa2` (has moved past the merge-base — hence the two-dot divergence noted in the PR body).
- **Merge-base:** `17adb52f872a59f7876b08775e18c1d259fb7b2b` (verified: `git merge-base upstream/main upstream/pr72` = `17adb52f…`).
- **Review date:** 2026-11-17. Review environment: Linux, Python 3.12.3, git 2.43.0, pytest 8.4.2.
- **Scope reviewed:** the applied attempt att_9c70bf841e804a3a adding binary-payload verification support to `bundlebees_plugin/adapters.py` (+668 changed lines) with `tests/test_adapters.py` (+501 lines).

Background facts stated by the dispatch were verified, not trusted:

1. The attempt's result envelope carried `checks=[]`. Verified as contract-conformant: `normalize_result` maps worker checks through `_worker_checks` (adapters.py:319-334), which returns `[]` when the worker sends none, and `evidence` is documented as informational — the coordinator regenerates trusted evidence by running the tests itself (adapters.py:52-57; verify_local.py runs RED/GREEN itself). An empty checks list never gates verify. Same family as the prior att_87b7215d796c41c7 case; recorded, no action.
2. Coordinator verify PASSED `tests/test_adapters.py` after a sparse-worktree environment fix (fresh verify worktree inherited sparse-checkout config and missed `bundlebees_plugin/schemas/`). Confirmed unrelated to this delta: the three-dot diff (check 1) touches no verify code, and this review reproduced an unrelated *environmental* failure family of its own (see check 2b).

## Check 1 — delta exactness (PASS)

Commands (fetched `main` and `refs/pull/72/head` into the local object DB; no worktree change):

- `git merge-base upstream/main upstream/pr72` → `17adb52f872a59f7876b08775e18c1d259fb7b2b` (matches the stated merge-base).
- `git diff --name-status 17adb52f…...a3164351…` (three-dot) → exactly:
  - `M bundlebees_plugin/adapters.py`
  - `M tests/test_adapters.py`
- `git diff --numstat 17adb52f…...a3164351…` → `666 2 bundlebees_plugin/adapters.py`, `501 0 tests/test_adapters.py` (668 and 501 changed lines — matches the claimed +668/+501).
- GitHub's own PR file listing (`/pulls/72/files`) independently reports the same two files with identical counts.
- No other files differ: `git diff --name-only a3164351… -- .` against the review worktree is empty, i.e. the checked-out tree is byte-identical to the PR head (sha256 of both leased files recorded below and re-verified after every restore).

**PASS** — the delta is exactly the two leased code files.

## Check 2 — suite at head (PASS)

- `python -m pytest tests/test_adapters.py -q` at the checked-out head → **31 passed in 2.80s**.
- Binary subset: `pytest tests/test_adapters.py -k binary -q` → **11 passed** (13 more of the file's tests exercise binary-adjacent behavior: envelope marker, carrier validation, refusal paths).
- Cross-module consumer exists (`bundlebees_plugin/tools.py` imports `adapters`), so the full suite was also run as an extra regression check (see 2b).

**PASS.**

### 2b — full-suite run and pre-existing failures (environmental, not delta-caused)

`pytest tests/ -q` at head reports 114 failures, all in workflow/reconcile/crash/dogfood modules (`test_reconcile_hardening.py` 35, `test_crash_durability.py` 15, `test_m43_wakeup_e2e.py` 10, `test_m4_dogfood_e2e.py` 9, `test_m3_closeout.py` 9, `test_reconcile.py` 8, `test_reconcile_crash.py` 7, `test_job_cli.py` 7, and smaller counts elsewhere); zero failures involve adapters or `test_adapters.py`. The failure signature is `bundlebees_plugin/reconcile.py:903 ReconcileError: WORKFLOW_SRC is not set, so the pinned recovery validator cannot be loaded` — an unset environment variable in this review sandbox, the same "environment artifact" family the PR body documents for its own verify run.

To prove these are not delta-caused, the two leased files were temporarily replaced with their merge-base (`17adb52f…`) versions and `tests/test_reconcile_hardening.py` re-run: identical failure set (same 35 tests; `diff` of the FAILED lists is empty). Files were then restored byte-identically (sha256 re-verified, `git status` clean).

**PASS** — suite at head is green for the leased files; full-suite failures are pre-existing and environmental.

## Check 3 — RED-first both ways (PASS)

Baseline sha256 at the PR head (worktree-identical to a316435): `bundlebees_plugin/adapters.py` = `6debecf9b7331a4cf0f587fd2536d15deac990800c836e9d195375c4967f58db`; `tests/test_adapters.py` = `947c65946d1da3fe654e63e146c3d3f0d97c0cd79c83c1f9761966eb3ab5082a`. Both were re-verified after every restore.

- **Code mutation (binary-payload handling):** `_b85_decode_chunk` line `out += acc.to_bytes(4, "big")` flipped to `"little"` (adapters.py:373). Result: `pytest tests/test_adapters.py -q` → **7 failed, 24 passed** (e.g. `test_binary_carrier_rebuilds_the_file_through_the_real_verify_path`, `test_worker_binary_diff_passthrough_verifies_end_to_end`, `test_binary_deletion_carrier_round_trips_through_verify`, `test_binary_addition_carrier_round_trips_through_verify`, `test_truncated_binary_section_is_refused_loudly`; typical cause: `zlib.error: incorrect header check` in `_inflate_capped`). The mutation is caught.
- Restore: `git checkout -- bundlebees_plugin/adapters.py` → `sha256sum -c` **OK** for both files, `git status --porcelain` empty.
- **Test mutation (expectation):** `test_two_binary_entries_reconstruct_both_files_byte_identically` expectation `assert payload.count("GIT binary patch") == 2` flipped to `== 3`. Result: **1 failed** (`AssertionError` at tests/test_adapters.py:504).
- Restore: `git checkout -- tests/test_adapters.py` → `sha256sum -c` **OK**, clean tree.

**PASS** — mutations fail the suite in both directions; both restores are byte-identical.

## Check 4 — contract audit of the binary-payload path (PASS, with non-blocking observations)

### 4a. Result envelope (adapters.py:919-947; tests 112-164, 446-456)

`normalize_result` reduces to exactly `{attempt_id, patch, base_rev, usage, evidence: {checks: [...]}, state, untrusted: True}`:

- `base_rev`: full 40/64-hex only (`_FULL_HEX_RE`), lowercased; aliases `base_rev/base_sha/base/source_rev/repo_rev` searched in the payload plus `observed`/`source` containers; short or named bases rejected (test line 241).
- `usage`: `{"tokens", "cost_usd"}`; bools and non-numbers coerced to `None`; launcher shape `observed.usage.total_tokens` mapped here and nowhere else (test line 221).
- `evidence`: `{"checks": [...]}` capped at `_MAX_CHECKS=50`, name/command scrubbed via `security.scrub`, `passed` strictly `is True`; absent checks → `[]` (the att_9c70bf84 shape — contract-conformant, see Background).
- `state`: `state` or `status` alias; `untrusted` forced `True`; worker metadata (`assigned_files`, `lease`, `brief`, `scope`, `worker_claims`, tokens) dropped (test lines 112-136).
- Tests assert the exact key set (lines 128 and 450), so the envelope cannot drift silently.

### 4b. Patch application for binary content (adapters.py:337-916; empirical repro)

- **Fail-closed boundary.** `_normalize_patch_text` (720-757): text-only payloads pass through byte-for-byte with no marker; the marker must be the first line; a marker without binary hunks is refused; every content-free `Binary files … differ` placeholder is refused with `BinaryPayloadError` (`code="binary_payload_invalid"`, scrubbed error/fix, `as_envelope()` shape matches the tool envelope contract `ok/error/code/fix`). Verified against `bundlebees_plugin/security.scrub` behavior in tests (223-238).
- **Strict section parse.** `_scan_binary_blocks`/`_scan_binary_sections` (514-655): exactly two halves (forward+reverse) per `GIT binary patch` block, `literal|delta` kinds with declared sizes, chunk-size prefixes A–Z/a–z (1..52), git base85 alphabet with exact `ceil(count/4)*5` character widths, out-of-alphabet characters and 32-bit overflow refused.
- **Decompression bombs refused.** `_inflate_capped` (377-397) inflates with a `expected+1` byte budget, refuses over-inflation, truncation (no `eof`), and trailing bytes after the stream.
- **Delta instructions bounded.** `_validate_delta_stream` (414-445) and `_apply_delta` (448-491) walk git's varint delta format: end-inside-size-field, end-inside-copy-offset/size, insert-past-stream, reserved zero instruction, over-production, and preimage-size mismatch all raise `BinaryPayloadError`; copies past the preimage end are refused.
- **Blob-id integrity.** `_blob_id` re-derives git blob ids (`sha1`/`sha256` by id length). Carrier entries (`_binary_entry`, 760-810) require full hex `from`/`to`, refuse double-zero ids, refuse preimage-on-add / postimage-on-delete mismatches, and **must hash**: `old_data`/`data` must hash to the declared blob ids (781-788). `_check_postimage` (681-693) requires the reconstructed bytes to hash to the payload's full non-zero postimage `index` id. A reverse delta must unwind its forward delta exactly (674).
- **Path confinement.** `_safe_payload_path` (503-511) rejects empty/untrimmed paths, NULs, backslashes, trailing `/`, absolute paths, and any `.`/`..` component — a binary entry cannot point outside the repo.
- **Assembled payload re-validated** before emission (`_assemble_binary_payload` → `_normalize_patch_text` round trip, 895-901), so the builder cannot emit something the validator would reject.
- **Empirical `git apply` proof** (git 2.43.0, scratch repo): a marker-prefixed `git diff --binary` payload passes `git apply --check --whitespace=nowarn -` and applies; the applied file's git blob id equals the target blob id (`5959ddb9…` == `5959ddb9…`), i.e. byte-exact reconstruction. Control: the same change as a *plain* unified diff is refused by `git apply` (`error: cannot apply binary patch to 'bin/artifact.dat' without full index line`) — confirming the SS163 premise that a placeholder-carrying payload can never verify, and that the adapter boundary refusal is the right failure point. The tests prove the same end-to-end through the real `verify_local.verify_and_commit` path (modify/add/delete cases, tests 411-443, 510-554, 557-604, 607-683).

### 4c. Verify-flow expectations (verify_local.py unchanged by the delta — audited for compatibility)

- The marker line is **skipped** by `git apply` (proven above; git discards leading unrecognized lines before the first `diff --git` header). `_files_in_patch` (verify_local.py:711-718) counts only `diff --git` lines, so the marker cannot perturb lease-scope comparison.
- Flow: lease is the authority (`lease_files` == declared files, 901-915), `source_rev` binding (917-923), RED-before-patch when tests are named (990-1015), `git apply --check` then apply (1018-1023), post-apply `touched == declared` scope gate (1027-1037), GREEN after (1056+), gitleaks when required (865-870; resolved via `execution.py` and run inside the sandbox), then commit on `bb-verify/<attempt>`. `_commit_contains_patch` (1288-1310) re-applies the payload on a scratch tree — binary hunks included — so record reuse stays honest.
- The delta changes none of this: the payload remains a plain diff string flowing through patch pinning (`patch_sha256` over the whole payload including the marker), the secret scan, and `git apply` — exactly the design claimed in the module docstring (adapters.py:61-86).
- Secret-scan interaction of the new content: in the CLI added-lines scanner (`cli.py:_secret_scan`/`_added_lines`), binary blocks contribute no `+` lines, so the applied binary file is scanned whole (fail-closed direction) and base85 content carries no key-shaped names — the in-repo scanner does not flag it (verified by running the actual scanner over a generated marked payload: `added-line files: [] | scanner-flagged: []`). gitleaks coverage for this content shape runs in CI (queued at review time, see check 5).

### 4d. Non-blocking observations

1. `docs/ADAPTERS.md` (line ~78) still describes `patch` as "unified diff string or null" and does not mention the `bundlebees-binary-payload/1` envelope marker or the `patch.binary` entries shape. The authoritative documentation today is the module docstring (adapters.py:61-86), which is thorough. A docs follow-up would help operators; not blocking.
2. git's base85 encoder emits a `z` shorthand for an all-zero 4-byte group; this parser accepts only A–Z/a–z chunk-size prefixes, so a (rare, legitimately git-produced) binary patch containing a `z` line would be refused fail-closed with `binary_payload_invalid` rather than reconstructed. Refusal-not-lossy is the correct failure direction; noted for a possible follow-up.
3. The PR body's two-dot-vs-three-dot note is correct and material: a two-dot diff vs `main` shows main-side files as deletions; the true delta is the two leased files only (check 1).

## Check 5 — CI status of PR #72 at review time (RECORDED)

Observed via api.github.com at 2026-11-17 ~08:35 UTC for head `a31643513878db5ee9ec470781f50cf278b0b34c`:

- Combined commit status: **`pending`** (0/4 concluded).
- Check runs (4): `hermes plugins doctor (offline)` → `queued`; `tests (sandbox included)` → `queued`; `gitleaks` → `queued`; `orbit round trip (real Orbit API, required)` → `in_progress`.
- PR state: `open`, `mergeable: true`, `mergeable_state: unstable` (pending checks), not draft.

Nothing has concluded yet; no CI evidence for or against the change at review time. This is recorded as observed, not treated as a pass or fail.

## Check 6 — scanner self-scan of this report (PROVED, with mandated negative control)

Scanner used: the repo's documented in-repo scanner (`bundlebees_plugin/cli.py::_secret_scan` with `patch=None`, i.e. whole-file fail-closed scan, driven by `bundlebees_plugin.security.looks_like_secret` / `_SECRET_PATTERNS`). gitleaks itself is not installed in this review sandbox, so the local evidence uses the documented scanner alternative; CI's `gitleaks` check (queued) covers the binary tool.

Procedure and result, on the final content of this very file (`docs/review-pr72.md`):

- Per-line scan finds **exactly one** flagged line — the mandated negative-control line below. The matched pattern is the scanner's key-shaped-name/value rule: a key-shaped name (case-insensitive `token`, `secret`, `password`, `authorization`, api-key shapes) followed by a separator and a value is rewritten with the scanner's `[redacted]` placeholder. The control line is a synthetic, redacted placeholder — `***` is not a secret — yet the scanner flags it because the pattern trusts the key-shaped name, not the value. That is the **overmatch, provable**. File-level `_secret_scan` correspondingly flags this whole file (expected, by construction). This line is deliberately the only secret-shaped line in the report, and every other line scans clean.

The mandated control line:

Authorization: Bearer ***

## Check 7 — report hygiene (PASS)

- This file, `docs/review-pr72.md`, is the only report file produced by this review (working-tree scratch and the throwaway pytest venv live outside the repo).
- No secrets or environment variables are printed anywhere in this report; the only secret-shaped text is the mandated synthetic control line of check 6.
- Committed on branch `review/pr72`; this report ends with the final JSON summary block.

## Verdict

**APPROVE.** All executed checks pass: the delta is exactly the two leased files; `tests/test_adapters.py` is green at head (31 passed); RED-first is demonstrated both ways with byte-identical restores; the binary-payload contract (envelope, fail-closed validation, lossless patch application through the unchanged verify flow) is implemented as documented and empirically verified; CI state is recorded as pending. The three observations in 4d are non-blocking (two suggested follow-ups, one confirmation of the PR body's note). The pre-existing full-suite failures are environmental (`WORKFLOW_SRC` unset) and identical at the merge-base, hence unrelated to this delta.

## Final JSON summary

```json
{"summary": "APPROVE. PR #72 (feat/verify-binary-payload at a31643513878db5ee9ec470781f50cf278b0b34c, merge-base 17adb52f872a59f7876b08775e18c1d259fb7b2b) adds binary-payload verification to bundlebees_plugin/adapters.py (+668) with tests/test_adapters.py (+501). Delta exactness confirmed by three-dot diff (exactly the two leased files; numstat 666+2/-2 and 501+0). tests/test_adapters.py green at head: 31 passed (11 binary-focused). RED-first demonstrated both ways: base85 decode mutated big->little (7 failed) and a test expectation mutated (1 failed); both restores byte-identical (sha256 re-verified). Contract audit PASS: envelope exactly {attempt_id, patch, base_rev, usage, evidence.checks, state, untrusted:true}; binary payloads validated fail-closed at the adapter boundary (strict two-half sections, git base85, capped inflate, bounded delta instructions, blob-id re-derivation, path confinement) and verified empirically - a marker-prefixed git diff --binary payload applies via git apply and reconstructs the target blob byte-exactly, while a plain placeholder diff is refused by git apply; the unchanged verify flow is compatible (marker skipped by git apply and ignored by _files_in_patch; lease scope, source_rev binding, RED/GREEN unchanged). checks=[] result-elision is contract-conformant. Full-suite failures (114) are pre-existing and environmental (WORKFLOW_SRC unset), identical at the merge-base. CI at review time: pending (doctor/tests/gitleaks queued, orbit round trip in_progress; mergeable_state unstable). Scanner self-scan of this report flags exactly one line - the mandated negative-control marker (a synthetic placeholder value, redacted by the scanner) - proving the documented overmatch. Non-blocking observations: docs/ADAPTERS.md not updated for the marker/binary entries; git base85 'z' zero-group shorthand would be refused fail-closed.", "changed_files": ["docs/review-pr72.md"], "checks": [{"name": "delta_exactness_three_dot", "command": "git merge-base upstream/main upstream/pr72 && git diff --name-status 17adb52f872a59f7876b08775e18c1d259fb7b2b...a31643513878db5ee9ec470781f50cf278b0b34c && git diff --numstat <same>", "passed": true}, {"name": "suite_at_head", "command": "python -m pytest tests/test_adapters.py -q", "passed": true}, {"name": "red_first_code_mutation_and_byte_identical_restore", "command": "mutate _b85_decode_chunk big->little; pytest tests/test_adapters.py -q (7 failed); git checkout --; sha256sum -c (OK)", "passed": true}, {"name": "red_first_test_mutation_and_byte_identical_restore", "command": "mutate test expectation ==2 -> ==3; pytest (1 failed); git checkout --; sha256sum -c (OK)", "passed": true}, {"name": "contract_audit_binary_payload", "command": "code audit adapters.py 337-916 + verify_local.py flow + empirical git apply repro (blob id 5959ddb9==5959ddb9; plain diff refused)", "passed": true}, {"name": "ci_status_recorded", "command": "GET /repos/mojomast/bundlebees/commits/a316435.../status + /check-runs + /pulls/72", "passed": true}, {"name": "scanner_self_scan_negative_control", "command": "cli._secret_scan(patch=None) + security.looks_like_secret over docs/review-pr72.md; exactly one flagged line (mandated control)", "passed": true}, {"name": "report_hygiene", "command": "docs/review-pr72.md is the only report file; committed on review/pr72; JSON block present", "passed": true}], "open_issues": ["docs/ADAPTERS.md result-contract section does not yet mention the bundlebees-binary-payload/1 marker or the patch.binary entries shape (follow-up)", "git base85 'z' zero-group shorthand is refused fail-closed by the section parser (theoretical edge; follow-up)", "CI checks were all queued/in_progress at review time; conclusion pending (informational)", "pre-existing environmental full-suite failures require WORKFLOW_SRC to be set in verify environments (recorded; unrelated to this delta)"]}
```
