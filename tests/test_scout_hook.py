"""RED-first contract for the interim scout -> factory-driver hook.

The 2-minute rizz-scout must be able to trigger exactly one factory-driver pass
when it observes a completed attempt, instead of waiting for the 30-minute
heartbeat.  The hook is deliberately a repository module, not live config: the
live scout script and its cron entry are untouched.

RED-then-GREEN run (recorded here as the reviewable receipt):

    # Before bundlebees_plugin/scout_hook.py and the CLI subcommand exist:
    $ python -m pytest -q tests/test_scout_hook.py
    E   ImportError: cannot import name 'scout_hook' from 'bundlebees_plugin'
    ... ERROR collecting tests/test_scout_hook.py
    ... 1 error in 0.xxs

    # After the hook and CLI subcommand are added:
    $ python -m pytest -q tests/test_scout_hook.py tests/test_factory_cli_contract.py
    ..................                                                       [100%]
    ... 18 passed

The tests use an injected pass runner throughout: no launcher, no network, no
model call, and no token is read.
"""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest

from bundlebees_plugin import cli, scout_hook
from tests.fakes import FakeCtx

COMPLETED = {"attempt_id": "attempt-1001", "state": "succeeded"}
RUNNING = {"attempt_id": "attempt-1002", "state": "running"}


class PassRecorder:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[int] = []
        self.result = {"idle": True} if result is None else result

    def __call__(self) -> dict:
        self.calls.append(len(self.calls) + 1)
        return dict(self.result)


def _write_attempts(path: Path, entries: list[dict]) -> Path:
    path.write_text(json.dumps({"attempts": entries}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# RED: no hook exists on the pre-change revision (see module docstring)
# ---------------------------------------------------------------------------


def test_a_completed_attempt_triggers_exactly_one_pass(tmp_path):
    recorder = PassRecorder()

    result = scout_hook.run_hook(
        tmp_path, [COMPLETED], run_pass=recorder, now=1_800_000_000
    )

    assert recorder.calls == [1]
    assert result["status"] == "pass"
    assert result["passes"] == 1
    assert result["new_attempts"] == [COMPLETED["attempt_id"]]


def test_a_second_run_over_the_same_state_triggers_no_pass(tmp_path):
    recorder = PassRecorder()

    first = scout_hook.run_hook(
        tmp_path, [COMPLETED], run_pass=recorder, now=1_800_000_000
    )
    second = scout_hook.run_hook(
        tmp_path, [COMPLETED], run_pass=recorder, now=1_800_000_060
    )

    assert first["passes"] == 1
    assert second["status"] == "noop"
    assert second["passes"] == 0
    assert recorder.calls == [1]
    # The watermark is durable, not an in-process set.
    watermark = scout_hook.watermark_path(tmp_path)
    assert watermark.is_file()
    text = watermark.read_text(encoding="utf-8")
    assert COMPLETED["attempt_id"] in text


def test_two_overlapping_invocations_coalesce_to_a_single_pass(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    recorder = PassRecorder()

    def slow_pass() -> dict:
        recorder.calls.append(len(recorder.calls) + 1)
        entered.set()
        assert release.wait(5), "first hook pass did not finish in time"
        return {"idle": False}

    first_result: dict = {}
    failure: list[BaseException] = []

    def first_invocation() -> None:
        try:
            first_result.update(
                scout_hook.run_hook(
                    tmp_path, [COMPLETED], run_pass=slow_pass, now=1_800_000_000
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    thread = threading.Thread(target=first_invocation)
    thread.start()
    try:
        assert entered.wait(5), "first hook never reached the pass runner"
        second = scout_hook.run_hook(
            tmp_path, [COMPLETED], run_pass=recorder, now=1_800_000_001
        )
        assert second["status"] == "coalesced"
        assert second["passes"] == 0
        assert recorder.calls == [1]
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert not failure
    assert first_result["passes"] == 1
    assert recorder.calls == [1]


def test_no_completed_attempt_triggers_no_pass(tmp_path):
    recorder = PassRecorder()

    result = scout_hook.run_hook(
        tmp_path, [RUNNING], run_pass=recorder, now=1_800_000_000
    )

    assert result["status"] == "noop"
    assert result["passes"] == 0
    assert recorder.calls == []
    assert not scout_hook.watermark_path(tmp_path).exists()


def test_a_failed_pass_does_not_advance_the_watermark(tmp_path):
    def broken() -> dict:
        raise RuntimeError("factory pass exploded")

    with pytest.raises(scout_hook.ScoutHookError) as excinfo:
        scout_hook.run_hook(
            tmp_path, [COMPLETED], run_pass=broken, now=1_800_000_000
        )

    assert excinfo.value.code == "factory_pass_failed"
    assert not scout_hook.watermark_path(tmp_path).exists()

    recorder = PassRecorder()
    retried = scout_hook.run_hook(
        tmp_path, [COMPLETED], run_pass=recorder, now=1_800_000_060
    )
    assert retried["passes"] == 1
    assert recorder.calls == [1]


def test_completed_states_are_terminal_not_running_or_queued():
    ids = scout_hook.completed_attempt_ids(
        [
            {"attempt_id": "a-running", "state": "running"},
            {"attempt_id": "a-queued", "status": "queued"},
            {"attempt_id": "a-succeeded", "state": "succeeded"},
            {"attempt_id": "a-failed", "status": "failed"},
            {"attempt_id": "a-completed", "state": "completed"},
        ]
    )

    assert ids == ["a-succeeded", "a-failed", "a-completed"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_factory_hook_is_registered_as_a_cli_subcommand():
    parser = argparse.ArgumentParser()
    cli._build_parser(parser)

    args = parser.parse_args(
        ["factory-hook", "--state-dir", "state", "--attempts-file", "attempts.json"]
    )

    assert args.command == "factory-hook"
    assert args.attempts_file == "attempts.json"


def test_cli_factory_hook_exits_zero_and_does_nothing_without_a_completion(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "state"
    state.mkdir()
    attempts = _write_attempts(tmp_path / "attempts.json", [RUNNING])
    calls: list[int] = []
    monkeypatch.setattr(
        cli,
        "_factory_hook_driver_pass",
        lambda *args, **kwargs: calls.append(1) or {"idle": True},
    )

    parser = argparse.ArgumentParser()
    cli._build_parser(parser)
    args = parser.parse_args(
        ["factory-hook", "--state-dir", str(state), "--attempts-file", str(attempts)]
    )

    code = cli._cmd_factory_hook(FakeCtx(settings={}), args)

    assert code == 0
    assert calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "noop"
    assert payload["passes"] == 0


def test_cli_factory_hook_fails_closed_on_a_real_error(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    parser = argparse.ArgumentParser()
    cli._build_parser(parser)
    args = parser.parse_args(
        [
            "factory-hook",
            "--state-dir",
            str(state),
            "--attempts-file",
            str(tmp_path / "missing.json"),
        ]
    )

    code = cli._cmd_factory_hook(FakeCtx(settings={}), args)

    assert code == scout_hook.EXIT_ERROR
    assert "attempts_unreadable" in capsys.readouterr().out


def test_cli_factory_hook_runs_one_pass_for_a_completed_attempt(
    tmp_path, monkeypatch, capsys
):
    state = tmp_path / "state"
    state.mkdir()
    attempts = _write_attempts(tmp_path / "attempts.json", [COMPLETED])
    calls: list[int] = []
    monkeypatch.setattr(
        cli,
        "_factory_hook_driver_pass",
        lambda *args, **kwargs: calls.append(1) or {"idle": False},
    )

    parser = argparse.ArgumentParser()
    cli._build_parser(parser)
    args = parser.parse_args(
        ["factory-hook", "--state-dir", str(state), "--attempts-file", str(attempts)]
    )

    code = cli._cmd_factory_hook(FakeCtx(settings={}), args)

    assert code == 0
    assert calls == [1]
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["passes"] == 1
