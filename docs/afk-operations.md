# AFK operations

The factory driver is `bundlebees_plugin.factory_driver`. Its parser requires
exactly one mode: `--once` for a single pass, or `--interval SECONDS` for the
resident loop. There is no enqueue, status or resume flag on the driver itself.
Campaign checkpoints are separate commands (`campaign init|status|reconcile|resume-plan`);
the driver only accepts an optional `--campaign-id` so it reconciles that
checkpoint on each pass. The campaign record never enqueues or grants work.

## Enqueue

The driver has no enqueue switch. Create the authorized job, then add a matching
item to the queue file it reads (`<state>/queue.json` by default, or the file
named by `--queue`).

```bash
hermes bundlebees job authorize \
  --job-id job_a \
  --repo owner/repo \
  --base-rev <40-hex-sha> \
  --scope 'src/**' \
  --statement "Describe the authorized work."

hermes bundlebees job create --job-id job_a --tasks tasks.json
```

When the owner is not at a keyboard, a one-time receipt written by the trusted
application replaces the typed authorization (the receipt is verified,
unexpired, unused and exactly bound; see
[docs/job-approval.md](job-approval.md)):

```bash
hermes bundlebees job authorize --job-id job_a \
  --approval-receipt "$STATE/job-approvals/receipts/apr_....json" \
  --tasks tasks.json
```

```json
{
  "feature-a": {
    "repo": "owner/repo",
    "job_id": "job_a",
    "title": "Feature A",
    "status": "queued"
  }
}
```

A queue file may also be the docket envelope the live state uses:

```json
{
  "schema": "bundlebees.queue/1",
  "generated_at": "2026-11-18T04:27:23Z",
  "features": [
    {
      "feature_id": "feature-a",
      "repo": "owner/repo",
      "job_id": "job_a",
      "status": "queued",
      "depends_on": []
    }
  ]
}
```

Both shapes are read and updated in place; any other JSON shape is a typed
`queue_invalid` refusal, never an `AttributeError`. A missing file is an empty
queue.

### Queue identity

One id, one card per shape. The authoritative id is the features-map key, the
features-list card's `feature_id` (falling back to `id`), or the flat-map map key
exactly as written. A card body may omit `feature_id`/`id` or restate that id.
Any differing declared `feature_id`/`id` refuses the whole document with
`queue_invalid`; a refused document is never partially read. A blank map key is
refused. Ids are compared after trimming surrounding whitespace, but trimming
does not merge two map keys that differ only in surrounding padding. A blank or
non-string `feature_id`/`id` is not an identity and is ignored. Keep keys
unpadded: the flat-map entry id is the key as written.

## Run

One pass, then exit:

```bash
python -m bundlebees_plugin.factory_driver --state "$STATE" --once
```

With no launcher URL this is the **observation-only** pass: the mechanical tick
cannot dispatch, verify, integrate or adopt a worker result, so the pass reports
the queued and active cards and moves none of them. Its JSON carries
`observation_only: true` and the queue action is `observation_only`; a one-line
warning goes to stderr.

A real job tick also needs the repository, scanner and launcher URL:

```bash
python -m bundlebees_plugin.factory_driver \
  --state "$STATE" \
  --repo "$REPO" \
  --gitleaks "$GITLEAKS" \
  --launcher-url "$BUNDLEBEES_LAUNCHER_URL" \
  --provisioning --provisioning-host HOST_LABEL \
  --once --suite-command python -m pytest -q
```

The selection is not silent: `--repo` without a launcher URL exits with the
missing option named instead of degrading to the observation tick. A launcher URL
without `--repo` is allowed for landing-only use and runs `observation_only`; it
never claims a card. A `--tick module:callable` override is used as given.

`--provisioning` binds the existing hyvv provisioning client over the same
authenticated launcher connection; `--provisioning-host` is the expected host
policy label, never a host address. The launcher token comes from
`BUNDLEBEES_LAUNCHER_TOKEN`. Every token after
`--suite-command` captures the remaining command line, including dash-prefixed
arguments, and is forwarded unchanged as the tick's `suite_command`. Put it last;
when omitted, the tick uses `python -m pytest -q`. `--once` prints the pass result as
JSON and exits 0.

## Project intake

A project spec is validated into a task plan before any job exists. The spec
contains `project_id`, `repo`, full `base_rev`, top-level acceptance criteria,
and features with disjoint `lease` paths, `depends_on` and acceptance tests.

```bash
hermes bundlebees job project plan \
  --spec pilot/todo/project.json \
  --out "$STATE/pilot-tasks.json"

hermes bundlebees job project create \
  --job-id pilot-todo-20261114 \
  --spec pilot/todo/project.json \
  --state-dir "$STATE"

hermes bundlebees job project status \
  --project-id pilot-todo \
  --state-dir "$STATE"
```

`plan` is pure and refuses malformed ids, unknown dependencies, cycles and
overlapping leases. `create` requires the human authorization first and records
the project next to the job; the job remains the execution authority. `status`
reports `complete`, `active`, `blocked` or `failed` from durable task states, not
from worker claims. Application-level acceptance is run separately:

```bash
python -m pilot.todo.acceptance --db "$STATE/pilot-todos.json"
```

## Status

Inspect the durable job, including tasks, attempts and any pending handoff:

```bash
hermes bundlebees job status --job-id job_a --state-dir "$STATE"
```

`--once` also prints the driver pass result, including the queue action,
landing and event counts, ready work, wake state, errors and whether the pass was
`observation_only`.

## Resume

Resume polling in the foreground by replacing `--once` with `--interval`:

```bash
python -m bundlebees_plugin.factory_driver --state "$STATE" --interval 1
```

Resident mode requires the `--interval` flag: bare `--interval` defaults to
1.0, and `--interval 1` is the explicit form. Ctrl-C exits 130. Add `--repo`,
`--gitleaks` and `--launcher-url` for a dispatch-capable loop; the command above
is the observation-only loop and never claims work.

After a crash or rejection, inspect the job, record or print the recovery
request, then advance it with one tick:

```bash
hermes bundlebees job status --job-id job_a --state-dir "$STATE"
hermes bundlebees job recover --job-id job_a --state-dir "$STATE"
hermes bundlebees job tick --job-id job_a --repo "$REPO" \
  --gitleaks "$GITLEAKS" --state-dir "$STATE"
```

The driver rejects a missing mode and rejects `--once` combined with
`--interval`.
