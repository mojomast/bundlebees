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
| C5 | Rails harness work: `rails` as a word in project, track, label, branch or title, or a `rails` path segment |
| C6 | the slice already failed twice: `failed_attempts` argument or brief field is 2 or more |
| C7 | the brief asks for it: `complexity: "complex"` |

A `complexity: "simple"` hint never cancels C1-C6 (routing errs toward the
stronger model).

## Acceptance criteria

- AC-001: a low-risk docs brief with one file routes to the default model with no reasons - tests/test_model_route.py::TestSelectWorkerModel::test_simple_brief_uses_default
- AC-002: a high-risk brief routes to the complex model with a C1 reason - tests/test_model_route.py::TestSelectWorkerModel::test_high_risk_is_complex
- AC-003: a 5-file lease is complex (C2); a 4-file medium lease is not - tests/test_model_route.py::TestSelectWorkerModel::test_multi_file_threshold
- AC-004: cross-repo briefs are complex (C3) - tests/test_model_route.py::TestSelectWorkerModel::test_cross_repo
- AC-005: a security/approval path is complex even when the declared tier is low (C4 and C1) - tests/test_model_route.py::TestSelectWorkerModel::test_security_path
- AC-006: Rails harness briefs are complex (C5), and "trails" does not match - tests/test_model_route.py::TestSelectWorkerModel::test_rails_harness
- AC-007: two failed attempts make a slice complex (C6); one does not - tests/test_model_route.py::TestSelectWorkerModel::test_failed_twice
- AC-008: `complexity: complex` forces C7; `complexity: simple` cannot cancel a rule - tests/test_model_route.py::TestSelectWorkerModel::test_complexity_hints
- AC-009: when the launcher allow-list is given and lacks the complex model, the result falls back to the default model with `fallback: true` and a reason (never silent) - tests/test_model_route.py::TestSelectWorkerModel::test_fallback_when_not_allowlisted
- AC-010: `model_pinned: true` keeps the brief's own model and reports source `pinned` - tests/test_model_route.py::TestSelectWorkerModel::test_pinned_model
- AC-011: a non-mapping brief or a negative/non-int failed_attempts raises ValueError (no dispatch on bad input) - tests/test_model_route.py::TestSelectWorkerModel::test_invalid_input_fails_closed
- AC-012: the CLI prints the decision as JSON for a brief file and exits 0; a missing file exits 2 - tests/test_model_route.py::TestCli::test_cli_json

## Out of scope

No launcher change, no restart, no new provider, no fallback provider in
Hermes. The launcher already allow-lists `k3.1-pro` with its own key kind;
this module only picks a model id. Human gates (merge, release, deploy,
publish) are unchanged.
