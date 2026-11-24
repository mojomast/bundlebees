# Contract: worker model routing for complex slices (acdd/1, draft)

Owner request: Kyle, "make sure it's used for more complex tasks for bees"
(t1640u), with the provider fixed to Kimi's own API (t1641u). Everything
else stays on OpenCode Go.

## Outcome

`bundlebees_plugin.model_route.select_worker_model(brief, ...)` is a pure,
deterministic function a coordinator calls before `bundlebees_dispatch`. It
returns the worker model for one brief and the reasons. Complex slices get
the Kimi route (`k3.1-pro`, the launcher's allow-listed Kimi model id);
every other slice gets `go:deepseek-v4.1-flash` on OpenCode Go. It never
contacts a network, never reads a credential, and never lowers a risk tier.

## Definition of "complex"

A brief is complex when ANY of these holds (each adds a reason):

| id | rule |
|---|---|
| C1 | effective risk tier is `high` (declared `risk.tier` combined with the path floor from `risk.effective_tier`; an invalid tier fails closed to high) |
| C2 | the lease (`assigned_files`) names 5 or more files (multi-file) |
| C3 | the brief spans repos: `cross_repo: true`, or `repos` lists 2+ distinct repos |
| C4 | the lease touches a security or approval-gate path (path floor reason from `risk.derive_floor`) |
| C5 | Rails harness work: both `rails` and `harness` as words in project, track, label, branch or title, or a `rails` path segment |
| C6 | the slice already failed twice: `failed_attempts` argument or brief field is 2 or more |
| C7 | the brief asks for it: `complexity: "complex"` |

A `complexity: "simple"` hint never cancels C1-C6 (routing errs toward the
stronger model).

## Nous low-risk lane (owner t1660u, t1665u; Nov 24 2026)

A brief that is not complex may take the Nous Portal lane (`nous:*` ids,
the launcher's `nous` key kind). `lane` and `chain` in the result report it.

| lane | when | model, then the launcher's fallback chain |
|---|---|---|
| `low` | effective tier `low` with at most 3 files on the first attempt (docs, tests-only), or a verify brief (`verify_of`) at tier low/medium | `nous:deepseek-v4.1-flash-1017` -> `nous:hy4-flash` -> `go:deepseek-v4.1-flash` |
| `low-harder` | tier `low` with 4 files, one failed attempt, or `complexity: "moderate"`; or tier `medium`, `complexity: "simple"`, at most 2 files (small refactor) | `nous:hy4-flash` -> `go:deepseek-v4.1-flash` |
| `default` | everything else, or `nous: false` on the brief | `go:deepseek-v4.1-flash` |

1017 is the discounted DeepSeek flash (20/20 in the Nov 24 benchmark, about
half Hy4's cost). When the launcher allow-list is given, the first allow-listed
id in the chain is used with `fallback: true` and a reason. The launcher, not
this module, falls back at run time (429 after retries, auth or 5xx, or a
missing nous key) and paces every `nous:*` model combined (max 8 active per
key, starts spaced 2 s) under the key's 180 requests/min.

## Acceptance criteria

- AC-001: a medium 3-file source brief that is not complex routes to the default model with no reasons - tests/test_model_route.py::TestSelectWorkerModel::test_simple_brief_uses_default
- AC-002: a high-risk brief routes to the complex model with a C1 reason - tests/test_model_route.py::TestSelectWorkerModel::test_high_risk_is_complex
- AC-003: a 5-file lease is complex (C2); a 4-file medium lease is not - tests/test_model_route.py::TestSelectWorkerModel::test_multi_file_threshold
- AC-004: cross-repo briefs are complex (C3) - tests/test_model_route.py::TestSelectWorkerModel::test_cross_repo
- AC-005: a security/approval path is complex even when the declared tier is low (C4 and C1) - tests/test_model_route.py::TestSelectWorkerModel::test_security_path
- AC-006: Rails harness briefs are complex (C5); "trails" and a Rails decision memo without harness work do not match - tests/test_model_route.py::TestSelectWorkerModel::test_rails_harness
- AC-007: two failed attempts make a slice complex (C6); one does not - tests/test_model_route.py::TestSelectWorkerModel::test_failed_twice
- AC-008: `complexity: complex` forces C7; `complexity: simple` cannot cancel a rule - tests/test_model_route.py::TestSelectWorkerModel::test_complexity_hints
- AC-009: when the launcher allow-list is given and lacks the complex model, the result falls back to the default model with `fallback: true` and a reason (never silent) - tests/test_model_route.py::TestSelectWorkerModel::test_fallback_when_not_allowlisted
- AC-010: `model_pinned: true` keeps the brief's own model and reports source `pinned` - tests/test_model_route.py::TestSelectWorkerModel::test_pinned_model
- AC-011: a non-mapping brief or a negative/non-int failed_attempts raises ValueError (no dispatch on bad input) - tests/test_model_route.py::TestSelectWorkerModel::test_invalid_input_fails_closed
- AC-013: a low-risk docs brief routes to `nous:deepseek-v4.1-flash-1017` with chain 1017 -> hy4 -> go - tests/test_model_route.py::TestLowRiskLane::test_low_risk_docs_brief_uses_the_discounted_flash
- AC-014: tests-only and verify briefs take the low lane - tests/test_model_route.py::TestLowRiskLane::test_tests_only_brief_is_low, tests/test_model_route.py::TestLowRiskLane::test_verify_brief_is_low
- AC-015: harder low-risk slices and small refactors use `nous:hy4-flash` - tests/test_model_route.py::TestLowRiskLane::test_harder_low_risk_slices_use_hy4, tests/test_model_route.py::TestLowRiskLane::test_small_refactor_uses_hy4
- AC-016: any complex rule beats the low lane - tests/test_model_route.py::TestLowRiskLane::test_complex_always_beats_the_low_lane
- AC-017: allow-list fallback inside the lane is never silent; `nous: false` opts out - tests/test_model_route.py::TestLowRiskLane::test_low_lane_falls_back_when_not_allowlisted, tests/test_model_route.py::TestLowRiskLane::test_brief_can_opt_out_of_nous
- AC-018: Nous pacing constants mirror the launcher - tests/test_model_route.py::TestLowRiskLane::test_nous_pacing_is_documented
- AC-012: the CLI prints the decision as JSON for a brief file and exits 0; a missing file exits 2 - tests/test_model_route.py::TestCli::test_cli_json

## Out of scope

No restart from this module. The Nous key kind, model entries, fallback chain
and pacing live in the launcher (hyvv). No fallback provider in Hermes. The launcher already allow-lists `k3.1-pro` with its own key kind;
this module only picks a model id. Human gates (merge, release, deploy,
publish) are unchanged.
