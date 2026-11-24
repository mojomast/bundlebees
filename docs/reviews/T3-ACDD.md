VERDICT: CHANGES

1. **Major - docs/ACDD.md:5**: the Status paragraph is required to cite the merge SHA of bundlebees #5, but the only SHA it contains is `82b911820c70ebfa1c9c20b9dc6eb9f1633cd9bb`, which is this PR's base (`source_rev`), not #5's merge commit. It is presented as "main at" and will be stale the moment this PR lands. In this checkout `git cat-file -t 82b911820c70ebfa1c9c20b9dc6eb9f1633cd9bb` fails with "could not get object info" and `git log --all --oneline` has a single squashed commit, so the citation cannot be substantiated. Concrete fix: replace it with #5's merge commit found with `git log --oneline --grep=ACDD` (or `git log --merges`), for example "bundlebees #5, merged as `<40-hex>`"; if #5's merge SHA cannot be verified, drop the SHA instead of citing the PR base.

2. **Minor - docs/ACDD.md:5**: "a high tier requires a frozen `acdd/1` contract (`contract_missing`)" conflates two refusal codes. `bundlebees_plugin/tools.py:619` returns `contract_missing` only when no contract is attached at all; a contract that is present but not approved is refused by `contract.py:603` (`check_frozen`) with `contract_not_frozen` through `_dispatch_acdd` (`tools.py:542-545`). Concrete fix: say "requires an `acdd/1` contract frozen before dispatch (`contract_missing` when absent, `contract_not_frozen` when present but not frozen)".

## Checks

- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_risk_docstrings.py` - exit 1: `/usr/bin/python3: No module named pytest` (pytest is not installed; no network and no local wheel cache, so the test could not be collected or run).
- `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests` - exit 1: same `No module named pytest`; the full suite could not be run, so "no new failures versus source_rev" is not verified.
