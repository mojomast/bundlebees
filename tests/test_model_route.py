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
        # Owner t1660u/t1665u: low-risk slices go to the Nous low lane; a medium source slice
        # that is not complex stays on the default model.
        r = mr.select_worker_model(brief(files=("src/x.py", "src/y.py", "src/z.py"), tier="medium"))
        assert r["model"] == mr.DEFAULT_MODEL == "go:deepseek-v4.1-flash"
        assert r["complex"] is False and r["reasons"] == [] and r["fallback"] is False
        assert r["source"] == "rule" and r["lane"] == "default"

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


class TestLowRiskLane:
    def test_low_risk_docs_brief_uses_the_discounted_flash(self):
        r = mr.select_worker_model(brief())
        assert r["model"] == mr.LOW_RISK_MODEL == "nous:deepseek-v4.1-flash-1017"
        assert r["lane"] == "low" and r["complex"] is False and r["fallback"] is False
        assert r["chain"] == ["nous:deepseek-v4.1-flash-1017", "nous:hy4-flash", "go:deepseek-v4.1-flash"]

    def test_tests_only_brief_is_low(self):
        r = mr.select_worker_model(brief(files=("tests/test_a.py", "tests/test_b.py"), tier="low"))
        assert r["lane"] == "low" and r["model"] == mr.LOW_RISK_MODEL

    def test_verify_brief_is_low(self):
        r = mr.select_worker_model(brief(files=("src/x.py",), tier="medium", verify_of="att_x"))
        assert r["lane"] == "low" and r["model"] == mr.LOW_RISK_MODEL

    def test_harder_low_risk_slices_use_hy4(self):
        four = [f"docs/d{i}.md" for i in range(4)]
        assert mr.select_worker_model(brief(files=four))["model"] == mr.LOW_RISK_HARDER_MODEL == "nous:hy4-flash"
        r = mr.select_worker_model(brief(), failed_attempts=1)
        assert r["lane"] == "low-harder" and r["model"] == mr.LOW_RISK_HARDER_MODEL
        assert r["chain"] == ["nous:hy4-flash", "go:deepseek-v4.1-flash"]
        assert mr.select_worker_model(brief(complexity="moderate"))["model"] == mr.LOW_RISK_HARDER_MODEL

    def test_small_refactor_uses_hy4(self):
        r = mr.select_worker_model(brief(files=("src/x.py", "src/y.py"), tier="medium", complexity="simple"))
        assert r["lane"] == "low-harder" and r["model"] == mr.LOW_RISK_HARDER_MODEL
        r3 = mr.select_worker_model(brief(files=("src/a.py", "src/b.py", "src/c.py"), tier="medium", complexity="simple"))
        assert r3["model"] == mr.DEFAULT_MODEL

    def test_complex_always_beats_the_low_lane(self):
        r = mr.select_worker_model(brief(files=("bundlebees_plugin/approvals.py",), tier="low"))
        assert r["model"] == mr.COMPLEX_MODEL and r["lane"] == "complex"
        assert mr.select_worker_model(brief(), failed_attempts=2)["model"] == mr.COMPLEX_MODEL

    def test_low_lane_falls_back_when_not_allowlisted(self):
        r = mr.select_worker_model(brief(), allowed_models=["go:deepseek-v4.1-flash", "nous:hy4-flash"])
        assert r["model"] == "nous:hy4-flash" and r["fallback"] is True
        assert any("not allow-listed" in x for x in r["reasons"])
        r2 = mr.select_worker_model(brief(), allowed_models=["go:deepseek-v4.1-flash"])
        assert r2["model"] == mr.DEFAULT_MODEL and r2["fallback"] is True

    def test_brief_can_opt_out_of_nous(self):
        r = mr.select_worker_model(brief(nous=False))
        assert r["model"] == mr.DEFAULT_MODEL and r["lane"] == "default"

    def test_nous_pacing_is_documented(self):
        assert mr.NOUS_REQUESTS_PER_MINUTE == 180
        assert 1 <= mr.NOUS_MAX_ACTIVE <= 8


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
