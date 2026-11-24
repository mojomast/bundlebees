"""Attempt binding: task identity on leases, launcher-reported routes (RED-first).

The Hive attempt ledger rendered "Task unknown" and "Route unknown" for every
wave attempt. The wave dispatcher's briefs carry the coordinator's own task
label (``label``) but no ``task_id``, and the route a bee runs on is chosen by
the launcher at claim time and was never written back.

Contracts pinned here:

* A lease records a top-level ``task_id`` (brief ``task_id``, else the brief's
  ``label``) and ``job_id`` (brief ``job_id`` only), both MAC-covered. Absent
  inputs add no key, so older lease shapes stay byte-identical.
* ``attempts`` rows carry ``task_id``. ``job_id`` is never derived from a
  wave or label: a wave is not a job document.
* ``route_sync`` writes one route observation per locally leased attempt from
  the launcher's own listing. The route comes only from an explicit provider
  prefix on the launcher-reported model (``nous:...``); an unprefixed model
  leaves the route unknown -- a route is never inferred from a model name.
* A verified attribution intent still outranks a reported route, and a
  reported model never becomes ``attributed_model``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bundlebees_plugin import leases, route_sync
from bundlebees_plugin.brief import validate_brief
from tests.test_cli_attempts_status import (
    CREATED_A,
    _attribution_intent,
    _ctx,
    _invoke,
)


def _brief(**extra) -> dict:
    brief = {
        "objective": "bind the attempt",
        "source_rev": "b" * 40,
        "branch": "feat/binding",
        "assigned_files": ["src/a.py"],
        "acceptance": {"criteria": ["sample passes"]},
    }
    brief.update(extra)
    return validate_brief(brief)


def _row(state: Path, capsys) -> dict:
    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    return json.loads(capsys.readouterr().out)["attempts"][0]


class FakeLauncher:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def attempts_raw(self):
        self.calls += 1
        return list(self.rows)


# ---------------------------------------------------------------- lease binding


def test_lease_records_the_brief_task_id_and_job_id(tmp_path: Path):
    record = leases.record_lease(
        tmp_path, "att-a", _brief(task_id="t_1234abcd", job_id="job-7", label="wa-a1"), now=CREATED_A
    )
    assert record["task_id"] == "t_1234abcd"
    assert record["job_id"] == "job-7"
    # The MAC covers the new fields: the stored record verifies as written.
    assert leases.load_lease(tmp_path, "att-a")["task_id"] == "t_1234abcd"


def test_lease_falls_back_to_the_coordinator_label_for_task_id(tmp_path: Path):
    record = leases.record_lease(tmp_path, "att-b", _brief(label="wa-a1b", wave="A"), now=CREATED_A)
    assert record["task_id"] == "wa-a1b"
    # A wave is not a job document: no job id is invented from it.
    assert "job_id" not in record


def test_lease_without_identity_keeps_the_old_shape(tmp_path: Path):
    record = leases.record_lease(tmp_path, "att-c", _brief(), now=CREATED_A)
    assert "task_id" not in record
    assert "job_id" not in record


def test_lease_refuses_an_unsafe_task_identity(tmp_path: Path):
    record = leases.record_lease(tmp_path, "att-d", _brief(label="../../etc/passwd"), now=CREATED_A)
    assert "task_id" not in record


def test_a_tampered_task_id_fails_the_lease_mac(tmp_path: Path):
    leases.record_lease(tmp_path, "att-e", _brief(label="wa-a1"), now=CREATED_A)
    path = tmp_path / "leases" / "att-e.json"
    data = json.loads(path.read_text())
    data["task_id"] = "wa-other"
    os.chmod(path, 0o600)
    path.write_text(json.dumps(data))
    with pytest.raises(leases.LeaseError) as err:
        leases.load_lease(tmp_path, "att-e")
    assert err.value.code == "lease_tampered"


# ---------------------------------------------------------------- attempts rows


def test_attempt_row_carries_the_task_id(tmp_path: Path, capsys):
    leases.record_lease(tmp_path, "att-f", _brief(label="wa-a1b", wave="A"), now=CREATED_A)
    row = _row(tmp_path, capsys)
    assert row["task_id"] == "wa-a1b"
    assert row["job_id"] is None


def test_attempt_row_task_id_is_null_without_identity(tmp_path: Path, capsys):
    leases.record_lease(tmp_path, "att-g", _brief(), now=CREATED_A)
    assert _row(tmp_path, capsys)["task_id"] is None


# ---------------------------------------------------------------- route sync


def test_route_sync_records_the_launcher_reported_route(tmp_path: Path, capsys):
    leases.record_lease(tmp_path, "att-h", _brief(label="wa-h"), now=CREATED_A)
    launcher = FakeLauncher([
        {"attempt_id": "att-h", "state": "running", "model": "nous:deepseek-v4.1-flash-1017"},
        {"attempt_id": "att-not-ours", "state": "running", "model": "go:deepseek-v4.1-flash"},
    ])
    result = route_sync.sync(tmp_path, launcher, now="2026-11-24T20:00:00Z")
    assert result == {"seen": 2, "recorded": 1, "unchanged": 0, "skipped": 1}
    route, model = route_sync.read_route(tmp_path, "att-h")
    assert (route, model) == ("nous", "nous:deepseek-v4.1-flash-1017")
    assert route_sync.read_route(tmp_path, "att-not-ours") == (None, None)

    row = _row(tmp_path, capsys)
    assert row["route"] == "nous"
    assert row["model"] == "nous:deepseek-v4.1-flash-1017"
    # Reported, not attributed: the verified-attribution field stays null.
    assert row["attributed_model"] is None


def test_route_sync_never_infers_a_route_from_an_unprefixed_model(tmp_path: Path):
    leases.record_lease(tmp_path, "att-i", _brief(), now=CREATED_A)
    launcher = FakeLauncher([{"attempt_id": "att-i", "state": "running", "model": "deepseek-v4.1-flash"}])
    route_sync.sync(tmp_path, launcher, now="2026-11-24T20:00:00Z")
    route, model = route_sync.read_route(tmp_path, "att-i")
    assert route is None
    assert model == "deepseek-v4.1-flash"


def test_route_sync_is_idempotent(tmp_path: Path):
    leases.record_lease(tmp_path, "att-j", _brief(), now=CREATED_A)
    launcher = FakeLauncher([{"attempt_id": "att-j", "state": "running", "model": "kimi:kimi-k3"}])
    route_sync.sync(tmp_path, launcher, now="2026-11-24T20:00:00Z")
    path = tmp_path / "attempt-routes" / "att-j.json"
    before = path.read_bytes()
    again = route_sync.sync(tmp_path, launcher, now="2026-11-24T20:05:00Z")
    assert again["unchanged"] == 1 and again["recorded"] == 0
    assert path.read_bytes() == before
    assert (path.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "attempt-routes").stat().st_mode & 0o777) == 0o700


def test_a_verified_attribution_outranks_a_reported_route(tmp_path: Path, capsys):
    leases.record_lease(tmp_path, "att-k", _brief(), now=CREATED_A)
    _attribution_intent(tmp_path, "att-k", route="zoo", model="zoo:glm-5.3-flash")
    route_sync.sync(tmp_path, FakeLauncher([{"attempt_id": "att-k", "model": "nous:hy4-flash"}]),
                    now="2026-11-24T20:00:00Z")
    row = _row(tmp_path, capsys)
    assert row["route"] == "zoo"
    assert row["attributed_model"] == "zoo:glm-5.3-flash"


def test_read_route_refuses_a_symlinked_or_mismatched_record(tmp_path: Path):
    store = tmp_path / "attempt-routes"
    store.mkdir(mode=0o700)
    other = tmp_path / "elsewhere.json"
    other.write_text(json.dumps({"schema": route_sync.SCHEMA, "attempt_id": "att-l", "route": "go",
                                 "model": "go:x", "observed_at": "2026-11-24T20:00:00Z", "source": "launcher"}))
    (store / "att-l.json").symlink_to(other)
    assert route_sync.read_route(tmp_path, "att-l") == (None, None)
    (store / "att-m.json").write_text(json.dumps({"schema": route_sync.SCHEMA, "attempt_id": "att-other",
                                                  "route": "go", "model": "go:x"}))
    assert route_sync.read_route(tmp_path, "att-m") == (None, None)
    (store / "att-n.json").write_text(json.dumps({"schema": route_sync.SCHEMA, "attempt_id": "att-n",
                                                  "route": "made-up", "model": "made-up:x"}))
    assert route_sync.read_route(tmp_path, "att-n") == (None, None)


def test_route_sync_refuses_traversal_attempt_ids(tmp_path: Path):
    launcher = FakeLauncher([{"attempt_id": "../leases/x", "model": "go:x"}])
    assert route_sync.sync(tmp_path, launcher, now="2026-11-24T20:00:00Z")["recorded"] == 0
    assert not (tmp_path / "leases").exists() or not any((tmp_path / "leases").iterdir())


def test_route_sync_main_requires_the_launcher_token_from_the_environment(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("BUNDLEBEES_LAUNCHER_TOKEN", raising=False)
    code = route_sync.main(["--state-dir", str(tmp_path), "--launcher-url", "http://127.0.0.1:9"])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": False, "error": {"code": "not_configured"}}
