"""Phase 1 (rizzstack cleanup): the contract gate's checks run in the live dispatch path,
REPORT-ONLY.

The provenance and acceptance-mismatch checks (contracts.check_provenance and
contracts.check_acceptance_ids) used to live only in contract_gate.py, which no production
path calls. They now run inside tools._dispatch_acdd for every contract brief, but they never
refuse: what they would have refused is recorded in the acdd envelope block under "gate" and
appended to <state_dir>/contract-gate/report-only.jsonl. Turning enforcement on is Kyle's call.

AC-001: a contract brief whose acceptance names an id the contract lacks still dispatches, and
        the envelope's gate block (mode "report_only") lists an acceptance_mismatch.
AC-002: a clean contract brief reports nothing: would_refuse == [] and the contract's ids.
AC-003: a packet pinned to the wrong workflow revision is reported as provenance_mismatch
        (and missing provenance as provenance_unavailable), never refused.
AC-004: each would-refuse finding is appended to <state_dir>/contract-gate/report-only.jsonl
        with the contract id and code, and the launcher create still happens exactly once.
AC-005: legacy (contract-free) briefs are untouched: no gate block, no report file.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bundlebees_plugin import contract as acdd
from tests import _packet_fixtures as packet_fixtures

SHA = "a" * 40
OTHER = "b" * 40


def _contract():
    return {
        "contract_version": "acdd/1",
        "id": "W1-pricing",
        "title": "Add apply_tax",
        "outcome": {"summary": "tax", "observable_behavior": ["apply_tax(1000, 10) == 1100"]},
        "acceptance_criteria": [
            {"id": "AC-001", "given": "1000", "when": "apply_tax", "then": "1100",
             "verification": "test", "tests": ["tests/test_pricing.py::test_apply_tax"]},
        ],
        "scope": {"assigned_files": ["shop/pricing.py"], "test_files": ["tests/test_pricing.py"]},
        "source_rev": SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }


def _frozen():
    return acdd.freeze_contract(_contract(), approved_by="kyle")


def _brief(contract=None, **over):
    b = {"objective": "implement", "project": "demo", "source_rev": SHA,
         "assigned_files": ["shop/pricing.py"], "acceptance": ["AC-001 passes"]}
    if contract is not None:
        b["contract"] = contract
    b.update(over)
    return b


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


@pytest.fixture()
def pinned(tmp_path, monkeypatch):
    repo = tmp_path / "workflow"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "feature_packet.py").write_text("# pinned validator\n", encoding="utf-8")
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()
    run("init", "-q")
    run("-c", "user.email=a@example.invalid", "-c", "user.name=a", "commit", "--allow-empty", "-m", "pin")
    monkeypatch.setenv("WORKFLOW_SRC", str(repo))
    return run("rev-parse", "HEAD")


def _gate(block):
    assert isinstance(block, dict), block
    gate = block.get("gate")
    assert isinstance(gate, dict), f"no report-only gate block in {block}"
    assert gate.get("mode") == "report_only"
    return gate


def _codes(gate):
    return sorted(item["code"] for item in gate["would_refuse"])


class TestGateReportOnly:
    def test_unknown_acceptance_id_is_reported_not_refused(self):
        from bundlebees_plugin import tools

        block = tools._dispatch_acdd(_brief(_frozen(), acceptance=["AC-001 passes", "AC-999 also"]), False)
        assert not isinstance(block, str), block
        assert block["mode"] == "contract"
        assert "acceptance_mismatch" in _codes(_gate(block))

    def test_clean_brief_reports_nothing(self):
        from bundlebees_plugin import tools

        gate = _gate(tools._dispatch_acdd(_brief(_frozen()), False))
        assert gate["would_refuse"] == []
        assert gate["acceptance_ids"] == ["AC-001"]

    def test_provenance_findings_are_reported_not_refused(self, pinned, monkeypatch):
        from bundlebees_plugin import tools

        frozen = _frozen()
        packet = packet_fixtures.exact_packet(
            contract=frozen, contract_sha256=str(frozen["review"]["contract_sha256"]), workflow_ref=OTHER)
        block = tools._dispatch_acdd(_brief(frozen, packet=packet), False)
        assert not isinstance(block, str), block
        assert "provenance_mismatch" in _codes(_gate(block))

        good = packet_fixtures.exact_packet(
            contract=frozen, contract_sha256=str(frozen["review"]["contract_sha256"]), workflow_ref=pinned)
        assert _gate(tools._dispatch_acdd(_brief(frozen, packet=good), False))["would_refuse"] == []

        monkeypatch.delenv("WORKFLOW_SRC", raising=False)
        block = tools._dispatch_acdd(_brief(frozen, packet=packet), False)
        assert not isinstance(block, str), block
        assert "provenance_unavailable" in _codes(_gate(block))

    def test_findings_are_recorded_and_dispatch_still_happens(self, tmp_path):
        from bundlebees_plugin import tools

        client = _Client()
        out = json.loads(tools._dispatch(client, {"brief": _brief(_frozen(), acceptance=["AC-001", "AC-777"])}, tmp_path))
        assert out.get("ok", True) is not False, out
        assert len(client.created) == 1
        assert "acceptance_mismatch" in _codes(out["acdd"]["gate"])
        lines = (tmp_path / "contract-gate" / "report-only.jsonl").read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        assert any(r.get("code") == "acceptance_mismatch" and r.get("contract_id") == "W1-pricing" for r in records)
        assert all(r.get("mode") == "report_only" for r in records)

    def test_legacy_briefs_are_untouched(self, tmp_path):
        from bundlebees_plugin import tools

        block = tools._dispatch_acdd(_brief(), False)
        assert block["mode"] == "legacy" and "gate" not in block
        client = _Client()
        json.loads(tools._dispatch(client, {"brief": _brief()}, tmp_path))
        assert not (tmp_path / "contract-gate" / "report-only.jsonl").exists()
