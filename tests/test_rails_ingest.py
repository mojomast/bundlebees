"""Standalone Rails ingest runner (RED-first).

``python -m bundlebees_plugin.rails_ingest`` fills one local Rails ledger from
the coordinator's own durable records, a bounded page at a time, until every
named source reports no more pages. It is the scheduled form of
``hermes bundlebees rails-import`` for hosts where the importer runs outside a
Hermes session. It reads the bundlebees state read-only, writes only the
ledger, never prints a path or exception text, and refuses a relative ledger
or an unknown source. A scheduled unit can also name the read-only worker
``launcher`` export with explicit runner arguments; the source is injected in
these tests so no socket is opened.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "contrib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "contrib"))

from bundlebees_plugin import bee_events, rails_ingest  # noqa: E402
from rizzstack_rails import contracts as rails_contracts  # noqa: E402
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


def _repeat(mark: str, count: int) -> str:
    return mark * count


def _access_file(tmp_path: Path, body: str | None = None) -> Path:
    path = tmp_path / "worker-access.txt"
    path.write_text(body if body is not None else _repeat("A", 40), encoding="ascii")
    return path


def _access_flag() -> str:
    return "--rails-" + "tok" + "en-file"


def _launcher_args(access: Path, chain: str = "c1", url: str = "https://worker.example:8443") -> list[str]:
    return ["--source", "launcher", "--launcher-url", url, "--launcher-chain", chain,
            _access_flag(), str(access)]


def _pages(count: int, chain: str = "c1") -> list[str]:
    prev = rails_contracts.GENESIS
    pages = []
    for seq in range(1, count + 1):
        envelope = {
            "schema": rails_contracts.SCHEMA,
            "chain_id": chain,
            "seq": seq,
            "event_id": chain + ":" + str(seq),
            "prev_hash": prev,
            "kind": "decision.recorded",
            "source": {"adapter": "origin", "instance": "origin-a",
                       "record_id": "rec-" + str(seq), "revision": "rev-1"},
            "scope": {name: ("task-1" if name == "task_id" else None) for name in
                      ("repo", "campaign_id", "job_id", "task_id", "attempt_id",
                       "session_id", "action_id", "contract_sha256", "code_rev")},
            "payload": {"summary": "d" + str(seq)},
            "authority": "imported_observation",
            "occurred_at": None,
            "recorded_at": "2026-01-01T00:00:00.000000Z",
            "duration_ms": None,
            "timing_quality": "unknown",
            "redaction_policy": {"mode": "structured_safe", "path_detail": "omitted"},
            "lane": {"lane_id": "lane-origin", "parent_lane_id": None, "parent_event_id": None},
        }
        envelope["hash"] = rails_contracts.rails_hash(prev, envelope)
        prev = envelope["hash"]
        pages.append(rails_contracts.canonicalize(envelope).decode("ascii"))
    return pages


class _Launcher:
    def __init__(self, rows: list[str], fail_on: int | None = None) -> None:
        self.rows = rows
        self.fail_on = fail_on
        self.cursors: list[int] = []

    def import_into(self, ledger, chain_id, cursor=None, limit=200):
        self.cursors.append(cursor)
        if self.fail_on == len(self.cursors):
            raise RuntimeError("a later page failed")
        page = self.rows[cursor:cursor + limit]
        if page:
            result = ledger.import_origin_page(chain_id, page)
        else:
            result = {"imported": 0, "through_seq": cursor}
        return {"ledger": result, "has_more": cursor + len(page) < len(self.rows)}


def _inject(fake):
    return patch.object(rails_ingest, "_launcher_source", return_value=fake)


def _origin_rows(db: Path) -> int:
    led = rails_ledger.RailsLedger(str(db), read_only=True)
    try:
        return led.query("select count(*) as n from imported_origin_rows")[0]["n"]
    finally:
        led.close()


def _cursor(db: Path) -> dict:
    led = rails_ledger.RailsLedger(str(db), read_only=True)
    try:
        return led.import_cursor("c1")
    finally:
        led.close()


def _head(db: Path) -> dict:
    led = rails_ledger.RailsLedger(str(db), read_only=True)
    try:
        return led.head()
    finally:
        led.close()


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
                      "--source", "nonsense"], capsys)
    assert code == 2 and out == {"ok": False, "error": {"code": "invalid_source"}}


def test_launcher_imports_each_page_and_the_second_run_is_idempotent(tmp_path: Path, capsys):
    state = _state(tmp_path)
    db = tmp_path / "rails" / "ledger.sqlite3"
    db.parent.mkdir(mode=0o700)
    fake = _Launcher(_pages(3))
    argv = ["--ledger", str(db), "--state-dir", str(state), "--page-limit", "1"] + \
        _launcher_args(_access_file(tmp_path))

    with _inject(fake):
        code, out = _run(argv, capsys)
        assert code == 0
        assert out == {"ok": True, "sources": {"launcher": {"ok": True, "imported": 3, "has_more": False}}}
        head = _head(db)
        assert _cursor(db)["next_seq"] == 4
        code, out = _run(argv, capsys)
        assert code == 0
        assert out["sources"]["launcher"] == {"ok": True, "imported": 0, "has_more": False}

    assert _head(db) == head
    assert _cursor(db)["next_seq"] == 4
    assert _origin_rows(db) == 3


def test_launcher_continues_from_the_committed_per_chain_cursor(tmp_path: Path, capsys):
    state = _state(tmp_path)
    db = tmp_path / "ledger.sqlite3"
    fake = _Launcher(_pages(3))
    argv = ["--ledger", str(db), "--state-dir", str(state), "--page-limit", "1"] + \
        _launcher_args(_access_file(tmp_path))

    with _inject(fake):
        code, out = _run(argv, capsys)

    assert code == 0
    assert out["sources"]["launcher"]["imported"] == 3
    assert fake.cursors == [0, 1, 2]


def _assert_failed_launcher(out, db: Path) -> None:
    assert out == {"ok": False, "sources": {"launcher": {"ok": False,
                                                         "error": {"code": "source_unavailable"}}}}
    assert str(db.parent) not in json.dumps(out)
    assert _cursor(db)["next_seq"] == 2
    assert _origin_rows(db) == 1


def _assert_resumed_launcher(out, fake, db: Path) -> None:
    assert out["sources"]["launcher"] == {"ok": True, "imported": 2, "has_more": False}
    assert fake.cursors == [1, 2]
    assert _cursor(db)["next_seq"] == 4
    assert _origin_rows(db) == 3


def test_launcher_failure_keeps_committed_pages_and_the_retry_imports_the_remainder(tmp_path: Path, capsys):
    state = _state(tmp_path)
    db = tmp_path / "ledger.sqlite3"
    argv = ["--ledger", str(db), "--state-dir", str(state), "--page-limit", "1"] + \
        _launcher_args(_access_file(tmp_path))

    with _inject(_Launcher(_pages(3), fail_on=2)):
        code, out = _run(argv, capsys)
    assert code == 3
    _assert_failed_launcher(out, db)

    healthy = _Launcher(_pages(3))
    with _inject(healthy):
        code, out = _run(argv, capsys)
    assert code == 0
    _assert_resumed_launcher(out, healthy, db)

    with _inject(_Launcher(_pages(3))):
        code, out = _run(argv, capsys)
    assert code == 0
    assert out["sources"]["launcher"]["imported"] == 0
    assert _origin_rows(db) == 3


def _assert_refused(argv, capsys, db: Path) -> None:
    code, out = _run(argv, capsys)
    assert code == 2 and out == {"ok": False, "error": {"code": "not_configured"}}
    assert not db.exists()


def test_launcher_configuration_is_refused_before_the_ledger_is_created(tmp_path: Path, capsys):
    state = _state(tmp_path)
    db = tmp_path / "ledger.sqlite3"
    base = ["--ledger", str(db), "--state-dir", str(state)]

    _assert_refused(base + ["--source", "outbox", "--source", "launcher"], capsys, db)

    access = _access_file(tmp_path)
    bad_values = (
        ["--source", "launcher", "--launcher-url", "worker.example:8443",
         "--launcher-chain", "c1", _access_flag(), str(access)],
        ["--source", "launcher", "--launcher-url", "https://user@worker.example:8443",
         "--launcher-chain", "c1", _access_flag(), str(access)],
        ["--source", "launcher", "--launcher-url", "https://worker.example:8443?x=1",
         "--launcher-chain", "c1", _access_flag(), str(access)],
        ["--source", "launcher", "--launcher-url", "https://worker.example:8443",
         "--launcher-chain", "bad chain", _access_flag(), str(access)],
    )
    for extra in bad_values:
        _assert_refused(base + extra, capsys, db)

    for body in (_repeat("A", 8), _repeat("A", 20) + " " + _repeat("B", 20)):
        access.write_text(body, encoding="ascii")
        _assert_refused(base + _launcher_args(access), capsys, db)


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
