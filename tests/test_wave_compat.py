"""Exercise the rollout preflight on disposable snapshots only."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contrib.unwired import wave_compat


def _run(capsys, *args):
    rc = wave_compat.main([str(arg) for arg in args])
    output = capsys.readouterr()
    assert not output.err
    return rc, json.loads(output.out)


def _snapshot(directory):
    return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in directory.rglob("*") if p.is_file()}


def test_state_snapshot_mixed_evidence_cli(tmp_path):
    waves = tmp_path / "waves"
    waves.mkdir()
    # Distinct sentinels in both data and out-of-scope state must stay private.
    sentinel = "DO-NOT-PRINT-WAVE-CONTENTS"
    (tmp_path / "job.json").write_text(sentinel)
    (waves / "in-progress.tmp").write_text(sentinel)
    records = {
        "pass": {"suite": {"exit_code": 0, "output_tail": sentinel}},
        "rejected": {"accepted": False},
        "legacy": {},
        "nullsuite": {"suite": None},
        "nocode": {"suite": {}},
        "textsuite": {"suite": sentinel},
        "falsecode": {"suite": {"exit_code": False}},
        "textcode": {"suite": {"exit_code": "0"}},
        "floatcode": {"suite": {"exit_code": 0.0}},
        "nullcode": {"suite": {"exit_code": None}},
        "failed": {"suite": {"exit_code": -1}},
        "timeout": {"suite": {"exit_code": 0, "timed_out": True}},
        "blocked": {"suite": {"exit_code": 0, "blocking": [sentinel]}},
        "ambiguous": {"accepted": "false"},
    }
    for name, fields in records.items():
        (waves / f"{name}.json").write_text(json.dumps({"accepted": True, "wave_id": sentinel, **fields}))
    (waves / "damaged.json").write_text("{" + sentinel)
    (waves / "encoding.json").write_bytes(b"\xff")
    (waves / "array.json").write_text("[]")
    before = _snapshot(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "contrib.unwired.wave_compat", "--state-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 1 and not proc.stderr
    assert sentinel not in proc.stdout and str(tmp_path) not in proc.stdout
    report = json.loads(proc.stdout)
    assert report["status"] == "needs_review" and report["records"] == len(records) + 3
    assert report["counts"] == {
        "suite_compatible": 1, "not_accepted": 1, "missing_suite": 2, "missing_exit_code": 1,
        "malformed_suite": 1, "malformed_exit_code": 4, "nonzero_exit": 1, "suite_timed_out": 1,
        "suite_blocked": 1, "malformed_record": 4,
    }
    assert {f["id"] for f in report["findings"]} == (
        set(records) - {"rejected", "pass"} | {"damaged", "encoding", "array"}
    )
    assert _snapshot(tmp_path) == before


def test_complete_scan_with_no_findings(tmp_path, capsys):
    for records, status, counts in [
        ([], "no_accepted_waves", {}),
        ([{"accepted": False}], "no_accepted_waves", {"not_accepted": 1}),
        ([{"accepted": True, "suite": {"exit_code": 0}}], "suite_compatible", {"suite_compatible": 1}),
    ]:
        for record in records:
            (tmp_path / "record.json").write_text(json.dumps(record))
        assert _run(capsys, "--waves-dir", tmp_path) == (
            0, {"status": status, "records": len(records), "counts": counts, "findings": []},
        )


def test_missing_and_linked_input_do_not_pass_as_empty(tmp_path, capsys):
    rc, report = _run(capsys, "--state-dir", tmp_path)
    assert (rc, report["status"]) == (1, "directory_missing")
    assert not (tmp_path / "waves").exists()
    (tmp_path / "copy").mkdir()
    (tmp_path / "waves").symlink_to(tmp_path / "copy", target_is_directory=True)
    rc, report = _run(capsys, "--state-dir", tmp_path)
    assert (rc, report["status"]) == (1, "directory_unreadable")
    rc, report = _run(capsys, "--waves-dir", f"{tmp_path}/waves/")
    assert (rc, report["status"]) == (1, "directory_unreadable")


def test_special_files_unsafe_names_and_oversize(tmp_path, capsys):
    external = tmp_path / "outside.json"
    external.write_text('{"accepted": true, "suite": {"exit_code": 0}}')
    waves = tmp_path / "waves"
    waves.mkdir()
    (waves / "linked.json").symlink_to(external)
    os.mkfifo(waves / "pipe.json")
    (waves / "bad\nname.json").write_text("{}")
    (waves / ("a1" * 20 + ".json")).write_text("{}")  # Valid id but scrubbed as key-shaped.
    with (waves / "big.json").open("wb") as stream:
        stream.truncate(wave_compat.MAX_RECORD_BYTES + 1)
    rc, report = _run(capsys, "--waves-dir", waves)
    assert (rc, report["status"]) == (1, "needs_review")
    assert report["counts"] == {"unreadable_record": 2, "invalid_record_name": 1,
                                "oversize_record": 1, "malformed_record": 1}
    assert {f["id"] for f in report["findings"]} == {"linked", "pipe", None, "big"}


def test_bounded_findings_do_not_truncate_counts(tmp_path, capsys):
    n = wave_compat.MAX_FINDINGS + 3
    for i in range(n):
        (tmp_path / f"wave{i}.json").write_text('{"accepted":true}')
    rc, report = _run(capsys, "--waves-dir", tmp_path)
    assert rc == 1 and report["counts"] == {"missing_suite": n}
    assert len(report["findings"]) == wave_compat.MAX_FINDINGS


@pytest.mark.parametrize("args", [[], ["--waves-dir", ""], ["--state-dir", "one", "--waves-dir", "two"]])
def test_directory_choice_required(args):
    with pytest.raises(SystemExit) as exc:
        wave_compat.main(args)
    assert exc.value.code == 2


def test_no_safe_directory_scan_is_an_explicit_refusal(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(os, "supports_fd", set())
    rc, report = _run(capsys, "--waves-dir", tmp_path)
    assert (rc, report["status"], report["records"]) == (1, "platform_unsupported", 0)
