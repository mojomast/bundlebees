"""Approved tests (T1-VWSHADOW): verify workers wired in SHADOW mode only.

bundlebees_plugin/verify_shadow.py is the one production entry into
verify_workers. The in-beat verdict is always authoritative: shadow observes,
records, and never changes, delays into failure, or re-runs the in-beat verdict.

API under test:
  verify_shadow.ENV_MODE == "BUNDLEBEES_VERIFY_WORKERS"
  verify_shadow.mode_from_env(environ) -> "in-beat" | "shadow"   (unset/"" -> "in-beat";
      "on" -> VerifyWorkersError code "mode_not_wired"; anything else -> code "mode_invalid")
  verify_shadow.observe(state_dir, repo, *, mode, request_fields, in_beat_verdict,
                        worker=None, key=None, now=None) -> the SAME in_beat_verdict object
  shadow failures append one JSON line {schema, at, attempt_id, code} to
  <state>/verify-workers/shadow/errors.jsonl and are never raised.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import verify_workers


class _LazyModule:
    """Import the module under test on first use, inside the test body.

    At source_rev the module does not exist yet; importing it at module level
    would make pytest fail collection for this file (and abort the session), so
    the approved test ids would not be collected and RED could not be recorded.
    A deferred import keeps every test collectable and makes each one fail on
    the missing interface instead.
    """

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        import importlib

        return getattr(importlib.import_module(self._name), attr)


verify_shadow = _LazyModule("bundlebees_plugin.verify_shadow")
from bundlebees_plugin.verify_workers import VerifyWorkersError

ROOT = Path(__file__).resolve().parents[1]
ATTEMPT = "att_0123456789abcdef"
FIELDS = {
    "attempt_id": ATTEMPT,
    "source_rev": "a" * 40,
    "workflow_ref": "b" * 40,
    "patch": "diff --git a/x.py b/x.py\n+print('hi')\n",
    "declared_files": ["x.py"],
    "tests": ["tests/test_x.py::TestX::test_x"],
    "suite_command": [],
    "gitleaks": "gitleaks",
}


class TestShadowMode:
    def test_default_is_in_beat_and_writes_nothing(self, tmp_path):
        assert verify_shadow.ENV_MODE == "BUNDLEBEES_VERIFY_WORKERS"
        assert verify_shadow.mode_from_env({}) == "in-beat"
        assert verify_shadow.mode_from_env({"BUNDLEBEES_VERIFY_WORKERS": ""}) == "in-beat"
        verdict = {"ok": True, "evidence": "commit abc verified locally"}
        out = verify_shadow.observe(tmp_path, tmp_path, mode="in-beat", request_fields=FIELDS, in_beat_verdict=verdict)
        assert out is verdict
        assert not (tmp_path / "verify-workers").exists()

    def test_shadow_failure_never_changes_the_verdict(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("worker exploded")
        monkeypatch.setattr(verify_workers, "dispatch_verification", boom)
        verdict = {"ok": True, "evidence": "commit abc verified locally"}
        out = verify_shadow.observe(tmp_path, tmp_path, mode="shadow", request_fields=FIELDS, in_beat_verdict=verdict)
        assert out is verdict
        lines = (tmp_path / "verify-workers" / "shadow" / "errors.jsonl").read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert set(rec) == {"schema", "at", "attempt_id", "code"}
        assert rec["attempt_id"] == ATTEMPT

    def test_shadow_dispatches_with_in_beat_verdict_without_rerunning(self, tmp_path, monkeypatch):
        seen = {}
        def fake(state, repo, request, **kw):
            seen.update(kw, request=request)
            seen["in_beat_result"] = kw["in_beat"]()
            return {"path": "in-beat", "mode": "shadow", "verdict": seen["in_beat_result"], "shadow": {"equal": True}}
        monkeypatch.setattr(verify_workers, "dispatch_verification", fake)
        verdict = {"ok": True, "evidence": "commit abc verified locally"}
        out = verify_shadow.observe(tmp_path, tmp_path, mode="shadow", request_fields=FIELDS, in_beat_verdict=verdict)
        assert out is verdict
        assert seen["mode"] == "shadow"
        assert seen["in_beat_result"] is verdict
        assert seen["request"]["attempt_id"] == ATTEMPT
        assert seen["request"]["patch_sha256"]

    def test_worker_cannot_overturn_a_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(verify_workers, "dispatch_verification",
                            lambda *a, **k: {"path": "on", "mode": "shadow", "verdict": {"ok": True, "evidence": "worker ok"}})
        verdict = {"ok": False, "evidence": "tests failed"}
        out = verify_shadow.observe(tmp_path, tmp_path, mode="shadow", request_fields=FIELDS, in_beat_verdict=verdict)
        assert out is verdict and out["ok"] is False

    def test_missing_worker_config_is_recorded_not_raised(self, tmp_path):
        verdict = {"ok": True, "evidence": "commit abc verified locally"}
        out = verify_shadow.observe(tmp_path, tmp_path, mode="shadow", request_fields=FIELDS, in_beat_verdict=verdict,
                                    worker=None, key=b"k" * 32)
        assert out is verdict
        rec = json.loads((tmp_path / "verify-workers" / "shadow" / "errors.jsonl").read_text().splitlines()[-1])
        assert rec["code"] == "worker_missing"

    def test_on_and_invalid_modes_are_refused(self):
        with pytest.raises(VerifyWorkersError) as on:
            verify_shadow.mode_from_env({"BUNDLEBEES_VERIFY_WORKERS": "on"})
        assert on.value.code == "mode_not_wired"
        with pytest.raises(VerifyWorkersError) as bad:
            verify_shadow.mode_from_env({"BUNDLEBEES_VERIFY_WORKERS": "maybe"})
        assert bad.value.code == "mode_invalid"


class TestJobCliWiring:
    def test_job_cli_routes_the_verdict_through_observe(self):
        src = (ROOT / "bundlebees_plugin" / "job_cli.py").read_text(encoding="utf-8")
        assert "verify_shadow.mode_from_env(" in src
        assert "verify_shadow.observe(" in src
        assert "verify_workers" not in src.replace("verify_workers_", "")
