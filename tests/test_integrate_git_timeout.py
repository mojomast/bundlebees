"""RED for wave A slice A8: git calls on the integration path have a timeout.

A hung git (credential prompt, stuck remote, lock) must not hold the
integration step forever. ``integrate_job._git`` passes a bounded timeout and
maps an expiry to a named ``git_timeout`` error.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job


def test_git_runs_with_a_bounded_timeout(monkeypatch, tmp_path):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(integrate_job.subprocess, "run", fake_run)
    integrate_job._git(["status"], Path(tmp_path))
    assert isinstance(seen.get("timeout"), (int, float))
    assert 0 < seen["timeout"] <= 600


def test_default_timeout_is_a_module_constant():
    assert 0 < integrate_job.GIT_TIMEOUT_S <= 600


def test_caller_can_pass_a_longer_timeout_for_push(monkeypatch, tmp_path):
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(integrate_job.subprocess, "run", fake_run)
    integrate_job._git(["push", "origin", "x"], Path(tmp_path), timeout_s=900)
    assert seen["timeout"] == 900


def test_expiry_is_a_named_error(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(integrate_job.subprocess, "run", fake_run)
    with pytest.raises(integrate_job.IntegrateError) as caught:
        integrate_job._git(["fetch", "origin"], Path(tmp_path))
    assert caught.value.code == "git_timeout"
    assert "fetch" in caught.value.message


def test_expiry_is_raised_even_when_check_is_false(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(integrate_job.subprocess, "run", fake_run)
    with pytest.raises(integrate_job.IntegrateError) as caught:
        integrate_job._git(["rev-parse", "HEAD"], Path(tmp_path), check=False)
    assert caught.value.code == "git_timeout"
