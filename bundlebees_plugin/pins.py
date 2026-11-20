"""The reviewed stack pins, single-sourced for every consumer.

``doctor`` compares the deployed checkouts against these revisions, and
``contracts`` seals the workflow commit into a scaffolded feature packet, so a
scaffold and the doctor's ``pinned_workflow`` check can never disagree.
"""

from __future__ import annotations

#: The pinned workflow commit (the deployed WORKFLOW_SRC checkout revision).
WORKFLOW_PIN = "9c69d3a987ea9544a77cdd9b5a259904eb36f006"
HYVV_PIN = "1fcf1039e67ec5fd07e2111cba1808f793c7b5bd"
LAUNCHER_PIN = "0.1.17"
