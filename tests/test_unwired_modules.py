"""Phase 1 (rizzstack cleanup): advertised-but-unreachable modules are gone or labelled.

AC-001: graceful recovery (never imported by any production path) is deleted: the module,
        its doc and its tests are gone.
AC-002: verify workers stay (research_gate's tests use publish_verify_result) but are
        clearly marked NOT WIRED: verify_workers.WIRED is False, the module docstring and
        docs/verify-workers.md both open with a "NOT WIRED" notice.
AC-003: no production module under bundlebees_plugin/ imports verify_workers.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestUnwiredModules:
    def test_graceful_recovery_is_deleted(self):
        assert importlib.util.find_spec("bundlebees_plugin.graceful_recovery") is None
        assert not (ROOT / "bundlebees_plugin" / "graceful_recovery.py").exists()
        assert not (ROOT / "docs" / "graceful-recovery.md").exists()
        assert not (ROOT / "tests" / "test_graceful_recovery.py").exists()

    def test_verify_workers_is_marked_not_wired(self):
        from bundlebees_plugin import verify_workers

        assert verify_workers.WIRED is False
        doc = (verify_workers.__doc__ or "").strip().splitlines()
        assert doc and "NOT WIRED" in doc[0]
        head = "\n".join(
            (ROOT / "docs" / "verify-workers.md").read_text(encoding="utf-8").splitlines()[:6]
        )
        assert "NOT WIRED" in head

    def test_no_production_module_imports_verify_workers(self):
        pattern = re.compile(r"^\s*(from\s+\.\s+import\s+[^\n]*\bverify_workers\b|from\s+\.verify_workers\s+import|import\s+bundlebees_plugin\.verify_workers|from\s+bundlebees_plugin\s+import\s+[^\n]*\bverify_workers\b)", re.M)
        hits = [
            path.name
            for path in (ROOT / "bundlebees_plugin").glob("*.py")
            if path.name != "verify_workers.py" and pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert hits == []
