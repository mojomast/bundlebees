"""Offline tests for the append-only, tamper-evident metrics log.

Everything runs against ``tmp_path``; there is no network, subprocess, token, or
private path involved.
"""

from __future__ import annotations

import json
import re
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bundlebees_plugin import metrics

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _log(state: Path, run_id: str = "default") -> Path:
    return state / "metrics" / f"{run_id}.jsonl"


def _append(state: Path, event: str, *, run_id: str = "default", seconds: int = 0, **fields):
    return metrics.append_event(
        state, run_id, event, now=_at(seconds), **fields
    )


def test_append_creates_private_file_and_increments_sequence(tmp_path: Path):
    state = tmp_path / "state"

    first = _append(state, "dispatch", attempt_id="att-1")
    second = _append(state, "verify", attempt_id="att-1", ok=True)

    log = _log(state)
    assert log.exists()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert first["prev_sha256"] == "0" * 64
    assert second["prev_sha256"] != "0" * 64


def test_read_events_verifies_sequence_and_hash_chain(tmp_path: Path):
    state = tmp_path / "state"
    for index in range(4):
        _append(state, "verify", seconds=index, attempt_id=f"att-{index}", ok=True)

    events = metrics.read_events(state, "default")

    assert [event["seq"] for event in events] == [1, 2, 3, 4]
    raw_lines = _log(state).read_bytes().splitlines()
    for previous, event in zip(raw_lines, events[1:]):
        import hashlib

        assert event["prev_sha256"] == hashlib.sha256(previous).hexdigest()
    assert metrics.read_events(state, "missing-run") == []


def test_editing_a_middle_line_is_detected(tmp_path: Path):
    state = tmp_path / "state"
    for index in range(3):
        _append(state, "verify", seconds=index, attempt_id=f"att-{index}", ok=True)

    lines = _log(state).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["ok"] = False
    lines[1] = json.dumps(record, separators=(",", ":"))
    _log(state).write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(metrics.MetricsError) as excinfo:
        metrics.read_events(state, "default")
    assert excinfo.value.code == "metrics_tampered"


def test_deleting_a_line_is_detected(tmp_path: Path):
    state = tmp_path / "state"
    for index in range(3):
        _append(state, "verify", seconds=index, attempt_id=f"att-{index}", ok=True)

    lines = _log(state).read_text(encoding="utf-8").splitlines()
    del lines[1]
    _log(state).write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(metrics.MetricsError) as excinfo:
        metrics.read_events(state, "default")
    assert excinfo.value.code == "metrics_tampered"


@pytest.mark.parametrize(
    "run_id",
    ["", "../escape", "has/slash", " space", "x" * 65, ".hidden"],
)
def test_invalid_run_id_is_rejected(tmp_path: Path, run_id: str):
    with pytest.raises(metrics.MetricsError) as excinfo:
        metrics.append_event(tmp_path, run_id, "dispatch")
    assert excinfo.value.code == "invalid_run_id"


def test_invalid_event_is_rejected(tmp_path: Path):
    with pytest.raises(metrics.MetricsError) as excinfo:
        metrics.append_event(tmp_path, "default", "deploy")
    assert excinfo.value.code == "invalid_event"


def test_invalid_field_is_rejected(tmp_path: Path):
    with pytest.raises(metrics.MetricsError) as excinfo:
        metrics.append_event(tmp_path, "default", "dispatch", surprise="value")
    assert excinfo.value.code == "invalid_field"

    with pytest.raises(metrics.MetricsError) as excinfo:
        metrics.append_event(tmp_path, "default", "dispatch", seq=99)
    assert excinfo.value.code == "invalid_field"


def test_secrets_in_note_are_scrubbed(tmp_path: Path):
    state = tmp_path / "state"
    secret = "abc123secretvalue"

    record = _append(state, "intervention", note=f"token={secret}")

    assert secret not in record["note"]
    assert "[redacted]" in record["note"]
    assert secret not in _log(state).read_text(encoding="utf-8")
    assert metrics.read_events(state, "default")[0]["note"] == record["note"]


def test_identifier_and_sha_fields_are_kept_verbatim(tmp_path: Path):
    state = tmp_path / "state"
    attempt_id = "a" * 31 + "1"
    sha = "abcdef0123456789" * 4

    record = _append(
        state,
        "integrate",
        attempt_id=attempt_id,
        retry_of=attempt_id,
        sha=sha,
        ok=True,
    )

    assert record["attempt_id"] == attempt_id
    assert record["retry_of"] == attempt_id
    assert record["sha"] == sha


def test_list_runs_returns_sorted_run_ids(tmp_path: Path):
    state = tmp_path / "state"
    _append(state, "dispatch", run_id="zeta")
    _append(state, "dispatch", run_id="alpha")
    _append(state, "dispatch", run_id="default")

    assert metrics.list_runs(state) == ["alpha", "default", "zeta"]
    assert metrics.list_runs(tmp_path / "empty") == []


def _build_synthetic_run(state: Path) -> None:
    _append(state, "dispatch", seconds=0, attempt_id="att-1")
    _append(state, "dispatch", seconds=10, attempt_id="att-2")
    _append(state, "dispatch", seconds=20, attempt_id="att-3")
    _append(state, "verify", seconds=60, attempt_id="att-1", ok=True)
    _append(state, "verify", seconds=90, attempt_id="att-2", ok=False, code="tests_failed")
    _append(state, "recovery", seconds=120, retry_of="att-2", note="worker restart")
    _append(state, "verify", seconds=130, attempt_id="att-3", ok=False, code="tests_failed")
    _append(state, "verify", seconds=140, attempt_id="att-3", ok=True, first_pass=False)
    _append(
        state,
        "integrate",
        seconds=200,
        attempt_id="att-3",
        wave_id="w-1",
        ok=True,
        attempts=2,
        tokens=1000,
        cost_usd=0.5,
        regressions=1,
    )
    _append(
        state,
        "integrate",
        seconds=240,
        attempt_id="att-1",
        wave_id="w-2",
        ok=True,
        attempts=1,
        tokens=500,
        cost_usd=0.2,
        regressions=0,
    )
    _append(
        state,
        "integrate",
        seconds=300,
        attempt_id="att-2",
        wave_id="w-3",
        ok=False,
        attempts=0,
        regressions=2,
    )
    _append(state, "intervention", seconds=360, note="review the failed wave")


def test_summarize_has_exact_numbers_for_a_synthetic_run(tmp_path: Path):
    state = tmp_path / "state"
    _build_synthetic_run(state)

    summary = metrics.summarize(metrics.read_events(state, "default"))

    assert set(summary) == {
        "run_id",
        "attempts_dispatched",
        "attempts_verified",
        "first_pass_rate",
        "accepted_changes",
        "tokens_total",
        "tokens_per_accepted",
        "cost_usd_total",
        "cost_per_accepted",
        "human_interventions",
        "plan_to_accepted_s",
        "regressions_found",
        "failed_worker_recoveries",
        "waves_accepted",
        "waves_rejected",
    }
    assert summary["run_id"] == "default"
    assert summary["attempts_dispatched"] == 3
    assert summary["attempts_verified"] == 3
    assert summary["first_pass_rate"] == pytest.approx(1 / 3)
    assert summary["accepted_changes"] == 2
    assert summary["tokens_total"] == 1500
    assert summary["tokens_per_accepted"] == pytest.approx(750.0)
    assert summary["cost_usd_total"] == pytest.approx(0.7)
    assert summary["cost_per_accepted"] == pytest.approx(0.35)
    assert summary["human_interventions"] == 1
    assert summary["plan_to_accepted_s"]["median"] == pytest.approx(210.0)
    assert summary["plan_to_accepted_s"]["max"] == pytest.approx(240.0)
    assert summary["regressions_found"] == 3
    assert summary["failed_worker_recoveries"] == 1
    assert summary["waves_accepted"] == 2
    assert summary["waves_rejected"] == 1


def test_summarize_reports_unknown_per_accepted_rates(tmp_path: Path):
    state = tmp_path / "state"
    _append(state, "dispatch", seconds=0, attempt_id="att-1")
    _append(state, "verify", seconds=10, attempt_id="att-1", ok=True)
    _append(state, "integrate", seconds=20, attempt_id="att-1", wave_id="w-1", ok=True)

    summary = metrics.summarize(metrics.read_events(state, "default"))

    assert summary["accepted_changes"] == 1
    assert summary["tokens_total"] == 0
    assert summary["cost_usd_total"] == 0
    assert summary["tokens_per_accepted"] is None
    assert summary["cost_per_accepted"] is None


def test_summarize_without_data_does_not_divide_by_zero():
    summary = metrics.summarize([])

    assert summary["run_id"] is None
    assert summary["attempts_dispatched"] == 0
    assert summary["attempts_verified"] == 0
    assert summary["first_pass_rate"] is None
    assert summary["accepted_changes"] == 0
    assert summary["tokens_total"] == 0
    assert summary["tokens_per_accepted"] is None
    assert summary["cost_usd_total"] == 0
    assert summary["cost_per_accepted"] is None
    assert summary["human_interventions"] == 0
    assert summary["plan_to_accepted_s"] == {"median": None, "max": None}
    assert summary["regressions_found"] == 0
    assert summary["failed_worker_recoveries"] == 0
    assert summary["waves_accepted"] == 0
    assert summary["waves_rejected"] == 0


def test_format_summary_lists_every_key_and_marks_missing_values():
    summary = metrics.summarize([])
    text = metrics.format_summary(summary)

    for key in summary:
        assert key in text
    assert "n/a" in text
    assert len(text.splitlines()) >= len(summary)


def test_format_summary_renders_a_populated_run(tmp_path: Path):
    state = tmp_path / "state"
    _build_synthetic_run(state)
    text = metrics.format_summary(metrics.summarize(metrics.read_events(state, "default")))

    assert "attempts_dispatched: 3" in text
    assert "accepted_changes: 2" in text
    assert "plan_to_accepted_s:" in text
    assert re.search(r"median=210(?:\.0)?", text)
    assert re.search(r"max=240(?:\.0)?", text)


def test_concurrent_appends_keep_a_valid_chain(tmp_path: Path):
    state = tmp_path / "state"
    workers = 2
    per_worker = 20
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            for item in range(per_worker):
                metrics.append_event(
                    state,
                    "default",
                    "verify",
                    attempt_id=f"att-{index}-{item}",
                    ok=item % 2 == 0,
                )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    events = metrics.read_events(state, "default")
    assert len(events) == workers * per_worker
    assert [event["seq"] for event in events] == list(
        range(1, workers * per_worker + 1)
    )
    assert len({event["attempt_id"] for event in events}) == workers * per_worker


def test_integrate_event_with_an_attempts_list_counts_every_attempt(tmp_path):
    from bundlebees_plugin import metrics as m

    m.append_event(tmp_path, "r", "dispatch", attempt_id="a1", now="2026-11-12T10:00:00Z")
    m.append_event(tmp_path, "r", "dispatch", attempt_id="a2", now="2026-11-12T10:00:10Z")
    m.append_event(tmp_path, "r", "integrate", wave_id="w1", ok=True, attempts=["a1", "a2"], regressions=0,
                   now="2026-11-12T10:01:00Z")
    summary = m.summarize(m.read_events(tmp_path, "r"))
    assert summary["accepted_changes"] == 2
    assert summary["plan_to_accepted_s"] == {"median": 55.0, "max": 60.0}
