"""Integration: the real approval protocol behind the rendered owner route.

Uses a temporary state directory and a test secret scope; no host state, no
worker, no launcher and no browser. The browser rehearsal is separate.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bundlebees_plugin import approval
from dashboard import plugin_api as API

KEY = "a" * 32
TASKS = [{
    "task_id": "T1",
    "objective": "Create smoke/answer.py that returns 42.",
    "lease": ["smoke/answer.py"],
    "acceptance": {
        "commands": ["/usr/bin/python3 -c \"import sys; sys.path.insert(0, '.'); import tests.test_smoke as t; t.test_answer_is_42()\""],
        "criteria": ["answer() == 42"],
        "tests": ["tests/test_smoke.py::test_answer_is_42"],
    },
}]


@pytest.fixture()
def real_route(tmp_path, monkeypatch):
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: KEY if name == "BUNDLEBEES_APPROVAL_SECRET" else default
    agent = types.ModuleType("agent")
    agent.secret_scope = scope
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    state = tmp_path / "state"
    state.mkdir()
    request = approval.create_request(
        state,
        job_id="restart-trial-20261115",
        statement="Run one tiny restart trial on the disposable smoke fixture.",
        repo="mojomast/rizzstack-smoke",
        base_rev="93b02f138b09db25a27fb79995d02ced460e75c1",
        scope=["smoke/answer.py"],
        tasks=TASKS,
        bees=1,
        wall_clock_s=900,
        worker_project="smoke-firejail5",
        worker_profile="smoke",
        code_rev=approval.code_revision(),
        coordinator_stop="after first dispatch, confirmed driver exit",
        restart="one restart, one 1200-second overall deadline",
        budgets={"attempts": 1, "accepted_waves": 1},
        ttl_s=900,
    )
    API.reset_state()
    API.set_data_dir(state)
    app = FastAPI()
    app.include_router(API.router, prefix="/api/plugins/bundlebees")
    with TestClient(app) as http:
        yield http, state, request
    API.reset_state()


def test_real_protocol_page_and_decision_use_the_same_receipt(real_route):
    http, state, request = real_route
    page = http.get(f"/api/plugins/bundlebees/approval/{request['request_id']}")
    assert page.status_code == 200
    assert request["request_digest"] in page.text
    assert "smoke-firejail5" in page.text

    payload = {"decision": "approve", "expected_digest": request["request_digest"]}
    first = http.post(f"/api/plugins/bundlebees/approvals/{request['request_id']}/decision", json=payload)
    second = http.post(f"/api/plugins/bundlebees/approvals/{request['request_id']}/decision", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["receipt"] == second.json()["receipt"]
    assert first.json()["receipt"]["decision"] == "approved"
    assert approval.load_receipt(state, request["request_id"], signing_key=KEY)["decision"] == "approved"

    settled = http.get(f"/api/plugins/bundlebees/approval/{request['request_id']}")
    assert settled.status_code == 200
    assert 'class="approve"' not in settled.text
    assert 'class="decline"' not in settled.text


def test_real_protocol_declined_receipt_cannot_authorize(real_route):
    http, state, request = real_route
    response = http.post(
        f"/api/plugins/bundlebees/approvals/{request['request_id']}/decision",
        json={"decision": "decline", "expected_digest": request["request_digest"]},
    )
    assert response.status_code == 200
    assert response.json()["receipt"]["decision"] == "declined"
    with pytest.raises(approval.ApprovalError) as caught:
        approval.consume_receipt(
            state,
            response.json()["receipt"],
            signing_key=KEY,
            consumer="job-authorization:test",
        )
    assert caught.value.code == "approval_declined"


def test_real_protocol_changed_digest_is_refused(real_route):
    http, _state, request = real_route
    response = http.post(
        f"/api/plugins/bundlebees/approvals/{request['request_id']}/decision",
        json={"decision": "approve", "expected_digest": "0" * 64},
    )
    assert response.status_code == 409
    assert "scope" in response.text or "changed" in response.text
