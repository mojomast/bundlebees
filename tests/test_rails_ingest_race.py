"""Regression: a rewrite during the retry cannot strand an unconsumed record.

#305 chose the resume point from the snapshot the retry later re-read. A
concurrent verify-writer can rewrite the anchor record between those two
enumerations, moving it after the record the ledger never saw; resuming from
the moved anchor then skips that record while the run reports ok. The fix
pins the retry to the anchor's snapshot, so the ordering cannot change.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from bundlebees_plugin import rails_bundlebees_source as bb_source
from bundlebees_plugin import rails_ingest
from rizzstack_rails import ledger as rails_ledger
from rizzstack_rails.importer import Importer


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return state


def _verify(state: Path, attempt: str, order: int) -> Path:
    path = state / "verify-results" / f"{attempt}.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({
        "attempt_id": attempt, "ok": True, "at": "2026-01-02T03:04:05Z",
        "sha": hashlib.sha1(attempt.encode()).hexdigest(),
    }), encoding="utf-8")
    os.utime(path, (order, order))
    return path


def _run(argv, capsys):
    code = rails_ingest.main(argv)
    return code, json.loads(capsys.readouterr().out)


class Legacy(bb_source.BundlebeesSource):
    _identity_cursors = False


class Racing(bb_source.BundlebeesSource):
    """Mutate the snapshot on the retry read, after the anchor is chosen."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def _snap(self):
        self.calls += 1
        if self.calls == 3:
            # A verify-writer moves att-a after the anchor enumeration.
            os.utime(self.state_dir / "verify-results" / "att-a.json", (50, 50))
        return super().events()

    def events(self):
        return self._snap()

    _rows = _snap


def test_a_rewrite_between_anchor_and_retry_does_not_strand_a_record(tmp_path: Path, capsys, monkeypatch):
    state = _state(tmp_path)
    _verify(state, "att-a", 10)
    _verify(state, "att-b", 20)
    db = tmp_path / "ledger.sqlite3"

    source = Legacy(state)
    page = source.read_batch(None, 500, 1 << 20)
    led = rails_ledger.RailsLedger(str(db))
    try:
        Importer(led).commit_batch(source.adapter_id, source.instance, page["records"],
                                   expected_cursor=None, next_cursor=page["next_cursor"])
    finally:
        led.close()

    # Anchor snapshot order: att-b (20), att-a (30), att-c (40).
    _verify(state, "att-a", 30)
    _verify(state, "att-c", 40)

    racing = Racing(state)
    authorities = {"imported_observation", "worker_report", "coordinator_record",
                   "verifier_record", "reviewer_record"}
    monkeypatch.setattr(rails_ingest, "_source", lambda name, state_dir: (racing, authorities))

    code, out = _run(["--ledger", str(db), "--state-dir", str(state),
                      "--source", "bundlebees", "--page-limit", "10"], capsys)
    assert code == 0
    assert out["sources"]["bundlebees"] == {"ok": True, "imported": 1, "has_more": False,
                                            "rebased": True}

    led = rails_ledger.RailsLedger(str(db), read_only=True)
    try:
        attempts = {row["attempt"] for row in led.query(
            "select json_extract(envelope_json,'$.scope.attempt_id') as attempt"
            " from event_rows where json_extract(envelope_json,'$.source.adapter')='bundlebees'"
            " and json_extract(envelope_json,'$.scope.attempt_id') is not null")}
        cursor = led.query("select cursor from source_pages order by page desc limit 1")[0]["cursor"]
    finally:
        led.close()
    assert attempts == {"att-a", "att-b", "att-c"}
    assert cursor.startswith("r1.")
    assert "att-c" not in cursor, "the checkpoint must be the imported record, not the stranded one"

    # A later resume reports zero: the pinned snapshot imported every record.
    code, out = _run(["--ledger", str(db), "--state-dir", str(state),
                      "--source", "bundlebees", "--page-limit", "10"], capsys)
    assert code == 0 and out["sources"]["bundlebees"]["imported"] == 0
