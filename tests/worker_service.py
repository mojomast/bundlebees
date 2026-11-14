"""A standalone worker service for crash-recovery tests.

The coordinator must never be the only holder of the attempt ledger: this module
runs in its **own OS process**, owns a JSON ledger file on disk, and answers over
HTTP. When the coordinator child is killed at a checkpoint and restarted, the
attempt it dispatched is still there — with its brief and its patch — because a
different process persisted it.

The protocol is exactly what the production launcher adapter speaks::

    POST /v1/attempts                 {"brief": {...}} -> {"attempt_id", "state"}
    GET  /v1/attempts                                   -> {"attempts": [...]}
    GET  /v1/attempts/<attempt_id>                      -> attempt | 404
    GET  /v1/attempts/<attempt_id>/artifact              -> {"patch", "source_rev", "patch_sha256"}
    GET  /v1/ledger                                     -> the whole durable ledger
    GET  /healthz                                       -> {"ok": true, "pid": int}

The worker is scripted, not intelligent: it returns the real diff for the
attempt's project from ``--script`` and reports the attempt as ``succeeded`` at
once. An unknown project is a hard 400, so a misrouted brief fails visibly
instead of producing an empty artifact.

Run it::

    python -m tests.worker_service --ledger state/worker-ledger.json \
        --script script.json --ready-file url.json

The ready file is written atomically once the socket is bound, and contains
``{"url": "http://127.0.0.1:<port>", "pid": <pid>}``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from bundlebees_plugin.client import ClientError

LEDGER_SCHEMA = "bb.worker-ledger/1"
REQUIRED_BRIEF_FIELDS = ("job_id", "task_id", "objective", "source_rev", "assigned_files")


# --------------------------------------------------------------------------- #
# durable ledger
# --------------------------------------------------------------------------- #


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Path, body: Mapping[str, Any]) -> None:
    """Replace ``path`` with ``body`` so a reader never sees a partial ledger."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class Ledger:
    """The service's durable state: every attempt ever dispatched, in order."""

    def __init__(self, path: Path, script: Mapping[str, Any]) -> None:
        self.path = Path(path)
        self.script = dict(script)
        self._lock = threading.RLock()
        self._body = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": LEDGER_SCHEMA, "next": 1, "attempts": []}
        if not isinstance(body, dict) or body.get("schema") != LEDGER_SCHEMA:
            raise SystemExit(f"the ledger at {self.path} is not a {LEDGER_SCHEMA} document")
        body.setdefault("next", len(body.get("attempts") or []) + 1)
        body.setdefault("attempts", [])
        return body

    def _save(self) -> None:
        write_json_atomic(self.path, self._body)

    # -- reads ---------------------------------------------------------------
    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._body["attempts"]]

    def find(self, attempt_id: str) -> dict[str, Any] | None:
        with self._lock:
            for entry in self._body["attempts"]:
                if entry.get("attempt_id") == attempt_id:
                    return dict(entry)
        return None

    # -- writes --------------------------------------------------------------
    def add(self, brief: Mapping[str, Any]) -> dict[str, Any]:
        missing = [name for name in REQUIRED_BRIEF_FIELDS if not brief.get(name)]
        if missing:
            raise ValueError(f"brief is missing {', '.join(missing)}")
        project = str(brief.get("project") or "")
        if not project:
            raise ValueError("brief has no project; the worker cannot route it")
        scripted = self.script.get(project)
        if not isinstance(scripted, Mapping) or not isinstance(scripted.get("patch"), str):
            raise ValueError(f"no scripted patch for project {project!r}")
        patch = str(scripted["patch"])
        with self._lock:
            number = int(self._body["next"])
            entry = {
                "attempt_id": f"att_{number:04d}",
                "state": "succeeded",
                # A float epoch, not a second-resolution string: adoption compares
                # this with the intent's write time and a truncated ISO string can
                # look older than the intent it belongs to.
                "created_at": time.time(),
                "brief": json.loads(json.dumps(brief)),
                "project": project,
                "source_rev": str(brief.get("source_rev") or ""),
                "patch": patch,
                "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            }
            self._body["attempts"].append(entry)
            self._body["next"] = number + 1
            self._save()
            return dict(entry)

    # -- projections ---------------------------------------------------------
    @staticmethod
    def public(entry: Mapping[str, Any]) -> dict[str, Any]:
        """The list/detail view: no patch bytes (the artifact endpoint has those)."""

        return {
            "attempt_id": entry.get("attempt_id"),
            "state": entry.get("state"),
            "created_at": entry.get("created_at"),
            "project": entry.get("project"),
            "brief": entry.get("brief"),
        }

    @staticmethod
    def artifact(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "patch": entry.get("patch"),
            "source_rev": entry.get("source_rev"),
            "patch_sha256": entry.get("patch_sha256"),
        }


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def make_handler(ledger: Ledger):
    class Handler(BaseHTTPRequestHandler):
        server_version = "bb-worker-service/1"

        def log_message(self, format, *args):  # noqa: A002,D401 - stdlib signature
            return

        def _send(self, status: int, body: Mapping[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, {"ok": True, "pid": os.getpid()})
            if path == "/v1/ledger":
                return self._send(200, {"schema": LEDGER_SCHEMA, "attempts": ledger.all()})
            if path == "/v1/attempts":
                return self._send(200, {"attempts": [Ledger.public(e) for e in ledger.all()]})
            prefix = "/v1/attempts/"
            if path.startswith(prefix):
                rest = path[len(prefix):]
                artifact = rest.endswith("/artifact")
                attempt_id = rest[: -len("/artifact")] if artifact else rest
                entry = ledger.find(attempt_id)
                if entry is None:
                    return self._send(404, {"error": "unknown_attempt", "attempt_id": attempt_id})
                if artifact:
                    return self._send(200, Ledger.artifact(entry))
                return self._send(200, Ledger.public(entry))
            return self._send(404, {"error": "not_found", "path": path})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.split("?", 1)[0] != "/v1/attempts":
                return self._send(404, {"error": "not_found", "path": self.path})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as exc:
                return self._send(400, {"error": "invalid_json", "detail": str(exc)})
            brief = body.get("brief") if isinstance(body, Mapping) else None
            if not isinstance(brief, Mapping):
                return self._send(400, {"error": "invalid_brief", "detail": "body must be {\"brief\": {...}}"})
            try:
                entry = ledger.add(brief)
            except ValueError as exc:
                return self._send(400, {"error": "brief_refused", "detail": str(exc)})
            return self._send(200, {"attempt_id": entry["attempt_id"], "state": entry["state"]})

    return Handler


class WorkerService:
    """The serving process: bind, announce, serve until killed."""

    def __init__(self, ledger_path: Path, script: Mapping[str, Any], ready_file: Path,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.ledger = Ledger(Path(ledger_path), script)
        self.ready_file = Path(ready_file)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.ledger))
        self.httpd.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def announce(self) -> None:
        write_json_atomic(self.ready_file, {"url": self.url, "pid": os.getpid()})

    def serve_forever(self) -> None:
        self.announce()
        try:
            self.httpd.serve_forever(poll_interval=0.05)
        finally:
            self.httpd.server_close()


# --------------------------------------------------------------------------- #
# client adapter for the coordinator
# --------------------------------------------------------------------------- #


class WorkerServiceClient:
    """The four-member launcher contract, spoken to :class:`WorkerService`.

    Errors are raised as the plugin's own ``ClientError`` so the production
    ``LauncherAdapter`` maps them exactly as it maps a real launcher: a 404 is an
    unknown attempt, a 4xx is a refusal that never started work.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)

    # -- transport -----------------------------------------------------------
    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        import urllib.error
        import urllib.request

        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = {"detail": raw.decode("utf-8", "replace")[:200]}
            code = str(detail.get("error") or "worker_refused")
            raise ClientError(str(detail.get("detail") or code), code=code, status=exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            # No status: the production adapter treats this as uncertain, never
            # as proof the request did not land.
            raise ClientError(f"the worker service is unreachable: {exc}", code="worker_unreachable") from exc

    # -- contract ------------------------------------------------------------
    def create(self, brief: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/v1/attempts", {"brief": dict(brief)})

    def attempts_raw(self) -> list[dict[str, Any]]:
        body = self._call("GET", "/v1/attempts")
        attempts = body.get("attempts") if isinstance(body, Mapping) else None
        if not isinstance(attempts, list):
            raise ClientError("the worker service returned no attempt list", code="worker_invalid")
        return attempts

    def get_raw(self, attempt_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/attempts/{attempt_id}")

    def artifact(self, attempt_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/attempts/{attempt_id}/artifact")


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scripted worker service for crash-recovery tests.")
    parser.add_argument("--ledger", required=True, help="JSON ledger file (created if absent)")
    parser.add_argument("--script", required=True, help="JSON: {\"projects\": {name: {\"patch\": text}}}")
    parser.add_argument("--ready-file", required=True, help="where to publish the bound URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    script_body = json.loads(Path(args.script).read_text(encoding="utf-8"))
    projects = script_body.get("projects") if isinstance(script_body, Mapping) else None
    if not isinstance(projects, Mapping):
        raise SystemExit("--script must be {\"projects\": {...}}")
    service = WorkerService(Path(args.ledger), projects, Path(args.ready_file),
                            host=args.host, port=args.port)

    def _stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    try:
        service.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
