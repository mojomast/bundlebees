"""Coordinator-side sync of the Hermes default kanban board.

The board has no archive verb in the supported CLI surface, so a pull request
closed unmerged is recorded as ``archive`` and moved out of the active lanes
with ``block`` plus one scrubbed comment.

The sync reflects durable lifecycle evidence -- bee landing/attempt events and
read-only pull-request state -- onto board columns through the public
``hermes kanban`` CLI only.  It never merges, dispatches, restarts, approves,
or edits the SQLite database.  The single file-level operation is a verified
backup copy of the board database before the first mutation; that copy is
opened read-only.

Identity is the card key, carried both in the card body as a stable marker line
and in ``<plugin data>/board-sync/state.json`` (``key -> task_id + status``).
Titles are presentation only: renaming a card never creates a duplicate.
Replaying an event stream whose desired states already match the applied state
issues no CLI calls at all.

Scheduling (documented here only; nothing installs or modifies a cron)::

    */5 * * * * hermes bundlebees board-sync --reconcile

``--dry-run`` prints the intended commands and changes nothing.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .security import child_env, scrub

SCHEMA = "bundlebees.board-sync/1"

COLUMNS = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done")
ARCHIVE_COLUMN = "archive"
MANAGED_COLUMNS = COLUMNS + (ARCHIVE_COLUMN,)

MARKER_PREFIX = "bundlebees-board-sync:"
STATE_DIRNAME = "board-sync"
STATE_FILENAME = "state.json"
BACKUP_DIRNAME = "backups"

BEE_STATE_COLUMNS = {
    "start": "running",
    "started": "running",
    "running": "running",
    "in_progress": "running",
    "blocked": "blocked",
    "failed": "blocked",
    "failure": "blocked",
    "error": "blocked",
    "verify": "review",
    "verifying": "review",
    "verify_pending": "review",
    "pending_verify": "review",
    "awaiting_review": "review",
    "review": "review",
    "landed": "done",
    "done": "done",
    "completed": "done",
    "complete": "done",
    "archive": ARCHIVE_COLUMN,
    "archived": ARCHIVE_COLUMN,
}
PR_STATE_COLUMNS = {
    "opened": "review",
    "open": "review",
    "reopened": "review",
    "review": "review",
    "merged": "done",
    "closed": ARCHIVE_COLUMN,
}
STATE_MAPS = {"bee": BEE_STATE_COLUMNS, "pr": PR_STATE_COLUMNS}
BEE_COLUMNS = BEE_STATE_COLUMNS
PR_COLUMNS = PR_STATE_COLUMNS

_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_KEY_RE = re.compile(r"^(?:attempt:[A-Za-z0-9][A-Za-z0-9._-]{0,127}|pr:[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}#[0-9]{1,9})$")
_PRIVATE_HOST_RE = re.compile(
    r"(?i)\b(?:[A-Za-z0-9-]+\.)+(?:ts\.net|local|localhost|internal|intranet|lan|home|corp|tailnet)\b"
)

_MAX_TITLE = 100
_MAX_REASON = 300
_MAX_ERROR = 300
_TIMEOUT_S = 120


class BoardSyncError(Exception):
    """A board-sync refusal that the caller may surface, never one that escapes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Refused(Exception):
    pass


class _CommandFailed(Exception):
    pass


CommandRunner = Callable[[Sequence[str], "str | None"], Any]
Source = Callable[[], Iterable[Mapping[str, Any]]]


def default_runner(argv: Sequence[str], stdin: str | None = None) -> Any:
    """Run one ``hermes kanban`` command with the scrubbed child environment."""
    return subprocess.run(
        list(argv),
        input=stdin,
        text=True,
        capture_output=True,
        timeout=_TIMEOUT_S,
        env=child_env(),
    )


def _result_rc(result: Any) -> int:
    if isinstance(result, tuple):
        return int(result[0]) if result else 1
    if isinstance(result, Mapping):
        return int(result.get("returncode", result.get("code", 1)))
    return int(getattr(result, "returncode", getattr(result, "code", 1)))


def _result_text(result: Any, name: str) -> str:
    if isinstance(result, tuple):
        return str(result[1] if name == "stdout" and len(result) > 1 else (result[2] if len(result) > 2 else "") or "")
    if isinstance(result, Mapping):
        return str(result.get(name) or "")
    return str(getattr(result, name, "") or "")


def _safe_error(result: Any) -> str:
    return _scrub_text(_result_text(result, "stderr") or _result_text(result, "stdout"), _MAX_ERROR)


def _scrub_text(value: Any, limit: int = _MAX_REASON) -> str:
    text = value if isinstance(value, str) else str(value or "")
    text = text.replace("\x00", " ")
    text = _PRIVATE_HOST_RE.sub("[redacted-host]", text)
    text = str(scrub(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _data(event: Mapping[str, Any]) -> Mapping[str, Any]:
    body = event.get("data")
    return body if isinstance(body, Mapping) else event


def _type_text(event: Mapping[str, Any]) -> str:
    return str(event.get("type") or event.get("event_type") or event.get("topic") or "").lower()


def _int_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    text = str(value or "").strip().lstrip("#")
    return text if text.isdigit() and int(text) > 0 else None


def _pr_parts(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    data = _data(event)
    number = _int_text(data.get("number") or data.get("pull_request") or data.get("pr_number"))
    repo_value = str(data.get("repo") or data.get("repository") or event.get("repo") or "").strip()
    owner = str(data.get("owner") or data.get("org") or event.get("owner") or "").strip()
    repo = str(data.get("name") or "").strip()
    if "/" in repo_value:
        left, _, right = repo_value.partition("/")
        owner = owner or left
        repo = repo or right
    elif repo_value and not repo:
        repo = repo_value
    type_text = _type_text(event)
    looks_pr = any(token in type_text for token in ("pull_request", "pull-request", "pull request")) or "pr." in type_text or type_text.endswith(".pr")
    if not number or not owner or not repo:
        return None
    if not looks_pr and not (data.get("owner") or data.get("org") or "/" in repo_value or data.get("pull_request")):
        return None
    if not _REPO_PART_RE.fullmatch(owner) or not _REPO_PART_RE.fullmatch(repo):
        raise BoardSyncError("event_unsafe", "pull-request owner/repo is not a safe slug")
    return owner, repo, number


def _pr_key(owner: str, repo: str, number: str) -> str:
    return f"pr:{owner}/{repo}#{number}"


def card_key_for_event(event: Mapping[str, Any]) -> str:
    """Stable identity for a card: ``pr:owner/repo#N`` or ``attempt:<id>``."""
    parts = _pr_parts(event)
    if parts is not None:
        return _pr_key(*parts)
    data = _data(event)
    attempt = data.get("attempt_id") or event.get("attempt_id")
    if attempt is None:
        raise BoardSyncError("event_unkeyed", "event carries neither a pull request nor an attempt id")
    attempt_text = str(attempt).strip()
    if not _ATTEMPT_RE.fullmatch(attempt_text):
        raise BoardSyncError("event_unsafe", "attempt id is not a safe identifier")
    return f"attempt:{attempt_text}"


def _state_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")
    return re.sub(r"_+", "_", text)


def _is_pr_event(event: Mapping[str, Any]) -> bool:
    try:
        return _pr_parts(event) is not None
    except BoardSyncError:
        return True


def column_for_event(event: Mapping[str, Any]) -> str | None:
    """Map one event's lifecycle state to a board column (None if unmapped)."""
    data = _data(event)
    state = _state_token(data.get("state") or event.get("state"))
    if not state and _type_text(event).endswith(".landed"):
        state = "landed"
    if _is_pr_event(event):
        return PR_STATE_COLUMNS.get(state)
    return BEE_STATE_COLUMNS.get(state)


def _event_reason(event: Mapping[str, Any]) -> str:
    data = _data(event)
    for name in ("reason", "error", "message", "detail", "note"):
        value = data.get(name) if name in data else event.get(name)
        if value:
            return _scrub_text(value)
    return ""


def _event_title(event: Mapping[str, Any], key: str) -> str:
    data = _data(event)
    value = data.get("title") or data.get("summary") or data.get("feature_id") or data.get("job_id")
    text = _scrub_text(value or key, _MAX_TITLE)
    return text or key


def _pr_title(pr: Mapping[str, Any], key: str) -> str:
    text = _scrub_text(pr.get("title") or key, _MAX_TITLE)
    return text or key


def _comment_signature(status: str, reason: str) -> str:
    return hashlib.sha256(f"{status}\x00{reason}".encode("utf-8")).hexdigest()


def _comment_text(status: str, reason: str) -> str:
    if status == ARCHIVE_COLUMN:
        detail = f" ({reason})" if reason else ""
        return f"bundlebees board-sync: pull request closed without merge; archived{detail}"
    detail = f": {reason}" if reason else ""
    return f"bundlebees board-sync: blocked{detail}"


def _equivalent(current: str, target: str) -> bool:
    if current == target:
        return True
    return target == ARCHIVE_COLUMN and current in (ARCHIVE_COLUMN, "blocked")


def _marker_line(key: str) -> str:
    return f"{MARKER_PREFIX} {key}"


def _body_for(key: str, title: str) -> str:
    return f"{_marker_line(key)}\n\nbundlebees managed card: {key}\n"


def _marker_key(card: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = [
        card.get("body"),
        card.get("description"),
        card.get("notes"),
        card.get("detail"),
    ]
    metadata = card.get("metadata") or card.get("fields")
    if isinstance(metadata, Mapping):
        candidates.extend(metadata.values())
    for value in candidates:
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            stripped = line.strip()
            if stripped.startswith(MARKER_PREFIX):
                key = stripped[len(MARKER_PREFIX):].strip()
                return key if _KEY_RE.fullmatch(key) else None
    return None


def _card_id(card: Mapping[str, Any]) -> str | None:
    for name in ("id", "task_id", "taskId"):
        value = card.get(name)
        if value:
            return str(value)
    task = card.get("task")
    if isinstance(task, Mapping):
        return _card_id(task)
    return None


def _card_status(card: Mapping[str, Any]) -> str:
    value = card.get("status") or card.get("column") or card.get("state") or card.get("lane")
    token = _state_token(value)
    return BEE_STATE_COLUMNS.get(token, token)


def _payload_cards(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [card for card in payload if isinstance(card, Mapping)]
    if isinstance(payload, Mapping):
        for name in ("tasks", "cards", "items", "data"):
            value = payload.get(name)
            if isinstance(value, list):
                return [card for card in value if isinstance(card, Mapping)]
    return []


def _created_id(result: Any) -> str | None:
    text = _result_text(result, "stdout").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if isinstance(payload, Mapping):
        for name in ("id", "task_id", "taskId"):
            if payload.get(name):
                return str(payload[name])
        task = payload.get("task")
        if isinstance(task, Mapping):
            return _card_id(task)
    return None


def _created_status(result: Any) -> str:
    text = _result_text(result, "stdout").strip()
    try:
        payload = json.loads(text) if text else None
    except ValueError:
        return "triage"
    if isinstance(payload, Mapping):
        status = _card_status(payload.get("task") if isinstance(payload.get("task"), Mapping) else payload)
        if status in MANAGED_COLUMNS:
            return status
    return "triage"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_events(state_dir: Path | str) -> list[dict[str, Any]]:
    """Read the durable bee event outbox read-only (reuses ``bee_events``)."""
    try:
        from . import bee_events
    except Exception:
        return []
    reader = getattr(bee_events, "_iter_outbox", None)
    if callable(reader):
        try:
            return [dict(event) for _event_id, event in reader(state_dir)]
        except Exception:
            return []
    return []


class BoardSync:
    """Idempotent, fail-soft board reconciler over the public kanban CLI."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        state_dir: Path | str | None = None,
        *,
        run: CommandRunner | None = None,
        events: Source | None = None,
        event_source: Source | None = None,
        prs: Source | None = None,
        pr_source: Source | None = None,
        kanban_db: Path | str | None = None,
        db_path: Path | str | None = None,
        dry_run: bool = False,
    ) -> None:
        command_runner = runner or run
        if command_runner is None:
            raise BoardSyncError("runner_missing", "a kanban command runner is required")
        if state_dir is None:
            raise BoardSyncError("state_dir_missing", "a plugin state directory is required")
        self.runner = command_runner
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.events = events if events is not None else event_source
        self.prs = prs if prs is not None else pr_source
        database = kanban_db if kanban_db is not None else db_path
        self.kanban_db = Path(database).expanduser() if database is not None else Path("~/.hermes/kanban.db").expanduser()
        self.dry_run = dry_run
        self._state_path = self.state_dir / STATE_DIRNAME / STATE_FILENAME
        self._backup_dir = self.state_dir / STATE_DIRNAME / BACKUP_DIRNAME
        self._state = self._load_state()
        self._dirty = False

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {"schema": SCHEMA, "cards": {}}
        if not isinstance(value, Mapping):
            return {"schema": SCHEMA, "cards": {}}
        cards = value.get("cards")
        body = dict(value)
        body["cards"] = dict(cards) if isinstance(cards, Mapping) else {}
        body.setdefault("schema", SCHEMA)
        return body

    def _save_state(self) -> None:
        if not self._dirty or self.dry_run:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self._state, schema=SCHEMA, updated_at=_now())
        tmp = self._state_path.with_name(self._state_path.name + f".{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._state_path)
        self._dirty = False

    def _cards(self) -> dict[str, dict[str, Any]]:
        cards = self._state.get("cards")
        if not isinstance(cards, dict):
            cards = {}
            self._state["cards"] = cards
        return cards

    def _ensure_backup(self, summary: dict[str, Any]) -> bool:
        record = self._state.get("backup") if isinstance(self._state.get("backup"), Mapping) else {}
        if record.get("verified") and record.get("path"):
            existing = Path(str(record["path"]))
            if existing.is_file() and self._verify_sqlite(existing)[0]:
                return True
        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            summary["refused"] = True
            summary["errors"].append({"action": "backup", "error": _scrub_text(f"backup directory is not writable: {exc}", _MAX_ERROR)})
            return False
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self._backup_dir / f"kanban-{stamp}.db"
        source = self.kanban_db
        if not source.is_file():
            summary["refused"] = True
            summary["errors"].append({
                "action": "backup",
                "error": "kanban database is not readable at the configured path",
            })
            return False
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            summary["refused"] = True
            summary["errors"].append({"action": "backup", "error": _scrub_text(f"backup copy failed: {exc}", _MAX_ERROR)})
            return False
        ok, detail = self._verify_sqlite(destination)
        if not ok:
            try:
                destination.unlink()
            except OSError:
                pass
            summary["refused"] = True
            summary["errors"].append({"action": "backup", "error": _scrub_text(f"backup integrity check failed: {detail}", _MAX_ERROR)})
            return False
        self._state["backup"] = {
            "path": str(destination),
            "verified": True,
            "integrity": "ok",
            "verified_at": _now(),
        }
        self._dirty = True
        summary["backup"] = str(destination)
        return True

    @staticmethod
    def _verify_sqlite(path: Path) -> tuple[bool, str]:
        """Open the *copy* read-only and run ``PRAGMA integrity_check``."""
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            return False, str(exc)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return False, str(exc)
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        if not row or str(row[0]).lower() != "ok":
            return False, str(row[0]) if row else "empty integrity result"
        return True, "ok"

    def _run_mutating(self, argv: list[str], stdin: str | None, summary: dict[str, Any], *, dry: bool = False) -> Any:
        if dry:
            summary["commands"].append(list(argv))
            summary["intended"].append(list(argv))
            return None
        if not self._ensure_backup(summary):
            raise _Refused("backup_unverified")
        summary["commands"].append(list(argv))
        try:
            result = self.runner(list(argv), stdin)
        except Exception as exc:
            raise _CommandFailed(_scrub_text(f"{type(exc).__name__}: {exc}", _MAX_ERROR)) from exc
        if _result_rc(result) != 0:
            raise _CommandFailed(_safe_error(result) or f"command exited {_result_rc(result)}")
        return result

    def _read_cards(self, summary: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, bool]:
        argv = ["hermes", "kanban", "list", "--json"]
        summary["commands"].append(list(argv))
        try:
            result = self.runner(list(argv), None)
        except Exception as exc:
            summary["errors"].append({"action": "list", "error": _scrub_text(f"{type(exc).__name__}: {exc}", _MAX_ERROR)})
            return None, False
        if _result_rc(result) != 0:
            summary["errors"].append({"action": "list", "error": _safe_error(result) or "kanban list failed"})
            return None, False
        try:
            payload = json.loads(_result_text(result, "stdout") or "[]")
        except ValueError:
            summary["errors"].append({"action": "list", "error": "kanban list did not return JSON"})
            return None, False
        cards = []
        for card in _payload_cards(payload):
            key = _marker_key(card)
            cards.append({
                "id": _card_id(card),
                "key": key,
                "status": _card_status(card),
            })
        return cards, True

    def _desired(self, events: Iterable[Mapping[str, Any]], prs: Iterable[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        ordered = sorted(enumerate(events), key=lambda item: (str(item[1].get("time") or ""), item[0]))
        desired: dict[str, dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []
        for _index, event in ordered:
            if not isinstance(event, Mapping):
                skipped.append({"reason": "event_not_object"})
                continue
            try:
                key = card_key_for_event(event)
            except BoardSyncError as exc:
                skipped.append({"reason": exc.code})
                continue
            column = column_for_event(event)
            if column is None:
                skipped.append({"key": key, "reason": "unmapped_state"})
                continue
            reason = _event_reason(event)
            desired[key] = {
                "key": key,
                "status": column,
                "title": _event_title(event, key),
                "reason": reason,
                "comment_signature": _comment_signature(column, reason) if column in ("blocked", ARCHIVE_COLUMN) else "",
            }
        for pr in prs:
            if not isinstance(pr, Mapping):
                skipped.append({"reason": "pr_not_object"})
                continue
            try:
                parts = _pr_parts({"type": "pull_request", "data": pr})
            except BoardSyncError as exc:
                skipped.append({"reason": exc.code})
                continue
            if parts is None:
                skipped.append({"reason": "pr_unkeyed"})
                continue
            key = _pr_key(*parts)
            state = _state_token(pr.get("state") or pr.get("status"))
            column = PR_STATE_COLUMNS.get(state)
            if column is None:
                skipped.append({"key": key, "reason": "unmapped_state"})
                continue
            reason = _scrub_text(pr.get("reason") or pr.get("message") or "", _MAX_REASON)
            desired[key] = {
                "key": key,
                "status": column,
                "title": _pr_title(pr, key),
                "reason": reason,
                "comment_signature": _comment_signature(column, reason) if column in ("blocked", ARCHIVE_COLUMN) else "",
            }
        return desired, skipped

    def run(
        self,
        events: Iterable[Mapping[str, Any]] | None = None,
        prs: Iterable[Mapping[str, Any]] | None = None,
        *,
        reconcile: bool = False,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """Apply or reconcile without ever raising through the caller."""
        dry = self.dry_run if dry_run is None else bool(dry_run)
        summary: dict[str, Any] = {
            "schema": SCHEMA,
            "ok": True,
            "dry_run": dry,
            "refused": False,
            "reconcile": bool(reconcile),
            "commands": [],
            "intended": [],
            "created": [],
            "updated": [],
            "comments": [],
            "adopted": [],
            "skipped": [],
            "errors": [],
            "backup": "",
            "cards": 0,
        }
        try:
            event_source = list(events) if events is not None else list(self.events() if self.events else [])
            pr_source = list(prs) if prs is not None else list(self.prs() if self.prs else [])
            desired, skipped = self._desired(event_source, pr_source)
            summary["skipped"].extend(skipped)
        except Exception as exc:
            summary["errors"].append({"action": "collect", "error": _scrub_text(f"{type(exc).__name__}: {exc}", _MAX_ERROR)})
            summary["ok"] = False
            return summary

        live: list[dict[str, Any]] | None = None
        if reconcile:
            live, _list_ok = self._read_cards(summary)

        if live is not None:
            cards = self._cards()
            managed_ids = {str(record.get("task_id")) for record in cards.values() if record.get("task_id")}
            for card in live:
                key = card.get("key")
                if key and key in desired:
                    continue
                if key and key in cards:
                    continue
                if card.get("id") and str(card.get("id")) in managed_ids:
                    continue
                summary["skipped"].append({"card": card.get("id"), "key": key, "reason": "unmanaged"})

        for key in sorted(desired):
            try:
                self._apply_one(key, desired[key], live, dry, summary)
            except _Refused:
                break
            except Exception as exc:
                summary["errors"].append({"action": "apply", "key": key, "error": _scrub_text(f"{type(exc).__name__}: {exc}", _MAX_ERROR)})

        if not dry:
            try:
                self._save_state()
            except Exception as exc:
                summary["errors"].append({"action": "state", "error": _scrub_text(f"{type(exc).__name__}: {exc}", _MAX_ERROR)})
        summary["cards"] = len(desired)
        summary["ok"] = not summary["errors"] and not summary["refused"]
        return summary

    def reconcile(
        self,
        events: Iterable[Mapping[str, Any]] | None = None,
        prs: Iterable[Mapping[str, Any]] | None = None,
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self.run(events=events, prs=prs, reconcile=True, dry_run=dry_run)

    def apply(
        self,
        events: Iterable[Mapping[str, Any]] | None = None,
        prs: Iterable[Mapping[str, Any]] | None = None,
        *,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        return self.run(events=events, prs=prs, reconcile=False, dry_run=dry_run)

    def _live_for(self, key: str, record: Mapping[str, Any] | None, live: list[dict[str, Any]] | None) -> Mapping[str, Any] | None:
        if live is None:
            return None
        task_id = str(record.get("task_id")) if record and record.get("task_id") else None
        for card in live:
            if card.get("key") == key:
                return card
            if task_id and card.get("id") and str(card.get("id")) == task_id:
                return card
        return None

    def _apply_one(
        self,
        key: str,
        desired: Mapping[str, Any],
        live: list[dict[str, Any]] | None,
        dry: bool,
        summary: dict[str, Any],
    ) -> None:
        cards = self._cards()
        record = cards.get(key) if isinstance(cards.get(key), dict) else None
        live_card = self._live_for(key, record, live)
        target = str(desired["status"])

        if live_card is not None and record is None:
            record = {
                "key": key,
                "task_id": str(live_card.get("id") or ""),
                "status": _state_token(live_card.get("status")),
                "title": desired["title"],
                "created_at": _now(),
            }
            if not dry:
                cards[key] = record
                self._dirty = True
            summary["adopted"].append(key)

        task_id = str(record.get("task_id") or "") if record else ""
        current = _state_token(live_card.get("status")) if live_card is not None else (_state_token(record.get("status")) if record else "")
        signature = str(desired.get("comment_signature") or "")
        needs_comment = target in ("blocked", ARCHIVE_COLUMN) and not (
            record is not None and signature == str(record.get("comment_signature") or "")
        )

        if task_id and record is not None and _equivalent(current, target) and not needs_comment:
            if _state_token(record.get("status")) != target:
                record["status"] = target
                record["title"] = desired["title"]
                if not dry:
                    self._dirty = True
            return

        if not task_id:
            if live_card is not None and live_card.get("id"):
                task_id = str(live_card["id"])
                if record is not None:
                    record["task_id"] = task_id
                    self._dirty = True
            else:
                argv = [
                    "hermes", "kanban", "create",
                    "--idempotency-key", key,
                    "--title", desired["title"],
                    "--body-file", "-",
                    "--json",
                ]
                if dry:
                    self._run_mutating(argv, _body_for(key, desired["title"]), summary, dry=True)
                    task_id = "<new>"
                    current = "triage"
                else:
                    result = self._run_mutating(argv, _body_for(key, desired["title"]), summary)
                    task_id = _created_id(result) or ""
                    if not task_id:
                        summary["errors"].append({"action": "create", "key": key, "error": "create returned no task id"})
                        return
                    current = _created_status(result)
                    record = {
                        "key": key,
                        "task_id": task_id,
                        "status": current,
                        "title": desired["title"],
                        "created_at": _now(),
                    }
                    cards[key] = record
                    self._dirty = True
                    summary["created"].append(key)
                if record is None:
                    record = {"key": key, "task_id": task_id, "status": current, "title": desired["title"]}

        if _equivalent(current, target) and not needs_comment:
            if record is not None:
                record["status"] = target
                record["title"] = desired["title"]
                if not dry:
                    self._dirty = True
            return

        if current in ("blocked", ARCHIVE_COLUMN) and target not in ("blocked", ARCHIVE_COLUMN):
            self._transition(key, task_id, "unblock", record, "ready", dry, summary)
            current = "ready"

        if target == "running":
            self._transition(key, task_id, "claim", record, "running", dry, summary)
            current = "running"
        elif target == "review":
            self._transition(key, task_id, "request-review", record, "review", dry, summary)
            current = "review"
        elif target == "done":
            self._transition(key, task_id, "complete", record, "done", dry, summary)
            current = "done"
        elif target == "blocked":
            if not _equivalent(current, "blocked"):
                self._transition(key, task_id, "block", record, "blocked", dry, summary)
            current = "blocked"
        elif target == ARCHIVE_COLUMN:
            if not _equivalent(current, ARCHIVE_COLUMN):
                self._transition(key, task_id, "block", record, "blocked", dry, summary)
            current = "blocked"
        elif not _equivalent(current, target):
            summary["skipped"].append({"key": key, "reason": "no_supported_transition", "target": target})
            return

        if needs_comment:
            argv = ["hermes", "kanban", "comment", task_id, "--body-file", "-"]
            body = _comment_text(target, str(desired.get("reason") or ""))
            self._run_mutating(argv, body, summary, dry=dry)
            summary["comments"].append(key)
            if record is not None and not dry:
                record["comment_signature"] = signature
                self._dirty = True

        if record is not None:
            record["status"] = target
            record["title"] = desired["title"]
            record["updated_at"] = _now()
            if not dry:
                self._dirty = True
                summary["updated"].append(key)

    def _transition(
        self,
        key: str,
        task_id: str,
        verb: str,
        record: dict[str, Any] | None,
        status: str,
        dry: bool,
        summary: dict[str, Any],
    ) -> None:
        argv = ["hermes", "kanban", verb, task_id]
        self._run_mutating(argv, None, summary, dry=dry)
        if record is not None:
            record["status"] = status
            if not dry:
                self._dirty = True

