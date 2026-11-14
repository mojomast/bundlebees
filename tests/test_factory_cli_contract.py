"""Contract tests for the factory driver command line."""

from __future__ import annotations

import json

import pytest

from bundlebees_plugin import factory_driver


def _parse(*argv: str):
    return factory_driver._parser().parse_args(["--state", "state", *argv])


def test_once_is_a_single_pass_without_an_interval():
    args = _parse("--once")

    assert args.once is True
    assert args.interval is None


def test_resident_mode_requires_an_explicit_interval():
    args = _parse("--interval", "1")

    assert args.once is False
    assert args.interval == 1.0


def test_resident_mode_accepts_the_interval_flag_alone():
    args = _parse("--interval")

    assert args.once is False
    assert args.interval == 1.0


def test_no_mode_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        _parse()

    assert excinfo.value.code == 2


def test_once_and_interval_are_mutually_exclusive():
    with pytest.raises(SystemExit) as excinfo:
        _parse("--once", "--interval", "1")

    assert excinfo.value.code == 2


def test_suite_command_arguments_are_forwarded_unchanged(monkeypatch, tmp_path):
    suite = ["pytest", "tests/test_factory_cli_contract.py"]
    args = _parse("--once", "--suite-command", *suite)
    assert args.suite_command == suite

    captured = {}

    def fake_tick(state, job_id, **kwargs):
        captured["state"] = state
        captured["job_id"] = job_id
        captured["suite_command"] = kwargs["suite_command"]
        return {"status": "idle"}

    monkeypatch.setattr(factory_driver.job_cli, "tick", fake_tick)
    tick = factory_driver.make_job_tick(
        tmp_path,
        client=object(),
        repo=tmp_path,
        suite_command=args.suite_command,
    )

    assert tick("job_a") == {"status": "idle"}
    assert captured["job_id"] == "job_a"
    assert list(captured["suite_command"]) == suite


def test_main_passes_suite_command_to_the_real_tick(monkeypatch, tmp_path):
    suite = ["pytest", "tests"]
    captured = {}

    def fake_make_job_tick(state, *, client, repo, gitleaks=None, suite_command=None, **kwargs):
        captured["state"] = state
        captured["repo"] = repo
        captured["gitleaks"] = gitleaks
        captured["suite_command"] = suite_command
        return lambda job_id: {"status": "idle"}

    class FakeDriver:
        def __init__(self, state, queue, **kwargs):
            captured["driver_state"] = state
            captured["tick"] = kwargs["tick"]

        def run_pass(self):
            return {"idle": True}

    monkeypatch.setenv("BUNDLEBEES_LAUNCHER_TOKEN", "x")
    monkeypatch.setattr(factory_driver, "make_job_tick", fake_make_job_tick)
    monkeypatch.setattr(factory_driver, "FactoryDriver", FakeDriver)

    code = factory_driver.main([
        "--state", str(tmp_path),
        "--launcher-url", "http://127.0.0.1:1",
        "--repo", str(tmp_path),
        "--gitleaks", "/bin/true",
        "--suite-command", *suite,
        "--once",
    ])

    assert code == 0
    assert captured["suite_command"] == suite
    assert captured["driver_state"] == tmp_path


def test_main_once_emits_one_pass(monkeypatch, tmp_path, capsys):
    runs = []

    class FakeDriver:
        def __init__(self, state, queue, **kwargs):
            pass

        def run_pass(self):
            runs.append(True)
            return {"idle": True}

    monkeypatch.setattr(factory_driver, "FactoryDriver", FakeDriver)

    code = factory_driver.main(["--state", str(tmp_path), "--once"])

    assert code == 0
    assert runs == [True]
    assert json.loads(capsys.readouterr().out) == {"idle": True}
