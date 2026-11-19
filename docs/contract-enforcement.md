# Contract enforcement (`require_contract`)

The reliability campaign's F2 machinery: a project, campaign or execution
profile can require every dispatch to carry a valid, frozen `acdd/1` contract.
The enforcement lives in `bundlebees_plugin/contracts.py` (validation and
refusals) and `bundlebees_plugin/contract_gate.py` (the dispatch gate); the setting
is resolved through `bundlebees_plugin/settings.py`. The gate module is
`contract_gate.py` — `dispatch.py` in the same package is the unrelated
lease-payload server and plays no part in enforcement.

## Live path: report-only

The live dispatch path (`tools._dispatch_acdd`) still enforces the frozen-hash
and brief-vs-contract checks unchanged. Its provenance and acceptance-id checks
run in **report-only** mode: the `acdd` block gains
`gate={mode: report_only, would_refuse, acceptance_ids, provenance}` and the
findings are appended to `<state>/contract-gate/report-only.jsonl`. Nothing is
refused by them. There is no setting for this: the mode is the constant
`CONTRACT_GATE_MODE = "report_only"` in `bundlebees_plugin/tools.py`, so
turning enforcement on is a deliberate, reviewed code change. Findings rotate to
`report-only.jsonl.1` once the log passes 1 MiB. The pinned workflow HEAD is
cached per process for 30 s and git is given 5 s, so dispatch never waits on a
slow checkout; an unresolvable checkout is still `provenance_unavailable`.

`contract_gate.gate()` and `contract_gate.dispatch()` are **not** called by the
live path. `python3 -m bundlebees_plugin.contract_gate check BRIEF.json` is a
dry-run operator tool: it reports what enforcement would decide.

## Where the flag lives

`require_contract` is **durable configuration**, not session state. It is read
from the plugin settings at the campaign/repository/execution-profile
boundary:

- the running execution profile's plugin settings
  (`plugins.entries.bundlebees.settings.require_contract`), read through
  `ctx.get_config`, or
- the repository's Hermes profile file, read through
  `settings.settings_from_hermes_config()`.

The gate re-reads the configuration on every dispatch — nothing is cached in
memory, so a restart or a recovery re-derives the same decision from the same
profile. A brief carries no vote: a `contract` field is the work contract
itself, and no field of the brief can switch enforcement off.

The flag is a plain boolean, or a mapping that scopes it per campaign or
repository. An explicit campaign or repository value wins over the default —
including an explicit `false`, which opts that one scope out; a scope that is
absent, `null` or unparsable does not vote and the default decides, and an
unparsable or absent default is off. The campaign table is consulted before
the repository table:

```json
{
  "require_contract": {
    "default": false,
    "campaigns": {"reliability": true},
    "repositories": {"mojomast/bundlebees": true}
  }
}
```

## Disabled by default; the M2 canary profile

Off is the shipped default (`settings.DEFAULTS`, `plugin.yaml`, README).
Enabling it is always an explicit opt-in in configuration; legacy briefs keep
working and every contract-free dispatch is *labelled* legacy
(`mode: legacy`, `note: "no ACDD contract; TDD evidence not required"`) —
never silently treated as contract-bound.

The one documented exception is the **M2 canary profile**: the execution
profile used for the M2 canary run ships with `require_contract: true` in its
plugin settings. That is still an explicit, reviewed opt-in — it is the
profile's committed configuration, not a code default — and it exists so the
enforcement path is exercised continuously on real work before any wider
rollout. Any other profile stays legacy until its owner sets the flag.

## The dispatch gate

`contract_gate.gate(brief, config=...)` returns the `acdd` envelope block, or
raises `ContractRefusal`. `contract_gate.dispatch(brief, config=..., send=...)`
refuses first and only then performs the send, so a refused brief never reaches
the launcher. Neither is called by the live dispatch path (see above); this
section describes the dry-run and future-enforcement tool. The CLI dry run is
`python3 -m bundlebees_plugin.contract_gate check BRIEF.json` (exit 0 admitted,
exit 1 refused, the refusal JSON on stdout; an unreadable brief file refuses
with `brief_invalid` instead of a traceback).

When the flag is on and the brief has no contract, the refusal is precisely:

```json
{"ok": false, "code": "contract_required",
 "error": "no frozen acdd/1 contract is attached to this brief and require_contract is enabled for this scope",
 "fix": "Attach a frozen acdd/1 contract under brief.contract; ..."}
```

All refusal text is scrubbed the same way tool envelopes are.

## Validation before dispatch

An attached contract is checked, in order, by the gate's dry run:

| Refusal code | Meaning | Fix |
|---|---|---|
| `contract_required` | The scope requires a contract; none is attached | Scaffold, review and freeze one (`contract freeze`) |
| `contract_invalid` | Schema or code-check failure (field named) | Fix the named field |
| `contract_not_frozen` | No approved review block | Freeze it with `contract freeze` |
| `contract_changed` | Content no longer matches `review.contract_sha256` — a tampered contract, or a feature packet sealed to a different contract | Revert the edit, or re-review and re-freeze |
| `stale_revision` | `brief.source_rev` differs from `contract.source_rev` | Re-freeze for the brief's revision (or dispatch at the contract's) |
| `scope_violation` | The brief's lease reaches outside `scope.assigned_files` — a widened lease | Dispatch only the contract's assigned files |
| `tests_tampered` | The brief assigns an approved test file | Keep the implementer's lease disjoint from `scope.test_files` |
| `provenance_unavailable` | The pinned workflow checkout is missing or unresolvable | Fix `WORKFLOW_SRC`; the gate fails closed instead of guessing |
| `provenance_mismatch` | The packet's `workflow_ref` is not the pinned checkout's `HEAD` | Re-pin the packet at the pinned revision |
| `provenance_invalid` | The packet names no `workflow_ref` (or is not an object) | Set the packet's pin before dispatch |
| `acceptance_mismatch` | Acceptance ids disagree across artifacts | Re-issue the artifact from the frozen contract |

Two checks deserve their own paragraphs:

- **Provenance.** The contract must name a known pinned protocol version
  (`acdd/1`; unknown versions are refused, not guessed). When a feature packet
  accompanies the brief, its `workflow_ref` must equal the pinned workflow
  checkout's `HEAD` exactly — the same pin `job` creation seals — and a packet
  whose `contract_sha256` (or embedded contract body) belongs to a different
  contract is refused as `contract_changed`. Unresolvable provenance fails
  closed in the dry run; it never passes on a maybe. On the live path these
  provenance findings are report-only.
- **Acceptance-id consistency.** Every artifact that names acceptance ids —
  the packet's `acceptance_ids`, the brief's acceptance criteria, and any
  verification evidence records' `criteria` — must draw only on ids the frozen
  contract defines, and the packet must carry the contract's complete id set.
  An invented id would bind evidence to nothing; a dropped one would leave a
  criterion silently unjudged. On the live path this check is report-only too;
  it is enforced only in the dry run.

Lease and scope checks speak in **exact file paths, never directories**: a
lease entry like `docs/` is not a lease, and scope verification requires the
same exact-path discipline the dispatching brief used.

## The scaffold command

```bash
python3 -m bundlebees_plugin.contracts scaffold \
  --id W1-pricing \
  --title "Add apply_tax" \
  --summary "apply_tax adds ten percent" \
  --behavior "apply_tax(1000, 10) == 1100" \
  --test tests/test_pricing.py::test_apply_tax \
  --assigned shop/pricing.py \
  --test-file tests/test_pricing.py \
  --source-rev 40-or-64-hex-commit \
  --out ./acdd
```

It writes `acdd/contract.json` and `acdd/packet.json` (create-only; it never
overwrites: a rerun into a used directory refuses with `scaffold_exists`).
The lease entries the operator passes are exact file paths, never directories —
a directory-style entry like `shop/` is refused with `contract_invalid`, the
same code a contract that fails validation carries. A scaffold that cannot
produce a valid contract — no `--test`, or an unusable `--out` — refuses with
`scaffold_invalid`; every scaffold refusal reaches the caller as the same
machine-readable envelope, never a traceback. The contract is **valid as
generated** — it passes
`contract validate` and can be frozen after review. The packet is a
**skeleton** for the pinned `rizzstack.feature-packet/1` protocol: its
`workflow_ref` seals the pinned workflow commit from
`bundlebees_plugin/pins.py`, the same pin the doctor's `pinned_workflow` check
uses; the authorization and budget blocks are placeholders. Before admission,
the operator must freeze the contract, seal
`review.contract_sha256` into the packet, and set `workflow_ref` to the pinned
checkout's `HEAD`. One acceptance criterion (`AC-001`, `AC-002`, ...) is
generated per `--test`; `--statement` overrides the derived statements in the
same order.

## Compatibility with the legacy gate

`tools.py`'s dispatch path keeps its own, older gate: a brief *with* a
contract is always enforced there (`contract_invalid`, `contract_not_frozen`,
`contract_changed`, `stale_revision`, `scope_violation`, `tests_tampered`),
and `require_contract: true` there refuses contract-free briefs with
`contract_missing`. This module's enforced-scope refusal is `contract_required`
— same semantics (no contract in an enforced scope), distinct code, so
evidence can tell the two layers apart. In the live path only `tools.py`'s gate
runs; `contract_gate` is the dry-run/operator tool (see "Live path:
report-only"), so its refusals are not applied there.
