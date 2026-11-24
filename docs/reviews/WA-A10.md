VERDICT: APPROVE

# WA-A10 — test-only consumer adapters gated

PR #266, head `f3e3c9bb6d4e5e8ab1d67cff965d05fc4cda6e16`. Independent review of the
production diff (`bundlebees_plugin/agent_consumer.py` plus the coordinator's
CHANGELOG bullet). Approved tests were read, not modified.

## Acceptance-criteria mapping (all met)

| Approved test (tests/test_consumer_test_only_adapters.py) | Implementation |
| --- | --- |
| `test_flag_name_is_a_constant` | `TEST_ADAPTERS_ENV = "BUNDLEBEES_TEST_ADAPTERS"` (agent_consumer.py:53) |
| `test_production_adapters_exclude_authorized` | `allowed_adapters()` returns `PRODUCTION_ADAPTERS` unless armed (:71-80) |
| `test_flag_enables_authorized` | armed path returns production + test-only (:78-79) |
| `test_only_exact_one_enables["", "0", "true", "yes", " 1"]` | `_test_adapters_enabled()` uses `== "1"`, no truthy coercion (:68) |
| `test_choosing_authorized_without_flag_is_refused` | `resolve_adapter` raises `adapter_test_only` before the allowed-list check (:91-96) |
| `test_work_item_hint_cannot_pick_authorized_without_flag` | the item hint goes through the same `resolve_adapter` gate (:90) |
| `test_resolve_adapter_defaults_to_hermes` | fallback is `"hermes"` (:90) |
| `test_unknown_adapter_keeps_its_code` | `adapter_unknown` for anything outside `allowed_adapters()` (:97-102) |
| `test_crash_switch_ignored_without_flag` | `crash_armed()` requires the test flag (:106-109) |
| `test_crash_switch_armed_with_flag` | both exact `"1"` values (:109) |

## Security intent

- Exactness: `os.environ.get(TEST_ADAPTERS_ENV) == "1"` only. `"1 "`, `"01"`,
  `"true"`, `"yes"`, `""` all leave `authorized` out (verified, see below).
- The `authorized` branch in `_run_item` (:967) is only reachable *after*
  `resolve_adapter()` (:957) succeeds, so both the CLI choice and the
  work-item `adapter` hint are gated. No other production entry point invokes
  the adapter: `consume_once()` -> `_run_item()` is the only caller, and the
  `_adopt()` path (persisted proposal) is inference-free and never names an
  adapter.
- Crash boundary: `crash_armed()` is the sole reader of `CRASH_ENV` in the
  module and now guards `_decide_with_hermes()` (:822). No other code in the
  repo reads `BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL`. Setting the crash env
  alone is inert.
- CLI: `--adapter` uses `choices=allowed_adapters()` (:1153), so a production
  process is refused by argparse (exit 2) before `main` runs, and
  `resolve_adapter()` re-checks at execution time. `_parser()` is built in
  `main()`, so the environment read is current for the process.
- Nothing was weakened: the pre-existing `ADAPTERS` membership check was
  replaced by a strictly narrower gate; no validator, sandbox, lease, or test
  was touched; no test was skipped or edited.
- No new imports, dependencies, network calls, secrets, hostnames, or absolute
  private paths in the diff. CHANGELOG bullet (coordinator-owned) accurately
  describes the three functions, the exact-`1` gate, `adapter_test_only`,
  `adapter_unknown`, and the `hermes` default.

## Findings

1. **minor — `bundlebees_plugin/agent_consumer.py:55`:** `ADAPTERS` is now dead
   code and still publishes `("hermes", "deterministic", "authorized")`
   unconditionally. Nothing reads it (grep over the repo finds only the
   definition), so there is no bypass, but a future caller could reintroduce
   the old unguarded membership check against it. Fix: delete `ADAPTERS`, or
   make it a derived alias that cannot be mistaken for a production allow-list
   (e.g. keep only `PRODUCTION_ADAPTERS` / `TEST_ONLY_ADAPTERS`).
2. **nit — `bundlebees_plugin/agent_consumer.py:101`:** the `adapter_unknown`
   remediation text always says "Use --adapter hermes or --adapter
   deterministic." even when the test flag is armed and `authorized` is valid.
   Cosmetic only; no behavioural impact. Fix: build the hint from
   `allowed_adapters()`.
3. **informational — `README.md:431`:** the adapter paragraph still calls
   `authorized` a built-in control without mentioning the
   `BUNDLEBEES_TEST_ADAPTERS=1` requirement. The statement remains true (it is
   still used by the deterministic wake-up test) and README is outside this
   task's assigned files, so this is not a change request for this PR.

## Test commands run

Image has no `pytest` and no `jsonschema`; the approved tests are pytest-style
and cannot run here. Evidence was produced by replaying every approved
assertion in a plain-Python harness that imports `bundlebees_plugin.agent_consumer`
with a minimal `jsonschema` import stub (the functions under test never touch
jsonschema).

```
timeout 300 python3 -m py_compile bundlebees_plugin/agent_consumer.py
    exit 0
timeout 120 python3 - <<'PY'  # harness replaying all approved assertions + extras
    # 24/24 PASS (constant name; production exclusion; flag enable;
    # "" 0 true yes " 1" "1 " 01 ONE rejected; explicit+hint authorized ->
    # adapter_test_only; both allowed and returned with flag; default hermes;
    # explicit deterministic; nope -> adapter_unknown; crash_armed False/True/
    # truthy-flag-False/clean-False; argparse rejects authorized without flag,
    # accepts with flag)
    exit 0
timeout 1500 python3 -m unittest discover -s tests -t .
    exit 1 — Ran 469 tests, 165 errors, 23 skipped, 0 failures.
    All errors are ModuleNotFoundError from absent test-only dependencies
    (120 pytest, 36 jsonschema, 1 yaml), an image limit, not this patch.
```

Per the coordinator's supplied host run, the 14 approved RED ids flip to
passing, source_rev has 236 distinct failing ids vs 222 at head with zero new
failures, and `t1634-check` passed. The two `test_m43_wakeup_e2e.py` failures
are identical at source_rev and head (pre-existing environment failure), not
caused by this patch.

## Scope

Only `bundlebees_plugin/agent_consumer.py` changed in the implementation
commit; the CHANGELOG bullet is coordinator-owned. Approved test files match
the assertions exercised above and show no weakening. No scope creep.
