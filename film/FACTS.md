# Approved v0.1.0 facts

These figures replace the earlier film's "≤900 budget" stand-in. They are the measured
and released v0.1.0 facts used on screen.

- **Tools:** 4 — `bundlebees_status`, `bundlebees_dispatch`, `bundlebees_attempt`, `bundlebees_host_prompt`
- **Schema tokens:** 571 o200k (2,617 chars, 561 cl100k), measured with the project's schema script on the release tree
- **Tests:** 150 passed
- **Release:** v0.1.0 at `57a6f12cdcfa789c5aba5f22f3f46efd2b8b3d5c`
- **Catalog PR:** [NousResearch/hermes-agent #153888](https://github.com/NousResearch/hermes-agent/pull/153888) (open)

On-screen terminal stills were rendered from actual command output captured on the
v0.1.0 tree, then scrubbed of local paths. The orch board is live launcher state
from the compose-v2 and bundlebees waves (CV01–04, BB01–04), including the BB02
rejection (`att_8d589c81592c66a7`, child_env allowlist mismatch) and retry
(`att_238d7f7d39f31891`, merged `da2aff1`). Hostnames, IPs, tokens and private
URLs were removed before the still was drawn.
