"""Remote landing export: authenticated HTTP read, durable cursor ingestion.

Every test uses a real loopback HTTP server, the real pinned-schema validator,
real coordinator leases and the real durable outbox/inbox path.  Only the
launcher's export producer is simulated.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from bundlebees_plugin import bee_events, event_consumer, factory_driver, landing_bridge, leases
from bundlebees_plugin.client import LANDING_EXPORT_SCHEMA, LauncherClient

TOKEN = "remote-export-test-token"
SUBSCRIBER = "coordinator"
SOURCE_REV = "a" * 40
FILES = ["src/remote.py"]


# ---------------------------------------------------------------------------
# Pinned workflow schema
# ---------------------------------------------------------------------------


def _write_schema(root: Path) -> None:
    schema_dir = root / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema", "feature_id", "job_id", "task_id", "attempt_id",
            "coordinator", "backend", "state", "source_rev", "lease", "result",
        ],
        "properties": {
            "schema": {"const": bee_events.SCHEMA},
            "feature_id": {"type": "string"},
            "job_id": {"type": "string"},
            "task_id": {"type": "string"},
            "attempt_id": {"type": "string", "minLength": 1},
            "coordinator": {
                "type": "object", "additionalProperties": False,
                "required": ["identity"],
                "properties": {"identity": {"type": "string"}, "session": {"type": "string"}},
            },
            "backend": {
                "type": "object", "additionalProperties": False,
                "required": ["harness"],
                "properties": {"harness": {"type": "string"}, "model": {"type": "string"}},
            },
            "state": {"enum": ["succeeded", "failed"]},
            "source_rev": {"type": "string", "minLength": 1},
            "lease": {
                "type": "object", "additionalProperties": False,
                "required": ["sha256", "assigned_files"],
                "properties": {
                    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "assigned_files": {"type": "array", "items": {"type": "string"}},
                },
            },
            "result": {
                "type": "object", "additionalProperties": False,
                "required": ["available"],
                "properties": {
                    "available": {"type": "boolean"},
                    "artifact_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "reference": {"type": "string"},
                },
            },
        },
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["specversion", "id", "source", "type", "data"],
        "properties": {
            "specversion": {"const": "1.0"},
            "id": {"type": "string", "pattern": r"^urn:rizzstack:bee-landed:[0-9a-f]{32}$"},
            "source": {"type": "string", "minLength": 1},
            "type": {"const": bee_events.EVENT_TYPE},
            "time": {"type": "string"},
            "datacontenttype": {"const": "application/json"},
            "data": data,
        },
    }
    (schema_dir / "bee-landed.schema.json").write_text(json.dumps(schema), encoding="utf-8")


@pytest.fixture(autouse=True)
def _schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_schema(tmp_path)
    monkeypatch.setenv("WORKFLOW_SRC", str(tmp_path))


# ---------------------------------------------------------------------------
# Fixture state and export pages
# ---------------------------------------------------------------------------


def _brief(source_rev: str = SOURCE_REV, files: list[str] | None = None) -> dict:
    return {
        "source_rev": source_rev,
        "assigned_files": list(files or FILES),
        "acceptance": ["the remote landing validates and is handled once"],
    }


def _state_with_lease(tmp_path: Path, attempt: str) -> tuple[Path, dict]:
    state = tmp_path / "state"
    lease = leases.record_lease(state, attempt, _brief())
    return state, lease


def _event(
    lease: dict,
    attempt: str,
    *,
    transition: str | None = None,
    state: str = "succeeded",
    job_id: str = "job-remote",
    task_id: str = "task-remote",
    source_rev: str = SOURCE_REV,
    assigned_files: list[str] | None = None,
    lease_sha256: str | None = None,
) -> dict:
    return bee_events.build_event(
        attempt_id=attempt,
        state=state,
        transition_id=transition or f"tr-{attempt}",
        feature_id="BEE-REMOTE",
        job_id=job_id,
        task_id=task_id,
        coordinator="remote-coordinator",
        backend="remote-worker",
        lease_sha256=lease_sha256 or lease["brief_sha256"],
        source_rev=source_rev,
        assigned_files=list(FILES if assigned_files is None else assigned_files),
        result_available=False,
        now=1_700_000_000,
    )


def _entry(event: dict, cursor: str, *, event_raw: bytes | None = None) -> dict:
    raw = event_raw if event_raw is not None else json.dumps(
        event, sort_keys=True, indent=2
    ).encode("utf-8")
    return {
        "cursor": cursor,
        "event_id": event["id"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "event": event,
        "event_raw": raw,
    }


def _page_bytes(
    entries: list[dict],
    *,
    next_cursor: str | None = None,
    has_more: bool = False,
    retention: dict | None = None,
) -> bytes:
    parts = []
    for entry in entries:
        parts.append(
            '{"cursor":%s,"event_id":%s,"sha256":%s,"event":%s}'
            % (
                json.dumps(entry["cursor"]),
                json.dumps(entry["event_id"]),
                json.dumps(entry["sha256"]),
                entry["event_raw"].decode("utf-8"),
            )
        )
    body = (
        '{"schema":%s,"events":[%s],"next_cursor":%s,"has_more":%s,"retention":%s}'
        % (
            json.dumps(LANDING_EXPORT_SCHEMA),
            ",".join(parts),
            json.dumps(next_cursor),
            json.dumps(bool(has_more)),
            json.dumps(retention or {"after": None}),
        )
    )
    return body.encode("utf-8")


class _ExportHandler(BaseHTTPRequestHandler):
    server_state: SimpleNamespace

    def log_message(self, *_args):  # keep test output quiet
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        state = type(self).server_state
        parsed = urlparse(self.path)
        if parsed.path != "/v1/landing/events":
            return self._send(404, b'{"error":"not_found"}')
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, b'{"error":"unauthorized"}')
        query = parse_qs(parsed.query)
        after = query.get("after", [None])[0]
        if isinstance(after, str) and after.isdigit():
            after = int(after)
        limit = int(query.get("limit", ["100"])[0])
        state.requests.append({"after": after, "limit": limit, "path": self.path})
        state.calls += 1
        if state.fail_on_call == state.calls:
            state.fail_on_call = None
            return self._send(503, b'{"error":"disconnected"}')
        status, body = state.responder(after, limit)
        return self._send(status, body)


@pytest.fixture()
def export_server():
    state = SimpleNamespace(
        requests=[],
        calls=0,
        fail_on_call=None,
        responder=lambda after, limit: (200, _page_bytes([])),
    )
    handler = type("ExportHandler", (_ExportHandler,), {"server_state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server = SimpleNamespace(
        state=state,
        base_url=f"http://127.0.0.1:{httpd.server_port}",
        httpd=httpd,
        thread=thread,
    )
    try:
        yield server
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _client(export_server) -> LauncherClient:
    return LauncherClient(export_server.base_url, token=TOKEN, timeout=2)


def _subscribe_file(state: Path) -> None:
    bee_events.subscribe(
        state, {"identity": SUBSCRIBER, "file": "inbox.jsonl"}
    )


def _outbox_files(state: Path) -> list[Path]:
    directory = state / "events" / "outbox"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _inbox_files(state: Path) -> list[Path]:
    directory = state / "events" / "inbox" / SUBSCRIBER
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def test_client_reads_paginated_export_and_preserves_raw_event_bytes(
    export_server, tmp_path: Path
):
    state, lease_a = _state_with_lease(tmp_path, "att-export-a")
    _state_b, lease_b = _state_with_lease(tmp_path, "att-export-b")
    event_a = _event(lease_a, "att-export-a", transition="tr-export-a")
    event_b = _event(lease_b, "att-export-b", transition="tr-export-b")
    raw_a = json.dumps(event_a, sort_keys=True, indent=2).encode("utf-8")
    raw_b = json.dumps(event_b, sort_keys=True, indent=4).encode("utf-8")

    def responder(after, _limit):
        if after is None:
            return 200, _page_bytes(
                [_entry(event_a, 1, event_raw=raw_a)],
                next_cursor=1,
                has_more=True,
            )
        if after == 1:
            return 200, _page_bytes(
                [_entry(event_b, 2, event_raw=raw_b)],
                next_cursor=2,
                has_more=False,
            )
        return 200, _page_bytes([])

    export_server.state.responder = responder
    client = _client(export_server)

    first = client.landing_events(limit=1)
    second = client.landing_events(after=first["next_cursor"], limit=1)

    assert first["schema"] == LANDING_EXPORT_SCHEMA
    assert first["events"][0]["event"] == event_a
    assert first["events"][0]["event_raw"] == raw_a
    assert first["events"][0]["sha256"] == hashlib.sha256(raw_a).hexdigest()
    assert second["events"][0]["event"] == event_b
    assert second["events"][0]["event_raw"] == raw_b
    assert second["has_more"] is False
    assert second["next_cursor"] == 2
    assert [request["after"] for request in export_server.state.requests] == [
        None,
        1,
    ]
    assert [request["limit"] for request in export_server.state.requests] == [1, 1]


def test_missing_token_never_reaches_the_export_endpoint(export_server):
    client = LauncherClient(
        export_server.base_url, token_provider=lambda: None, timeout=2
    )
    with pytest.raises(Exception) as excinfo:
        client.landing_events(limit=1)
    assert getattr(excinfo.value, "code", "") == "not_configured"
    assert export_server.state.requests == []


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def test_remote_ingest_paginates_into_the_existing_bridge_and_consumer_path(
    export_server, tmp_path: Path
):
    state, lease_a = _state_with_lease(tmp_path, "att-ingest-a")
    _state_b, lease_b = _state_with_lease(tmp_path, "att-ingest-b")
    event_a = _event(lease_a, "att-ingest-a", transition="tr-ingest-a")
    event_b = _event(lease_b, "att-ingest-b", transition="tr-ingest-b")
    _subscribe_file(state)

    def responder(after, _limit):
        if after is None:
            return 200, _page_bytes(
                [_entry(event_a, 1)],
                next_cursor=1,
                has_more=True,
            )
        if after == 1:
            return 200, _page_bytes(
                [_entry(event_b, 2)],
                next_cursor=2,
                has_more=False,
            )
        return 200, _page_bytes([])

    export_server.state.responder = responder
    result = landing_bridge.ingest_remote(state, _client(export_server), limit=1)

    assert result["failed"] == 0
    assert result["processed"] == 2
    assert result["duplicates"] == 0
    assert result["pages"] == 2
    assert result["cursor"] == 2
    assert len(_outbox_files(state)) == 2
    assert len(_inbox_files(state)) == 2
    stored = json.loads(landing_bridge.export_cursor_path(state).read_text())
    assert stored["schema"] == landing_bridge.EXPORT_CURSOR_SCHEMA
    assert stored["cursor"] == 2

    handled: list[dict] = []

    def handler(event: dict) -> dict:
        handled.append(event)
        return {"accepted": event["id"]}

    first = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="remote-consumer"
    )
    second = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="remote-consumer"
    )
    third = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="remote-consumer"
    )
    assert first is not None and first["status"] == "acked"
    assert second is not None and second["status"] == "acked"
    assert third is not None and third["status"] == "duplicate"
    assert [event["id"] for event in handled] == [event_a["id"], event_b["id"]]


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"state": "landed"}, "event_schema_invalid"),
        ({"source_rev": "b" * 40}, "source_rev_mismatch"),
        ({"assigned_files": ["src/other.py"]}, "lease_mismatch"),
    ],
)
def test_ingest_validates_schema_source_and_lease_before_recording(
    export_server, tmp_path: Path, overrides, expected
):
    state, lease = _state_with_lease(tmp_path, "att-bad")
    event = _event(lease, "att-bad", transition="tr-bad", **overrides)
    _subscribe_file(state)
    export_server.state.responder = lambda after, limit: (
        200,
        _page_bytes(
            [_entry(event, "cursor-bad")],
            next_cursor="cursor-bad",
            has_more=False,
        ),
    )

    result = landing_bridge.ingest_remote(state, _client(export_server), limit=10)

    assert result["processed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == expected
    assert _outbox_files(state) == []
    assert not landing_bridge.export_cursor_path(state).exists()


def test_ingest_refuses_an_export_whose_event_bytes_do_not_match_sha256(
    export_server, tmp_path: Path
):
    state, lease = _state_with_lease(tmp_path, "att-sha")
    event = _event(lease, "att-sha", transition="tr-sha")
    _subscribe_file(state)
    entry = _entry(event, "cursor-sha")
    entry["sha256"] = "0" * 64
    export_server.state.responder = lambda after, limit: (
        200,
        _page_bytes([entry], next_cursor="cursor-sha", has_more=False),
    )

    result = landing_bridge.ingest_remote(state, _client(export_server), limit=10)

    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "event_sha256_mismatch"
    assert _outbox_files(state) == []
    assert not landing_bridge.export_cursor_path(state).exists()


def test_replayed_pages_are_idempotent_and_do_not_duplicate_consumer_effects(
    export_server, tmp_path: Path
):
    state, lease = _state_with_lease(tmp_path, "att-replay")
    event = _event(lease, "att-replay", transition="tr-replay")
    _subscribe_file(state)
    export_server.state.responder = lambda after, limit: (
        200,
        _page_bytes(
            [_entry(event, "cursor-replay")],
            next_cursor="cursor-replay",
            has_more=False,
        ),
    )
    client = _client(export_server)

    first = landing_bridge.ingest_remote(state, client, limit=10)
    replay = landing_bridge.ingest_remote(state, client, after=None, limit=10)

    assert first["processed"] == 1
    assert replay["processed"] == 0
    assert replay["duplicates"] == 1
    assert len(_outbox_files(state)) == 1
    assert len(_inbox_files(state)) == 1

    handled: list[dict] = []

    def handler(payload: dict) -> dict:
        handled.append(payload)
        return {"accepted": True}

    assert event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="remote-consumer"
    )["status"] == "acked"
    duplicate = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="remote-consumer"
    )
    assert duplicate is not None and duplicate["status"] == "duplicate"
    assert len(handled) == 1


def test_cursor_only_advances_after_durable_delivery_and_inbox(
    export_server, tmp_path: Path
):
    state, lease = _state_with_lease(tmp_path, "att-pending")
    event = _event(lease, "att-pending", transition="tr-pending")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    bee_events.subscribe(
        state,
        {
            "identity": SUBSCRIBER,
            "url": f"http://127.0.0.1:{port}/inbox",
            "secret": "remote-pending-secret",
        },
    )
    export_server.state.responder = lambda after, limit: (
        200,
        _page_bytes(
            [_entry(event, "cursor-pending")],
            next_cursor="cursor-pending",
            has_more=False,
        ),
    )

    blocked = landing_bridge.ingest_remote(
        state, _client(export_server), limit=10
    )
    assert blocked["failed"] == 1
    assert blocked["failures"][0]["error"] == "delivery_pending"
    assert len(_outbox_files(state)) == 1
    assert _inbox_files(state) == []
    assert not landing_bridge.export_cursor_path(state).exists()

    assert bee_events.unsubscribe(state, SUBSCRIBER) is True
    _subscribe_file(state)
    recovered = landing_bridge.ingest_remote(
        state, _client(export_server), after=None, limit=10
    )

    assert recovered["failed"] == 0
    assert recovered["processed"] == 0
    assert recovered["duplicates"] == 1
    assert len(_outbox_files(state)) == 1
    assert len(_inbox_files(state)) == 1
    assert json.loads(landing_bridge.export_cursor_path(state).read_text())[
        "cursor"
    ] == "cursor-pending"


def test_disconnect_then_restart_replays_from_the_last_durable_cursor(
    export_server, tmp_path: Path
):
    state, lease_a = _state_with_lease(tmp_path, "att-restart-a")
    _state_b, lease_b = _state_with_lease(tmp_path, "att-restart-b")
    event_a = _event(lease_a, "att-restart-a", transition="tr-restart-a")
    event_b = _event(lease_b, "att-restart-b", transition="tr-restart-b")
    _subscribe_file(state)

    def responder(after, _limit):
        if after is None:
            return 200, _page_bytes(
                [_entry(event_a, 1)],
                next_cursor=1,
                has_more=True,
            )
        if after == 1:
            return 200, _page_bytes(
                [_entry(event_b, 2)],
                next_cursor=2,
                has_more=False,
            )
        return 200, _page_bytes([])

    export_server.state.responder = responder
    export_server.state.fail_on_call = 2

    first = landing_bridge.ingest_remote(
        state, _client(export_server), limit=1
    )
    assert first["failed"] == 1
    assert first["processed"] == 1
    assert first["cursor"] == 1
    assert len(_outbox_files(state)) == 1
    assert len(_inbox_files(state)) == 1
    assert json.loads(landing_bridge.export_cursor_path(state).read_text())[
        "cursor"
    ] == 1

    export_server.state.requests.clear()
    restarted = landing_bridge.ingest_remote(
        state, _client(export_server), limit=1
    )

    assert restarted["failed"] == 0
    assert restarted["processed"] == 1
    assert restarted["cursor"] == 2
    assert export_server.state.requests[0]["after"] == 1
    assert len(_outbox_files(state)) == 2
    assert len(_inbox_files(state)) == 2

    idle = landing_bridge.ingest_remote(state, _client(export_server), limit=1)
    assert idle["processed"] == 0
    assert idle["duplicates"] == 0
    assert len(_outbox_files(state)) == 2


# ---------------------------------------------------------------------------
# Production driver wiring
# ---------------------------------------------------------------------------


def test_factory_driver_prefers_remote_ingestion_over_the_worker_outbox(
    export_server, tmp_path: Path
):
    state, lease_remote = _state_with_lease(tmp_path, "att-driver-remote")
    _state_trap, lease_trap = _state_with_lease(tmp_path, "att-driver-trap")
    remote_event = _event(
        lease_remote, "att-driver-remote", transition="tr-driver-remote"
    )
    trap_event = _event(lease_trap, "att-driver-trap", transition="tr-driver-trap")
    _subscribe_file(state)

    trap_dir = tmp_path / "worker-outbox"
    trap_dir.mkdir()
    (trap_dir / f"{trap_event['id']}.json").write_text(
        json.dumps(trap_event), encoding="utf-8"
    )

    export_server.state.responder = lambda after, limit: (
        200,
        _page_bytes(
            [_entry(remote_event, "cursor-driver")],
            next_cursor="cursor-driver",
            has_more=False,
        ),
    )
    queue_path = tmp_path / "queue.json"
    queue_path.write_text("{}", encoding="utf-8")

    driver = factory_driver.FactoryDriver(
        state,
        queue_path,
        tick=lambda job_id: {"status": "idle"},
        launcher_outbox=trap_dir,
        landing_client=_client(export_server),
        subscribers=[SUBSCRIBER],
        startup=False,
    )
    result = driver.run_pass()

    assert result["landing"]["failed"] == 0
    assert result["landing"]["processed"] == 1
    outbox_ids = {path.stem for path in _outbox_files(state)}
    assert outbox_ids == {remote_event["id"]}
    assert trap_event["id"] not in outbox_ids
    assert len(_inbox_files(state)) == 1
    assert (trap_dir / f"{trap_event['id']}.json").is_file()


def test_production_main_wires_a_remote_landing_client_not_the_outbox(
    monkeypatch, tmp_path: Path
):
    captured: dict = {}

    class FakeDriver:
        def __init__(self, state, queue, **kwargs):
            captured.update(kwargs)

        def run_pass(self):
            return {"idle": True}

    monkeypatch.setenv("BUNDLEBEES_LAUNCHER_TOKEN", TOKEN)
    monkeypatch.setattr(factory_driver, "FactoryDriver", FakeDriver)
    outbox = tmp_path / "legacy-outbox"

    code = factory_driver.main(
        [
            "--state", str(tmp_path),
            "--once",
            "--launcher-url", "http://127.0.0.1:9",
            "--launcher-outbox", str(outbox),
        ]
    )

    assert code == 0
    assert captured["landing_client"] is not None
    assert captured["launcher_outbox"] is None


def test_legacy_hash_cursor_replays_from_the_beginning_once(export_server, tmp_path):
    state, lease = _state_with_lease(tmp_path, "att-legacy-cursor")
    _subscribe_file(state)
    event = _event(lease, "att-legacy-cursor", transition="tr-legacy")
    landing_bridge._write_export_cursor(
        landing_bridge.export_cursor_path(state), "a" * 64
    )
    seen = []

    def responder(after, _limit):
        seen.append(after)
        return 200, _page_bytes(
            [_entry(event, 1)], next_cursor=1, has_more=False
        )

    export_server.state.responder = responder
    result = landing_bridge.ingest_remote(state, _client(export_server), limit=1)

    assert result["failed"] == 0
    assert result["legacy_replay"] is True
    assert result["processed"] == 1
    assert seen == [None]
    assert json.loads(landing_bridge.export_cursor_path(state).read_text())["cursor"] == 1
    assert len(_outbox_files(state)) == 1
