"""RED for the owner decision (Kyle t1650u): modules with no production caller leave the plugin package.

AC-001: trial_harness and trial_simulated_driver live in contrib/trial/, not bundlebees_plugin/.
AC-002: risk_policy, rework_metrics, rails_owner_bridge, hive_docket and wave_compat live in
        contrib/unwired/, not bundlebees_plugin/.
AC-003: contrib/unwired/README.md and contrib/trial/README.md name every moved module and why.
AC-004: no module under bundlebees_plugin/ imports a moved module.
AC-005: each moved module still imports from its new home (its own tests keep running).
verify_workers and contract_gate are NOT moved (decided separately).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRIAL = ("trial_harness", "trial_simulated_driver")
UNWIRED = ("risk_policy", "rework_metrics", "rails_owner_bridge", "hive_docket", "wave_compat")
MOVED = {**{m: "trial" for m in TRIAL}, **{m: "unwired" for m in UNWIRED}}


@pytest.mark.parametrize("module,home", sorted(MOVED.items()))
def test_module_left_the_plugin_package(module, home):
    assert not (ROOT / "bundlebees_plugin" / f"{module}.py").exists()
    assert (ROOT / "contrib" / home / f"{module}.py").is_file()


@pytest.mark.parametrize("home,modules", [("trial", TRIAL), ("unwired", UNWIRED)])
def test_contrib_readme_names_every_module(home, modules):
    text = (ROOT / "contrib" / home / "README.md").read_text(encoding="utf-8")
    for module in modules:
        assert f"`{module}`" in text


def test_verify_workers_and_contract_gate_stay():
    assert (ROOT / "bundlebees_plugin" / "verify_workers.py").is_file()
    assert (ROOT / "bundlebees_plugin" / "contract_gate.py").is_file()


def test_no_production_module_imports_a_moved_module():
    names = "|".join(MOVED)
    pattern = re.compile(rf"^\s*(from\s+\.+\s*import\s+[^\n]*\b({names})\b|from\s+\.({names})\s+import|import\s+bundlebees_plugin\.({names})\b|from\s+bundlebees_plugin\s+import\s+[^\n]*\b({names})\b)", re.M)
    hits = [p.name for p in (ROOT / "bundlebees_plugin").glob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == []


@pytest.mark.parametrize("module,home", sorted(MOVED.items()))
def test_moved_module_imports_from_its_new_home(module, home):
    path = ROOT / "contrib" / home / f"{module}.py"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(f"contrib_{home}_{module}", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
