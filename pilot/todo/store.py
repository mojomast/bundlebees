"""Persistent todo storage for the disposable pilot application."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "rizzstack.pilot-todo-store/1"
MAX_TITLE = 200
MAX_DESCRIPTION = 2000


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > limit:
        raise ValueError(f"{field} must be at most {limit} characters")
    return value


class TodoStore:
    """One small JSON document, replaced atomically on every mutation."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._load()

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            self._todos: list[dict[str, Any]] = []
            return self._todos
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"todo store is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("todos"), list):
            raise ValueError("todo store has an unsupported schema")
        self._todos = [dict(row) for row in raw["todos"] if isinstance(row, dict)]
        return self._todos

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": SCHEMA, "todos": self._todos}
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def list(self, query: str = "", status: str = "all") -> list[dict[str, Any]]:
        if status not in {"all", "open", "done"}:
            raise ValueError("status must be all, open or done")
        text = str(query or "").strip().lower()
        rows = [
            dict(row) for row in sorted(self._todos, key=lambda item: str(item.get("created_at", "")), reverse=True)
            if (status == "all" or (status == "done") == bool(row.get("completed")))
            and (not text or text in str(row.get("title", "")).lower() or text in str(row.get("description", "")).lower())
        ]
        return rows

    def get(self, todo_id: str) -> dict[str, Any] | None:
        for row in self._todos:
            if row.get("id") == str(todo_id):
                return dict(row)
        return None

    def create(self, title: str, description: str = "") -> dict[str, Any]:
        title = _text(title, "title", limit=MAX_TITLE)
        description = str(description or "").strip()
        if len(description) > MAX_DESCRIPTION:
            raise ValueError(f"description must be at most {MAX_DESCRIPTION} characters")
        timestamp = _now()
        row = {
            "id": uuid.uuid4().hex,
            "title": title,
            "description": description,
            "completed": False,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._todos.append(row)
        self._save()
        return dict(row)

    def update(self, todo_id: str, *, title: str | None = None, description: str | None = None, completed: bool | None = None) -> dict[str, Any]:
        row = self.get(todo_id)
        if row is None:
            raise ValueError(f"todo {todo_id!r} does not exist")
        if title is not None:
            row["title"] = _text(title, "title", limit=MAX_TITLE)
        if description is not None:
            description = str(description).strip()
            if len(description) > MAX_DESCRIPTION:
                raise ValueError(f"description must be at most {MAX_DESCRIPTION} characters")
            row["description"] = description
        if completed is not None:
            if not isinstance(completed, bool):
                raise ValueError("completed must be a boolean")
            row["completed"] = completed
        row["updated_at"] = _now()
        for index, existing in enumerate(self._todos):
            if existing.get("id") == str(todo_id):
                self._todos[index] = row
                break
        self._save()
        return dict(row)
