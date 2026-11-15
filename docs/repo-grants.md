# Standing repository grants

A repository grant is a one-time, human-approved standing authorization for
bounded bee work in one repository. It removes the per-job `job authorize`
step for that repository, while keeping every excluded action behind its own
human gate.

## Enroll, inspect, revoke

Grant enrollment is a **human terminal command**. The agent cannot run it, and
the command refuses when stdin/stdout are not a TTY. The statement is stored
verbatim in the grant and copied into every job it authorizes.

```bash
# One command per repository, run by the approver in a real terminal.
hermes bundlebees repo grant \
  --repo mojomast/rizzstack-smoke \
  --max-bees 3 \
  --wall-clock-max 3600 \
  --expires never \
  --statement "Standing approval for bounded smoke work in rizzstack-smoke."

hermes bundlebees repo list
hermes bundlebees repo revoke --repo mojomast/rizzstack-smoke
```

`--expires` accepts an ISO-8601 date/datetime or `never` (the default).
`--max-bees` defaults to 3 and may not exceed the global cap of 12.
`--wall-clock-max` defaults to 3600 seconds.

Revocation applies immediately to **new** jobs. The default is to let running
jobs finish; `--pause-running` records pause mode for a supervisor that
supports it. A revoked or expired grant is refused for every new job.

## What a grant covers

A repo grant covers the whole repository for bounded implementation work:

- read the repository and create branches;
- commit and push to non-protected feature branches;
- open draft pull requests;
- run tests and CI in the repository.

It does **not** cover human-only actions. These always require a separate
explicit approval, even when a job is otherwise covered by a grant:

- merging or approving pull requests;
- pushing to protected branches;
- force-push, branch deletion, or history rewrite;
- tags, releases, publishing, or catalog changes;
- deploys or worker restarts;
- worker-host configuration or credentials;
- anything touching secrets.

The generated job authorization carries these excluded actions as gates. A
task that needs one of them stops at approval rather than treating the grant as
permission.

## Auto-authorization and evidence

When `job create` (or `job project create`) has no per-job authorization, it
looks up the active grant for the job's repository. It derives a narrow job
authorization containing:

- the job's pinned revision and task file leases;
- the task bee count and wall-clock budget;
- the grant id, grant digest, grant statement, approval time, and expiry;
- the excluded-action gates.

If no usable grant exists, or a grant is expired, revoked, malformed, or
tampered, job creation is refused. There is no fallback to an unapproved job.

The job document records the grant evidence in its `authorization` block and
the authorization document is sealed with the state key. An agent can read
grants with `repo list`, but cannot create, renew, widen, or revoke one.

## Tests

`tests/test_repo_grants.py` covers enrollment TTY refusal, agent write
refusal, listing, revocation, expiry, tampering, bee and wall-clock limits,
excluded-action gates, missing-grant refusal, and CLI auto-authorization. The
existing `tests/test_grants.py` covers the underlying atomic reservation and
concurrency behavior.
