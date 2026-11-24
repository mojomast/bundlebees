"""Approved tests for W0-2: risk module docstrings say what is actually enforced.

AC-001: risk.py's docstring says the floor is enforced at dispatch (``_dispatch`` / ``risk_tier_below_floor``).
AC-002: risk_policy.py's docstring says its process table is report-only and names where enforcement lives.
AC-003: the stale "not wired into dispatch" phrase is gone and the code outside docstrings is unchanged.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "bundlebees_plugin"

# sha256 of ast.dump() of each module with its module docstring removed, at the W0-2 source_rev.
CODE_FINGERPRINTS = {
    "risk.py": "2680950e0c8d8dbae0bd59f0eecd4f1f56b4826a7c4d3eb3a828bee48b284688",
    "risk_policy.py": "3cff4ee6aab0415686d971509bb7d8c34ba9360608d284564a8aaf5b035e5a41",
}


def _doc(name: str) -> str:
    return ast.get_docstring(ast.parse((ROOT / name).read_text(encoding="utf-8"))) or ""


def _code_fingerprint(name: str) -> str:
    tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    tree.body = body
    return hashlib.sha256(ast.dump(tree).encode("utf-8")).hexdigest()


class TestRiskDocstrings:
    def test_risk_says_enforced(self):
        doc = _doc("risk.py").lower()
        assert "enforced" in doc
        assert "_dispatch" in doc
        assert "risk_tier_below_floor" in doc
        assert "acdd/1" in doc or "contract" in doc

    def test_policy_says_report_only(self):
        doc = _doc("risk_policy.py").lower()
        assert "report-only" in doc or "report only" in doc
        assert "not enforced" in doc
        assert "_dispatch" in doc or "risk.py" in doc

    def test_code_unchanged(self):
        for name in ("risk.py", "risk_policy.py"):
            text = (ROOT / name).read_text(encoding="utf-8").lower()
            assert "not wired into dispatch" not in text
            assert "nothing here is wired" not in text
            assert _code_fingerprint(name) == CODE_FINGERPRINTS[name], name
