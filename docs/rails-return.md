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

Choose immutable event IDs relevant to each named criterion. For a worker observation, its attempt must belong to the task, and the authenticated dispatch lease must bind that job, task and revision. Verifier links require a local `verifier_record` for the same AC, contract and revision. Supporting event IDs must also be selected for the criterion. A native BundleBees record reference must resolve to the underlying evidence already checked by the RETURN; its bytes must match the source revision. Worker-only selections have no verification link.

The producer writes `jobs/<job>/rails-evidence.json`. Both `return.json` and `RETURN.md` reference the file through the criterion's evidence column. The `#sha256=…` fragment identifies the exact file bytes; open the path before the fragment relative to the state directory. The snapshot timestamp comes from the coordinator head, so the same snapshot produces identical bytes. The receiver uses its own trusted anchors and checks the sidecar and attachment with `--verify`; the decision stays pending. Rails reads do not fill in missing tests, signed RED, measurements, or approvals.

Verification requires the exact heads. If the live ledger has advanced, verify against a retained SQLite snapshot at the pinned heads. Bounds: 1–8 workers, 4,096 total origin rows, 8 MiB canonical evidence, 500 references, 128 KiB per input/output file. A missing or halted import, mismatched receipt, unsupported envelope, unresolved reference or exceeded bound refuses the attachment. Coverage describes the observed sources only; a valid hash is not proof of complete coverage.
