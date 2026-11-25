# Observability and Rails rollout: coordinator host

This is the state of the rollout that #273 enables on the coordinator host, as of
2026-11-24 15:30 ET, and the steps that remain.

## Already done on the host

- Pinned checkout `~/rizzstack-ingest/bundlebees` at `9b4a315`. It is kept apart from
  the Hermes plugin worktree, which the plan lane manages.
- Units `bundlebees-rails-ingest.{service,timer}` and
  `bundlebees-route-sync.{service,timer}` are installed in `~/.config/systemd/user`.
  Each service ran once and succeeded:
  - route sync: seen 200, recorded 125, skipped 75 (unprefixed models get no route);
  - Rails ingest: bundlebees 705 and outbox 325 events into
    `~/.local/state/rizzstack-rails/ledger.sqlite3` (0600, in a 0700 dir).
  The **timers are not enabled yet.**
- `~/.config/bundlebees/route-sync.env` (0600) holds only the launcher URL and token,
  copied from `factory-driver.env`.
- The state root `~/.hermes/plugin-data/bundlebees` was tightened from 775 to 700,
  because the Rails source's private-writer check requires it. The original modes are in
  `~/rizzstack-change-backups/t1685/modes-before.txt`.
- The stale Nov 14–18 job records were archived (moved, not deleted) to
  `~/.hermes/plugin-data/bundlebees-archive/t1685-20261124T201137Z/`. It holds
  `MANIFEST.tsv` (path, mode, size and sha256 for each entry), `restore.sh` (refuses to
  overwrite) and `queue.json.before`. The archived jobs are job_m11_retention,
  rizzstack-m2-canary-1/-2, acceptance-20261115 and smoke-20261118/19/20/21/22/24/25.
  `queue.json` dropped feature t_a7d7725d, and its envelope is unchanged.
- The stray regular file
  `evidence/wf-failure-report-evidence-2026-11-16.bundle` was left in place. The
  source now skips it.
