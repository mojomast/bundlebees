# bundlebees — screen script

The delivered film uses music and on-screen typography only. These are the exact principal screen lines; this document can also guide optional future narration. All substantive visuals remain above the lower third.

**00:00–00:06**\
One agent. A swarm of work.\
Give it a hive.

**00:06–00:12**\
Small briefs. Clear boundaries.\
OBJECTIVE / FILE LEASES / ACCEPTANCE\
Contracts first. Budgets in the brief.

**00:12–00:20**\
A sandbox for every bee.\
REAL ORCH STATE · CV01–04 / BB01–04\
Live orch state · including BB02 reject and retry

**00:20–00:26**\
Dispatch. Observe. Review.\
bundlebees_dispatch → bundlebees_attempt\
Tracked on Hermes’ own kanban.

**00:26–00:32**\
Evidence before acceptance.\
File scope / Tests / Secret scan\
REJECTED\
Worker claims are not evidence.

**00:32–00:40**\
A failed attempt is feedback.\
REVISE → RETRY → ACCEPTED\
BB02 rejected att_8d589c81592c66a7 · child_env allowlist\
retried att_238d7f7d39f31891 · merged da2aff1\
Verify again. Then integrate.\
Feature branch first. Merge stays with the owner.

**00:40–00:46**\
The agent coordinates. You run the checks.\
hermes bundlebees verify\
hermes bundlebees deploy\
User-run CLI · not model tools

**00:46–00:52**\
No model-executed untrusted code.\
Minimal child environment\
Confined worktree paths\
Secrets via get_secret only\
Worker sandboxes do not sandbox the Hermes process.

**00:52–00:58**\
Measured interface. Deliberate scope.\
4 tools\
bundlebees_status / bundlebees_dispatch / bundlebees_attempt / bundlebees_host_prompt\
571 o200k tokens · measured\
150 tests passed\
v0.1.0 · 57a6f12

**00:58–01:05**\
One pane. The whole operation.\
Health + live attempts / Verify results / Bundled operator skill\
ACTUAL PANE · HEALTH

**01:05–01:12**\
bundlebees\
Lead the swarm. Keep control.\
github.com/mojomast/bundlebees\
A plugin for Hermes Agent · NousResearch/hermes-agent\
OPEN SOURCE / BY MOJOMAST\
v0.1.0 · 57a6f12 · catalog PR #153888

## Fact ledger

- film/FACTS.md and the v0.1.0 release tree (`57a6f12`): identity, four exact tool names, measured 571 o200k schema tokens, 150 passing tests, catalog PR #153888, MIT license.
- docs/RESEARCH.md §3.2–3.3: worker claims are self-reports; model does not run verify/deploy or untrusted code.
- docs/RESEARCH.md §4: Hermes kanban, dependency waves.
- docs/RESEARCH.md §6 and §9: briefs, budgets, DeepSeek Flash, two waves of two.
- docs/RESEARCH.md §7: exact-name child environment, confined worktrees, get_secret, worker sandbox scope.
- docs/tasks/BB00_CONTRACTS.md: objective/assigned_files/acceptance, scope rejection, named tests and secret scan, feature-branch-only push, user-owned merge.

On-screen proof uses the measured v0.1.0 figures: 4 tools, 571 o200k tokens, 150 tests. Terminal stills and the orch board were rendered from real command output and live launcher state, then scrubbed of local paths, tokens, IPs and private URLs.
