"""Stdlib HTTP API for the disposable todo pilot."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .store import TodoStore

STATIC = Path(__file__).with_name("static") / "index.html"


def _json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _error(handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
    _json(handler, status, {"error": {"code": code, "message": message}})


class _Handler(BaseHTTPRequestHandler):
    store: TodoStore
    server_version = "pilot-todo/1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("request body is required")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise ValueError("request body must be JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _route(self, method: str) -> tuple[list[str], dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return [unquote(part) for part in parts.path.split("/") if part], parse_qs(parts.query)

    def do_GET(self) -> None:
        segments, query = self._route("GET")
        if segments == []:
            if not STATIC.exists():
                _error(self, 503, "ui_missing", "the pilot UI has not been built")
                return
            raw = STATIC.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if segments == ["api", "todos"]:
            try:
                rows = self.store.list((query.get("q") or [""])[0], (query.get("status") or ["all"])[0])
            except ValueError as exc:
                _error(self, 400, "validation", str(exc))
                return
            _json(self, 200, {"todos": rows})
            return
        if len(segments) == 3 and segments[:2] == ["api", "todos"]:
            row = self.store.get(segments[2])
            if row is None:
                _error(self, 404, "not_found", "todo does not exist")
                return
            _json(self, 200, {"todo": row})
            return
        _error(self, 405 if segments[:2] == ["api", "todos"] else 404, "not_found", "route does not exist")

    def do_POST(self) -> None:
        segments, _ = self._route("POST")
        try:
            if segments == ["api", "todos"]:
                body = self._body()
                row = self.store.create(str(body.get("title", "")), str(body.get("description", "")))
                _json(self, 201, {"todo": row})
                return
            if len(segments) == 4 and segments[:2] == ["api", "todos"] and segments[3] == "complete":
                row = self.store.update(segments[2], completed=True)
                _json(self, 200, {"todo": row})
                return
        except ValueError as exc:
            _error(self, 400, "validation", str(exc))
            return
        _error(self, 405, "not_found", "route does not exist")

    def do_PATCH(self) -> None:
        segments, _ = self._route("PATCH")
        if not (len(segments) == 3 and segments[:2] == ["api", "todos"]):
            _error(self, 405, "not_found", "route does not exist")
            return
        try:
            body = self._body()
            row = self.store.update(
                segments[2],
                title=body.get("title") if "title" in body else None,
                description=body.get("description") if "description" in body else None,
                completed=body.get("completed") if "completed" in body else None,
            )
        except ValueError as exc:
            _error(self, 400, "validation", str(exc))
            return
        _json(self, 200, {"todo": row})

    def do_DELETE(self) -> None:
        _error(self, 405, "method_not_allowed", "delete is not supported")


def make_server(store: TodoStore, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Bound(_Handler):
        pass

    Bound.store = store
    return ThreadingHTTPServer((host, port), Bound)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the pilot todo app")
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = make_server(TodoStore(Path(args.db)), args.host, args.port)
    try:
        print(f"http://{args.host}:{server.server_address[1]}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
