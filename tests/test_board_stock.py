"""Ready-column stocking flag for the factory (M14(e)), off by default.

The check may only *observe* two numbers: approved cards sitting in the board's
ready column and the bee slots the factory could still use.  It raises a durable
signal (the Hive reads it) and, when a low observation changes, one board
comment.  It never creates, moves, approves or dispatches a card.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import beat
from bundlebees_plugin import board_stock
from bundlebees_plugin import factory_driver
from bundlebees_plugin import work_scheduler
from tests.test_factory_service_mode import RecordingWorker, _driver

FLAG = board_stock.FLAG_ENV
CARD = board_stock.CARD_ENV


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cards(count: int) -> list[dict]:
    return [{"id": f"t_ready_{index}", "title": f"approved brief {index}"} for index in range(count)]


def _runner(*, ready: int = 0, calls: list[list[str]] | None = None):
    def run(argv: list[str]) -> str:
        if calls is not None:
            calls.append(list(argv))
        return json.dumps(_cards(ready))

    return run


def _claimed(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    monkeypatch.setattr(
        board_stock,
        "_default_items_reader",
        lambda state_dir: [{"work_id": f"ws_{i}", "status": "claimed"} for i in range(count)],
    )


class CommentSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, card: str, text: str) -> bool:
        self.calls.append((card, text))
        return True


class FakeLauncher:
    def __init__(self, entries: list[dict] | None = None) -> None:
        self.entries = list(entries or [])

    def list_attempts(self) -> list[dict]:
        return list(self.entries)


# ---------------------------------------------------------------------------
# Default off
# ---------------------------------------------------------------------------


def test_the_flag_is_on_only_for_the_exact_value_one():
    assert board_stock.enabled({}) is False
    assert board_stock.enabled({FLAG: ""}) is False
    assert board_stock.enabled({FLAG: "0"}) is False
    assert board_stock.enabled({FLAG: "true"}) is False
    assert board_stock.enabled({FLAG: "yes"}) is False
    assert board_stock.enabled({FLAG: " 1 "}) is True


def test_a_flag_off_beat_reports_no_stock_duty_and_writes_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)

    report = beat.beat(
        tmp_path,
        launcher=FakeLauncher(),
        origin_branches={},
        open_prs={},
    )

    assert "stock_ready_column" not in report["duties"]
    assert not board_stock.signal_dir(tmp_path).exists()


def test_a_flag_off_service_pass_reports_no_stock_and_writes_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], interval_s=30.0)

    result = service.run_pass()
    service.close()

    assert "stock" not in result
    assert not board_stock.signal_dir(state).exists()


# ---------------------------------------------------------------------------
# The observation itself
# ---------------------------------------------------------------------------


def test_a_ready_column_below_the_free_slots_is_low(tmp_path: Path):
    record = board_stock.check(
        tmp_path,
        runner=_runner(ready=2),
        cap_reader=lambda: 8,
        items_reader=lambda state_dir: [{"status": "claimed"}] * 3,
    )

    assert record["schema"] == board_stock.SCHEMA
    assert record["ready_approved"] == 2
    assert record["free_slots"] == 5
    assert record["state"] == "low"
    assert record["shortfall"] == 3


def test_enough_ready_work_is_stocked(tmp_path: Path):
    record = board_stock.check(
        tmp_path,
        runner=_runner(ready=5),
        cap_reader=lambda: 8,
        items_reader=lambda state_dir: [{"status": "claimed"}] * 3,
    )

    assert record["state"] == "stocked"
    assert record["shortfall"] == 0


def test_no_free_slot_means_nothing_to_stock(tmp_path: Path):
    record = board_stock.check(
        tmp_path,
        runner=_runner(ready=0),
        cap_reader=lambda: 4,
        items_reader=lambda state_dir: [{"status": "claimed"}] * 4,
    )

    assert record["free_slots"] == 0
    assert record["state"] == "saturated"
    assert record["shortfall"] == 0


def test_the_board_is_read_through_the_public_kanban_cli():
    assert board_stock.kanban_argv() == [
        "hermes",
        "kanban",
        "list",
        "--status",
        "ready",
        "--json",
    ]
    assert board_stock.kanban_argv("factory") == [
        "hermes",
        "kanban",
        "--board",
        "factory",
        "list",
        "--status",
        "ready",
        "--json",
    ]


def test_an_unreadable_board_refuses_without_writing(tmp_path: Path):
    def broken(argv: list[str]) -> str:
        raise OSError("kanban is not installed")

    with pytest.raises(board_stock.StockError) as raised:
        board_stock.flag(tmp_path, runner=broken, cap_reader=lambda: 8, items_reader=lambda state_dir: [])

    assert raised.value.code == "board_unreadable"
    assert not board_stock.signal_dir(tmp_path).exists()


def test_a_board_payload_that_is_not_a_task_list_refuses(tmp_path: Path):
    with pytest.raises(board_stock.StockError) as raised:
        board_stock.flag(
            tmp_path,
            runner=lambda argv: json.dumps({"ok": True}),
            cap_reader=lambda: 8,
            items_reader=lambda state_dir: [],
        )

    assert raised.value.code == "board_unreadable"


def test_an_unreadable_cap_refuses(tmp_path: Path):
    def broken_cap() -> int:
        raise factory_driver.FactoryError("cap_unreadable", "no cap")

    with pytest.raises(board_stock.StockError) as raised:
        board_stock.flag(
            tmp_path,
            runner=_runner(ready=0),
            cap_reader=broken_cap,
            items_reader=lambda state_dir: [],
        )

    assert raised.value.code == "cap_unreadable"
    assert not board_stock.signal_dir(tmp_path).exists()


# ---------------------------------------------------------------------------
# Durable signal and one comment per shortfall episode
# ---------------------------------------------------------------------------


def test_a_shortfall_writes_one_signal_and_comments_once(tmp_path: Path):
    spy = CommentSpy()
    kwargs = {
        "card": "t_coord",
        "runner": _runner(ready=1),
        "cap_reader": lambda: 6,
        "items_reader": lambda state_dir: [{"status": "claimed"}] * 2,
        "comment": spy,
    }

    first = board_stock.flag(tmp_path, **kwargs)
    second = board_stock.flag(tmp_path, **kwargs)

    assert first["state"] == "low"
    assert first["revision"] == 1
    assert first["changed"] is True
    assert first["notified"] is True
    assert second["revision"] == 1
    assert second["changed"] is False
    assert second["notified"] is False
    assert len(spy.calls) == 1
    card, text = spy.calls[0]
    assert card == "t_coord"
    assert "1 ready" in text
    assert "4 free" in text
    assert "short by 3" in text

    signal = json.loads(board_stock.signal_path(tmp_path).read_text(encoding="utf-8"))
    assert signal["schema"] == board_stock.SCHEMA
    assert signal["state"] == "low"
    assert signal["revision"] == 1
    history = board_stock.history_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(history) == 1


def test_a_recovery_then_a_new_shortfall_comments_again(tmp_path: Path):
    spy = CommentSpy()
    state = {"ready": 1}

    def runner(argv: list[str]) -> str:
        return json.dumps(_cards(state["ready"]))

    kwargs = {
        "card": "t_coord",
        "runner": runner,
        "cap_reader": lambda: 6,
        "items_reader": lambda state_dir: [{"status": "claimed"}] * 2,
        "comment": spy,
    }

    first = board_stock.flag(tmp_path, **kwargs)
    state["ready"] = 5
    recovered = board_stock.flag(tmp_path, **kwargs)
    state["ready"] = 0
    again = board_stock.flag(tmp_path, **kwargs)

    assert (first["revision"], first["notified"]) == (1, True)
    assert (recovered["revision"], recovered["state"], recovered["notified"]) == (2, "stocked", False)
    assert (again["revision"], again["state"], again["notified"]) == (3, "low", True)
    assert len(spy.calls) == 2


def test_a_drifting_low_comments_on_each_change_and_the_docs_say_so(tmp_path: Path):
    spy = CommentSpy()
    state = {"ready": 1}

    def runner(argv: list[str]) -> str:
        return json.dumps(_cards(state["ready"]))

    kwargs = {
        "card": "t_coord",
        "runner": runner,
        "cap_reader": lambda: 6,
        "items_reader": lambda state_dir: [{"status": "claimed"}] * 2,
        "comment": spy,
    }

    first = board_stock.flag(tmp_path, **kwargs)
    state["ready"] = 0
    drifted = board_stock.flag(tmp_path, **kwargs)
    repeated = board_stock.flag(tmp_path, **kwargs)

    assert first["state"] == "low"
    assert (first["changed"], first["notified"]) == (True, True)
    assert drifted["state"] == "low"
    assert (drifted["changed"], drifted["notified"]) == (True, True)
    assert repeated["state"] == "low"
    assert (repeated["changed"], repeated["notified"]) == (False, False)
    assert len(spy.calls) == 2

    docs = (Path(__file__).resolve().parent.parent / "docs" / "board-stock.md").read_text(
        encoding="utf-8"
    )
    assert "On a transition into `low`, and only then" not in docs
    assert "A continuing shortfall does not comment again" not in docs
    assert "when a low observation changes" in docs
    assert "Identical repeats do not comment again" in docs
    assert "starts a new episode" in docs


def test_without_a_card_the_signal_is_still_written(tmp_path: Path):
    record = board_stock.flag(
        tmp_path,
        runner=_runner(ready=0),
        cap_reader=lambda: 4,
        items_reader=lambda state_dir: [],
        comment=CommentSpy(),
    )

    assert record["state"] == "low"
    assert record["notified"] is False
    assert board_stock.signal_path(tmp_path).is_file()


def test_the_signal_carries_no_paths_hosts_or_tokens(tmp_path: Path):
    board_stock.flag(
        tmp_path,
        board="https://private.example.invalid/token=abc",
        runner=_runner(ready=0),
        cap_reader=lambda: 4,
        items_reader=lambda state_dir: [],
    )

    text = board_stock.signal_path(tmp_path).read_text(encoding="utf-8")
    assert "http" not in text
    assert "private.example" not in text
    assert "token" not in text


# ---------------------------------------------------------------------------
# Beat and service wiring (flag on)
# ---------------------------------------------------------------------------


def test_the_beat_runs_the_stock_duty_when_the_flag_is_on(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.delenv(CARD, raising=False)
    monkeypatch.setattr(board_stock, "_default_runner", _runner(ready=0))
    monkeypatch.setattr(board_stock, "_default_cap_reader", lambda: 4)
    monkeypatch.setattr(board_stock, "_default_items_reader", lambda state_dir: [])

    report = beat.beat(
        tmp_path,
        launcher=FakeLauncher(),
        origin_branches={},
        open_prs={},
    )

    duty = report["duties"]["stock_ready_column"]
    assert duty["status"] == "ok"
    assert duty["state"] == "low"
    assert report["status"] == "ok"


def test_an_unreadable_board_does_not_eat_the_beat(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FLAG, "1")

    def broken(argv: list[str]) -> str:
        raise RuntimeError("board unreachable")

    monkeypatch.setattr(board_stock, "_default_runner", broken)

    report = beat.beat(
        tmp_path,
        launcher=FakeLauncher(),
        origin_branches={},
        open_prs={},
    )

    assert report["duties"]["stock_ready_column"]["status"] == "error"
    assert report["duties"]["reconcile_completed_attempts"]["status"] == "ok"
    assert report["status"] == "partial"


def test_the_service_pass_flags_stock_when_the_flag_is_on(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setattr(board_stock, "_default_runner", _runner(ready=1))
    monkeypatch.setattr(board_stock, "_default_cap_reader", lambda: 6)
    monkeypatch.setattr(board_stock, "_default_items_reader", lambda state_dir: [])

    worker = RecordingWorker()
    state, driver = _driver(tmp_path, worker)
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], interval_s=30.0)

    result = service.run_pass()
    service.close()

    assert result["stock"]["state"] == "low"
    assert result["stock"]["ready_approved"] == 1
    assert result["stock"]["free_slots"] == 6
    assert board_stock.signal_path(state).is_file()


def test_the_service_pass_survives_an_unreadable_board(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(FLAG, "1")

    def broken(argv: list[str]) -> str:
        raise RuntimeError("board unreachable")

    monkeypatch.setattr(board_stock, "_default_runner", broken)

    worker = RecordingWorker()
    _, driver = _driver(tmp_path, worker)
    service = factory_driver.FactoryService(driver, subscribers=["coordinator"], interval_s=30.0)

    result = service.run_pass()
    service.close()

    assert result["stock"]["status"] == "error"
    assert result["stock"]["code"] == "board_unreadable"
    assert result["dispatch"]["dispatched"] == 0
