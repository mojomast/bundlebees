"""Offline tests for the coordinator-written file-lease record.

The lease authority is the immutable record under ``<state_dir>/leases``. These
tests use only ``tmp_path`` and the local filesystem: no network, Docker, SSH,
or real credentials.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from bundlebees_plugin.leases import (
    LeaseError,
    ignored_worker_claims,
    lease_files,
    load_lease,
    record_lease,
)

SHA40 = "a" * 40
SHA64 = "b" * 64


def _brief(**overrides):
    brief = {
        "objective": "change one file",
        "source_rev": SHA40,
        "branch": "feat/hardening",
        "assigned_files": ["src/a.py"],
        "acceptance": ["tests pass"],
        "tests": ["tests/test_a.py"],
        "run_id": "run-1",
        "wave": 2,
        "retry_of": "att-0",
    }
    brief.update(overrides)
    return brief


def _record(state: Path, attempt_id: str = "att-1", brief=None):
    return record_lease(state, attempt_id, brief or _brief())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_round_trip_preserves_record_and_fields(tmp_path: Path):
    brief = _brief()
    record = record_lease(tmp_path, "att-1", brief, now="2026-01-02T03:04:05Z")

    assert record["version"] == 1
    assert record["attempt_id"] == "att-1"
    assert record["created_at"] == "2026-01-02T03:04:05Z"
    assert record["source_rev"] == SHA40
    assert record["branch"] == "feat/hardening"
    assert record["assigned_files"] == ["src/a.py"]
    assert record["acceptance"] == ["tests pass"]
    assert record["tests"] == ["tests/test_a.py"]
    assert record["run_id"] == "run-1"
    assert record["wave"] == 2
    assert record["retry_of"] == "att-0"
    assert record["brief"] == brief
    assert len(record["brief_sha256"]) == 64
    assert len(record["mac"]) == 64

    loaded = load_lease(tmp_path, "att-1")
    assert loaded == record
    assert lease_files(loaded) == ["src/a.py"]


def test_defaults_and_64_char_source_rev(tmp_path: Path):
    record = record_lease(
        tmp_path,
        "att-2",
        {
            "source_rev": SHA64,
            "assigned_files": ["src/b.py"],
            "acceptance": "ok",
        },
    )

    assert record["branch"] is None
    assert record["tests"] == []
    assert record["run_id"] == "default"
    assert record["wave"] is None
    assert record["retry_of"] is None
    assert load_lease(tmp_path, "att-2") == record


def test_record_file_is_read_only_and_leases_dir_is_private(tmp_path: Path):
    _record(tmp_path)

    leases_dir = tmp_path / "leases"
    record_path = leases_dir / "att-1.json"
    assert record_path.is_file()
    assert _mode(leases_dir) == 0o700
    assert _mode(record_path) == 0o400


def test_lease_key_is_private_and_stable(tmp_path: Path):
    _record(tmp_path, "att-1")
    key_path = tmp_path / "lease.key"
    assert key_path.is_file()
    assert _mode(key_path) == 0o600
    first = key_path.read_bytes()
    assert len(first) == 32

    _record(tmp_path, "att-2")
    assert key_path.read_bytes() == first


def test_second_record_for_same_id_refuses_and_keeps_bytes(tmp_path: Path):
    _record(tmp_path)
    record_path = tmp_path / "leases" / "att-1.json"
    before = record_path.read_bytes()

    with pytest.raises(LeaseError) as excinfo:
        _record(tmp_path)
    assert excinfo.value.code == "lease_exists"
    assert record_path.read_bytes() == before


def test_editing_assigned_files_is_detected(tmp_path: Path):
    _record(tmp_path)
    record_path = tmp_path / "leases" / "att-1.json"
    record_path.chmod(0o600)
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data["assigned_files"] = ["src/a.py", "src/evil.py"]
    record_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(LeaseError) as excinfo:
        load_lease(tmp_path, "att-1")
    assert excinfo.value.code == "lease_tampered"


def test_editing_brief_only_is_detected(tmp_path: Path):
    _record(tmp_path)
    record_path = tmp_path / "leases" / "att-1.json"
    record_path.chmod(0o600)
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data["brief"]["assigned_files"] = ["src/evil.py"]
    record_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(LeaseError) as excinfo:
        load_lease(tmp_path, "att-1")
    assert excinfo.value.code == "lease_tampered"


def test_replacing_record_with_symlink_is_detected(tmp_path: Path):
    _record(tmp_path)
    record_path = tmp_path / "leases" / "att-1.json"
    target = tmp_path / "elsewhere.json"
    target.write_bytes(record_path.read_bytes())
    record_path.unlink()
    record_path.symlink_to(target)

    with pytest.raises(LeaseError) as excinfo:
        load_lease(tmp_path, "att-1")
    assert excinfo.value.code == "lease_tampered"


def test_missing_record_is_reported_with_a_fix(tmp_path: Path):
    with pytest.raises(LeaseError) as excinfo:
        load_lease(tmp_path, "att-1")
    assert excinfo.value.code == "lease_missing"
    text = f"{excinfo.value.error} {excinfo.value.fix}"
    assert "bundlebees_dispatch" in text or "bundlebees lease" in text


@pytest.mark.parametrize("attempt_id", ["../x", "", "a/b", "..", "a..b"])
def test_bad_attempt_ids_are_invalid(tmp_path: Path, attempt_id: str):
    with pytest.raises(LeaseError) as excinfo:
        record_lease(tmp_path, attempt_id, _brief())
    assert excinfo.value.code == "lease_invalid"

    with pytest.raises(LeaseError) as excinfo:
        load_lease(tmp_path, attempt_id)
    assert excinfo.value.code == "lease_invalid"


@pytest.mark.parametrize(
    "brief",
    [
        {"source_rev": "not-a-sha", "assigned_files": ["src/a.py"], "acceptance": "ok"},
        {"source_rev": SHA40, "assigned_files": [], "acceptance": "ok"},
        {"source_rev": SHA40, "assigned_files": ["src/a.py"]},
    ],
)
def test_incomplete_briefs_are_invalid(tmp_path: Path, brief):
    with pytest.raises(LeaseError) as excinfo:
        record_lease(tmp_path, "att-1", brief)
    assert excinfo.value.code == "lease_invalid"


def test_worker_claims_cannot_widen_lease(tmp_path: Path):
    record = _record(tmp_path)
    result = {
        "assigned_files": ["src/a.py", "src/evil.py"],
        "brief": {"assigned_files": ["src/a.py", "src/evil.py"]},
        "observed": {"assigned_files": ["src/evil.py"]},
        "lease": {"assigned_files": ["src/evil.py"]},
    }

    claims = ignored_worker_claims(record, result)

    assert "assigned_files" in claims
    assert "brief.assigned_files" in claims
    assert "observed.assigned_files" in claims
    assert "lease" in claims
    assert lease_files(load_lease(tmp_path, "att-1")) == ["src/a.py"]


def test_matching_worker_claims_are_not_reported(tmp_path: Path):
    record = _record(tmp_path)
    result = {
        "assigned_files": ["src/a.py"],
        "brief": {"assigned_files": ["src/a.py"]},
        "observed": {"assigned_files": ["src/a.py"]},
        "lease": {"assigned_files": ["src/a.py"]},
    }
    assert ignored_worker_claims(record, result) == []


def test_key_never_appears_in_record_or_error_text(tmp_path: Path):
    record = _record(tmp_path)
    key = (tmp_path / "lease.key").read_bytes()
    key_hex = key.hex()

    record_text = json.dumps(record)
    assert key_hex not in record_text
    assert key.hex() not in record_text
    assert key_hex not in json.dumps(load_lease(tmp_path, "att-1"))

    record_path = tmp_path / "leases" / "att-1.json"
    record_path.chmod(0o600)
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data["assigned_files"] = ["src/evil.py"]
    record_path.write_text(json.dumps(data), encoding="utf-8")

    messages: list[str] = []
    try:
        load_lease(tmp_path, "att-1")
    except LeaseError as exc:
        messages.extend([str(exc), exc.error, exc.fix, repr(exc.args)])

    text = "\n".join(messages).lower()
    assert key_hex.lower() not in text
    assert key.hex() not in text


def test_parallel_first_leases_share_one_complete_key(tmp_path: Path, monkeypatch):
    """Regression: concurrent first dispatches must never read a half-written key.

    Seen in the workflow cold-start acceptance on CI: three parallel
    dispatches on a fresh state dir, one failed with "the lease key is not 32
    bytes" because it opened the key file after another thread created it but
    before that thread wrote the bytes.
    """
    import os
    import threading
    import time

    from bundlebees_plugin import leases

    real_urandom = os.urandom

    def slow_urandom(n):
        time.sleep(0.2)
        return real_urandom(n)

    monkeypatch.setattr(leases.os, "urandom", slow_urandom)
    barrier = threading.Barrier(6)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            _record(tmp_path, f"att-{i}")
        except BaseException as exc:  # noqa: BLE001 - collected for the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert [str(e) for e in errors] == []
    assert len(list((tmp_path / "leases").glob("*.json"))) == 6
    key = (tmp_path / "lease.key").read_bytes()
    assert len(key) == 32
    for i in range(6):
        load_lease(tmp_path, f"att-{i}")
