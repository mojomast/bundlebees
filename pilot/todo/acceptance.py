"""Application-level acceptance for the pilot todo app.

This is deliberately outside the per-feature unit tests: it starts the real
stdlib HTTP server on an ephemeral loopback port, talks to it over HTTP, then
reopens the store to prove persistence. It uses only the standard library.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .server import make_server
from .store import TodoStore


def _request(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode() or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode() or "null")
        except ValueError:
            payload = raw.decode(errors="replace")
        return exc.code, payload


def run(db_path: Path | str) -> dict[str, Any]:
    store = TodoStore(Path(db_path))
    server = make_server(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    checks: list[str] = []
    try:
        status, body = _request(base, "POST", "/api/todos", {"title": "write acceptance", "description": "real HTTP"})
        assert status == 201, (status, body)
        todo = body["todo"]
        checks.append("create")

        status, body = _request(base, "GET", "/api/todos")
        assert status == 200 and any(row["id"] == todo["id"] for row in body["todos"]), (status, body)
        checks.append("list")

        query = urllib.parse.urlencode({"q": "acceptance", "status": "open"})
        status, body = _request(base, "GET", "/api/todos?" + query)
        assert status == 200 and any(row["id"] == todo["id"] for row in body["todos"]), (status, body)
        checks.append("search")

        status, body = _request(base, "PATCH", f"/api/todos/{todo['id']}", {"title": "edited acceptance"})
        assert status == 200 and body["todo"]["title"] == "edited acceptance", (status, body)
        checks.append("edit")

        status, body = _request(base, "POST", f"/api/todos/{todo['id']}/complete")
        assert status == 200 and body["todo"]["completed"] is True, (status, body)
        checks.append("complete")

        status, body = _request(base, "POST", "/api/todos", {"title": ""})
        assert status == 400 and body.get("error"), (status, body)
        checks.append("validation")

        status, body = _request(base, "GET", "/api/todos/not-a-real-id")
        assert status == 404 and body.get("error"), (status, body)
        checks.append("not-found")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    reopened = TodoStore(Path(db_path))
    persisted = reopened.get(todo["id"])
    assert persisted is not None and persisted["completed"] is True, persisted
    checks.append("persist")
    return {"ok": True, "checks": checks, "todo_id": todo["id"], "port": base.rsplit(":", 1)[-1]}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    print(json.dumps(run(Path(args.db)), indent=2, sort_keys=True))
