"""Offline tests for board_sync (AC-1..AC-5).

Every kanban interaction goes through a fake runner that records ``(argv,
stdin)``; event and pull-request sources are plain fixtures.  No test shells out
to a live board, gh, or Hermes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from bundlebees_plugin import board_sync


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """A tiny in-memory board implementing the verbs the sync may use."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.cards: dict[str, dict[str, Any]] = {}
        self.comments: list[tuple[str, str]] = []
        self.fail = False
        self.on_mutation = None
        self._next = 0

    def _card_by_key(self, key: str) -> dict[str, Any] | None:
        return next((card for card in self.cards.values() if card.get("key") == key), None)

    def __call__(self, argv: list[str], stdin: str | None = None) -> Result:
        argv = [str(part) for part in argv]
        self.calls.append((list(argv), stdin))
        verb = argv[2] if len(argv) > 2 and argv[0] == "hermes" and argv[1] == "kanban" else ""
        if self.fail or (self.fail and verb != "list"):
            return Result(1, "", "simulated outage")
        if verb != "list" and self.on_mutation is not None:
            self.on_mutation()
        if verb == "list":
            return Result(0, json.dumps(list(self.cards.values())), "")
        if verb == "create":
            key = argv[argv.index("--idempotency-key") + 1]
            existing = self._card_by_key(key)
            if existing is not None:
                return Result(0, json.dumps({"id": existing["id"], "status": existing["status"]}), "")
            self._next += 1
            task_id = f"task-{self._next}"
            title = argv[argv.index("--title") + 1]
            self.cards[task_id] = {
                "id": task_id,
                "status": "triage",
                "title": title,
                "body": stdin or "",
                "key": key,
            }
            return Result(0, json.dumps({"id": task_id, "status": "triage"}), "")
        if verb == "comment":
            self.comments.append((argv[3], stdin or ""))
            return Result(0, "{}", "")
        if verb == "claim":
            return self._move(argv[3], "running")
        if verb == "block":
            return self._move(argv[3], "blocked")
        if verb == "unblock":
            return self._move(argv[3], "ready")
        if verb == "request-review":
            return self._move(argv[3], "review")
        if verb == "complete":
            return self._move(argv[3], "done")
        return Result(1, "", f"unsupported verb {verb}")

    def _move(self, task_id: str, status: str) -> Result:
        card = self.cards.get(task_id)
        if card is None:
            return Result(1, "", "unknown task")
        card["status"] = status
        return Result(0, json.dumps({"id": task_id, "status": status}), "")


def _mutations(runner: FakeRunner) -> list[list[str]]:
    return [argv for argv, _stdin in runner.calls if len(argv) > 2 and argv[2] != "list"]


def _verbs(runner: FakeRunner) -> list[str]:
    return [argv[2] for argv in _mutations(runner)]


def _bee(attempt_id: str, state: str, *, title: str | None = None, reason: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"attempt_id": attempt_id, "state": state}
    if title is not None:
        data["title"] = title
    if reason is not None:
        data["reason"] = reason
    return {"type": "rizzstack.bee.landed/1", "data": data}


def _pr(number: int, state: str, *, title: str = "Widget") -> dict[str, Any]:
    return {
        "type": "rizzstack.pull_request/1",
        "owner": "acme",
        "repo": "widget",
        "number": number,
        "state": state,
        "title": title,
    }


def _make_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE cards (id TEXT PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO cards VALUES ('seed', 'todo')")
        connection.commit()
    finally:
        connection.close()
    return path


def _sync(tmp_path: Path, runner: FakeRunner, *, name: str = "state", kanban_db: Path | None = None) -> board_sync.BoardSync:
    return board_sync.BoardSync(
        runner,
        tmp_path / name,
        kanban_db=kanban_db if kanban_db is not None else _make_db(tmp_path / f"{name}-kanban.db"),
    )


def test_ac1_pr_opened_then_merged_moves_review_to_done_once(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync = _sync(tmp_path, runner)
    opened = _pr(7, "opened")

    first = sync.apply(events=[opened])
    assert first["ok"], first
    assert len(runner.cards) == 1
    assert next(iter(runner.cards.values()))["status"] == "review"

    second = sync.apply(events=[opened, _pr(7, "merged")])
    assert second["ok"], second
    assert len(runner.cards) == 1
    assert next(iter(runner.cards.values()))["status"] == "done"
    assert _verbs(runner).count("create") == 1
    assert _verbs(runner).count("complete") == 1


def test_ac2_bee_start_block_land_moves_running_blocked_done(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync = _sync(tmp_path, runner)
    secret_reason = "disk full at https://box.tailnet.ts.net/private token sk-" + "a" * 32
    started = _bee("attempt-1", "started", title="Alpha")
    blocked = _bee("attempt-1", "blocked", reason=secret_reason)
    landed = _bee("attempt-1", "landed", title="Renamed after human edit")

    assert sync.apply(events=[started])["ok"]
    assert next(iter(runner.cards.values()))["status"] == "running"

    summary = sync.apply(events=[started, blocked])
    assert summary["ok"], summary
    assert next(iter(runner.cards.values()))["status"] == "blocked"
    assert runner.comments and "disk full" in runner.comments[0][1]

    final = sync.apply(events=[started, blocked, landed])
    assert final["ok"], final
    assert next(iter(runner.cards.values()))["status"] == "done"
    assert len(runner.cards) == 1
    assert _verbs(runner).count("create") == 1
    assert "unblock" in _verbs(runner)

    rendered = json.dumps([argv for argv, _stdin in runner.calls]) + json.dumps(runner.comments)
    assert "box.tailnet.ts.net" not in rendered
    assert "sk-" + "a" * 32 not in rendered


def test_ac3_replaying_the_same_stream_issues_zero_mutating_commands(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync = _sync(tmp_path, runner)
    stream = [
        _bee("attempt-1", "started", title="Alpha"),
        _bee("attempt-1", "blocked", reason="waiting on input"),
        _bee("attempt-1", "landed"),
    ]
    assert sync.apply(events=stream)["ok"]
    state_path = tmp_path / "state" / "board-sync" / "state.json"
    before_calls = list(runner.calls)
    before_state = state_path.read_bytes()

    summary = sync.apply(events=stream)

    assert summary["ok"], summary
    assert runner.calls == before_calls
    assert state_path.read_bytes() == before_state


def test_ac4_reconcile_recovers_after_a_simulated_outage(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync = _sync(tmp_path, runner)
    started = _bee("attempt-1", "started", title="Alpha")
    blocked = _bee("attempt-1", "blocked", reason="waiting")
    landed = _bee("attempt-1", "landed")

    assert sync.apply(events=[started])["ok"]
    assert next(iter(runner.cards.values()))["status"] == "running"

    runner.fail = True
    outage = sync.reconcile(events=[started, blocked])
    assert not outage["ok"]
    assert outage["errors"]
    assert next(iter(runner.cards.values()))["status"] == "running"

    runner.fail = False
    recovered = sync.reconcile(events=[started, blocked])
    assert recovered["ok"], recovered
    assert next(iter(runner.cards.values()))["status"] == "blocked"

    finished = sync.reconcile(events=[started, blocked, landed])
    assert finished["ok"], finished
    assert next(iter(runner.cards.values()))["status"] == "done"


def test_ac5_backup_runs_before_first_mutation_and_bad_copy_refuses(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync = _sync(tmp_path, runner)
    backup_dir = tmp_path / "state" / "board-sync" / "backups"
    seen_at_mutation: list[bool] = []
    runner.on_mutation = lambda: seen_at_mutation.append(
        backup_dir.is_dir() and any(backup_dir.glob("kanban-*.db"))
    )

    summary = sync.apply(events=[_bee("attempt-1", "started")])
    assert summary["ok"], summary
    assert summary["backup"]
    assert seen_at_mutation and all(seen_at_mutation)
    backup = Path(summary["backup"])
    assert backup.is_file()

    connection = sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

    missing = _sync(tmp_path, FakeRunner(), name="missing", kanban_db=tmp_path / "no-such-kanban.db")
    runner_missing = missing.runner
    refused = missing.apply(events=[_bee("attempt-2", "started")])
    assert refused["refused"] and not refused["ok"]
    assert _mutations(runner_missing) == []
    assert runner_missing.cards == {}

    corrupt_path = tmp_path / "corrupt.db"
    corrupt_path.write_bytes(b"this is not a sqlite database")
    corrupt_runner = FakeRunner()
    corrupt = _sync(tmp_path, corrupt_runner, name="corrupt", kanban_db=corrupt_path)
    bad = corrupt.apply(events=[_bee("attempt-3", "started")])
    assert bad["refused"] and not bad["ok"]
    assert _mutations(corrupt_runner) == []


def test_dry_run_prints_intent_and_changes_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    sync = _sync(tmp_path, runner)

    summary = sync.apply(events=[_pr(9, "opened")], dry_run=True)

    assert summary["dry_run"] and summary["ok"], summary
    assert summary["intended"]
    assert runner.calls == []
    assert not (tmp_path / "state" / "board-sync" / "state.json").exists()


def test_reconcile_skips_cards_it_did_not_create(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.cards["human-1"] = {"id": "human-1", "status": "running", "title": "Human card", "body": ""}
    sync = _sync(tmp_path, runner)

    summary = sync.reconcile(events=[])

    assert summary["ok"], summary
    assert summary["skipped"] and summary["skipped"][0]["reason"] == "unmanaged"
    assert runner.cards["human-1"]["status"] == "running"
    assert _mutations(runner) == []


def test_cli_registers_the_board_sync_subcommand() -> None:
    import argparse

    from bundlebees_plugin import cli

    parser = argparse.ArgumentParser()
    cli._build_parser(parser)
    args = parser.parse_args(["board-sync", "--reconcile", "--dry-run", "--state-dir", "/tmp/state"])
    assert args.command == "board-sync"
    assert args.reconcile and args.dry_run
    assert args.state_dir == "/tmp/state"


def test_state_maps_cover_verify_pending_and_closed_unmerged(tmp_path: Path) -> None:
    assert board_sync.card_key_for_event(_bee("attempt-9", "started")) == "attempt:attempt-9"
    assert board_sync.card_key_for_event(_pr(4, "opened")) == "pr:acme/widget#4"
    assert board_sync.column_for_event(_bee("attempt-9", "verifying")) == "review"
    assert board_sync.column_for_event(_bee("attempt-9", "failed")) == "blocked"
    assert board_sync.column_for_event(_pr(4, "closed")) == "archive"

    runner = FakeRunner()
    sync = _sync(tmp_path, runner)
    summary = sync.apply(events=[_pr(4, "closed")], prs=[{"owner": "acme", "repo": "widget", "number": 4, "state": "closed"}])

    assert summary["ok"], summary
    assert next(iter(runner.cards.values()))["status"] == "blocked"
    assert runner.comments and "closed without merge" in runner.comments[0][1]
    assert json.loads((tmp_path / "state" / "board-sync" / "state.json").read_text())["cards"]["pr:acme/widget#4"]["status"] == "archive"
