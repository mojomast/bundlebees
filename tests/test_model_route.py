"""Contract tests for docs/contracts/model-route.md (written before the code)."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from bundlebees_plugin import model_route as mr


def brief(files=("docs/notes.md",), tier="low", **extra):
    b = {"project": "workflow", "title": "T x", "branch": "bees/x", "label": "x",
         "risk": {"tier": tier}, "assigned_files": list(files), "model": "go:deepseek-v4.1-flash"}
    b.update(extra)
    return b


class TestSelectWorkerModel:
    def test_simple_brief_uses_default(self):
        r = mr.select_worker_model(brief())
        assert r["model"] == mr.DEFAULT_MODEL == "go:deepseek-v4.1-flash"
        assert r["complex"] is False and r["reasons"] == [] and r["fallback"] is False
        assert r["source"] == "rule"

    def test_high_risk_is_complex(self):
        r = mr.select_worker_model(brief(files=("src/x.py",), tier="high"))
        assert r["model"] == mr.COMPLEX_MODEL == "k3.1-pro"
        assert r["complex"] is True
        assert any(x.startswith("C1") for x in r["reasons"])

    def test_multi_file_threshold(self):
        five = [f"src/m{i}.py" for i in range(5)]
        r = mr.select_worker_model(brief(files=five, tier="medium"))
        assert r["complex"] and any(x.startswith("C2") for x in r["reasons"])
        r4 = mr.select_worker_model(brief(files=five[:4], tier="medium"))
        assert r4["complex"] is False and r4["model"] == mr.DEFAULT_MODEL

    def test_cross_repo(self):
        assert any(x.startswith("C3") for x in mr.select_worker_model(brief(cross_repo=True))["reasons"])
        r = mr.select_worker_model(brief(repos=["hyvv", "bundlebees"]))
        assert r["complex"] and any(x.startswith("C3") for x in r["reasons"])
        assert mr.select_worker_model(brief(repos=["hyvv", "hyvv"]))["complex"] is False

    def test_security_path(self):
        r = mr.select_worker_model(brief(files=("bundlebees_plugin/approvals.py",), tier="low"))
        assert r["complex"] and r["model"] == mr.COMPLEX_MODEL
        assert any(x.startswith("C4") for x in r["reasons"])
        assert any(x.startswith("C1") for x in r["reasons"])

    def test_rails_harness(self):
        r = mr.select_worker_model(brief(track="Rails harness"))
        assert r["complex"] and any(x.startswith("C5") for x in r["reasons"])
        r2 = mr.select_worker_model(brief(files=("harness/rails/p1.py",), tier="medium"))
        assert any(x.startswith("C5") for x in r2["reasons"])
        assert mr.select_worker_model(brief(title="trails harness docs"))["complex"] is False
        assert mr.select_worker_model(brief(title="Rails decision memo"))["complex"] is False

    def test_failed_twice(self):
        assert mr.select_worker_model(brief(), failed_attempts=1)["complex"] is False
        r = mr.select_worker_model(brief(), failed_attempts=2)
        assert r["complex"] and any(x.startswith("C6") for x in r["reasons"])
        assert mr.select_worker_model(brief(failed_attempts=3))["complex"] is True

    def test_complexity_hints(self):
        r = mr.select_worker_model(brief(complexity="complex"))
        assert r["complex"] and any(x.startswith("C7") for x in r["reasons"])
        r2 = mr.select_worker_model(brief(files=("src/x.py",), tier="high", complexity="simple"))
        assert r2["complex"] is True and r2["model"] == mr.COMPLEX_MODEL

    def test_fallback_when_not_allowlisted(self):
        r = mr.select_worker_model(brief(tier="high", files=("src/x.py",)),
                                   allowed_models=["go:deepseek-v4.1-flash"])
        assert r["complex"] is True and r["fallback"] is True
        assert r["model"] == mr.DEFAULT_MODEL
        assert any("not allow-listed" in x for x in r["reasons"])
        ok = mr.select_worker_model(brief(tier="high", files=("src/x.py",)),
                                    allowed_models=["go:deepseek-v4.1-flash", "k3.1-pro"])
        assert ok["model"] == "k3.1-pro" and ok["fallback"] is False

    def test_pinned_model(self):
        r = mr.select_worker_model(brief(tier="high", files=("src/x.py",), model_pinned=True))
        assert r["model"] == "go:deepseek-v4.1-flash" and r["source"] == "pinned"

    def test_invalid_input_fails_closed(self):
        with pytest.raises(ValueError):
            mr.select_worker_model(["not", "a", "brief"])
        with pytest.raises(ValueError):
            mr.select_worker_model(brief(), failed_attempts=-1)
        with pytest.raises(ValueError):
            mr.select_worker_model(brief(), failed_attempts="2")
        with pytest.raises(ValueError):
            mr.select_worker_model(brief(model_pinned=True, model=""))


class TestCli:
    def test_cli_json(self, tmp_path):
        p = tmp_path / "b.json"
        p.write_text(json.dumps(brief(tier="high", files=("src/x.py",))))
        out = subprocess.run([sys.executable, "-m", "bundlebees_plugin.model_route", str(p),
                              "--failed-attempts", "0"], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        d = json.loads(out.stdout)
        assert d["model"] == "k3.1-pro" and d["complex"] is True
        miss = subprocess.run([sys.executable, "-m", "bundlebees_plugin.model_route",
                               str(tmp_path / "nope.json")], capture_output=True, text=True, timeout=60)
        assert miss.returncode == 2
