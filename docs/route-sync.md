# Route sync: which provider route a bee ran on

The launcher chooses the model, and with it the provider route, when it claims
a bee. The coordinator's lease is written before that, so `hermes bundlebees
attempts` used to report `route: null` for every attempt dispatched without an
attribution intent, and the Hive attempt ledger showed "Route unknown".

`python -m bundlebees_plugin.route_sync --state-dir DIR` reads the launcher's
own attempt listing (`GET /v1/attempts`, public fields only) and, for each
attempt this coordinator leased, writes one observation:

```
<state>/attempt-routes/<attempt_id>.json   # 0600; directory 0700
{"schema": "bundlebees.attempt-route/1", "attempt_id": "...", "model": "nous:hy4-flash",
 "route": "nous", "observed_at": "...Z", "source": "launcher"}
```

Rules:

- The route is taken only from an explicit provider prefix on the reported
  model (`go`, `deepseek`, `zoo`, `kimi`, `nous`). An unprefixed model keeps
  its model but no route; a route is never inferred from a model name.
- Attempts the coordinator did not lease are skipped. Unsafe attempt ids are
  skipped. Replays that change nothing write nothing.
- `attempts` prefers a verified attribution intent's route; a reported route
  fills in only when there is none, and a reported model never becomes
  `attributed_model`.
- The launcher URL comes from `--launcher-url` or `BUNDLEBEES_LAUNCHER_URL`; the
  credential only from `BUNDLEBEES_LAUNCHER_TOKEN` in the environment. Output
  is counts or a generic error code.

`contrib/systemd/bundlebees-route-sync.{service,timer}` run it every minute
from a pinned checkout with a narrow 0600 environment file. Create
`<state>/attempt-routes` (0700) once before enabling the timer.
