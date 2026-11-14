# AFK operations

The factory driver is `bundlebees_plugin.factory_driver`. Its parser requires
exactly one mode: `--once` for a single pass, or `--interval SECONDS` for the
resident loop. There is no separate enqueue, status or resume flag.

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

## Run

One pass, then exit:

```bash
python -m bundlebees_plugin.factory_driver --state "$STATE" --once
```

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

`--provisioning` binds the existing hyvv provisioning client over the same
authenticated launcher connection; `--provisioning-host` is the expected host
policy label, never a host address. The launcher token comes from
`BUNDLEBEES_LAUNCHER_TOKEN`. Every token after
`--suite-command` captures the remaining command line, including dash-prefixed
arguments, and is forwarded unchanged as the tick's `suite_command`. Put it last;
when omitted, the tick uses `python -m pytest -q`. `--once` prints the pass result as
JSON and exits 0.

## Status

Inspect the durable job, including tasks, attempts and any pending handoff:

```bash
hermes bundlebees job status --job-id job_a --state-dir "$STATE"
```

`--once` also prints the driver pass result, including the queue action,
landing and event counts, ready work, wake state and errors.

## Resume

Resume polling in the foreground by replacing `--once` with `--interval`:

```bash
python -m bundlebees_plugin.factory_driver --state "$STATE" --interval 1
```

Resident mode requires the `--interval` flag: bare `--interval` defaults to
1.0, and `--interval 1` is the explicit form. Ctrl-C exits 130.

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
