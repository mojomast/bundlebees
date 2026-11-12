"""Approved acceptance tests for FL-2 (contract FL2-task-id, RED first).

Orbit keys local-worker attempts by ``task_id``: a new ``create`` with the same
task_id supersedes and cancels the previous active attempt. bundlebees used to
send no task_id, so every attempt shared ``task_id=undefined`` and a parallel
dispatch cancelled its sibling. ``OrbitAdapterClient.create`` must always send
a non-empty task_id: the explicit ``task_id`` argument, else ``kanban_task_id``,
else ``"bb-" + sha256(canonical brief JSON)[:24]`` (canonical = ``json.dumps(brief,
sort_keys=True, separators=(",", ":"))``). Offline: a fake Orbit that applies
Orbit's supersede-by-task_id rule.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bundlebees_plugin.client import OrbitAdapterClient

TOKEN = "orbit-test-token"


class SupersedingOrbit:
    """Fake Orbit create: one active attempt per task_id, like server/local-workers.mjs."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.attempts: dict[str, dict] = {}
        orbit = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                orbit.requests.append(body)
                if body.get("op") != "create":
                    status, payload = 400, {"error": "unsupported op in fake"}
                else:
                    task = body.get("task_id")
                    key = "undefined" if task is None else str(task)
                    for att in orbit.attempts.values():
                        if att["task_key"] == key and att["state"] == "queued":
                            att["state"] = "superseded"
                    aid = "att_%016x" % (len(orbit.attempts) + 1)
                    orbit.attempts[aid] = {"task_key": key, "state": "queued"}
                    status, payload = 201, {"attempt_id": aid, "state": "queued", "task_id": task}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def orbit():
    o = SupersedingOrbit()
    yield o
    o.close()


def _client(o, tmp_path):
    return OrbitAdapterClient(o.url, token_provider=lambda: TOKEN, capability_dir=tmp_path / "caps")


BRIEF_A = {"objective": "slice A", "project": "p", "source_rev": "a" * 40, "assigned_files": ["a.py"], "acceptance": ["x"]}
BRIEF_B = {"objective": "slice B", "project": "p", "source_rev": "a" * 40, "assigned_files": ["b.py"], "acceptance": ["y"]}


def _derived(brief):
    canon = json.dumps(brief, sort_keys=True, separators=(",", ":"))
    return "bb-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


# AC-001
def test_parallel_dispatch_keeps_both_siblings_active(orbit, tmp_path):
    c = _client(orbit, tmp_path)
    a = c.create(BRIEF_A)
    b = c.create(BRIEF_B)
    assert orbit.attempts[a["attempt_id"]]["state"] == "queued"
    assert orbit.attempts[b["attempt_id"]]["state"] == "queued"
    sent = [r.get("task_id") for r in orbit.requests if r.get("op") == "create"]
    assert all(isinstance(t, str) and t for t in sent)
    assert sent[0] != sent[1]


# AC-002
def test_task_id_without_kanban_is_derived_from_the_brief(orbit, tmp_path):
    c = _client(orbit, tmp_path)
    c.create(BRIEF_A)
    sent = orbit.requests[-1]["task_id"]
    assert re.fullmatch(r"bb-[0-9a-f]{24}", sent)
    assert sent == _derived(BRIEF_A)


# AC-003
def test_explicit_task_id_wins_then_kanban_task_id(orbit, tmp_path):
    c = _client(orbit, tmp_path)
    c.create(BRIEF_A, task_id="t_explicit", kanban_task_id="t_kanban")
    assert orbit.requests[-1]["task_id"] == "t_explicit"
    assert orbit.requests[-1]["kanban_task_id"] == "t_kanban"
    c.create(BRIEF_B, kanban_task_id="t_kanban2")
    assert orbit.requests[-1]["task_id"] == "t_kanban2"


# AC-004 (regression): re-dispatching the same task still supersedes, as Orbit intends.
def test_redispatch_of_the_same_task_still_supersedes(orbit, tmp_path):
    c = _client(orbit, tmp_path)
    first = c.create(BRIEF_A)
    second = c.create(dict(BRIEF_A))
    assert orbit.attempts[first["attempt_id"]]["state"] == "superseded"
    assert orbit.attempts[second["attempt_id"]]["state"] == "queued"
