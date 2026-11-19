"""The documented ``job queue`` commands through the real CLI boundary.

RED before the fix: ``job_cli.add_parsers`` set the queue namespace's
``command`` to ``"queue"`` while ``cli._dispatch`` routes on ``args.command``,
so ``job queue status`` never reached :func:`job_cli.cmd_queue`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from bundlebees_plugin import cli, execution, job_cli


VALID_QUEUE = {
    "schema": "bundlebees.queue/1",
    "generated_at": "2026-11-19T00:00:00Z",
    "features": [
        {"feature_id": "f1", "repo": "owner/one", "job_id": "job_1", "status": "queued"},
        {"feature_id": "f2", "repo": "owner/two", "job_id": "job_2", "status": "running"},
    ],
}

COLLIDING_QUEUE = {
    "features": {
        "f1": {"repo": "owner/one", "job_id": "job_1"},
        " f1 ": {"repo": "owner/two", "job_id": "job_2"},
    }
}


def _parser() -> argparse.ArgumentParser:
    return cli._build_parser(argparse.ArgumentParser())


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(config={}, bundlebees_client=object())


def _write_queue(state_dir: Path, document: dict) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "queue.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _dispatch(argv: list[str], ctx: SimpleNamespace) -> int:
    args = _parser().parse_args(argv)
    return cli._dispatch(ctx, args)


def test_status_and_doctor_parsers_share_the_job_namespace(tmp_path):
    parser = _parser()

    status = parser.parse_args(["job", "queue", "status", "--state-dir", str(tmp_path)])
    assert status.command == "job"
    assert status.job_handler == "queue"
    assert status.queue_handler == "status"
    assert status.state_dir == str(tmp_path)

    doctor = parser.parse_args(["job", "queue", "doctor", "--state-dir", str(tmp_path)])
    assert doctor.command == "job"
    assert doctor.job_handler == "queue"
    assert doctor.queue_handler == "doctor"
    assert doctor.state_dir == str(tmp_path)

    inspect = parser.parse_args(
        ["job", "queue", "inspect", "f1", "--state-dir", str(tmp_path)]
    )
    assert (inspect.command, inspect.job_handler, inspect.queue_handler) == (
        "job",
        "queue",
        "inspect",
    )
    assert inspect.feature_id == "f1"
    assert inspect.state_dir == str(tmp_path)


def test_queue_status_and_doctor_render_through_dispatch(tmp_path, capsys):
    state_dir = tmp_path / "state"
    queue_path = _write_queue(state_dir, VALID_QUEUE)
    original = queue_path.read_bytes()
    tmp_before = sorted(state_dir.rglob("*.tmp"))

    assert _dispatch(
        ["job", "queue", "status", "--state-dir", str(state_dir)], _ctx()
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert [(item["feature_id"], item["status"]) for item in status["items"]] == [
        ("f1", "queued"),
        ("f2", "running"),
    ]

    assert _dispatch(
        ["job", "queue", "doctor", "--state-dir", str(state_dir)], _ctx()
    ) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["items"] == 2
    assert doctor["queue_exists"] is True

    # inspect is read-only too; an unclaimed feature is a typed refusal.
    assert _dispatch(
        ["job", "queue", "inspect", "f1", "--state-dir", str(state_dir)], _ctx()
    ) == 1
    inspect = json.loads(capsys.readouterr().out)
    assert inspect["error"] == "not_claimed"

    assert queue_path.read_bytes() == original
    assert hashlib.sha256(queue_path.read_bytes()).hexdigest() == hashlib.sha256(
        original
    ).hexdigest()
    assert sorted(state_dir.rglob("*.tmp")) == tmp_before


def test_queue_invalid_features_map_is_a_typed_refusal(tmp_path, capsys):
    state_dir = tmp_path / "state"
    queue_path = _write_queue(state_dir, COLLIDING_QUEUE)
    before = queue_path.read_bytes()

    rc = _dispatch(["job", "queue", "status", "--state-dir", str(state_dir)], _ctx())

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "queue_invalid"
    assert payload["error"] != "internal_error"
    assert "'f1'" in payload["message"]
    assert "' f1 '" in payload["message"]
    assert payload["fix"].strip()

    assert queue_path.read_bytes() == before
    assert not list(state_dir.rglob("*.tmp"))


def test_queue_run_without_gitleaks_refuses_before_claiming(tmp_path, capsys, monkeypatch):
    state_dir = tmp_path / "state"
    queue_path = _write_queue(state_dir, VALID_QUEUE)
    before = queue_path.read_bytes()
    monkeypatch.setattr(execution, "scanner", lambda *_args, **_kwargs: None)

    rc = _dispatch(
        [
            "job",
            "queue",
            "run",
            "--once",
            "--repo",
            str(tmp_path),
            "--state-dir",
            str(state_dir),
        ],
        _ctx(),
    )

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "gitleaks_unavailable"
    assert queue_path.read_bytes() == before
    assert not list((state_dir / "queue").glob("*.json"))


def test_non_queue_job_status_is_unchanged(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    job_id = f"bogus-{uuid.uuid4().hex}"

    rc = _dispatch(
        ["job", "status", "--job-id", job_id, "--state-dir", str(state_dir)], _ctx()
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "job_missing"
