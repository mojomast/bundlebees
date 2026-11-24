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

# sha256 of each module's source text after its module docstring, at the W0-2 source_rev.
# Source text (not ast.dump) so the fingerprint is the same on every supported Python (3.11+).
CODE_FINGERPRINTS = {
    "risk.py": "3b5cb85c0d6dc614f5b1e13f923fe0ff1c75b6c8ba23f3a8755dd857d094b937",
    "risk_policy.py": "ebc4f0f1f3fff12fee6a32879845c0b55c6b68774ef396a26bbd0d997984b3b1",
}


def _doc(name: str) -> str:
    return ast.get_docstring(ast.parse((ROOT / name).read_text(encoding="utf-8"))) or ""


def _code_fingerprint(name: str) -> str:
    text = (ROOT / name).read_text(encoding="utf-8")
    body = ast.parse(text).body
    start = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        start = body[0].end_lineno
    rest = "\n".join(text.splitlines()[start:])
    return hashlib.sha256(rest.encode("utf-8")).hexdigest()


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
