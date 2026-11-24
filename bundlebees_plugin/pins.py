"""The reviewed stack pins, single-sourced for every consumer.

``doctor`` compares the deployed checkouts against these revisions, and
``contracts`` seals the workflow commit into a scaffolded feature packet, so a
scaffold and the doctor's ``pinned_workflow`` check can never disagree.
"""

from __future__ import annotations

#: The pinned workflow commit (the deployed WORKFLOW_SRC checkout revision).
WORKFLOW_PIN = "1dacd5bb2552cc7052f436dcba04601995c82167"
HYVV_PIN = "fdd235446e8fb28f8074fa06ea5c6ee1e466e129"
LAUNCHER_PIN = "0.1.17"
