"""Approved acceptance tests for contract BM2-attempt-counts (review of bundlebees #11, RED first).

``attempts_dispatched`` means unique attempts, not dispatch events: a re-sent or duplicated dispatch event for
the same attempt id counts once. The raw event count is published separately as ``dispatch_events``. A legacy
dispatch event without an attempt id still counts as one attempt (it cannot be de-duplicated) and is reported
in ``dispatch_events_without_attempt_id`` so the number is auditable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from bundlebees_plugin import metrics


def _at(seconds: int) -> dt.datetime:
    return dt.datetime(2026, 11, 12, 12, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)


def _run(state: Path, *events: tuple[str, dict]) -> dict:
    for i, (name, fields) in enumerate(events):
        metrics.append_event(state, "r1", name, now=_at(i), **fields)
    return metrics.summarize(metrics.read_events(state, "r1"))


def test_duplicate_dispatch_events_for_one_attempt_count_once(tmp_path: Path):
    s = _run(tmp_path / "state",
             ("dispatch", {"attempt_id": "att-1"}), ("dispatch", {"attempt_id": "att-1"}),
             ("dispatch", {"attempt_id": "att-2"}), ("verify", {"attempt_id": "att-1", "ok": True}))
    assert s["attempts_dispatched"] == 2
    assert s["dispatch_events"] == 3
    assert s["dispatch_events_without_attempt_id"] == 0


def test_legacy_dispatch_events_without_ids_are_counted_and_disclosed(tmp_path: Path):
    s = _run(tmp_path / "state", ("dispatch", {}), ("dispatch", {"attempt_id": "att-1"}), ("dispatch", {}))
    assert s["attempts_dispatched"] == 3
    assert s["dispatch_events"] == 3
    assert s["dispatch_events_without_attempt_id"] == 2


def test_the_text_summary_shows_attempts_and_events(tmp_path: Path):
    s = _run(tmp_path / "state", ("dispatch", {"attempt_id": "att-1"}), ("dispatch", {"attempt_id": "att-1"}))
    text = metrics.format_summary(s)
    assert "attempts_dispatched: 1" in text and "dispatch_events: 2" in text
