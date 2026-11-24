"""Approved tests for W0-1: ``preflight`` runs the dispatch gates without dispatching.

AC-001: a brief declaring low on a source path is refused with risk_tier_below_floor.
AC-002: a high-tier brief without a contract is refused with contract_missing.
AC-003: a docs-only low brief passes with tier low and acdd_mode legacy.
AC-004: preflight and _dispatch return the same error code for every brief in the parity table.
AC-005: preflight never builds a client, opens a socket or writes state.
AC-006: a box-shaped brief (title, tier, label, budget, wave, map_context, ...) gates exactly like the bare brief.
AC-007: ``python -m bundlebees_plugin.preflight`` exits 0/3/2 and prints one JSON line; ``hermes bundlebees preflight`` exists.
AC-008: preflight (stricter than _dispatch) refuses a brief that declares no risk tier: risk_tier_missing.
"""
from __future__ import annotations

import argparse
import json
import socket

import pytest

from bundlebees_plugin import contract as acdd
from bundlebees_plugin import tools

SHA = "b" * 40


def _preflight_mod():
    from bundlebees_plugin import preflight  # noqa: WPS433 - module under test
    return preflight


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
        "contract_version": "acdd/1", "id": "W0-1-test", "title": "t",
        "outcome": {"summary": "s", "observable_behavior": ["x"]},
        "acceptance_criteria": [{"id": "AC-001", "given": "g", "when": "w", "then": "t",
                                 "verification": "test", "tests": ["tests/test_x.py::test_x"]}],
        "scope": {"assigned_files": files, "test_files": ["tests/test_x.py"]},
        "source_rev": SHA, "tdd": {"applicable": True}, "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }


def _brief(files, risk_tier="medium", contract=False, **over):
    b = {"objective": "implement", "project": "demo", "source_rev": SHA,
         "assigned_files": list(files), "acceptance": ["AC-001 passes"]}
    if risk_tier is not None:
        b["risk"] = {"tier": risk_tier}
    if contract:
        b["contract"] = acdd.freeze_contract(_contract(list(files)), approved_by="kyle")
    b.update(over)
    return b


BOX_KEYS = {
    "title": "W0-x demo", "fallback_model": "deepseek-v4.1-flash", "tier": "deepseek-v4.1-flash",
    "label": "w0-x", "budget": {"timeout_s": 3600, "max_tool_calls": 250}, "test_first": True,
    "approved_tests": ["tests/test_x.py"], "deps": [], "wave": "rizzstack-plan-wave0",
    "map_context": {"arm": "map", "slice_tokens_est": 900}, "deliverables": ["docs/a.md"],
    "model": "go:deepseek-v4.1-flash", "branch": "plan/x", "merge_target": "plan/x",
    "commands": ["timeout 60 true"],
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _refuse(*args, **kwargs):
        raise AssertionError("preflight must not open a socket")
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket.socket, "connect", _refuse)


class TestPreflight:
    def test_low_on_source_path_refused(self, tmp_path):
        out = _preflight_mod().preflight(_brief(["bundlebees_plugin/foo.py"], risk_tier="low"), state_dir=tmp_path)
        assert out["ok"] is False
        assert out["code"] == "risk_tier_below_floor"
        assert out["error"] and out["fix"]

    def test_high_without_contract_refused(self, tmp_path):
        out = _preflight_mod().preflight(_brief(["bundlebees_plugin/security.py"], risk_tier="high"), state_dir=tmp_path)
        assert out["ok"] is False
        assert out["code"] == "contract_missing"

    def test_docs_only_low_passes(self, tmp_path):
        out = _preflight_mod().preflight(_brief(["docs/a.md"], risk_tier="low"), state_dir=tmp_path)
        assert out["ok"] is True, out
        assert out["tier"] == "low"
        assert out["floor"] == "low"
        assert isinstance(out["reasons"], list)
        assert out["acdd_mode"] == "legacy"

    def test_high_with_frozen_contract_passes(self, tmp_path):
        files = ["bundlebees_plugin/security.py"]
        out = _preflight_mod().preflight(_brief(files, risk_tier="high", contract=True), state_dir=tmp_path)
        assert out["ok"] is True, out
        assert out["tier"] == "high"
        assert out["acdd_mode"] == "contract"

    def test_no_network_no_client(self, tmp_path, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError("preflight must not build a client")
        monkeypatch.setattr(tools, "_client_for", _boom)
        before = sorted(p.name for p in tmp_path.rglob("*"))
        pf = _preflight_mod()
        for brief in (_brief(["docs/a.md"], risk_tier="low"), _brief(["bundlebees_plugin/foo.py"], risk_tier="low")):
            pf.preflight(brief, state_dir=tmp_path)
        assert sorted(p.name for p in tmp_path.rglob("*")) == before

    def test_box_keys_dropped_unknown_kept(self, tmp_path):
        pf = _preflight_mod()
        for files, tier in ((["docs/a.md"], "low"), (["bundlebees_plugin/foo.py"], "low"),
                            (["bundlebees_plugin/security.py"], "high")):
            bare = pf.preflight(_brief(files, risk_tier=tier), state_dir=tmp_path)
            boxed = pf.preflight(_brief(files, risk_tier=tier, **BOX_KEYS), state_dir=tmp_path)
            assert bare == boxed, (files, bare, boxed)
        # the top-level box "tier" is a model tier, never a risk tier
        model_tier_only = _brief(["bundlebees_plugin/foo.py"], risk_tier=None)
        model_tier_only["tier"] = "high"
        out = pf.preflight(model_tier_only, state_dir=tmp_path)
        assert out["ok"] is False and out["code"] == "risk_tier_missing"

    def test_missing_risk_tier_refused(self, tmp_path):
        out = _preflight_mod().preflight(_brief(["docs/a.md"], risk_tier=None), state_dir=tmp_path)
        assert out["ok"] is False
        assert out["code"] == "risk_tier_missing"
        assert out["fix"]

    def test_invalid_json_or_shape_refused(self, tmp_path):
        pf = _preflight_mod()
        for bad in ("not json", [], None, {"objective": "x"}):
            out = pf.preflight(bad, state_dir=tmp_path)
            assert out["ok"] is False
            assert out["code"] in {"brief_invalid", "risk_tier_missing"}


PARITY = [
    (["bundlebees_plugin/foo.py"], "low", False),
    (["bundlebees_plugin/security.py"], "medium", False),
    (["bundlebees_plugin/security.py"], "high", False),
    (["docs/a.md"], "high", False),
    ([".github/workflows/ci.yml"], "high", False),
    (["../escape.py"], "high", False),
]


class TestPreflightParity:
    def test_codes_match_dispatch(self, tmp_path):
        pf = _preflight_mod()
        for files, tier, contract in PARITY:
            brief = _brief(files, risk_tier=tier, contract=contract)
            client = _Client()
            dispatched = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path / "d"))
            pre = pf.preflight(brief, state_dir=tmp_path / "p")
            assert dispatched["ok"] is False, (files, dispatched)
            assert pre["ok"] is False
            assert pre["code"] == dispatched["code"], (files, tier, pre, dispatched)
            assert client.created == []

    def test_dispatch_unchanged_for_passing_briefs(self, tmp_path):
        pf = _preflight_mod()
        for files, tier in ((["docs/a.md"], "low"), (["bundlebees_plugin/foo.py"], "medium")):
            brief = _brief(files, risk_tier=tier)
            client = _Client()
            dispatched = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))
            assert dispatched.get("ok") is not False, dispatched
            assert len(client.created) == 1
            assert pf.preflight(brief, state_dir=tmp_path)["ok"] is True


def _last_json_line(capsys, exactly_one=False):
    lines = capsys.readouterr().out.strip().splitlines()
    if exactly_one:
        assert len(lines) == 1, lines
    return json.loads(lines[-1])


class TestPreflightCli:
    def test_exit_codes(self, tmp_path, capsys):
        pf = _preflight_mod()
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_brief(["docs/a.md"], risk_tier="low")))
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(_brief(["bundlebees_plugin/foo.py"], risk_tier="low")))
        junk = tmp_path / "junk.json"
        junk.write_text("{not json")
        missing = tmp_path / "missing.json"

        assert pf.main([str(good), "--state-dir", str(tmp_path / "s")]) == 0
        assert _last_json_line(capsys, exactly_one=True)["ok"] is True

        assert pf.main([str(bad), "--state-dir", str(tmp_path / "s")]) == 3
        assert _last_json_line(capsys, exactly_one=True)["code"] == "risk_tier_below_floor"

        assert pf.main([str(junk)]) == 2
        assert _last_json_line(capsys)["ok"] is False
        assert pf.main([str(missing)]) == 2
        assert _last_json_line(capsys)["ok"] is False

    def test_hermes_subcommand_registered(self):
        from bundlebees_plugin import cli
        parser = argparse.ArgumentParser()
        cli._build_parser(parser)
        args = parser.parse_args(["preflight", "brief.json"])
        assert args.command == "preflight"
