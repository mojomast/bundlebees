"""A legacy wave record belongs to one job; a foreign one must not block another.

``waves/w1.json`` is a legacy, pre-job-scoped wave name.  Every job's wave 1
has that name as a legacy alias, so a record left by an unrelated job or a
standalone integration used to refuse *every* later job's wave-1 recovery.
This module pins the ownership rule: only evidence that can belong to this job
(a sealed identity naming it, or a shared attempt) requires reconciliation;
provably foreign evidence is ignored, and unreadable or empty evidence still
fails closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job as ij, job as jobs


def _job(job_id: str = "job_x", *, attempts: tuple[str, ...] = (), wave: str = "w1") -> dict:
    return {
        "job_id": job_id,
        "goal": {"base_rev": "0" * 40},
        "integration": {"wave": wave, "wave_id": f"{job_id}.{wave}"},
        "tasks": [{"task_id": "T1", "state": "pending", "attempts": list(attempts)}],
    }


def _record(state: Path, name: str, *, attempts: tuple[str, ...]) -> Path:
    path = state / "waves" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "accepted": True,
                "attempts": list(attempts),
                "at": "2026-11-17T23:42:55Z",
                "head_sha": "a" * 40,
                "problems": {},
                "regressions": 0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _sidecar(state: Path, name: str, *, job_id: str) -> Path:
    path = state / "wave-identities" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"identity": {"job_id": job_id}}), encoding="utf-8")
    return path


def test_foreign_legacy_wave_does_not_block_an_unrelated_job(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _record(state, "w1", attempts=("att_canary",))
    job = _job()

    assert jobs.recover_integration(state, job) is False
    assert job["tasks"][0]["state"] == "pending"


def test_own_legacy_wave_still_requires_reconciliation(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _record(state, "w1", attempts=("att_canary",))
    job = _job(attempts=("att_canary",))

    with pytest.raises(jobs.JobError) as refused:
        jobs.recover_integration(state, job)
    assert refused.value.code == "wave_reconciliation_required"
    assert job["tasks"][0]["state"] == "pending"


def test_foreign_identity_sidecar_proves_another_job(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _record(state, "w1", attempts=())
    _sidecar(state, "w1", job_id="job_other")

    assert jobs.recover_integration(state, _job()) is False


def test_unreadable_legacy_evidence_still_fails_closed(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "waves").mkdir()
    (state / "waves" / "w1.json").write_text("{damaged", encoding="utf-8")

    with pytest.raises(jobs.JobError) as refused:
        jobs.recover_integration(state, _job())
    assert refused.value.code == "wave_reconciliation_required"


def test_adopt_legacy_wave_ignores_a_foreign_record(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _record(state, "w1", attempts=("att_canary",))

    assert (
        ij.adopt_legacy_wave(
            state, wave="job_x.w1", legacy="w1", attempts=["att_mine"], job_id="job_x"
        )
        is None
    )


def test_adopt_legacy_wave_still_refuses_its_own_record(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    _record(state, "w1", attempts=("att_mine",))

    with pytest.raises(ij.IntegrateError) as refused:
        ij.adopt_legacy_wave(
            state, wave="job_x.w1", legacy="w1", attempts=["att_mine"], job_id="job_x"
        )
    assert refused.value.code == "wave_reconciliation_required"
