# Risk tiers

`contrib/unwired/risk_policy.py` maps a risk tier to how much process a task
gets. It is data and pure functions only: the table is **report-only until the owner
enables enforcement** and the phase and reviewer table below is still not wired into any gate.

**Enforced at dispatch (owner approved, fail closed):** `bundlebees_plugin/tools.py::_dispatch` now
refuses (1) a brief whose declared `risk.tier` is below the path-derived floor
(`risk_tier_below_floor`) and (2) a high-tier brief, meaning sensitive, invalid or unknown paths or a
human-raised tier, that has no frozen ACDD contract (`contract_missing`). Low and medium briefs are
unchanged. Human gates, extra reviewers and minimum verifications for high tier are not yet enforced.

| Tier | Phases | Reviewer | Plan | Human gates | Min verifications |
|---|---|---|---|---|---|
| `low` | `red`, `green` | `opencode-go` | `short` | no | 1 |
| `medium` | `red`, `green`, `refactor` | `opencode-go` | `full` | no | 2 |
| `high` | `red`, `green`, `refactor`, `integration`, `review`, `security` | `deepseek-api` | `full+review` | yes | 3 |

`policy_for(tier)` returns a fresh deep copy each call; mutating it never
changes the table.

## Preflight

Every dispatcher outside Hermes (for example the coordinator box posting straight
to the launcher) must run the gates first and refuse on a non-zero exit:

```
python -m bundlebees_plugin.preflight brief.json --state-dir plugin-data
```

It runs the same validation, risk-floor, contract and research gates as
`bundlebees_plugin/tools.py::_dispatch`, but never builds a client, opens a socket
or writes state. It exits `0` when the brief passes, `3` when the gates refuse
(JSON `code`, `error`, `fix`), and `2` when the brief file is unreadable or not
JSON. Unlike `_dispatch`, preflight requires an explicit `risk.tier`; a box-level
model `tier` is never read as a risk tier.

## Fail-closed rules

- A tier is exactly the string `low`, `medium` or `high`. Anything else —
  `None`, an empty string, an unknown string, or a non-string value — gets the
  high policy, labelled `high`.
- `missing_phases(tier, recorded_phases)` reports policy phases not present in
  the recorded evidence, in policy order. Non-string items are ignored and a
  non-iterable evidence value counts as empty, so unreadable evidence reports
  gaps instead of passing silently.
- An unknown tier in `missing_phases` uses the high policy, so the widest set of
  gaps is reported rather than a smaller one.

## Where the floor comes from

The floor comes from path-based derivation: the tier follows the files a task
touches, not what the brief claims. `risk.derive_floor` reads the versioned
`bundlebees_plugin/policy/risk-floor.json` policy (`bundlebees.risk-floor/1`).
Its case-insensitive basename and path-pattern rules classify high and low
paths, with medium as the default. `risk_policy` separately describes the
process a tier receives.

Absolute paths, traversal, sensitive security and verification modules,
schemas, CI workflows and authentication or approval code are high. Test
infrastructure is high too: every `conftest.py`, everything under
`tests/fixtures/`, `pytest.ini`, `tox.ini`, `setup.cfg`, `noxfile.py` and
`sitecustomize.py`. Ordinary test and documentation paths remain low.

A policy that is missing, unreadable, malformed or uses the wrong schema fails
closed to high and adds a policy reason. Empty or invalid path lists also fail
closed to high. Callers may pass `policy_path` to `derive_floor` to select
another policy; a load failure never falls back to a lower tier.
