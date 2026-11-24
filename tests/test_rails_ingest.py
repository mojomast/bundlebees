"""Standalone Rails ingest runner (RED-first).

``python -m bundlebees_plugin.rails_ingest`` fills one local Rails ledger from
the coordinator's own durable records, a bounded page at a time, until every
named source reports no more pages. It is the scheduled form of
``hermes bundlebees rails-import`` for hosts where the importer runs outside a
Hermes session. It reads the bundlebees state read-only, writes only the
ledger, never prints a path or exception text, and refuses a relative ledger
or an unknown source.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "contrib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "contrib"))

from bundlebees_plugin import bee_events, rails_ingest  # noqa: E402
from rizzstack_rails import ledger as rails_ledger  # noqa: E402


def _land(state: Path, attempt_id: str, transition: str) -> None:
    event = bee_events.build_event(
        {"attempt_id": attempt_id, "state": "succeeded", "transition_id": transition},
        feature_id="BEE-1", job_id=None, task_id="wa-1",
        coordinator={"identity": "coordinator-1", "session": "run-1"},
        backend={"harness": "hermes", "model": "deepseek-v4.1-flash"},
        source_rev="a" * 40, lease={"sha256": "b" * 64, "assigned_files": ["src/a.py"]},
        result={"available": False}, now=1_800_000_000.0,
    )
    bee_events.record_landing(state, event)


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return state


def _run(argv, capsys):
    code = rails_ingest.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_ingest_fills_the_ledger_from_the_outbox_and_is_idempotent(tmp_path: Path, capsys):
    state = _state(tmp_path)
    for n in range(3):
        _land(state, f"att-{n}", f"tr-{n}")
    db = tmp_path / "rails" / "ledger.sqlite3"
    db.parent.mkdir(mode=0o700)
    argv = ["--ledger", str(db), "--state-dir", str(state), "--source", "outbox", "--page-limit", "2"]

    code, out = _run(argv, capsys)
    assert code == 0
    assert out["ok"] is True
    assert out["sources"]["outbox"] == {"ok": True, "imported": 3, "has_more": False}

    code, out = _run(argv, capsys)
    assert code == 0
    assert out["sources"]["outbox"]["imported"] == 0

    led = rails_ledger.RailsLedger(str(db), read_only=True)
    try:
        assert led.head()["seq"] >= 3
    finally:
        led.close()


def test_ingest_reports_each_source_and_fails_without_leaking_paths(tmp_path: Path, capsys):
    state = _state(tmp_path)
    (state / "evidence").mkdir(mode=0o700)
    db = tmp_path / "ledger.sqlite3"
    os.chmod(state, 0o775)  # a shared-writable root is refused by the source
    code, out = _run(["--ledger", str(db), "--state-dir", str(state), "--source", "bundlebees",
                      "--source", "outbox"], capsys)
    assert code == 3
    assert out["ok"] is False
    assert out["sources"]["bundlebees"] == {"ok": False, "error": {"code": "source_unsafe"}}
    assert str(tmp_path) not in json.dumps(out)


def test_ingest_refuses_a_relative_ledger_and_unknown_sources(tmp_path: Path, capsys):
    state = _state(tmp_path)
    code, out = _run(["--ledger", "ledger.sqlite3", "--state-dir", str(state), "--source", "outbox"], capsys)
    assert code == 2 and out == {"ok": False, "error": {"code": "not_configured"}}
    code, out = _run(["--ledger", str(tmp_path / "l.sqlite3"), "--state-dir", str(state),
                      "--source", "launcher"], capsys)
    assert code == 2 and out == {"ok": False, "error": {"code": "invalid_source"}}


def test_a_stray_file_under_evidence_does_not_block_the_bundlebees_source(tmp_path: Path, capsys):
    from bundlebees_plugin import rails_bundlebees_source as source

    state = _state(tmp_path)
    evidence = state / "evidence"
    evidence.mkdir(mode=0o700)
    (evidence / "contract-1").mkdir(mode=0o700)
    (evidence / "report-2026-11-16.bundle").write_bytes(b"not a contract directory")
    # A regular file beside the contract directories is skipped, not a refusal.
    assert source.collect_snapshot(str(state))["records"] == []


def test_a_symlinked_contract_directory_is_still_refused(tmp_path: Path):
    import pytest
    from bundlebees_plugin import rails_bundlebees_source as source

    state = _state(tmp_path)
    evidence = state / "evidence"
    evidence.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.mkdir()
    (evidence / "contract-1").symlink_to(target, target_is_directory=True)
    with pytest.raises(source.SourceError) as err:
        source.collect_snapshot(str(state))
    assert err.value.code == "source_unsafe"
