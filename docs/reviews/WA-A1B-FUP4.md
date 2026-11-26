VERDICT: APPROVE
BLOCKING: none

Independent review of bundlebees PR #308, squashed local commit `86a8715`
whose message records the PR head `720f5ed3fdd3eca6295a28014f9200e7ac3136b2`.
This checkout has exactly one commit (`git rev-list --count HEAD` = 1), so
there is no base commit to diff; the pre-change state is the one supplied in
the slice brief. I read the three product functions and their callers, the
approved test file in full (198 lines, 6 tests), and the new CHANGELOG bullet,
then replayed all six approved test bodies and ran independent probes against
the real functions. Both findings are closed exactly as the objective scopes
them, with no over-reach on any valid path that I could construct.

## Findings

1. **minor — `bundlebees_plugin/verify_local.py:1709`**: `_landed_counts`
   retains the same absence-vs-falsy pattern in a consumer outside this
   slice's named scope: `_count` does `int(junit.get(name) or 0)`, so a
   present falsy malformed counter (`""`, `[]`, `{}`) becomes `0` instead of
   being refused or reported absent. It cannot produce a false GREEN (the
   landed record's `counts` are informational, and `check_green` uses
   `result["cases"]`, not `counts`), so it is not a blocker for AC-001..006.
   Fix if the owner wants the class fully closed: mirror the new helper's
   rule here (`0 if junit.get(name) is None else junit.get(name)` before the
   existing `int`/`max` clamp), or document in the docstring that malformed
   present counters collapse to `0` by design. No action required for this
   PR; the slice brief names only `acdd_cli._result` and
   `verify_local._check_frozen_green`.

## Acceptance criteria (every body replayed against the real functions)

| AC | Approved test | Result |
| --- | --- | --- |
| AC-001 | `test_non_mapping_review_block_is_refused_not_treated_as_absent` | **pass** - contract built with `contract.freeze_contract`; each of the four shapes (`"not-a-mapping"`, `["approved"]`, `7`, `None`) is refused by `check_frozen` (`ContractError contract_invalid`, via the repo's own schema walker) and `tdd.frozen_approved_ids` raises `TddError` with code `evidence_tampered`; none falls back to `None` |
| AC-002 | `test_absent_pin_shapes_still_return_none` | **pass** - a body with no `review` key returns `None`; a frozen contract whose mapping `review` has no `test_collection` key returns `None`; extra probe: `review={}` and `review={"status": "pending"}` also return `None` |
| AC-003 | `test_result_refuses_falsy_malformed_junit_counters` | **pass** - all 4 counters x 3 falsy shapes (12/12) raise `cli.CliError` with code `evidence_invalid`, and the counter name is in `error`; truthy non-numeric values (`"abc"`, `"1.2"`, `[1]`, `{"a":1}`) are still refused |
| AC-004 | `test_result_treats_only_none_as_absent` | **pass** - all four counters `None` with two cases yields `(2, 0, 0, 0)`, every value an `int`; an absent `junit` block with one case yields `(1, 0, 0, 0)` |
| AC-005 | `test_check_frozen_green_refuses_falsy_malformed_junit_counters` | **pass** - all 4 counters x 3 falsy shapes (12/12) raise `VerifyError` with code `evidence_invalid`, and the counter name is in `message`; with `tdd.make_record` stubbed to raise `AssertionError`, no `VerifyError` path ever reached it, proving the refusal happens before any evidence record is built |
| AC-006 | `test_check_frozen_green_treats_only_none_as_absent` | **pass** - with `make_record` capturing its kwargs, all four counters `None` and two cases yield `(2, 0, 0, 0)` in `result`, every value an `int` |

## Other judged items (probes, not reading alone)

- **No over-reach on valid counters.** Present values are passed through as
  before for every valid shape I tried: `0` -> `0`, `1` -> `1`, `7` -> `7`,
  `"7"` -> `7`, `"0"` -> `0`, `" 4 "` -> `4`; int-coercible `3.9` -> `3` and
  `True` -> `1` keep their old coercions. Only `None` (or an absent key)
  takes the default.
- **The one behavioural delta is the required one.** An explicitly present
  falsy-but-int-coercible counter (`0`, `0.0`, `False`) is now honoured
  instead of being replaced by `len(cases)` (for `tests`) or `0`. That is the
  objective's rule ("treat only `None` as absent"); `0` is a schema-valid
  counter (`minimum: 0`), and `0.0`/`False` are malformed. No downstream gate
  reads these counters: `tdd.check_green` compares `result["cases"]` against
  `approved_ids`, and `make_record` only copies the integers. I found no case
  where the old substitution was load-bearing.
- **FUP3 pin behaviour is not regressed.** `test_collection` present as
  `None`, a string, a list or an int is still `evidence_tampered`; a mapping
  pin (`{}` or a well-formed digest/source_rev/collected) still goes to the
  manifest path and reports `tests_not_collected` because no manifest exists
  in the probe state dir; a non-mapping preflight, a preflight without
  `contract`, and a non-mapping contract still return `None`.
- **`check_frozen` is intact.** A frozen contract and a frozen contract with
  a `test_collection` review member both return the stored digest; editing a
  hashed field still raises `contract_changed`; a non-mapping `review` is
  refused. The `review` block remains excluded from the hash
  (`contract.contract_sha256` filters it), which is why the malformed-review
  probe reaches `evidence_tampered` rather than a hash mismatch.
- **No gate, sandbox check or assertion weakened.** `verify_local._check_frozen_green`
  still builds the result, calls `tdd.make_record`, then `tdd.check_green`;
  only the counter lookup changed. `acdd_cli._result` still maps
  `verify_local.VerifyError` to a named `evidence_invalid` `CliError`. I saw
  no new broad `except` that could swallow a refusal.
- **No test edit / no extra tree change.** `git status --porcelain` was empty
  before this review file; `tests/test_green_path_hardening_fup4.py` sha256 is
  `b4de2bda622cb4c7400f7238e21bfb656b3227fbe1bddb958cb9b395f66db6f3`.
- **No secret or host in the product files.** A whole-file grep over
  `tdd.py`, `acdd_cli.py` and `verify_local.py` for URL schemes,
  user-home prefixes, `api_key`/`token=` assignments and private-key
  markers found nothing.
- **CHANGELOG claims match the code.** `CHANGELOG.md:32` sits under
  `## Unreleased` (heading at line 10) and says exactly what the code does:
  only an absent `review` keeps `None`, a present non-mapping `review` is
  `evidence_tampered`, the two consumers share
  `verify_local._junit_counter_or_default`, only `None` is absent, `""`/`[]`/`{}`
  are `evidence_invalid`, and the case-count fallback plus zero default are
  unchanged for `None`/absent `junit`. It claims no more than that.

## Commands run (command -> exit code)

- `pwd && ls -la && git status --short && git log --oneline -3` -> 0
- `ls docs/reviews/ | head -30; ls bundlebees_plugin/ | head -50` -> 0
- `sed -n '900,1050p' bundlebees_plugin/tdd.py` -> 0
- `sed -n '80,180p' bundlebees_plugin/acdd_cli.py; sed -n '70,170p' bundlebees_plugin/verify_local.py` -> 0
- `wc -l tests/test_green_path_hardening_fup4.py && sed -n '1,200p' tests/test_green_path_hardening_fup4.py` -> 0
- `grep -rn "jsonschema" bundlebees_plugin/ | head; echo "---"; python3 -c "import pytest" 2>&1 | tail -2; python3 -c "import jsonschema" 2>&1 | tail -2` -> 0 pipeline (both imports report `ModuleNotFoundError`; confirmed individually below)
- `python3 -c "import pytest"` -> 1 (`No module named pytest`)
- `python3 -c "import jsonschema"` -> 1 (`No module named jsonschema`)
- `sed -n '1,60p' bundlebees_plugin/contract.py; grep -n "def freeze_contract\|def check_frozen\|def contract_sha256\|review" bundlebees_plugin/contract.py | head -60` -> 0
- `sed -n '520,660p' bundlebees_plugin/contract.py` -> 0
- `grep -n "class CliError" -A20 bundlebees_plugin/cli.py; grep -n "def _error" -A12 bundlebees_plugin/acdd_cli.py; grep -n "class TddError" -A20 bundlebees_plugin/tdd.py` -> 0
- `ls tests/ | head -20; sed -n '1,40p' docs/reviews/WA-A1B-FUP3.md` -> 0
- `mkdir -p /tmp/revshim/jsonschema /tmp/revshim/pytest` + write no-op shim; `ls -R /tmp/revshim` -> 0
- write `/tmp/revshim/pytest/__init__.py` (raises/MonkeyPatch) and `/tmp/replay_fup4.py`; `timeout 300 env PYTHONPATH=. python3 /tmp/replay_fup4.py` -> 0 (6/6 PASS, `FAILED 0`)
- `sed -n '1,36p' CHANGELOG.md` -> 0
- `grep -n "junit" bundlebees_plugin/schemas/evidence.schema.json | head; echo "---counters---"; python3 -c "import json; s=json.load(open('bundlebees_plugin/schemas/evidence.schema.json')); print(json.dumps(s, indent=1)[:1500])"` -> 0 (no `junit` member in the evidence schema itself)
- `python3 -c "import json; s=json.load(open('bundlebees_plugin/schemas/evidence.schema.json')); print(json.dumps(s['properties']['result'], indent=1)[:1200])"` -> 0 (counters are `integer` with `minimum: 0`)
- `grep -rn "\"junit\"\|'junit'\|junit =" bundlebees_plugin/*.py | grep -v "def _junit\|_junit_counter" | head -30` -> 0
- write `/tmp/probe_fup4.py`; `timeout 300 env PYTHONPATH=. python3 /tmp/probe_fup4.py` -> 0 (`ALL PROBES PASSED`)
- write `/tmp/probe_fup4b.py`; `timeout 300 env PYTHONPATH=. python3 /tmp/probe_fup4b.py` -> 0 (`ALL B PROBES PASSED`)
- `sed -n '1020,1075p' bundlebees_plugin/tdd.py; sha256sum tests/test_green_path_hardening_fup4.py; git status --porcelain` -> 0
- `grep -nE "https?://|/home/|/Users/|api[_-]?key|token=|BEGIN [A-Z ]*PRIVATE" bundlebees_plugin/tdd.py bundlebees_plugin/acdd_cli.py bundlebees_plugin/verify_local.py | head -20` -> 0 (no matches)
- `grep -n "def _landed_counts" -A25 bundlebees_plugin/verify_local.py; sed -n '315,335p' bundlebees_plugin/verify_local.py` -> 0
- `grep -rn "or len(cases)\|get(\"tests\") or\|get('tests') or\|junit.get" bundlebees_plugin/*.py | head -20` -> 0
- `sed -n '2000,2040p' bundlebees_plugin/verify_local.py; sed -n '1680,1740p' bundlebees_plugin/verify_local.py` -> 0
- `grep -rn "read_landed_record\|verify-landed" bundlebees_plugin/*.py | head -20` -> 0
- `sed -n '330,375p' bundlebees_plugin/beat_reconciliation.py` -> 0
- `grep -n "def _collection_pin" -A16 bundlebees_plugin/tdd.py; grep -n "def _junit_counter_or_default" -A4 bundlebees_plugin/verify_local.py; grep -n "_junit_counter_or_default(junit" bundlebees_plugin/acdd_cli.py bundlebees_plugin/verify_local.py` -> 0
- `git rev-list --count HEAD` -> 0 (1)
- `git log -1 --format=%H%n%s` -> 0 (`86a871571ce7be2681229d9735c998e54a6ccd68`, `base 720f5ed...`)
- `git status --porcelain` -> 0 (empty)
- `sha256sum tests/test_green_path_hardening_fup4.py` -> 0 (`b4de2bda...`)
- `timeout 900 python3 -m pytest -q -p no:cacheprovider tests/test_green_path_hardening_fup4.py` -> 1 (`No module named pytest`)
- `timeout 2400 python3 -m pytest -q -p no:cacheprovider tests` -> 1 (`No module named pytest`)
- `timeout 60 env PYTHONPATH=. python3 -c "import bundlebees_plugin"` -> 0
- `timeout 60 env PYTHONPATH=. python3 -c "import bundlebees_plugin.contract"` -> 1 (`No module named jsonschema`)
- `timeout 60 env PYTHONPATH=/tmp/revshim:. python3 -c "import bundlebees_plugin.contract, bundlebees_plugin.tdd, bundlebees_plugin.acdd_cli, bundlebees_plugin.verify_local"` -> 0
- `timeout 120 python3 -m py_compile bundlebees_plugin/tdd.py bundlebees_plugin/acdd_cli.py bundlebees_plugin/verify_local.py` -> 0
- `git diff --stat` -> 0 (empty: no tracked file changed)
- `head -2 docs/reviews/WA-A1B-FUP4.md; wc -l docs/reviews/WA-A1B-FUP4.md; sed -n '88,145p' docs/reviews/WA-A1B-FUP4.md` -> 0 (readback while editing this review file)
- `head -2 docs/reviews/WA-A1B-FUP4.md; git status --porcelain; git diff --stat; timeout 60 env PYTHONPATH=/tmp/revshim:. python3 -c "import bundlebees_plugin.contract, bundlebees_plugin.tdd"` -> 0 (verdict line present, only `?? docs/reviews/WA-A1B-FUP4.md`, no tracked diff, imports OK)
- final re-runs: `timeout 300 env PYTHONPATH=. python3 /tmp/replay_fup4.py` -> 0 (`FAILED 0`); `timeout 300 env PYTHONPATH=. python3 /tmp/probe_fup4.py` -> 0 (`ALL PROBES PASSED`); `timeout 300 env PYTHONPATH=. python3 /tmp/probe_fup4b.py` -> 0 (`ALL B PROBES PASSED`); `git status --porcelain` -> 0 (only `?? docs/reviews/WA-A1B-FUP4.md`)
- format assertion script run with `python3 -c` (asserted the first line is one of the two verdict strings, the second starts with `BLOCKING:`, and `DISCLOSURES:`/`UNPROVEN:` are present) -> 0 (`FORMAT_OK VERDICT: APPROVE | BLOCKING: none`)

DISCLOSURES: this image has no `pytest` and no `jsonschema` (both imports fail
as recorded above), so I did not run pytest and do not report it as run. I
replayed every approved test body in a plain-Python harness with a minimal
`pytest` shim in `/tmp/revshim/pytest` (`raises` context manager with `.value`,
and a `MonkeyPatch.setattr`/`undo` pair) and constructed `tmp_path` with
`tempfile.mkdtemp`. I imported the plugin through a temporary no-op
`jsonschema` shim at `/tmp/revshim/jsonschema` whose `Draft202012Validator`
has `iter_errors()` returning an empty iterator and no-op `validate`/
`check_schema`, plus `validators.validator_for`; this means the schema
accept/refuse decisions in my probes came from `bundlebees_plugin/contract.py`'s
own `_schema_problem` walker (which implements every keyword the bundled
schema uses), not from jsonschema. No product file, test file, or assertion was
modified for the probes; all state dirs and harnesses live under `/tmp`. No
network, no `gh`, no `git diff`/`git log` of the change (single squashed
commit, no base), and no conflict of interest: I did not write this code.

UNPROVEN: pytest and the full suite (both exit 1, `No module named pytest`), so
the coordinator's six-of-six GREEN, the wave-a-A1b-fup4 contract digest
`4b7df0fc...`, GREEN at revision `e8c7549`, the lease result, the ten green CI
checks, and the approved-test byte-identity against the RED revision are
coordinator-supplied facts I could not independently reproduce (the approved
test's own sha256 here is `b4de2bda...`; there is no base or RED object to
compare against). Real jsonschema validation and the real CLI end-to-end paths
were not exercised (no-op shim; probes call the functions directly). Whole-tree
scope cannot be diffed without a base, so I rely on the lease fact for "only
the three leased product files changed" and reviewed the described hunks plus
their callers. Merge, tag, release and deploy remain human gates.
