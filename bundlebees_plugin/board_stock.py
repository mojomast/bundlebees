"""Ready-column stocking flag for the factory (default off).

The factory never invents or self-approves work.  This module only *observes*
two numbers and raises a signal when they disagree:

* how many cards sit in the board's ``ready`` column (the board's own
  approved-and-dispatchable queue, moved there by the owner or the board sync),
* how many bee slots the factory could still use (the shared cap minus the work
  items currently claimed).

When ready work drops below the free slots it writes one durable record -- the
Hive signal -- and, if a coordinator card is configured, a board comment
whenever the low observation changes.  It creates, moves, approves, dispatches
and merges nothing; the only board write it can make is that comment.

Everything is behind ``BUNDLEBEES_BOARD_STOCK=1``.  With the flag off nothing
in this module runs: no board read, no subprocess, no file.

Fail closed: an unreadable board, cap or scheduler raises :class:`StockError`
with a stable code, and no signal is written for an observation that was not
actually made.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import flags
from .security import child_env, scrub

SCHEMA = "rizzstack.board-stock/1"

#: The flag is off unless the shared flag source resolves it to exactly ``1``.
#: Any other value keeps the feature dark rather than guessing what an operator
#: meant. The factory driver and the Hermes gateway read the same file.
FLAG_ENV = flags.FLAG_BOARD_STOCK
#: Optional coordinator card that receives the shortfall comment.
CARD_ENV = "BUNDLEBEES_BOARD_STOCK_CARD"
#: Optional board slug (``hermes kanban --board``); empty means the default board.
BOARD_ENV = "BUNDLEBEES_BOARD_STOCK_BOARD"

SIGNAL_DIRNAME = ("factory", "board-stock")
SIGNAL_FILENAME = "signal.json"
HISTORY_FILENAME = "history.jsonl"

#: The Hive signal is a small, fixed observation; anything larger is a bug.
MAX_READY_CARDS = 10_000
_COMMENT_LIMIT = 500
_KANBAN_TIMEOUT_S = 60
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_HIGH = "stocked"
_LOW = "low"
_SATURATED = "saturated"


class StockError(Exception):
    """The observation could not be made; no signal is written for it.

    ``code`` is a stable refusal name so a caller can surface the reason
    without re-parsing a message.
    """

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)


# ---------------------------------------------------------------------------
# Default readers (replaced by tests)
# ---------------------------------------------------------------------------


def kanban_argv(board: str = "") -> list[str]:
    """The public CLI shape used to read the ready column (never the SQLite file)."""

    argv = ["hermes", "kanban"]
    label = _safe_label(board)
    if label:
        argv.extend(["--board", label])
    argv.extend(["list", "--status", "ready", "--json"])
    return argv


def _safe_label(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SAFE_LABEL_RE.match(text) else ""


def _default_runner(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_KANBAN_TIMEOUT_S,
        env=child_env(),
    )
    if completed.returncode != 0:
        raise OSError(
            f"kanban exited {completed.returncode}: {scrub(completed.stderr or '')}"
        )
    return completed.stdout


def _default_cap_reader() -> int:
    from . import factory_driver

    return factory_driver.read_shared_cap()


def _default_items_reader(state_dir: Any) -> list[dict[str, Any]]:
    from . import work_scheduler

    return list(work_scheduler.list_items(Path(state_dir)))


def _default_comment(card: str, text: str) -> bool:
    try:
        completed = subprocess.run(
            ["hermes", "kanban", "comment", card, text],
            capture_output=True,
            text=True,
            timeout=_KANBAN_TIMEOUT_S,
            env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# ---------------------------------------------------------------------------
# Flags and paths
# ---------------------------------------------------------------------------


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the shared flag source resolves the flag to ``1``."""

    return flags.enabled(flags.FLAG_BOARD_STOCK, environ=env)


def signal_dir(state_dir: Any) -> Path:
    return Path(state_dir).joinpath(*SIGNAL_DIRNAME)


def signal_path(state_dir: Any) -> Path:
    return signal_dir(state_dir) / SIGNAL_FILENAME


def history_path(state_dir: Any) -> Path:
    return signal_dir(state_dir) / HISTORY_FILENAME


# ---------------------------------------------------------------------------
# The observation
# ---------------------------------------------------------------------------


def read_ready_approved(
    *,
    board: str = "",
    runner: Callable[[list[str]], str] | None = None,
) -> int:
    """Count the cards the board itself reports in ``ready``.

    The count comes from the public ``hermes kanban`` CLI.  A payload that is
    not a task list is refused, never guessed at.
    """

    run = runner or _default_runner
    try:
        raw = run(kanban_argv(board))
    except StockError:
        raise
    except Exception as exc:  # noqa: BLE001 - any unreadable board is one refusal
        raise StockError(
            "board_unreadable",
            f"the kanban ready column could not be read: {type(exc).__name__}",
            "Check that the hermes CLI and the board exist; the check never "
            "invents a ready count.",
        ) from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StockError(
            "board_unreadable",
            "the kanban ready column returned no JSON task list",
            "Run `hermes kanban list --status ready --json` by hand.",
        ) from exc
    rows: Any = payload
    if isinstance(payload, Mapping):
        rows = payload.get("tasks")
    if not isinstance(rows, list):
        raise StockError(
            "board_unreadable",
            "the kanban ready payload is not a task list",
            "Run `hermes kanban list --status ready --json` by hand.",
        )
    return min(len(rows), MAX_READY_CARDS)


def free_slots(
    state_dir: Any,
    *,
    cap_reader: Callable[[], int] | None = None,
    items_reader: Callable[[Any], list[dict[str, Any]]] | None = None,
) -> int:
    """The shared bee cap minus the scheduler items currently claimed."""

    read_cap = cap_reader or _default_cap_reader
    try:
        cap = int(read_cap())
    except Exception as exc:  # noqa: BLE001 - an unreadable cap never guesses
        raise StockError(
            "cap_unreadable",
            f"the shared bee cap could not be read: {type(exc).__name__}",
            "Restore grants.GLOBAL_MAX_BEES; stocking is never estimated.",
        ) from exc
    if cap < 1:
        raise StockError(
            "cap_unreadable",
            f"the shared bee cap {cap} is below one",
            "Restore grants.GLOBAL_MAX_BEES; stocking is never estimated.",
        )
    read_items = items_reader or _default_items_reader
    try:
        rows = list(read_items(Path(state_dir)))
    except Exception as exc:  # noqa: BLE001
        raise StockError(
            "scheduler_unreadable",
            f"the work scheduler could not be read: {type(exc).__name__}",
            "Restore coordinator/work.json; the active count is never guessed.",
        ) from exc
    claimed = sum(
        1
        for row in rows
        if isinstance(row, Mapping) and str(row.get("status") or "") == "claimed"
    )
    return max(0, cap - claimed)


def evaluate(*, ready_approved: int, free_slots: int) -> dict[str, Any]:
    """Compare the two observations.  Pure: no reads, no writes."""

    ready = max(0, int(ready_approved))
    free = max(0, int(free_slots))
    if free <= 0:
        return {"state": _SATURATED, "shortfall": 0}
    if ready < free:
        return {"state": _LOW, "shortfall": free - ready}
    return {"state": _HIGH, "shortfall": 0}


def _iso(now: Any = None) -> str:
    if now is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    elif isinstance(now, (int, float)):
        moment = datetime.fromtimestamp(float(now), tz=timezone.utc)
    else:
        return str(now)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(
    state_dir: Any,
    *,
    board: str = "",
    runner: Callable[[list[str]], str] | None = None,
    cap_reader: Callable[[], int] | None = None,
    items_reader: Callable[[Any], list[dict[str, Any]]] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Observe the board and the cap, and return one record (no writes)."""

    label = _safe_label(board) or _safe_label(os.environ.get(BOARD_ENV, ""))
    ready = read_ready_approved(board=label, runner=runner)
    free = free_slots(
        state_dir,
        cap_reader=cap_reader,
        items_reader=items_reader,
    )
    verdict = evaluate(ready_approved=ready, free_slots=free)
    return {
        "schema": SCHEMA,
        "at": _iso(now),
        "state": verdict["state"],
        "ready_approved": ready,
        "free_slots": free,
        "shortfall": int(verdict["shortfall"]),
        "board": label,
    }


# ---------------------------------------------------------------------------
# Durable signal and a comment when the low observation changes
# ---------------------------------------------------------------------------


def _read_signal(state_dir: Any) -> dict[str, Any] | None:
    try:
        body = json.loads(signal_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _write_signal(state_dir: Any, record: Mapping[str, Any]) -> None:
    directory = signal_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = signal_path(state_dir)
    payload = json.dumps(dict(record), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_history(state_dir: Any, record: Mapping[str, Any]) -> None:
    directory = signal_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), sort_keys=True) + "\n"
    with open(history_path(state_dir), "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _unchanged(previous: Mapping[str, Any] | None, record: Mapping[str, Any]) -> bool:
    if previous is None:
        return False
    for key in ("state", "ready_approved", "free_slots", "shortfall", "board"):
        if previous.get(key) != record.get(key):
            return False
    return True


def comment_text(record: Mapping[str, Any]) -> str:
    return (
        f"factory stock: {record.get('ready_approved')} ready card(s), "
        f"{record.get('free_slots')} free bee slot(s); short by {record.get('shortfall')}.\n"
        "Observed only: the factory never creates, moves, approves or dispatches work."
    )[:_COMMENT_LIMIT]


def flag(
    state_dir: Any,
    *,
    card: str = "",
    board: str = "",
    runner: Callable[[list[str]], str] | None = None,
    cap_reader: Callable[[], int] | None = None,
    items_reader: Callable[[Any], list[dict[str, Any]]] | None = None,
    now: Any = None,
    comment: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """Observe, write the durable Hive signal, and comment when a low
    observation changes."""

    record = check(
        state_dir,
        board=board,
        runner=runner,
        cap_reader=cap_reader,
        items_reader=items_reader,
        now=now,
    )
    previous = _read_signal(state_dir)
    changed = not _unchanged(previous, record)
    revision = int((previous or {}).get("revision") or 0) + (1 if changed else 0)
    if revision < 1:
        revision = 1
    record["revision"] = revision
    record["changed"] = changed

    notified = False
    target = _safe_label(card) or _safe_label(os.environ.get(CARD_ENV, ""))
    if changed and record["state"] == _LOW and target:
        post = comment or _default_comment
        notified = bool(post(target, scrub(comment_text(record))))
    record["notified"] = notified

    _write_signal(state_dir, record)
    if changed:
        _append_history(state_dir, record)
    return record


# ---------------------------------------------------------------------------
# CLI (the heartbeat calls this; nothing here runs on import)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m bundlebees_plugin.board_stock",
        description="Observe the ready column and flag a shortfall (flag-gated).",
    )
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--board", default="")
    parser.add_argument("--card", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not enabled():
        print(f"{FLAG_ENV} is off; nothing observed and nothing written")
        return 0
    try:
        record = flag(args.state_dir, card=args.card, board=args.board)
    except StockError as exc:
        print(f"board stock: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(record, indent=2, sort_keys=True))
    else:
        print(
            f"board stock: {record['state']} "
            f"({record['ready_approved']} ready, {record['free_slots']} free, "
            f"short by {record['shortfall']})"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())


__all__ = [
    "BOARD_ENV",
    "CARD_ENV",
    "FLAG_ENV",
    "SCHEMA",
    "StockError",
    "check",
    "comment_text",
    "enabled",
    "evaluate",
    "flag",
    "free_slots",
    "history_path",
    "kanban_argv",
    "main",
    "read_ready_approved",
    "signal_dir",
    "signal_path",
]
