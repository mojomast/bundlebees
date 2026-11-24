"""The reviewed stack pins, single-sourced for every consumer.

``doctor`` compares the deployed checkouts against these revisions, and
``contracts`` seals the workflow commit into a scaffolded feature packet, so a
scaffold and the doctor's ``pinned_workflow`` check can never disagree.
"""

from __future__ import annotations

#: The pinned workflow commit (the deployed WORKFLOW_SRC checkout revision).
WORKFLOW_PIN = "c8ef4c8e62ea550e1541fd0562c857404f900bdb"
HYVV_PIN = "9691837b0ef3bd2d5c23f7d5b539c6c22d7c8ebb"
LAUNCHER_PIN = "0.1.17"
