# Publish one coordinator docket

`hive.queue/1` is an operator-invoked observation of one **public feature and job pair**. Use the exact IDs, not their titles. The application has no coordinator connection. Publication requires a separate directory; granting a consumer read access to that directory is a separate decision.

```sh
python3 -m bundlebees_plugin.hive_docket \
  --state-dir "$PRIVATE_STATE" --publication-dir "$PUBLICATION" \
  --feature-id F1 --job-id job_a
```

Both directories must already exist, belong to the caller and have mode 0700. Use trusted, owner-controlled ancestors and a separate publication tree outside the state directory. Do not use bind mounts or aliases between the two trees. Use genuinely public IDs (1–64 ASCII letters/digits/`_`/`-`, first character alphanumeric). Never enroll IDs containing sensitive information. Each invocation fsyncs a new 0600 file and atomically replaces **only** `queue.json`. The exporter never runs a verifier, reads a URL, dispatches work or creates coordinator state. Nothing runs on startup.

## Sources and joins

- `queue/<feature>.json` (`QueueStore`) must name **both** selected IDs. The reader does not enumerate other jobs or use an ID guessed from a title. A missing required claim or mismatch refuses publication. This version does not describe unclaimed backlog entries.
- A MAC-checked `jobs/<job>.json` must identify exactly one task with attempts for additional evidence. Multiple attempted tasks leave the optional sections unknown; the reader never picks an attempt for its favorable result. For that task's latest attempt the factory's `work-jobs` mapping must match the scheduler work ID, job and attempt. `overlap` is a same-snapshot intersection with other **claimed scheduler file scopes**. It says nothing about external leases or capacity.
- The signed `leases/<attempt>.json` must verify with the existing private key and agree with the sealed task's files and job base revision. “Recorded” is historical, not live. The reader uses the native sealing primitives on the bounded bytes it actually read, without creating a key or recovering a job.
- Coverage is optional. Supply `--head <40-hex> --pr <number> --candidate-state draft|merged` together and retain the **private**, authentic inputs below. Inputs are operator-owned evidence. This tool neither authenticates a GitHub response nor queries current PR state. PR posture means *as captured*.

| Fixed private input | What is required |
| --- | --- |
| `coverage.json` | Retained native `bundlebees coverage … --json` / `coverage.reconcile` output. `task_id` must be the feature ID, `repo` the full job repository, `contract` the exact source revision (`brief.source_rev` in the CLI), and `assigned_files` the union of sealed job task scopes. |
| `coverage-pr.json` | Separately retained `gh pr view … --json number,url,headRefOid,baseRefName,state,isDraft,mergedAt` object for the exact PR on `main`. Open draft or explicitly merged only. |
| `verify-results/<attempt>.json` | Native successful commit-local verifier record, matching attempt, base, head and the signed lease file set. The sealed job task must be verified, integrating or accepted. |

The producer reuses the merged #83 **file** classifier on verified candidate files, compares its output to the retained record, and requires the full repository, PR number and head to agree across the evidence. `PARTIAL` is an open draft with a strict subset; `COVERED_BY_DRAFT` is full file coverage of an open draft; `LANDED` is full file coverage with a recorded merged PR. None measures acceptance criteria, signed reviewer approval, current eligibility, execution or deployment. Those require different evidence. `MISSING`, an absent optional record or an ambiguous job is **unknown**, never permission to dispatch. Malformed, conflicting, cross-head or changing records refuse the new generation; an older publication is not refreshed by a failed invocation.

## Wire format and limits

The only top-level fields are `version: "hive.queue/1"`, `subject: {featureId,jobId}`, `observedAt`, `queue`, `scheduler`, `lease`, and `coverage`. All keys, including nullable ones, are present. The producer emits ASCII JSON (UTF-8), maximum 64 KiB. Read regular files without following symlinks; maximum 1 MiB per source, 256 rows or file keys per array. Before replace, recheck the identities, ctimes, sizes and mtimes of all observed inputs including absent files. This is an observation across multiple durable records, not a transactional snapshot of the coordinator.

| Object | Exact fields |
| --- | --- |
| `queue` | `state`: claimed/running/ready_for_review/needs_approval/failed/blocked/unknown; `updatedAt`: UTC timestamp or null; `sha256`: SHA-256 of native feature record bytes |
| `scheduler` | `state`: pending/blocked/claimed/done/unknown; `overlap`: recorded/none/unknown; `sha256`: exact scheduler document digest or null |
| `lease` | `state`: recorded/unknown; `sha256`: exact verified lease file digest or null |
| `coverage` | `state`: PARTIAL/COVERED_BY_DRAFT/LANDED/UNKNOWN; `head`: full lowercase Git SHA; `covered`,`total`: file counts 1–256; `sha256`,`verificationSha256`: exact native report and verifier file SHA-256. All five evidence fields null when unknown. |

All SHA-256 fields are lowercase 64 hex. Digests identify exact records; they are not a signature of the publication. UTC timestamps use `YYYY-MM-DDTHH:mm:ssZ`. `observedAt` is the local read time, **not** the age of the upstream PR/CI evidence. Consumers show snapshot age (stale after 300 seconds, refuse >30 seconds into the future), reject unknown keys before display, and independently bound bytes before parsing. No raw titles, file names, URLs, commands, attempt names, credentials or signing material are published.

### Rehearsal and prior art

`python3 -m pytest -q tests/test_hive_docket.py` creates actual signed jobs, queue claims, scheduler events/mappings, leases and local verifier commits under pytest's temporary directory. Only candidate PR metadata is simulated. The test uses the repository's disposable sandbox runner and standard-library unit tests. It exercises cross-head rejection, source immutability, stale-generation refusal and draft-vs-merged classification without opening an actual PR for the job.

The bounded JSON decoder and same-snapshot file-key intersection adapt `hive_queue_read.py` at `07e02ba`. The offline `hive_coverage_view.py` on that branch evaluates a different claim (named criterion evidence against a retained RETURN); it is **not** used for file coverage. Classification belongs to `coverage.py` from #83. Reviewer identities and their receipts from #85 remain at the approval boundary and are not published.
