"""Approved acceptance tests for the ACDD dispatch gate in tools.py (contract BC1).

Written by the coordinator before implementation. Do not edit.
"""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeCtx, load_plugin

SHA = "a" * 40


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
def tools(monkeypatch, tmp_path):
    from bundlebees_plugin import tools as mod

    return mod


def _call(tools, client, brief, tmp_path, **kw):
    out = tools._dispatch(client, {"brief": brief}, tmp_path, **kw)
    return json.loads(out)


def _frozen():
    from bundlebees_plugin import contract

    return contract.freeze_contract(_contract(), approved_by="kyle")


# AC-007: a frozen, matching contract dispatches and the envelope says so.
def test_contract_brief_dispatches_with_acdd_envelope(tools, tmp_path):
    client = _Client()
    out = _call(tools, client, _brief(_frozen()), tmp_path)
    assert out.get("ok", True) is not False, out
    assert out["acdd"]["mode"] == "contract"
    assert out["acdd"]["contract_id"] == "W1-pricing"
    assert len(out["acdd"]["contract_sha256"]) == 64
    assert out["acdd"]["tdd_applicable"] is True
    assert len(client.created) == 1


# AC-008: invalid, unfrozen, stale or widened contracts never reach the launcher.
@pytest.mark.parametrize(
    "make, code",
    [
        (lambda: _brief(dict(_contract(), source_rev="zz")), "contract_invalid"),
        (lambda: _brief(_contract()), "contract_not_frozen"),
        (lambda: _brief(dict(_frozen(), title="changed")), "contract_changed"),
        (lambda: _brief(_frozen(), source_rev="b" * 40), "stale_revision"),
        (lambda: _brief(_frozen(), assigned_files=["shop/pricing.py", "shop/x.py"]), "scope_violation"),
        (lambda: _brief(_frozen(), assigned_files=["shop/pricing.py", "tests/test_pricing.py"]), "tests_tampered"),
    ],
)
def test_bad_contracts_are_rejected_before_dispatch(tools, tmp_path, make, code):
    client = _Client()
    out = _call(tools, client, make(), tmp_path)
    assert out.get("ok") is False, out
    assert out.get("code") == code, out
    assert out.get("fix")
    assert client.created == []


# AC-009: legacy briefs keep working and are labelled legacy.
def test_legacy_brief_still_dispatches_and_is_labelled(tools, tmp_path):
    client = _Client()
    out = _call(tools, client, _brief(), tmp_path)
    assert out.get("ok", True) is not False, out
    assert out["acdd"]["mode"] == "legacy"
    assert "TDD evidence not required" in out["acdd"]["note"]
    assert len(client.created) == 1


# AC-010: require_contract opts a project into enforcement.
def test_require_contract_rejects_legacy_briefs(tools, tmp_path):
    client = _Client()
    out = _call(tools, client, _brief(), tmp_path, require_contract=True)
    assert out.get("ok") is False and out.get("code") == "contract_missing", out
    assert client.created == []


# AC-011: the tool schema stays within budget (o200k <= 600 for all four tools).
def test_tool_schema_budget():
    tiktoken = pytest.importorskip("tiktoken")
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    body = json.dumps([rec["schema"] for rec in ctx.tools.values()], separators=(",", ":"), ensure_ascii=False)
    assert len(tiktoken.get_encoding("o200k_base").encode(body)) <= 600
