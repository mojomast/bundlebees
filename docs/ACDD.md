# ACDD and TDD in bundlebees

> This is bundlebees' copy of the protocol in [workflow chapter 16](https://github.com/mojomast/workflow/blob/main/docs/16-acdd-tdd.md), regenerated from it. `bundlebees_plugin/schemas/contract.schema.json` and `evidence.schema.json` are byte-identical copies of the workflow repository's `schemas/` files.

**Status (Nov 12, 2026):** the protocol is defined here and enforced in the [reference demo](https://github.com/mojomast/workflow/blob/main/reference/README.md) (workflow repository). In [bundlebees](https://github.com/mojomast/bundlebees) it is enforced at dispatch, verify and integrate on the `feat/acdd-tdd` branch, which is under review and not merged or released. Orbit's read-only evidence view is built on an orbitdesktop branch, also under review, but it is not yet wired into the board; see [§9](#9-orbit). Nothing in this chapter changes who may merge, tag, release, deploy or publish.

The unit of progress is still an accepted artifact with evidence ([12](https://github.com/mojomast/workflow/blob/main/docs/12-three-layer-architecture.md)). This chapter makes two parts of that sentence machine-checkable:

- **ACDD (Agentic Contract-Driven Development)** says what correct work means *before* a worker starts: a contract with observable behavior, scope, acceptance criteria with ids, and the evidence that has to come back.
- **TDD (test-driven development)** shows that the required behavior was actually missing and is now implemented: RED against the pinned baseline, GREEN against the implementation, REFACTOR without changing the approved tests.

Neither replaces integration tests, security review, independent verification or the human gates.

## 1. Lifecycle

```text
USER OBJECTIVE
  -> RESEARCH / ARCHITECTURE
  -> ACDD CONTRACT            outcome, interfaces, scope, AC-ids, test requirements
  -> CONTRACT REVIEW          validate, resolve dependencies, freeze (contract_sha256)
  -> TEST-FIRST PREPARATION   write approved tests, run on baseline + tests only,
                              classify RED, record test fingerprints
  -> PARALLEL WORKER DISPATCH non-overlapping leases; approved tests are read-only
  -> GREEN                    verifier reruns the same tests on the implementation
  -> REFACTOR                 optional; same tests, same fingerprints, still green
  -> INDEPENDENT VERIFICATION scope, revision, sandboxed tests; worker claims ignored
  -> INTEGRATION              combined branch, full suite
  -> HUMAN ACCEPTANCE         Kyle's own yes for merge / tag / release / deploy / publish
```

Each arrow either produces an evidence record or stops with a named failure state ([§7](#7-failure-states)).

## 2. The contract (`acdd/1`)

Machine-readable form: [`schemas/acdd-contract.schema.json`](https://github.com/mojomast/workflow/blob/main/bundlebees_plugin/schemas/contract.schema.json). Human-readable form: [`templates/CONTRACT.md`](https://github.com/mojomast/workflow/blob/main/templates/CONTRACT.md). A worked example: [`templates/contract.example.json`](https://github.com/mojomast/workflow/blob/main/templates/contract.example.json).

| Field | Meaning |
|---|---|
| `id`, `title` | Stable contract id (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) |
| `outcome` | `summary` and a list of `observable_behavior` |
| `interfaces` | Affected names with `kind` and `change` (`add`, `modify`, `remove`, `none`) |
| `acceptance_criteria` | Each has a unique `AC-NNN` id, `given`/`when`/`then` (or a `statement`), a `verification` kind (`test`, `integration`, `review`, `manual`), the `tests` that prove it (`path::name`), and `regression: true` for existing behavior that must keep passing |
| `dependencies`, `assumptions` | Other contract ids or external prerequisites; assumptions that would change the result |
| `scope` | `assigned_files` (the worker's lease), `test_files` (approved tests, read-only for the implementer), `read_only`, `prohibited` (glob patterns) |
| `source_rev` | Full 40- or 64-hex baseline commit |
| `tdd` | `applicable`; a `reason` when it is not; `test_first: false` records honestly that tests came after the code |
| `verification` | `independent: true` is mandatory; optional `commands`, `integration` |
| `evidence_required` | Subset of `red`, `green`, `refactor`, `integration`, `review`, `security` |
| `authorization` | `human_gates` (must include `merge`, `release`, `deploy` and `publish`), plus `authorized` / `not_authorized` notes |
| `review` | `status` (`draft` or `approved`), `approved_by`, `approved_at`, `contract_sha256` |

### Checks beyond the JSON Schema

A validator rejects a contract, with the failing field and a fix, when:

1. two criteria share an id;
2. `tdd.applicable` is true and a `test` criterion lists no tests, or lists a test whose file is not in `scope.test_files`;
3. `tdd.applicable` is true and a file is in both `scope.assigned_files` and `scope.test_files` (the implementer must not be able to edit the tests that judge it);
4. `tdd.applicable` is true and `evidence_required` lacks `green`, or lacks `red` while `tdd.test_first` is not `false`;
5. `tdd.applicable` is false and `tdd.reason` is empty;
6. a path is absolute, contains `..`, is under `.git`, or is assigned and also matches `scope.prohibited`;
7. `authorization.human_gates` drops any of `merge`, `release`, `deploy`, `publish`;
8. `dependencies` names the contract itself.

### Freezing

`contract_sha256` is the SHA-256 of the contract's canonical JSON (keys sorted, separators `,` and `:`, ASCII escapes) **with the `review` object removed**. Review sets `status: approved`, `approved_by`, `approved_at` and that hash. A frozen contract is one whose stored hash matches a recomputation. Any later edit (a widened lease, a relaxed criterion, an easier test list) changes the hash, and every evidence record carries the hash it was produced against, so stale evidence cannot be reused.

### Collected test ids (before freeze and dispatch)

A reference in `acceptance_criteria[].tests` must resolve to a test that actually exists. `contract freeze --workdir REPO` checks out `source_rev` (the approved test revision), runs a bounded `pytest --collect-only` of `scope.test_files` in the verify sandbox (at most 300 s, 20,000 ids), and refuses to freeze unless every reference resolves:

- exactly to a collected id, or to its parametrized (`id[...]`) or class (`path::Class`) children;
- never as a bare file: `tests/test_x.py` alone is refused as too broad (`test_unresolved`).

A test-first file that can't be imported yet (ImportError, ModuleNotFoundError, AttributeError or NameError, because the interface doesn't exist) is read from its syntax tree instead and labelled `static` in the manifest. Any other collection error refuses the contract (`tests_uncollectable`). The result is a `test-collection/1` manifest: the contract hash, `source_rev`, the test-file fingerprints, every collected id and how each reference resolved. It's HMAC-signed with the evidence key and stored under `collections/<contract_id>/<contract_sha256>.json` in the coordinator state dir. The frozen contract records its digest in `review.test_collection`. `bundlebees_dispatch`, `verify` and `tdd red` refuse a contract whose manifest is missing, unsigned, for another hash or revision, or doesn't resolve every reference (`tests_not_collected`, `test_unresolved`, `evidence_tampered`). For a contract frozen without the CLI, `tdd red` collects first. `tdd red` also refuses test files that differ from the collected ones (`tests_tampered`).

## 3. Evidence records (`tdd-evidence/1`)

Schema: [`schemas/tdd-evidence.schema.json`](https://github.com/mojomast/workflow/blob/main/bundlebees_plugin/schemas/evidence.schema.json). One record is one check on one revision:

- `contract_id`, `contract_sha256`, `phase` (`red`, `green`, `refactor`, `integration`, `review`);
- `base_rev` (the contract's `source_rev`) and `revision` (the commit actually tested);
- `test_fingerprints`: SHA-256 of every file in `scope.test_files` as tested;
- `command`, `environment`, `result` (exit code, counts, per-case outcomes with `error_type`);
- `classification` ([§4](#4-classifying-a-run)) and `criteria` (`AC-NNN` → `passed`, `failed`, `not_run`, `review_required`);
- `produced_by` (`verifier`, `worker`, `reviewer`, `human`) and `test_first`;
- `sequence` and `prev_sha256`, set when the record is written: its position in this contract's history (1..N) and the digest of the previous signed record. Both are inside the signature, so records written in the same second keep a provable order, and a removed or reordered record breaks the chain;
- on review records, `attestation` ([§5](#5-red-green-refactor)).

Only records produced by the verifier count for acceptance. A worker may attach its own RED output, but it is labelled `produced_by.role: worker` and never authorizes anything (AC-006). In bundlebees the coordinator signs records with its existing lease key; the reference demo does not sign (teaching only).

## 4. Classifying a run

The **RED set** is every test named by a `test` criterion that is not `regression: true`. A case *matches* a test id when the ids are equal or one is a `::` / `[` prefix of the other (a file-level collection error covers every test in that file). Classification, first match wins:

| Condition | Classification |
|---|---|
| No result (the check never ran) | `not_run` |
| Timed out | `timeout` |
| Any case errored with `SyntaxError`, `IndentationError` or `TabError` | `syntax_error` |
| The runner itself failed (pytest exit 3, 4 or 5 — internal error, usage error, no tests collected) | `environment_error` |
| A failing or erroring case is outside the RED set | `unrelated_failure` (the baseline is broken) |
| A RED-set case *errored* (setup, collection, or a non-assertion error in unittest) and it is not a missing declared interface | `collection_error` at collection, else `environment_error` |
| Exit 0 and nothing failed or errored | `passed` |
| Nonzero exit with no failing case | `environment_error` |
| Otherwise (every failing case is in the RED set and *failed*, or errored with a missing declared interface) | `behavioral_failure` |

A *missing declared interface* is `NotImplementedError`, or `ImportError` / `ModuleNotFoundError` / `AttributeError` whose message quotes an interface the contract declares with `change: add` (its full name or last dotted segment). That is the honest RED for "the function does not exist yet". A `ModuleNotFoundError` for a module the contract does not add is an environment problem, not RED.

## 5. RED, GREEN, REFACTOR

**RED is valid** when the verifier ran the approved tests on `source_rev` plus *only* the `scope.test_files` changes, the classification is `behavioral_failure`, **every** test in the RED set failed, the record's `contract_sha256` matches the frozen contract, and `revision` is not `source_rev` itself unless the tests already existed there.

**GREEN is valid** when there is a valid RED for the same contract hash (or the contract says `tdd.test_first: false`), the verifier ran the same tests on the implementation revision, the classification is `passed`, the `test_fingerprints` equal RED's exactly (no edited, deleted or substituted tests), `revision` descends from `base_rev`, and the patch touches only `scope.assigned_files` (plus nothing in `scope.test_files`).

**REFACTOR is valid** when it follows a valid GREEN, fingerprints still match, the classification is `passed`, and the contract hash is unchanged. "No refactor needed" is recorded as a refactor record on the GREEN revision with a note, not silently skipped, when `refactor` is in `evidence_required`.

**A REVIEW is a coordinator-recorded review, not an authenticated human sign-off.** `tdd review --reviewer NAME [--role human]` writes `produced_by.role: reviewer` and an `attestation` of kind `coordinator_recorded_review` with `reviewer_label`, `claimed_reviewer_kind` and `identity_verified: false`. The HMAC proves the coordinator wrote the record, not who reviewed. A review record may only carry role `human` with a `human_authorization` receipt from an external identity provider. That kind is reserved: bundlebees can't verify one yet and never reports one as authenticated. `tdd report` shows `review_identity` (`coordinator_recorded`, or `coordinator_recorded (legacy human label)` for older records) and `human_authorization: not_recorded`. Merge, tag, release, deploy and publish stay human gates outside bundlebees.

**REVIEW is valid** when a named reviewer (role `reviewer`, or `human` on older records; never `worker` or `verifier`) recorded it after a valid GREEN, and after a valid REFACTOR when the contract requires one, for that revision, with classification `passed` and a note. A rejection is recorded too, with classification `failed` (the only use of `failed`; test runs always use the §4 table), and makes the phase `review_failed`. Criteria with `verification: review` or `manual` count as passed only when REVIEW is valid. Integration requires REFACTOR as well as GREEN when `refactor` is in `evidence_required`.

**REVIEW is valid** when it was produced by a `reviewer` or `human` and is classified `passed`; it is what turns `review_required` criteria into acceptable ones. **INTEGRATION is valid** when the combined-branch run is `passed`.

**Tests written after the code** are allowed only with `tdd.test_first: false` (or `tdd.applicable: false` with a reason). The GREEN record then carries `test_first: false`, reports say "tests added after implementation", and no RED is claimed.

## 6. Parallel dispatch and integration

Parallel contracts must have disjoint `scope.assigned_files`. Test files may be shared read-only. Integration applies only patches with a valid GREEN (and REFACTOR when required; bundlebees' `integrate` currently checks GREEN only, see [§10](#10-where-this-is-enforced)), then runs the full suite on the combined branch, producing an `integration` record. A contract is *accepted* only when every phase in `evidence_required` has a valid verifier record, every criterion is `passed` or explicitly `review_required` with a review record, and integration passed. A human acceptance (merge and later gates) is still Kyle's.

## 7. Failure states

Every stop has a name, so reports and Orbit can show it without interpretation:

| Code | Meaning |
|---|---|
| `contract_invalid` | Schema or code check failed (field and fix in the message) |
| `contract_not_frozen` / `contract_changed` | Not approved, or the hash no longer matches |
| `red_invalid` | RED classified as anything but `behavioral_failure` (the classification is included) |
| `red_incomplete` | Some RED-set test passed on the baseline |
| `red_missing` | GREEN attempted with no valid RED |
| `stale_revision` | Evidence `base_rev` or `contract_sha256` does not match the contract |
| `tests_tampered` | Test fingerprints differ from RED, or the patch touches `scope.test_files` |
| `scope_violation` | Patch touches a file outside `scope.assigned_files` or inside `scope.prohibited` |
| `green_failed` / `refactor_failed` | The approved tests did not pass |
| `integration_failed` | Combined-branch suite failed |
| `unrun` | A required check never ran; it is reported as unrun, never as passed |

## 8. Compatibility

- Briefs without a `contract` keep working exactly as before (`legacy` mode). Tools report "no ACDD contract; TDD evidence not required" so nobody mistakes legacy work for test-first work.
- A brief with a `contract` is enforced: invalid or unfrozen contracts are rejected before dispatch.
- Projects can opt every brief into enforcement with the `require_contract` setting (bundlebees): dispatch then refuses a brief without a contract (`contract_missing`). The default stays off for `acdd/1`; a later version can flip it after a migration window. The reference demo runs contract-first by default; `--legacy` runs the old flow.
- New schema versions get a new `contract_version` / `evidence_version` string. Validators refuse versions they do not know rather than guessing.

## 9. Orbit

In progress: a read-only model and panel renderer are built (orbitdesktop `server/acdd-evidence.mjs`, `src/acdd-evidence-panel.ts`, on a branch under review), but they are not wired into the board yet. When they are, Orbit shows each contract's criteria with their latest verifier status, the RED → GREEN → REFACTOR timeline, failure codes from [§7](#7-failure-states), and unrun checks in their own colour. Orbit only displays; the decision stays with bundlebees' verifier and Kyle's gates. The integration contract for that view is a separate Orbit plan, tracked in [17](https://github.com/mojomast/workflow/blob/main/docs/17-orbit-plans.md).

## 10. Where this is enforced

| Layer | What enforces it | Status |
|---|---|---|
| Workflow (protocol) | This chapter, the two schemas, [`templates/CONTRACT.md`](https://github.com/mojomast/workflow/blob/main/templates/CONTRACT.md) | Defined |
| Reference demo | `reference/workflow/acdd.py`, `reference/workflow/tdd.py`, `reference/workflow/lifecycle.py` (the default run of `run_demo.py`), `reference/check_schemas.py` | Implemented, tested in CI. Evidence is HMAC-signed with a demo key kept in the run directory; runs use bwrap when available and say `sandbox: none` otherwise; workers and the reviewer are scripted (teaching only) |
| bundlebees | `contract.py` (validation, hash, freezing, the dispatch gate and `require_contract`), `tdd.py` (classification, signed append-only records, RED/GREEN/REFACTOR checks, acceptance report), and the CLI: `contract validate\|freeze`, `tdd red\|refactor\|review\|report`, a contract-aware `verify` (approved tests read-only, RED required, signed GREEN) and `integrate` (GREEN required, signed integration record) | On `feat/acdd-tdd`, under review; not on `main`, not released. `tdd refactor --rev SHA` reruns the read-only approved tests on a descendant of GREEN and refuses edited tests or out-of-scope paths; `tdd refactor --none --note` records "no refactor needed". `tdd review` records a signed REVIEW by a named reviewer or human; `integrate` requires REFACTOR too when the contract lists it |
| Orbit | Read-only contract and evidence view | Model and panel built on a branch under review; wiring into the board, routes and activity feed is planned |

The ACDD + TDD work itself was built this way; the RED/GREEN evidence for all three repositories is in [`evidence/2026-11-12-acdd-build/`](https://github.com/mojomast/workflow/blob/main/evidence/2026-11-12-acdd-build/README.md).
