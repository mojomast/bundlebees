VERDICT: APPROVE

# WA-M1 review — `metrics.weekly()` (PR #265, head dd2c20c)

## Summary

The final `weekly()` satisfies the stated contract, and the round-2 extraction is a
behavior-preserving decomposition: the single event-ordered pass, the branch order, the
set/dict mutation sites and the completeness short-circuit are unchanged in effect. The
CI lint finding is removed by lowering `weekly`'s McCabe score (2) and moving the scan
into `_scan_window` (9), with no baseline edit and no suppression. I found no blocking
defect. Two non-blocking observations are recorded below (F1 is a contract-wording
ambiguity at a genuine-zero edge; F2 is a float-sum ordering nit).

## Findings

### F1 — Low (non-blocking, contract-wording ambiguity): rate for a genuine zero total
- File: `bundlebees_plugin/metrics.py:851-854` (`weekly`), enabled by
  `bundlebees_plugin/metrics.py:806-811` (`_window_totals`).
- Observation: when every dispatched attempt reports usable tokens and the sum is a
  genuine `0`, `tokens_total` is `0` (correct, "an observed zero stays zero") and
  `tokens_per_verified_patch` is `0` whenever `verified_patches > 0`. The task contract
  literally says the rate is `None` when `tokens_total` "is unknown or zero"; under that
  reading the guard should also reject `tokens_total == 0`.
- Why I do not treat this as blocking: this PR's own changelog says "a genuine zero
  report is still zero", the approved test module's docstring says the rate is `None`
  "unless every attempt reported tokens", and the module's existing convention
  (`_rate` / `tokens_per_accepted` at `metrics.py:700`) returns `0` for a genuine zero
  numerator. On the reading "None only when the total is unknown or the verified count
  is zero, never a zero stand-in", the code is correct. A 0-token verified patch is not
  a stand-in; it is a measured zero.
- Concrete fix only if the coordinator confirms the literal reading:
  `if tokens_total and verified_patches else None` (or `tokens_total not in (None, 0)`).
  That is a behavior change and needs its own approved test; it is out of scope for this
  review round and for this PR's frozen test file.

### F2 — Informational: subtotal is summed in set-iteration order
- File: `bundlebees_plugin/metrics.py:801-802` (`_window_totals`).
- Observation: `sum(tokens_by_attempt[key] for key in dispatched ...)` iterates a
  `set[tuple[str, str]]`, whose order is hash-randomized across processes. For integer
  token counts (the realistic and tested case) every partial sum is exact, so this is
  deterministic in practice. For non-integral `tokens` values (allowed by
  `_usage_tokens`, which accepts any finite non-negative float), the last ULP of
  `tokens_known_subtotal` / `tokens_total` could vary by process. This does not affect
  *which* report wins; the winning-report order (F2 is separate from that) is preserved.
- Concrete fix if desired: `math.fsum(tokens_by_attempt[key] for key in sorted(dispatched)
  if key in tokens_by_attempt)`, or accumulate the subtotal in event order inside
  `_scan_window`. Not required.

## Contract check, clause by clause

Verified by reading and by direct probes (temporary state dirs, `append_event` +
`weekly`):

- `window_days` echoes the validated `days`: `_window_days` rejects bool/non-int/<1 with
  `MetricsError("invalid_field")` and returns the int (`metrics.py:723-727`).
- Inclusive window `[now - days, now]`: `_window_events` keeps only
  `not (at < start or at > end)` (`metrics.py:760`). Probe: events exactly at `start`
  and exactly at `end` were counted; one second outside on either side was not.
- `verified_patches`: distinct `(run, attempt)` pairs with `event == "verify"`,
  usable attempt id, `bool(ok)` (`metrics.py:783-784`), deduped in a set. Probe:
  boundary verifies counted, a failed verify not counted, a re-verify counted once.
- `attempts`: `len(dispatched) + anonymous_dispatches` where `dispatched` is the deduped
  set of usable-id dispatch pairs and each unusable-id dispatch event increments by one
  (`metrics.py:778-782`, `800`). Probe: duplicate dispatch deduped; one anonymous
  dispatch added exactly one.
- `tokens_total`: `None` unless `known_subtotal is not None and anonymous_dispatches == 0
  and with_tokens == len(dispatched)` (`metrics.py:806-811`). Probe: an anonymous dispatch
  and a missing report each forced `None`; a complete set produced the sum; a genuine 0
  stayed 0.
- `tokens_per_verified_patch`: `None` when `tokens_total is None` or `verified_patches` is
  0, else `_tidy(total / verified)` (`metrics.py:851-854`). See F1 for the genuine-zero
  total reading.
- `tokens_known_subtotal`: partial sum over dispatched attempts with a report, else
  `None` when none is known (`metrics.py:801-805`). Probe: 10 + missing -> 10; nothing
  -> `None`; anonymous excluded.
- `coverage`: `attempts_with_tokens = with_tokens`;
  `attempts_without_tokens = attempts - with_tokens` (unknown reports and anonymous
  dispatches fall on the "without" side), and the two sum to `attempts`
  (`metrics.py:855-858`).
- Token semantics: cumulative per attempt, last valid report wins. `read_events` returns
  records in validated `seq` order, and `_scan_window` has exactly one write site,
  `tokens_by_attempt[key] = tokens`, guarded by `tokens is not None`
  (`metrics.py:785-787`). An unusable later report (`None`) does not overwrite a prior
  valid one, and a later valid zero does overwrite. Probe: 100 then 120 then -5 -> 120;
  then a 0 -> 0, restoring `tokens_total` to 0 and the rate to 0.
- Empty state: `verified_patches=0`, `attempts=0`, `tokens_total=None`,
  `tokens_known_subtotal=None`, coverage `{0, 0}` — no zero stand-ins.

## Round-2 refactor: behavior preservation

The round-1 tree is not in this sandbox (one squashed commit at the head), so I could not
diff the two implementations. I judged equivalence structurally and by probes:

- `weekly` keeps the same signature `(state_dir, *, now=None, days=7)`, the same
  docstring, and returns the same seven keys.
- `_attempt_key` isolates exactly the usable-id predicate
  (`isinstance(attempt_id, str) and _ID_RE.fullmatch(attempt_id)`), matching the existing
  `_event_attempts` / `valid_ids` convention in the module; it returns `None` otherwise.
- `_window_events` preserves the parse-and-bound filter and the outer loop over
  `list_runs` (sorted) then `read_events` (seq order). No sort was added; no event is
  reordered.
- `_scan_window` preserves a single loop with the same branch order
  (dispatch -> verify -> tokens), the same `set.add` dedup, and the same single
  `tokens_by_attempt` assignment. Because only one statement can change the winning
  report, and it remains inside the one ordered loop, the "last valid report wins"
  property cannot be altered by the extraction.
- `_window_totals` preserves `attempts = len(dispatched) + anonymous_dispatches`, the
  `with_tokens` count, the "subtotal only when something is known" rule, and the exact
  three-way `complete` short-circuit. `_tidy` is applied at the same stage.
- `weekly` now does only: validate bounds, one scan, one totals computation, rate, and
  dict assembly. No state crosses the helper boundary that could reorder mutations.

## Scope, lint and baseline

- `git status --porcelain` was empty at review start; the tree is the single squashed
  commit `9042a4f` ("base dd2c20c05f0fe7c18a59942bf40a2f7ef194f60a").
- `bundlebees_plugin/metrics.py` contains no `# noqa` / slop suppression comments.
- `slop-baseline.json` allows `bundlebees_plugin/metrics.py: {"complexity": 4}`. Counting
  with the vendored `slop_lint.rules` gives exactly 4 over-limit functions — `read_events`
  13, `_plan_to_accepted` 15, `_cost_summary` 22, `summarize` 30 — all pre-existing. The
  new code is under the limit: `weekly` 2, `_scan_window` 9, `_window_totals` 5,
  `_window_events` 6, `_attempt_key` 3, `_window_days` 4, `_window_end` 5. The round-2 fix
  therefore removed the new finding without raising the baseline; the baseline was not
  weakened. (The baseline file is also outside the slice's lease per the supplied scope
  report.)
- `CHANGELOG.md:20` accurately describes the seven keys, cumulative-per-attempt usage,
  last-valid-wins, unknown -> `None` (never zero) for total and rate, partial subtotal,
  and genuine-zero semantics.
- The approved `tests/test_metrics_weekly.py` (76 lines, 6 tests) was read in full and
  matches the stated approved content; I did not modify it.
- I could not independently diff the three files against the merge base (no base commit
  in this sandbox), so the "nothing else changed" judgement rests on the clean tree, the
  supplied scope report, and the consistency of the three files with the described slice.

## What I read and ran

Read: the end of `bundlebees_plugin/metrics.py` (helpers at 723-902 plus the supporting
`_number`, `_timestamp`, `_ordered`, `read_events`, `list_runs`, `append_event`),
`tests/test_metrics_weekly.py` in full, the `CHANGELOG.md` Unreleased bullet, the
vendored slop-lint rules/engine/baseline, and `slop-baseline.json`.

Ran, inside the working tree (pytest, gh, network and base diffs are unavailable here by
design and were not attempted):
- `python3 -c "import ast; ast.parse(open('bundlebees_plugin/metrics.py').read())"` ->
  `metrics.py parses`.
- `python3 -m compileall -q bundlebees_plugin` -> exit 0.
- Read-only complexity tally with the vendored `slop_lint.rules` (results above).
- Contract probes in a throwaway `tempfile.TemporaryDirectory` via `metrics.append_event`
  and `metrics.weekly`: inclusive start/end bounds, out-of-window exclusion, anonymous
  dispatch, missing token report, last-valid-wins with an unusable later report, genuine
  zero winning, empty state, and `days=0` / bad `now` raising `MetricsError invalid_field`.

No blocking defect found.
