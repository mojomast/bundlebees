"""Risk-tier enforcement at dispatch (owner approved; fail closed).

AC-001: a declared tier below the path-derived floor is refused (risk_tier_below_floor); nothing is sent.
AC-002: a high-tier brief (sensitive paths) without a frozen ACDD contract is refused (contract_missing); nothing is sent.
AC-003: a high-tier brief WITH a frozen contract dispatches.
AC-004: a human may raise the tier above the floor (declared high on docs) and it then needs a contract.
AC-005: low and medium briefs are unaffected (still dispatch without a contract).
AC-006: refusals use the standard error envelope (ok false, code, error, fix) and the launcher is never called.
"""
from __future__ import annotations

import json

from bundlebees_plugin import contract as acdd
from bundlebees_plugin import tools

SHA = "a" * 40


class _Client:
    def __init__(self):
        self.created = []

    def attempts(self):
        return []

    def list_attempts(self):
        return []

    def create(self, brief, **kwargs):
        self.created.append(brief)
        return {"attempt_id": "att_0123456789abcdef", "state": "queued"}

    def create_attempt(self, brief, **kwargs):
        return self.create(brief, **kwargs)


def _contract(files):
    return {
        "contract_version": "acdd/1", "id": "W1-risk", "title": "t",
        "outcome": {"summary": "s", "observable_behavior": ["x"]},
        "acceptance_criteria": [{"id": "AC-001", "given": "g", "when": "w", "then": "t",
                                 "verification": "test", "tests": ["tests/test_x.py::test_x"]}],
        "scope": {"assigned_files": files, "test_files": ["tests/test_x.py"]},
        "source_rev": SHA, "tdd": {"applicable": True}, "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }


def _brief(files, contract=False, **over):
    b = {"objective": "implement", "project": "demo", "source_rev": SHA,
         "assigned_files": files, "acceptance": ["AC-001 passes"]}
    if contract:
        b["contract"] = acdd.freeze_contract(_contract(files), approved_by="kyle")
    b.update(over)
    return b


def _run(brief, tmp_path):
    client = _Client()
    return json.loads(tools._dispatch(client, {"brief": brief}, tmp_path)), client


def test_ac001_declared_below_floor_is_refused(tmp_path):
    out, client = _run(_brief(["bundlebees_plugin/security.py"], risk={"tier": "low"}), tmp_path)
    assert out["ok"] is False and out["code"] == "risk_tier_below_floor"
    assert client.created == []


def test_ac002_high_tier_without_contract_is_refused(tmp_path):
    out, client = _run(_brief(["bundlebees_plugin/security.py"]), tmp_path)
    assert out["ok"] is False and out["code"] == "contract_missing"
    assert "high" in out["error"]
    assert client.created == []


def test_ac003_high_tier_with_frozen_contract_dispatches(tmp_path):
    out, client = _run(_brief(["bundlebees_plugin/security.py"], contract=True), tmp_path)
    assert out.get("ok") is not False, out
    assert len(client.created) == 1


def test_ac004_human_raise_above_floor_needs_a_contract(tmp_path):
    out, client = _run(_brief(["docs/a.md"], risk={"tier": "high"}), tmp_path)
    assert out["ok"] is False and out["code"] == "contract_missing"
    assert client.created == []


def test_ac005_low_and_medium_are_unaffected(tmp_path):
    for files in (["docs/a.md"], ["bundlebees_plugin/foo.py"]):
        out, client = _run(_brief(files), tmp_path)
        assert out.get("ok") is not False, (files, out)
        assert len(client.created) == 1


def test_ac006_refusals_have_the_standard_envelope(tmp_path):
    for brief in (_brief(["bundlebees_plugin/security.py"], risk={"tier": "low"}), _brief(["bundlebees_plugin/security.py"])):
        out, client = _run(brief, tmp_path)
        assert set(out) >= {"ok", "code", "error", "fix"} and out["fix"]
        assert client.created == []
