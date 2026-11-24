# ADR 0001: adapters for harness, model, sandbox, forge and coordinator

Status: **accepted as design** (Kyle, chat t1650u). Design only. Wave E of the Nov 24 next-level review ([rizzstack #17](https://github.com/mojomast/rizzstack/pull/17)). Recommendation recorded: the first second-harness spike is Claude Code over ACP (its headless JSON mode is the fallback); nothing is switched on by this ADR.

## Why bundlebees owns this ADR

Three of the five seams (sandbox, forge, coordinator) are in bundlebees, and so is the acceptance
suite that proves a second harness or model works: verify runs here. The harness and model seams are
implemented in hyvv; their interfaces are specified in this ADR and the hyvv slices cite it.

## Problem (coupling found on main, 24 Nov)

| Seam | Coupled today |
|---|---|
| Coordinator runtime | Hermes named in 149 files; `register_*`/`get_secret` in 15 files; the coordinator recipe lives in host-side skills outside any repo |
| Worker harness | OpenCode via Cua and Antigravity hard-coded as `KINDS` (`hyvv/launcher.py:130`); the `opencode` and `codex` adapters here are stubs (`adapters.py:1011-1042`) |
| Model and provider | provider URLs, model ids, free-tier rules and error regexes are constants (`hyvv/launcher.py:40-130`); `client.py:40` `DEFAULT_MODEL` |
| Sandbox and OS | verify needs bwrap or firejail (`sandbox.py:55-57`), so it is Linux only and uses the host toolchain |
| Forge | `gh` called inline in 15 files (for example `beat_reconciliation.py:465`) |

The guard test `tests/test_adapters.py:246-255` checks only 5 modules and does not ban "hermes".

## Decision (proposed)

1. **Harness (hyvv):** a `Harness` interface (`start`, `resume`, `cancel`, `result -> git branch`)
   with `opencode` and one ACP implementation. A spike per agent before it is listed. HY-2's
   `resume(a, prompt)` is the first method of this interface.
2. **Model (hyvv):** `models.yaml` (ids, tiers, provider, per-provider error regexes as data) plus an
   optional OpenAI-compatible gateway; rizzstack keeps per-attempt token accounting. Kimi on its own
   API, never routed through OpenCode Go, is the first config entry to prove it (MI-31). Routing keys
   off `risk.tier`, which wave A slice A7 makes the policy file's job.
3. **Sandbox (bundlebees):** a `container` backend in `sandbox.py` behind the existing backend
   switch: one OCI container per verify from the project's `devcontainer.json`, `--network none`.
   bwrap stays the Linux default; Seatbelt for macOS. Fail closed: no backend, no verify.
4. **Forge (bundlebees):** one `forge.py` module (PRs, checks, merges) that wraps today's `gh` calls;
   git stays the contract.
5. **Coordinator (bundlebees):** `bundlebees mcp` (stdio), reusing the `tools.py` schemas; the host
   skills are committed to the repo as Agent Skills.
6. **Lint:** widen the guard test into a CI lint that bans vendor, model and host names in core paths.

## Slices

| Slice | Repo | What |
|---|---|---|
| E1 | bundlebees | widen the guard test into the lint (report-only first) |
| E2 | bundlebees | `forge.py` wrapping the existing `gh` calls (no behaviour change) |
| E3 | hyvv | `models.yaml` loader; constants become defaults |
| E4 | hyvv | `Harness` interface + opencode; then the ACP spike |
| E5 | bundlebees | `container` sandbox backend |
| E6 | bundlebees | `bundlebees mcp` stdio server |

## Exit

A second harness and a second model provider pass the acceptance suite, and verify runs on macOS.

## Decisions for the owner

- Which second harness to spike first (recommended: Claude Code over ACP, because its headless JSON
  mode is the fallback).
- Whether a gateway (LiteLLM or Bifrost) is used at all, after a bake-off.
