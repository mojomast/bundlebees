# Independent Review: PR #70 (rails/r02-ledger)

Reviewed head: `2f8ba1004931cdc2cb1c5a873d58177dbe36ea64` (branch `rails/r02-ledger`), base `main` = `17adb52f872a59f7876b08775e18c1d259fb7b2b`. Lane: RAILS-R02 usage-ledger contract, produced by attempt `att_f67c7573c44241fc`.

The review sandbox was seeded with a squashed snapshot of the reviewed head; its tree hash `e9719040d9f96f3485e6a5dec77372ff593b6d63` was verified byte-identical to `2f8ba10^{tree}` by cloning the true upstream into a scratch mirror outside the workspace. All evidence below is grounded at the reviewed head.

## VERDICT: APPROVE

## Blocking items

None.

## 1. Delta exactness (three-dot view vs origin/main)

Method: bare mirror clone of the upstream repo into scratch, then `git diff refs/heads/main...FETCH_HEAD` where `refs/heads/main` = `17adb52f872a59f7876b08775e18c1d259fb7b2b` and `FETCH_HEAD` = `2f8ba1004931cdc2cb1c5a873d58177dbe36ea64`. Merge-base: `b2db04b8c8463cef5cbf25a64dcaf2130edd142e`.

```
A  contrib/rizzstack_rails/__init__.py
A  contrib/rizzstack_rails/contracts.py
A  contrib/rizzstack_rails/ledger.py
A  contrib/rizzstack_rails/privacy.py
A  contrib/rizzstack_rails/vendor-manifest.json
A  scripts/vendor_rails.py
A  tests/test_rails_ledger.py
7 files changed, 2507 insertions(+)
```

EXACT MATCH: exactly the seven leased files, nothing else, all additions (+2507 lines, matching the lane brief). No modifications or deletions to existing files.

## 2. Acceptance suite at the reviewed head

Command: `python3 -m unittest discover -s tests -p test_rails_ledger.py` (no `python` alias in the sandbox; `python3` is Python 3.12.3).

Result: **Ran 35 tests in 0.688s — OK. 35 passed, 0 failed, 0 errors.** One harmless `ResourceWarning` (unclosed handle in a test cleanup path, `tests/test_rails_ledger.py:456`); non-blocking.

## 3. RED-first mutation checks (both ways)

Baseline suite: 35/35 OK (see check 2).

**Mutation A — production code** (`contrib/rizzstack_rails/ledger.py`, semantic mutation of the dedupe comparison at line 465: `existing["semantic_sha"] != candidate_sha` inverted to `==`; chosen after confirming `candidate_sha` feeds only this comparison, so a hash-formula change there would be self-consistent and NOT bite — the strongest available ledger-side mutation is the comparison itself):

- sha256 before: `708c51044031b9a35078a0db2d38d6342ac1ec129d477897e9057e89f2bb653a`
- sha256 mutated: `e5525a6ae47ce468c689d93f70f072fe73b7f28d29d19f4d0af7dbd1b241319e`
- Suite: **FAILED (failures=4, errors=2)** — RED confirmed (identical retries raise, conflicting retries slip through, crash-retry paths break).
- sha256 after restore: `708c51044031b9a35078a0db2d38d6342ac1ec129d477897e9057e89f2bb653a` — byte-identical to before. Suite re-run: 35/35 OK.

**Mutation B — test expectation** (`tests/test_rails_ledger.py`, line 323: expected status code of the conflict refusal changed from 409 to 410):

- sha256 before: `e5b7a929981b7e341eb86d374520ed26b668f6ba8af39ac8d21cf6794adea6fa`
- sha256 mutated: `222149c6b374df0ea941145061571347fd0a7b6fb9a865b477a994548eeceb6c`
- Suite: **FAILED (failures=1)** — RED confirmed.
- sha256 after restore: `e5b7a929981b7e341eb86d374520ed26b668f6ba8af39ac8d21cf6794adea6fa` — byte-identical to before. Suite re-run: 35/35 OK.

Post-restore workspace `git status` clean; the two mutated files were restored from byte-exact copies. RED-first both ways: PASS.

## 4. Acceptance-coverage spot checks (R02 acceptance row)

| Acceptance item | Covering test function(s) | Evidence |
|---|---|---|
| Concurrent 10 writers serialize, commit-before-ack | `ConcurrencyTests.test_ten_concurrent_writers_serialize_with_commit_before_ack`; commit-before-ack semantics also pinned by `test_crash_after_commit_loses_nothing_acknowledged` (child dies in `after_commit`, event still durable) and `test_crash_before_commit_leaves_head_unchanged` (no event, head unchanged) | 10 threads x 5 appends, seq contiguous 1..50, chain verifies ok |
| Identical retry returns original receipt | `ConcurrencyTests.test_identical_retry_returns_original_receipt` | same event_id + hash, `duplicated=True`, still 1 row |
| Conflicting retry is 409 idempotency_conflict | `ConcurrencyTests.test_conflicting_retry_is_409` | asserts `IdempotencyConflict` with `status_code == 409`, still 1 row |
| Mutation/hash detection bites both ways | `HashVectorTests.test_positive_vectors`, `test_negative_vectors`, `test_ledger_row_hash_matches_formula`; tamper detection on the persisted chain: `BackupTests.test_backup_restore_validates_both_chains` | positive vectors match the frozen formula; different payload gives different digest; mutated envelope row makes `verify_chain` fail (rails chain), mutated `why` column fails legacy chain while rails stays ok |
| Full disk surfaces unhealthy, rejects appends without ack | `HealthTests.test_full_disk_surfaces_unhealthy_and_rejects_appends` | `LedgerUnavailable` raised, `health()["healthy"]` False, 0 rows persisted; healthy ledger stays healthy |
| Backup restore validates both chains in disposable copy | `BackupTests.test_backup_restore_validates_both_chains` | restore copy verifies `ok` + `legacy_ok`; then rails-chain-only and legacy-chain-only tamper vectors each detected |
| Readonly missing DB refusal | `ReadonlyTests.test_missing_db_refused` (+ `test_readonly_connection_is_query_only`) | `ReadonlyRefusal` on absent path; query-only pragma asserted; append refused |
| Planted strings never in DB or output | `PrivacyTests.test_planted_strings_never_reach_db_or_output` (+ `test_scrub_text_removes_every_shape`, `test_raw_transcript_ingestion_refused`) | six planted shapes absent from raw DB bytes and from `envelope_json`/`why` output columns; scrub redacts every shape; raw transcript keys refused |

All eight items covered by named tests. No coverage gaps.

## 5. Vendor manifest + vendor script determinism

- `contrib/rizzstack_rails/vendor-manifest.json` digests vs actual sha256 of the four contrib modules — all match exactly:
  - `__init__.py` `3a8119c2...2887f74` (791 bytes) — match
  - `contracts.py` `0fa1be97...92887f74` (18561 bytes) — match
  - `ledger.py` `708c5104...bb653a` (36061 bytes) — match
  - `privacy.py` `e9205592...aa2220b` (3063 bytes) — match
  (Full digests recorded in check 3 / section 3 baseline hashes; byte sizes match the manifest `bytes` fields.)
- `scripts/vendor_rails.py`: standard library only (`argparse, hashlib, json, os, subprocess, sys`); no network modules (no urllib/requests/http/socket imports). The single `subprocess` call is a local `git rev-parse HEAD` for the pin label — offline. Writes are verified post-write by hash and made idempotent (skip when content already matches), entries sorted, stable JSON — deterministic.
- Independent run at the reviewed head: `python3 scripts/vendor_rails.py --check` → `{"check": "ok"}`, exit 0. Pins green.
- Vendor behavior is also acceptance-tested in the suite: `VendorTests.test_copy_is_pinned_and_deterministic`, `test_rerun_restores_tampered_copy`, `test_check_mode_passes`, `test_manifest_covers_package`.

## 6. CI status of PR #70

`gh pr checks 70` was unavailable in the sandbox: `gh` 2.101.0 is installed but unauthenticated (no token; exiting with the auth prompt). Recorded instead via the public GitHub REST API for the reviewed head SHA (`GET /repos/mojomast/bundlebees/commits/2f8ba1004931cdc2cb1c5a873d58177dbe36ea64/check-runs`):

| Check run | Status | Conclusion |
|---|---|---|
| tests (sandbox included) | in_progress | — (still running at review time) |
| hermes plugins doctor (offline) | completed | success |
| orbit round trip (real Orbit API, required) | completed | success |
| gitleaks | completed | success |

Combined commit status: `pending` (solely because the tests job was still in progress when sampled; the local run of that suite is green, see check 2). Non-blocking observation, not a code defect.

## 7. House scanner self-scan of this report

Command: the house secret-scanner regex exactly as mandated in the review brief, run verbatim in the shell against this file. (The pattern itself is not reproduced here because quoting it verbatim makes the report self-match the scanner — one of the credential prefixes named in the pattern would appear literally.)

Result: exactly one match — the mandated negative-control line below. No other matches.

```
Negative control (NOT a secret; synthetic marker owned by the coordinator): Authorization: Bearer SYNTHETIC-NEGATIVE-CONTROL-NOT-A-SECRET
```

## Reviewer notes

- The suite is genuinely RED-first in construction: the file header states hash vectors are self-derived in-workspace and cross-applied against R01 later (R17/R18), which is consistent with the lane's RED-first evidence claim.
- Planted-string vectors in the tests are deliberately split-string built, so the scanner does not trip on the test file itself; the review report likewise avoids quoting planted shapes verbatim.
- Git identity for the review branch was set repo-locally (reviewer-bee) to satisfy the commit mandate; no global config touched, nothing pushed.
