"""Factory-driver service mode: event driven, capped and fail-closed.

The suite is intentionally sandbox portable: it never needs a network, a live
systemd user manager, or a real worker.  A fake tick records dispatch calls and
the gateway wake is replaced by an in-process spy.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bundlebees_plugin import event_consumer
from bundlebees_plugin import factory_driver
from bundlebees_plugin import queue as queue_mod
from bundlebees_plugin import work_scheduler

JOB = "job-service-1"
FILES = ["src/a.py", "tests/test_a.py"]
SOURCE_REV = "a" * 40


@pytest.fixture(autouse=True)
def _offline_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an event-driven pass from ever reaching the network in the sandbox."""

    monkeypatch.delenv("HERMES_API_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class RecordingWorker:
    def __init__(self, action: str = "running") -> None:
        self.action = action
        self.calls: list[str] = []

    def __call__(self, job_id: str) -> dict:
        self.calls.append(job_id)
        return {"status": self.action, "transitions": []}


def _queue_file(root: Path) -> Path:
    path = root / "queue.json"
    path.write_text(json.dumps({}), encoding="utf-8")
    return path


def _event(attempt_id: str, *, event_id: str | None = None, state: str = "landed") -> dict:
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]
    return {
        "specversion": "1.0",
        "id": event_id or f"urn:rizzstack:bee-landed:{digest}",
        "source": "test-service-mode",
        "type": "rizzstack.bee.landed.v1",
        "data": {
            "schema": "rizzstack.bee.landed/1",
            "feature_id": "F-SERVICE",
            "job_id": JOB,
            "task_id": "T1",
            "attempt_id": attempt_id,
            "coordinator": {"identity": "coordinator", "session": "session-1"},
            "backend": {"harness": "fake"},
            "state": state,
            "source_rev": SOURCE_REV,
            "lease": {"sha256": "b" * 64, "assigned_files": list(FILES)},
            "result": {"available": True, "reference": "refs/heads/feat/service"},
        },
    }


def _driver(tmp_path: Path, worker: RecordingWorker, *, subscribers: list[str] | None = None):
    state = tmp_path / "state"
    state.mkdir()
    driver = factory_driver.FactoryDriver(
        state,
        queue_mod.JsonQueue(_queue_file(tmp_path)),
        tick=worker,
        subscribers=subscribers or ["coordinator"],
        startup=False,
    )
    return state, driver


def _add_item(
    state: Path,
    work_id: str,
    *,
    kind: str = "feature",
    lease_files: list[str] | None = None,
    job_id: str = JOB,
    approved: bool | None = True,
    scope: object = None,
) -> None:
    work_scheduler.add_item(
        state,
        work_id=work_id,
        title=f"service item {work_id}",
        kind=kind,
        priority=0,
        lease_files=lease_files if lease_files is not None else ["src/a.py"],
    )
    mapping = state / "factory" / "work-jobs"
    mapping.mkdir(parents=True, exist_ok=True)
    (mapping / f"{work_id}.json").write_text(
        json.dumps({"work_id": work_id, "job_id": job_id}), encoding="utf-8"
    )
    if approved is not None:
        factory_driver.record_dispatch_approval(
            state,
            work_id,
            job_id=job_id,
            scope=list(FILES) if scope is None else scope,
            approved=approved,
        )


def _wake_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(event, **kwargs):
        calls.append(str(event.get("id") or ""))
        return {"event_id": event.get("id"), "delivered": True, "status": "sent"}

    monkeypatch.setattr(factory_driver.hermes_adapter, "notify_coordinator", fake)
    return calls


# ---------------------------------------------------------------------------
# Event-driven wake-up
# ---------------------------------------------------------------------------


def test_inbox_event_triggers_a_pass_before_the_interval_elapses(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    event = _event("att-service-event")
    event_consumer.publish_event(state, event, subscriber="coordinator")

    service = factory_driver.FactoryService(
        driver, subscribers=["coordinator"], interval_s=30.0
    )

    started = time.monotonic()
    result = service.serve(max_passes=1, startup_pass=False)
    elapsed = time.monotonic() - started
    service.close()

    assert service.passes == 1
    assert elapsed < 5.0, f"the pass waited {elapsed:.2f}s instead of waking on the event"
    assert [entry["event_id"] for entry in result["events"]] == [event["id"]]
    assert result["events"][0]["state"] == "acked"


def test_the_watcher_wakes_on_a_new_inbox_file_when_inotify_exists(tmp_path: Path):
    state = tmp_path / "state"
    inbox = state / "events" / "inbox" / "coordinator"
    inbox.mkdir(parents=True)
    watcher = factory_driver.InboxEventWatcher(state, interval_s=0.05)
    if not watcher.uses_inotify:
        watcher.close()
        pytest.skip("inotify is unavailable in this sandbox")

    timer = threading.Timer(
        0.15,
        lambda: (inbox / "new-event.json").write_text("{}", encoding="utf-8"),
    )
    timer.start()
    started = time.monotonic()
    woke = watcher.wait(5.0)
    elapsed = time.monotonic() - started
    timer.cancel()
    watcher.close()

    assert woke is True
    assert elapsed < 3.0


# ---------------------------------------------------------------------------
# Completion consumer drain
# ---------------------------------------------------------------------------


def test_a_landing_drains_the_completion_consumer(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    event_consumer.publish_event(
        state, _event("att-service-completion"), subscriber="coordinator-actions"
    )
    service = factory_driver.FactoryService(
        driver, completion_subscribers=["coordinator-actions"], interval_s=1.0
    )

    result = service.run_pass()
    service.close()

    assert [entry["state"] for entry in result["completions"]] == ["acked"]
    actions = {
        path.name
        for path in (state / "coordinator" / "completion-actions").glob("*.json")
        if not path.name.startswith(".")
    }
    assert "verify:att-service-completion.json" in actions
    assert "process-completion:att-service-completion.json" in actions


# ---------------------------------------------------------------------------
# Shared cap of 12
# ---------------------------------------------------------------------------


def test_a_burst_larger_than_the_shared_cap_dispatches_twelve_and_queues_the_rest(
    tmp_path: Path,
):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    for index in range(13):
        _add_item(
            state,
            f"ws_burst_{index}",
            lease_files=[f"src/module_{index}.py"],
            scope=["src/*.py"],
        )

    service = factory_driver.FactoryService(driver, interval_s=1.0)
    result = service.run_pass()
    service.close()

    assert len(worker.calls) == 12
    assert result["dispatch"]["dispatched"] == 12
    assert result["dispatch"]["blocked"]
    assert result["errors"] == []
    statuses = {item["work_id"]: item["status"] for item in work_scheduler.list_items(state)}
    assert sum(1 for status in statuses.values() if status == "claimed") == 12
    assert sum(1 for status in statuses.values() if status == "pending") == 1


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def _blocked_wake_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    _add_item(state, "ws_gate", lease_files=["src/a.py"], scope=["src/a.py"])
    calls = _wake_spy(monkeypatch)

    if kind == "approval_absent":
        (state / "factory" / "dispatch-approvals" / "ws_gate.json").unlink()
    elif kind == "approval_unreadable":
        (state / "factory" / "dispatch-approvals" / "ws_gate.json").write_text(
            "{ not json", encoding="utf-8"
        )
    elif kind == "scope_unreadable":
        factory_driver.record_dispatch_approval(
            state, "ws_gate", job_id=JOB, scope="not-a-scope-list", approved=True
        )
    elif kind == "cap_unreadable":
        monkeypatch.setattr(
            factory_driver,
            "read_shared_cap",
            lambda *a, **k: (_ for _ in ()).throw(
                factory_driver.FactoryError("cap_unreadable", "cap is not readable")
            ),
        )
    elif kind == "limits_unreadable":
        monkeypatch.setattr(
            work_scheduler,
            "planning_limits",
            lambda: (_ for _ in ()).throw(RuntimeError("limits are not readable")),
        )
    else:  # pragma: no cover - a typo in the parametrization
        raise AssertionError(kind)

    service = factory_driver.FactoryService(driver, interval_s=1.0)
    result = service.run_pass()
    service.close()
    return worker, result, calls


@pytest.mark.parametrize(
    "case",
    [
        "approval_absent",
        "approval_unreadable",
        "scope_unreadable",
        "cap_unreadable",
        "limits_unreadable",
    ],
)
def test_a_failed_cap_approval_or_scope_read_fails_closed_with_one_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
):
    worker, result, calls = _blocked_wake_case(tmp_path, monkeypatch, case)

    assert worker.calls == []
    assert result["dispatch"]["dispatched"] == 0
    assert result["dispatch"]["blocked"]
    assert len(calls) == 1, f"expected one coalesced wake, got {calls!r}"


def test_a_non_preapproved_item_dispatches_nothing_and_wakes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    _add_item(state, "ws_gate", lease_files=["src/a.py"], approved=False)
    calls = _wake_spy(monkeypatch)
    service = factory_driver.FactoryService(driver, interval_s=1.0)

    result = service.run_pass()
    service.close()

    assert worker.calls == []
    assert result["dispatch"]["dispatched"] == 0
    assert result["dispatch"]["blocked"][0]["reason"] == "not_preapproved"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_duplicate_events_for_one_attempt_dispatch_only_once(tmp_path: Path):
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    first = _event("att-service-duplicate", event_id="urn:rizzstack:bee-landed:duplicate-1")
    second = _event("att-service-duplicate", event_id="urn:rizzstack:bee-landed:duplicate-2")
    event_consumer.publish_event(state, first, subscriber="coordinator")
    event_consumer.publish_event(state, second, subscriber="coordinator")
    factory_driver.record_dispatch_approval(
        state,
        factory_driver.work_id_for_event(first),
        job_id=JOB,
        scope=list(FILES),
    )
    service = factory_driver.FactoryService(driver, interval_s=1.0)

    service.run_pass()
    service.run_pass()
    service.close()

    assert len(worker.calls) == 1
    assert len(work_scheduler.list_items(state)) == 1


# ---------------------------------------------------------------------------
# Shipped unit file (no live systemd user manager required)
# ---------------------------------------------------------------------------


def _unit_path() -> Path:
    return Path(__file__).resolve().parents[1] / "contrib" / "systemd" / "bundlebees-factory-driver.service"


def test_the_shipped_unit_file_has_strict_structural_sections():
    path = _unit_path()
    assert path.is_file()

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # keep systemd's case-sensitive keys
    parser.read(path, encoding="utf-8")

    assert "Unit" in parser
    assert "Service" in parser
    assert "Install" in parser
    assert parser.get("Unit", "Description").strip()
    assert parser.get("Service", "Type") == "simple"

    execstart = parser.get("Service", "ExecStart")
    assert execstart.startswith("%h/rizzstack/.venv/bin/python ")
    assert "bundlebees_plugin.factory_driver" in execstart
    assert "--service" in execstart
    assert "--state" in execstart
    assert parser.get("Service", "Restart") == "on-failure"
    assert parser.get("Install", "WantedBy") == "default.target"


def test_the_shipped_unit_passes_dispatch_settings_from_the_environment_file():
    """The unit carries names only; the 0600 env file carries the values.

    Without every one of these the driver used to fall back to the mechanical
    observation tick, which looks healthy while dispatching nothing.
    """

    path = _unit_path()
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")

    environment_file = parser.get("Service", "EnvironmentFile").strip()
    assert environment_file == "%h/.config/bundlebees/factory-driver.env", (
        "a missing environment file must be fatal, not silently ignored"
    )
    execstart = parser.get("Service", "ExecStart")
    for expected in (
        "--launcher-url ${BUNDLEBEES_LAUNCHER_URL}",
        "--repo ${BUNDLEBEES_REPO}",
        "--gitleaks ${BUNDLEBEES_GITLEAKS}",
        "--require-dispatch-config",
    ):
        assert expected in execstart, f"{expected!r} is missing from ExecStart"
    assert "BUNDLEBEES_LAUNCHER_TOKEN" not in path.read_text(encoding="utf-8")
    assert "http://" not in execstart and "https://" not in execstart


def test_the_driver_fails_closed_when_required_dispatch_settings_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    for name in ("BUNDLEBEES_LAUNCHER_URL", "BUNDLEBEES_REPO", "BUNDLEBEES_GITLEAKS", "GITLEAKS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as caught:
        factory_driver.main(
            [
                "--state", str(tmp_path / "state"),
                "--queue", str(tmp_path / "queue.json"),
                "--service",
                "--require-dispatch-config",
            ]
        )

    message = str(caught.value)
    assert caught.value.code not in (0, None)
    for name in ("BUNDLEBEES_LAUNCHER_URL", "BUNDLEBEES_REPO", "BUNDLEBEES_GITLEAKS"):
        assert name in message
    assert "refusing" in message



def _render_unit(tmp_path: Path, interpreter: str) -> Path:
    """Copy the shipped unit into a sandbox home, keeping every shipped directive.

    ``systemd-analyze verify`` refuses a unit whose ``ExecStart`` binary is
    missing, and the shipped unit deliberately names the operator's venv
    (``%h/rizzstack/.venv/bin/python``), which exists on the deployment host and
    not on a CI runner.  The copy expands ``%h`` into ``tmp_path`` and rewrites
    only that interpreter; the structural test above still asserts the shipped
    file names the venv path, so this substitution cannot hide a change there.
    """

    home = tmp_path / "home"
    (home / "rizzstack" / "bundlebees").mkdir(parents=True, exist_ok=True)
    rendered = _unit_path().read_text(encoding="utf-8").replace("%h", str(home))
    shipped_interpreter = f"{home}/rizzstack/.venv/bin/python"
    assert shipped_interpreter in rendered, "the shipped unit does not name its interpreter as a %h-relative path"
    rendered = rendered.replace(shipped_interpreter, interpreter)
    path = tmp_path / "bundlebees-factory-driver.service"
    path.write_text(rendered, encoding="utf-8")
    return path


def _systemd_analyze() -> str:
    found = shutil.which("systemd-analyze")
    if not found:
        pytest.skip("systemd-analyze is not available in this sandbox")
    assert found
    return found


def test_systemd_verify_accepts_the_unit_with_an_interpreter_that_exists(tmp_path: Path):
    systemd_analyze = _systemd_analyze()
    unit = _render_unit(tmp_path, sys.executable)

    completed = subprocess.run(
        [systemd_analyze, "--user", "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_systemd_verify_still_flags_a_missing_interpreter(tmp_path: Path):
    """Guard the portability rewrite: a missing ExecStart binary must still fail."""

    systemd_analyze = _systemd_analyze()
    unit = _render_unit(tmp_path, str(tmp_path / "no-such-python"))

    completed = subprocess.run(
        [systemd_analyze, "--user", "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0, (
        "systemd-analyze no longer rejects a missing ExecStart binary; "
        "the interpreter substitution would then hide real breakage"
    )
    assert "no-such-python" in (completed.stderr or "") + (completed.stdout or "")


def test_the_shipped_unit_turns_the_reconciliation_sweep_on():
    """Kyle, 2026-11-18: the reconciliation sweep is ON in the deploy the cleanup ships.

    The CLI default stays off (opt-in flag); the shipped unit opts in, with the
    1800 s default rate limit, so the backstop runs at most every 30 minutes.
    """
    import shlex

    from bundlebees_plugin import factory_driver

    path = _unit_path()
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    argv = shlex.split(parser.get("Service", "ExecStart"))
    assert "--sweep" in argv, "the shipped unit must pass --sweep"
    module_at = argv.index("bundlebees_plugin.factory_driver")
    args = factory_driver._parser().parse_args(argv[module_at + 1:])
    assert args.sweep is True
    assert args.sweep_interval_s == factory_driver.DEFAULT_SWEEP_INTERVAL_S == 1800
    # The flag alone never turns the sweep on in the library: it stays opt-in.
    assert factory_driver._parser().parse_args(["--state", "state", "--once"]).sweep is False
