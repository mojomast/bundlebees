# Attach a Rails snapshot to a BundleBees RETURN

Use the coordinator profile's `rails_db` setting (absolute path on local disk). The report reads it with `mode=ro` and `query_only`; it never contacts a worker or advances an import cursor. A restricted query profile cannot verify whole chains. Finish importing the worker pages first.

```sh
hermes bundlebees job report --job-id job_a --rails-anchors /private/return-heads.json
hermes bundlebees job report --job-id job_a --rails-anchors /private/return-heads.json --verify
```

Supply an operator-controlled JSON file, not an attachment retrieved from a worker. Record each enrolled worker's head independently of its copy in the coordinator database, and pin the coordinator head separately. Keep the file and its directory private. The input has exactly these fields (replace placeholders with real values):

```json
{
  "schema": "bundlebees.rails-return-input/1",
  "job_id": "job_a",
  "task_id": "T1",
  "code_rev": "<full commit named by the RETURN>",
  "contract_sha256": "<reviewed frozen task contract digest>",
  "coordinator": {"chain_id": "<coordinator chain>", "seq": 8, "hash": "<head hash>"},
  "workers": [{"chain_id": "<enrolled worker chain>", "seq": 3, "hash": "<head hash>"}],
  "evidence": {"AC-001": ["<worker chain>:1", "<coordinator chain>:8"]}
}
```

Choose immutable event IDs relevant to each named criterion. For a worker observation, the attempt must belong to the task and its authenticated dispatch lease must name the exact originating chain, job, task, frozen `contract_sha256` and source revision. The chain assignment is the top-level `dispatch_chain_id` recorded by the coordinator when issuing the lease. A field in the brief, an event scope, a worker head or a later selection is not an assignment.

The current production dispatchers have no authoritative dispatch-to-Rails-chain registry. Their leases therefore do not qualify worker rows for a criterion's RETURN attachment. Retain and replay the enrolled origins as observations. Do not backfill old leases from imported events or generated anchors. The offline R18 fixture explicitly assigns a fresh local worker ledger before recording events; this is test-only dispatch evidence.

Verifier links require a local `verifier_record` for the same AC, contract and revision. Supporting event IDs must also be selected for the criterion. A native BundleBees record reference must resolve to the underlying evidence already checked by the RETURN; its bytes must match the source revision. Worker-only selections have no verification link.

The producer writes `jobs/<job>/rails-evidence.json`. Both `return.json` and `RETURN.md` reference the file through the criterion's evidence column. The `#sha256=…` fragment identifies the exact file bytes; open the path before the fragment relative to the state directory. The snapshot timestamp comes from the coordinator head, so the same snapshot produces identical bytes. The receiver uses its own trusted anchors and checks the sidecar and attachment with `--verify`; the decision stays pending. Rails reads do not fill in missing tests, signed RED, measurements, or approvals.

Verification requires the exact heads. If the live ledger has advanced, verify against a retained SQLite snapshot at the pinned heads. Bounds: 1–8 workers, 4,096 total origin rows, 8 MiB canonical evidence, 500 references, 128 KiB per input/output file. A missing or halted import, mismatched receipt, unsupported envelope, unresolved reference or exceeded bound refuses the attachment. Coverage describes the observed sources only; a valid hash is not proof of complete coverage.

## Verify the enclosing RETURN

Set `WORKFLOW_SRC` to the pinned protocol checkout. BundleBees runs its unmodified `tools/return_manifest.py` schema and semantic validator on the final native manifest (including the sidecar reference) before writing. You can run the same gate separately:

```sh
python "$WORKFLOW_SRC/tools/return_manifest.py" jobs/job_a/return.json
```

`verification.suite.command` is the recorded regression command; suite and gitleaks results require executed checks. A missing scan cannot be reported as PASS or FAIL. An attempt needs its actual project, from the authenticated lease or the task binding/provision record. Exact and dependent authority labels describe verified authorization records; neither is a receiver decision. A single-contract packet missing mandatory protocol evidence cannot produce a protocol RETURN. Packet-free and multi-contract jobs keep a local `bundlebees.job-report/1` progress report at the existing path; that label is **not** `rizzstack.return/1`.

RED/GREEN references name a state-relative `record_path`, its **raw file SHA-256** (including any trailing newline), `phase` and tested `revision`. Rebuilding checks the signature with the existing coordinator key, baseline/candidate and contract. `test_first` reports a proven RED, not a requested RED. A generic tool observation or unsupported verifier claim does not become an EXPECTED_RED link. The accepted wave, when present, names its wave ID, state-relative evidence file, commit and **canonical JSON** digest. An absent integration has four null fields.

For the pre-fix, public synthetic counterexample see `tests/fixtures/return/r18-malformed.json`. File SHA-256 `7eeae1946c2e3261443720faab4f357a02db34a64f1775f8e46ddf95783336e6`; valid body digest `000940e81034bf2b02dbce23e03e203b48f71d8311b6443ff91383dec80e38b2`. The pinned validator rejects `authorization.authority`, `red_green.red`, `red_green.green`, `verification.suite`, `verification.gitleaks`, `integration.wave` and `attempts[0].project`. A self-consistent body digest alone cannot establish conformance.

A review recorded under the coordinator's TDD key is an observation. Even when it names the correct criterion, contract and revision and says `passed`, the `reviewer` or `human` label does not authenticate a separate reviewer. The native RETURN reports the review criterion as `UNVERIFIED` and names the applicable record; an executed test for another criterion keeps its own result.

### Publication and crash recovery

The producer fsyncs each temporary file and replaces the output names separately: optional `rails-evidence.json`, then `RETURN.md`, then `return.json`. Each replacement is atomic for that one file. There is no transaction across the three names and no parent-directory fsync. A crash can leave files from different generations; the last successful write does not guarantee that this generation, or the previous one, survives power loss.

Before exposing a handoff, take a stable snapshot (or pause writers) and run `job report --verify` on it, using independently supplied anchors for an attached report. This rebuilds the manifest from the records and checks the rendering and attachment against that same generation. Expose only the verified snapshot. If a file is missing or mismatched, withhold the handoff and rebuild from intact evidence. Successful verification never substitutes for receiver approval: `receiver_decision` stays `pending`.
