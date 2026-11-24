VERDICT: APPROVE

# Review: WA-A11 preflight honours `require_contract` (PR #264, head 1cb2803)

Reviewed the three changed files themselves (`bundlebees_plugin/preflight.py`,
`bundlebees_plugin/cli.py`, `CHANGELOG.md`), the approved read-only test file,
and the live dispatch helpers the patch must mirror (`tools._require_contract`,
`tools._gate`, `tools._dispatch_acdd`, `security.config_flag`,
`settings.settings_from_ctx`).

The patch does close the stated gap: a registered `preflight` run now resolves
the same `require_contract` setting the dispatch tool resolves, and a
contract-less brief is refused with the same code `contract_missing` (exit 3,
`ok:false`) instead of `ok:true`. The gate itself is still `tools._gate`, so no
contract rule was copied or relaxed. No gate, sandbox, check or test was
weakened, skipped or made optional; the approved test is unchanged and the diff
touches only the three permitted files.

## Findings

1. **minor** - `bundlebees_plugin/cli.py:734` (branch at 729-736) re-implements
   the setting resolution with `config_flag(_config(ctx).get("require_contract"),
   False)` instead of reusing `tools._require_contract(ctx)`. It is exactly
   equivalent today: `cli._config` (`cli.py:1283-1288`) and `tools._ctx_config`
   (`tools.py:123-126`) both call `settings.settings_from_ctx`
   (`settings.py:125-137`), and both feed it to the same
   `security.config_flag` (`security.py:528-541`). All argument forms therefore
   agree with dispatch: `True`/`False` -> same; `"1"`/`"true"`/`"yes"`/`"on"`
   -> true; `"0"`/`"false"`/`"no"`/`"off"`/`""` -> false; `None` is replaced by
   the `False` default in `settings_from_ctx`; an int is `bool(int)`; any other
   value (including a dict) falls back to the `False` default. The residual risk
   is maintenance: if `tools._require_contract` ever gains scoped/dict handling,
   this copy would silently drift and recreate exactly the disagreement this PR
   fixes. Fix: lazily `from .tools import _require_contract` in this branch and
   use `if _require_contract(ctx): argv.append("--require-contract")`. Do not
   substitute `settings.require_contract`, which honours the scoped mapping and
   would then make preflight *stricter* than the live dispatch path for a dict
   value.

2. **minor (pre-existing, informational; no change required in this PR)** - a
   dict-shaped (scoped) `require_contract` value is resolved to `False` by both
   paths: `config_flag` stringifies the mapping, finds no token in either set,
   and returns the `False` default (`security.py:536-541`). `tools._require_contract`
   (`tools.py:128-130`) does the same, so preflight and dispatch cannot disagree
   on this form - but `docs/contract-enforcement.md:44-70` and
   `settings.require_contract` (`settings.py:56-90`) document a campaign/
   repository mapping that the live dispatch path never consults. This is
   inherited from dispatch, not introduced by #264; the correct follow-up is to
   teach `tools._require_contract` the scoped resolution (with campaign/
   repository context) and then call that helper from `cli.py` per finding 1,
   keeping one source of truth.

## Coordinator flags

- **(a) re-implementation vs `tools._require_contract`:** acceptable risk for
  this patch, recorded as minor finding 1. Behaviourally identical today and
  covered by the approved registered-CLI test; the one-line import removes the
  drift hazard and is worth doing while the branch is open.
- **(b) keyword-only `require_contract` in `preflight()`:** correct failure
  mode. Every existing call site passes at most `(brief, state_dir)` (checked in
  `tests/test_preflight.py` and the module itself); a future third positional
  argument fails loudly with `TypeError` instead of silently keeping the
  permissive default. There is no new silent fallback on the registered path,
  which always forwards the resolved setting.
- **(c) delegation to `tools._gate`:** confirmed. `preflight()` does its own
  only pre-existing `_declares_tier` check and then calls
  `tools._gate({"brief": parsed}, state_dir, require_contract=...)`
  (`preflight.py:84-86`); `tools._gate` preserves the high-tier forced
  `require_contract = True` (`tools.py:625`) and calls the same
  `_dispatch_acdd` (`tools.py:640`). Contract rules cannot drift between
  preflight and dispatch because there is only one implementation.

## Acceptance criteria

- Refusal parity: `preflight(..., require_contract=True)` forwards the flag to
  `_gate`, which returns the `contract_missing` envelope; `preflight` maps its
  `code` unchanged (`preflight.py:87-94`). `test_require_contract_refuses_like_dispatch`
  compares that code with `tools._gate` directly, and `test_default_is_unchanged`
  proves the same brief is otherwise admissible, so the refusal is not reachable
  only through a happy-path side effect.
- Flag plumbing: `--require-contract` is `action="store_true"` and only adds a
  requirement (`preflight.py:113-116`); it cannot lower the tier floor, skip the
  contract checks, or be turned into a false. It is forwarded to `preflight()`
  (`preflight.py:132-134`). A one-off stricter run is possible via the module
  CLI; the registered subcommand (`cli.py:485-491`) does not declare the flag
  and is driven solely by the durable setting, which is consistent with the
  task and cannot weaken anything.
- `CHANGELOG.md:13` matches the code: keyword-only parameter, module flag, same
  `tools._gate`, registered command reading `config_flag(settings_from_ctx)`
  like `tools._require_contract`. No absolute paths, hosts, IPs, tokens or
  secrets were added anywhere in the diff.
- Scope: only `bundlebees_plugin/preflight.py`, `bundlebees_plugin/cli.py` and
  `CHANGELOG.md` differ from the single squashed head; `tests/test_preflight_require_contract.py`
  is byte-for-byte the approved RED file pasted in the brief and was not edited.

## Commands run

| command | exit | result |
| --- | --- | --- |
| `timeout 300 python3 -m compileall -q bundlebees_plugin` | 0 | all modules compile; no syntax/import-time error in the changed files |
| `python3 -c "import pytest"` | 1 | `ModuleNotFoundError: No module named 'pytest'` - pytest is absent in this image, so the four approved tests could not be executed here; this is environmental, not a finding. Behaviour above was judged by reading the code and the coordinator's supplied GREEN/RED evidence. |
| `git status --porcelain` | 0 | clean before the review file was written (single squashed commit; no uncommitted patch files in the checkout) |
