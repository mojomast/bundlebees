"""RED for wave A slice M1: tokens per verified patch, per week.

This is wave A's exit metric. A verified patch is an attempt with a passing
``verify`` event inside the window. Spend includes every attempt dispatched in
the window, including the ones that never verified. Missing usage is ``None``,
never zero, so the rate is ``None`` unless every attempt reported tokens.
"""

from __future__ import annotations

from bundlebees_plugin import metrics

NOW = "2026-11-24T12:00:00Z"


def _ev(state, run, event, at, **fields):
    metrics.append_event(state, run, event, now=at, **fields)


def test_weekly_rate_over_all_runs(tmp_path):
    _ev(tmp_path, "r1", "dispatch", "2026-11-20T10:00:00Z", attempt_id="att_a")
    _ev(tmp_path, "r1", "verify", "2026-11-20T11:00:00Z", attempt_id="att_a", ok=True, tokens=100_000)
    _ev(tmp_path, "r2", "dispatch", "2026-11-21T10:00:00Z", attempt_id="att_b")
    _ev(tmp_path, "r2", "verify", "2026-11-21T11:00:00Z", attempt_id="att_b", ok=False, code="tests_failed", tokens=60_000)
    _ev(tmp_path, "r2", "dispatch", "2026-11-22T10:00:00Z", attempt_id="att_c")
    _ev(tmp_path, "r2", "verify", "2026-11-22T11:00:00Z", attempt_id="att_c", ok=True, tokens=40_000)
    week = metrics.weekly(tmp_path, now=NOW, days=7)
    assert week["window_days"] == 7
    assert week["verified_patches"] == 2
    assert week["attempts"] == 3
    assert week["tokens_total"] == 200_000
    assert week["tokens_per_verified_patch"] == 100_000


def test_events_outside_the_window_are_ignored(tmp_path):
    _ev(tmp_path, "r1", "dispatch", "2026-11-01T10:00:00Z", attempt_id="att_old")
    _ev(tmp_path, "r1", "verify", "2026-11-01T11:00:00Z", attempt_id="att_old", ok=True, tokens=999)
    _ev(tmp_path, "r1", "dispatch", "2026-11-23T10:00:00Z", attempt_id="att_new")
    _ev(tmp_path, "r1", "verify", "2026-11-23T11:00:00Z", attempt_id="att_new", ok=True, tokens=10)
    week = metrics.weekly(tmp_path, now=NOW, days=7)
    assert week["verified_patches"] == 1
    assert week["tokens_total"] == 10


def test_missing_usage_makes_the_rate_unknown_not_zero(tmp_path):
    _ev(tmp_path, "r1", "dispatch", "2026-11-23T10:00:00Z", attempt_id="att_a")
    _ev(tmp_path, "r1", "verify", "2026-11-23T11:00:00Z", attempt_id="att_a", ok=True, tokens=10)
    _ev(tmp_path, "r1", "dispatch", "2026-11-23T12:00:00Z", attempt_id="att_b")
    _ev(tmp_path, "r1", "verify", "2026-11-23T13:00:00Z", attempt_id="att_b", ok=True)
    week = metrics.weekly(tmp_path, now=NOW, days=7)
    assert week["tokens_per_verified_patch"] is None
    assert week["tokens_known_subtotal"] == 10
    assert week["coverage"] == {"attempts_with_tokens": 1, "attempts_without_tokens": 1}


def test_no_verified_patch_gives_none(tmp_path):
    _ev(tmp_path, "r1", "dispatch", "2026-11-23T10:00:00Z", attempt_id="att_a")
    _ev(tmp_path, "r1", "verify", "2026-11-23T11:00:00Z", attempt_id="att_a", ok=False, code="tests_failed", tokens=5)
    week = metrics.weekly(tmp_path, now=NOW, days=7)
    assert week["verified_patches"] == 0
    assert week["tokens_per_verified_patch"] is None


def test_a_reverified_attempt_counts_once(tmp_path):
    _ev(tmp_path, "r1", "dispatch", "2026-11-23T10:00:00Z", attempt_id="att_a")
    _ev(tmp_path, "r1", "verify", "2026-11-23T11:00:00Z", attempt_id="att_a", ok=True, tokens=10)
    _ev(tmp_path, "r1", "verify", "2026-11-23T12:00:00Z", attempt_id="att_a", ok=True, tokens=12)
    week = metrics.weekly(tmp_path, now=NOW, days=7)
    assert week["verified_patches"] == 1
    assert week["tokens_total"] == 12


def test_empty_state(tmp_path):
    week = metrics.weekly(tmp_path, now=NOW, days=7)
    assert week["verified_patches"] == 0 and week["attempts"] == 0
    assert week["tokens_per_verified_patch"] is None
