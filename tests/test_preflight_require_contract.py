"""RED for wave A slice A11: preflight honours ``require_contract``.

Real dispatch passes the ``require_contract`` setting into the gates;
preflight did not, so the two could disagree when the setting is on.
"""

from __future__ import annotations

import argparse
import json

from bundlebees_plugin import preflight, tools
from tests.fakes import FakeCtx, load_plugin

SHA = "b" * 40


def _brief():
    return {
        "objective": "implement", "project": "demo", "source_rev": SHA,
        "assigned_files": ["bundlebees_plugin/foo.py"], "acceptance": ["AC-001 passes"],
        "risk": {"tier": "medium"},
    }


def test_default_is_unchanged(tmp_path):
    assert preflight.preflight(_brief(), state_dir=tmp_path)["ok"] is True


def test_require_contract_refuses_like_dispatch(tmp_path):
    out = preflight.preflight(_brief(), state_dir=tmp_path, require_contract=True)
    gated = tools._gate({"brief": _brief()}, tmp_path, require_contract=True)
    assert isinstance(gated, str)
    assert out["ok"] is False
    assert out["code"] == json.loads(gated)["code"]


def test_module_cli_flag(tmp_path, capsys):
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(_brief()), encoding="utf-8")
    assert preflight.main([str(path), "--state-dir", str(tmp_path), "--require-contract"]) == 3
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_registered_cli_reads_the_setting(tmp_path, capsys):
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(_brief()), encoding="utf-8")
    plugin = load_plugin()
    ctx = FakeCtx(settings={"require_contract": True})
    plugin.register(ctx)
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    code = record["handler_fn"](parser.parse_args(["preflight", str(path), "--state-dir", str(tmp_path)]))
    assert code == 3
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["ok"] is False
