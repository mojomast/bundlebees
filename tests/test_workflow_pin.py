"""Phase 1 (rizzstack cleanup): the workflow revision is pinned, not a placeholder.

AC-001: contracts.PIN_WORKFLOW_REVISION is a real 40-hex commit and is the same single
        pin doctor checks (bundlebees_plugin.pins.WORKFLOW_PIN), not "PIN-THE-WORKFLOW-REV".
AC-002: a scaffolded feature packet carries that pinned revision as its workflow_ref.
AC-003: the placeholder string no longer appears anywhere in bundlebees_plugin/.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEX40 = re.compile(r"[0-9a-f]{40}")


class TestWorkflowPin:
    def test_pin_is_a_real_commit_and_single_sourced(self):
        from bundlebees_plugin import contracts, doctor, pins

        assert HEX40.fullmatch(pins.WORKFLOW_PIN), pins.WORKFLOW_PIN
        assert contracts.PIN_WORKFLOW_REVISION == pins.WORKFLOW_PIN
        assert doctor.WORKFLOW_PIN == pins.WORKFLOW_PIN

    def test_scaffolded_packet_carries_the_pinned_revision(self):
        from bundlebees_plugin import contracts, pins

        built = contracts.scaffold(
            contract_id="pin-check",
            title="Pin check",
            summary="Scaffold carries the pinned workflow revision.",
            tests=["tests/test_x.py::test_y"],
            assigned_files=["src/x.py"],
            source_rev="a" * 40,
        )
        assert built["packet"]["workflow_ref"] == pins.WORKFLOW_PIN
        assert HEX40.fullmatch(built["packet"]["workflow_ref"])

    def test_placeholder_is_gone_from_the_plugin(self):
        hits = [
            str(path.relative_to(ROOT))
            for path in (ROOT / "bundlebees_plugin").rglob("*.py")
            if "PIN-THE-WORKFLOW-REV" in path.read_text(encoding="utf-8")
        ]
        assert hits == []
