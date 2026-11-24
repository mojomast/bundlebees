VERDICT: APPROVE

Findings (all non-blocking; no change requested for #259)

1. **Minor (verification limitation) - docs/ACDD.md:5**: the cited merge SHA `2f83ae44261ae065f7b7413471ad1ebe039107dc` and the PR numbers `#241/#242/#246/#247/#252` cannot be independently checked in this checkout. It is one squashed commit (`8d63f87`, message "base d6456be11cb4606383e5469adaed652fc680dc52"), and `git cat-file -t` fails for both `82b911820c70ebfa1c9c20b9dc6eb9f1633cd9bb` and `d6456be11cb4606383e5469adaed652fc680dc52`, so `git log --merges` shows no merge commits. This is a limitation of the supplied checkout, not a contradiction: the SHA is no longer the PR base `82b9118` (the concrete defect the earlier review named is fixed), and every claim in the new paragraph matches the coordinator bullet at CHANGELOG.md:204. Concrete fix only if the landing machine cannot resolve it either: drop the parenthetical SHA and keep "bundlebees #5".

2. **Minor (pre-existing, outside this PR's permitted scope) - docs/ACDD.md:133**: "bundlebees' `integrate` currently checks GREEN only" is stale. `acdd_cli.integrate_preflight` also requires a valid REFACTOR whenever `refactor` is in `evidence_required` (bundlebees_plugin/acdd_cli.py:866-878, called from bundlebees_plugin/cli.py:3468), which docs/ACDD.md:172 and CHANGELOG.md:89-91 already state. The PR SPEC freezes the protocol text, so #259 correctly left this sentence alone. Concrete fix (follow-up docs-only PR): change the parenthetical to "(and REFACTOR when the contract requires it)".

3. **Minor (verification limitation) - docs/ACDD.md:164,173**: Orbit #26 and its file paths are a cross-repository claim that cannot be checked offline. This checkout corroborates the module and scheme only: tests/test_signed_report.py:8-9,94-107 names Orbit #26 and `server/acdd-evidence.mjs` with `verifyRecordSignature` / `bundlebeesEvidenceKey`. No contradiction found; no change requested.

## Verification performed

- Status paragraph is dated "Nov 24, 2026" (docs/ACDD.md:5). A merge-status scan of the whole file (`rg -ni "not merged|unmerged|under review|in review|pending merge|not yet|feat/acdd" docs/ACDD.md`) returns no match; the only "stale" matches are the protocol's `stale_revision` (docs/ACDD.md:147) and "stale evidence" (docs/ACDD.md:67).
- Enforcement claims match the code: `_gate` refuses a declared tier below the floor with `risk_tier_below_floor` and a high tier without a contract with `contract_missing` (bundlebees_plugin/tools.py:568-637, risk math in bundlebees_plugin/risk.py:104-129); an attached but unapproved contract reaches `check_frozen` and is refused with `contract_not_frozen` (bundlebees_plugin/tools.py:520-555, bundlebees_plugin/contract.py:603-616). Preflight is side-effect-free and reuses the same gate (bundlebees_plugin/preflight.py:55-96). Verify requires RED and records a signed GREEN (bundlebees_plugin/acdd_cli.py:740-803); integrate requires GREEN and, when listed, REFACTOR (bundlebees_plugin/acdd_cli.py:833-878).
- "the phase/reviewer policy table in risk-tiers.md remains report-only" matches docs/risk-tiers.md:4-11 and the risk_policy docstring (bundlebees_plugin/risk_policy.py:1-12).
- No private paths, hosts or IPs: a scan of docs/ACDD.md for absolute user-home paths, drive-letter Windows paths, loopback addresses and `.local` names returns no match.
- Approved tests: no test asserts docs/ACDD.md text; tests/test_risk_docstrings.py pins only risk.py and risk_policy.py, and its three assertions pass (both code fingerprints are unchanged), so a docs-only change cannot weaken a gate, sandbox, check or test. Scope note: the stated range `82b9118..d6456be` is absent from this checkout (single squashed commit), so I reviewed the complete final docs/ACDD.md and the change description in CHANGELOG.md:204 rather than a per-line diff.

## Test commands (exact, with exit codes)

- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_risk_docstrings.py` -> exit 1 (`/usr/bin/python3: No module named pytest`).
- `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests` -> exit 1 (same `No module named pytest`; the suite could not be collected or run).
- `python3` inline reproduction of the three assertions in tests/test_risk_docstrings.py (AST docstrings, SHA-256 code fingerprints, stale-phrase scan) -> exit 0, all True. No network or local wheel cache is available, so pytest could not be installed; "no new failures versus source_rev" is therefore not machine-measured, but the PR changes no code or test and the pinned fingerprints match.
