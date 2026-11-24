"""RED for wave A slice T1: ``bundlebees_status op=attempts`` is bounded.

Today the op returns every attempt (about 14.7k tokens for 296 attempts).
Default to the 20 newest compact rows, with a state filter and a ``detail``
switch for the full public row.
"""

from __future__ import annotations

import json

from tests.fakes import FakeCtx, load_plugin
from tests.test_tools import FakeClient

COMPACT_KEYS = {"attempt_id", "state", "project", "branch", "model", "updated_at"}


def _client(n: int = 50) -> FakeClient:
    client = FakeClient()
    client.attempts_data = [
        {
            "attempt_id": f"att_{i:03d}",
            "task_id": f"t{i}",
            "state": "running" if i % 5 == 0 else "succeeded",
            "project": "proj",
            "branch": f"feat/{i}",
            "model": "flash",
            "created_at": f"2026-11-{1 + i // 10:02d}T00:{i % 60:02d}:00Z",
            "updated_at": f"2026-11-24T00:{i % 60:02d}:00Z",
            "started_at": "2026-11-24T00:00:00Z",
            "event_cursor": i,
            "kanban_task_id": f"k{i}",
        }
        for i in range(n)
    ]
    return client


def _status(args, client):
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    ctx.bundlebees_client = client
    return json.loads(ctx.tools["bundlebees_status"]["handler"](args)), ctx


def test_default_is_twenty_newest_compact_rows():
    payload, _ = _status({"op": "attempts"}, _client(50))
    assert payload["ok"] is True
    rows = payload["attempts"]
    assert len(rows) == 20
    assert payload["total"] == 50
    assert payload["truncated"] is True
    assert rows[0]["attempt_id"] == "att_049"
    assert all(set(row) <= COMPACT_KEYS for row in rows)


def test_limit_is_honoured_and_capped():
    payload, _ = _status({"op": "attempts", "limit": 5}, _client(50))
    assert len(payload["attempts"]) == 5
    payload, _ = _status({"op": "attempts", "limit": 10_000}, _client(300))
    assert len(payload["attempts"]) == 200


def test_state_filter():
    payload, _ = _status({"op": "attempts", "state": "running", "limit": 100}, _client(50))
    assert payload["total"] == 10
    assert {row["state"] for row in payload["attempts"]} == {"running"}
    assert payload["truncated"] is False


def test_detail_returns_full_public_rows():
    payload, _ = _status({"op": "attempts", "detail": True, "limit": 3}, _client(10))
    assert len(payload["attempts"]) == 3
    assert "kanban_task_id" in payload["attempts"][0]
    assert "event_cursor" in payload["attempts"][0]


def test_small_lists_are_not_marked_truncated():
    payload, _ = _status({"op": "attempts"}, _client(3))
    assert payload["total"] == 3 and payload["truncated"] is False


def test_schema_declares_the_new_arguments():
    _, ctx = _status({"op": "health"}, _client(1))
    props = ctx.tools["bundlebees_status"]["schema"]["parameters"]["properties"]
    assert props["limit"]["type"] == "integer"
    assert props["state"]["type"] == "string"
    assert props["detail"]["type"] == "boolean"
