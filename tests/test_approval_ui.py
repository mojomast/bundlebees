"""Owner-facing Bundlebees approval routes: card projection, decisions, refusals."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import plugin_api as API


class FakeApprovalError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


class FakeApproval:
    def __init__(self) -> None:
        self.decisions: list[tuple] = []
        self.fail: tuple[str, str] | None = None
        self.existing_receipt: dict | None = None
        self.receipt = {
            "schema": "rizzstack.job-approval-decision/1",
            "request_id": "apr-0123456789abcdef",
            "decision": "approved",
            "request_digest": "d" * 64,
            "decided_at": 1_800_000_100.0,
            "consumed_at": None,
        }
        self.request = {
            "schema": "rizzstack.job-approval/1",
            "request_id": "apr-0123456789abcdef",
            "request_digest": "d" * 64,
            "created_at": 1_800_000_000.0,
            "expires_at": 1_800_000_900.0,
            "status": "pending",
            "statement": "Run one tiny disposable restart trial.",
            "binding": {
                "job_id": "restart-trial-20261115",
                "repo": "mojomast/rizzstack-smoke",
                "base_rev": "93b02f138b09db25a27fb79995d02ced460e75c1",
                "scope": ["smoke/answer.py"],
                "tasks": [
                    {
                        "task_id": "T1",
                        "objective": "Create smoke/answer.py that returns 42.",
                        "lease": ["smoke/answer.py"],
                    }
                ],
                "worker": {"project": "smoke-firejail5", "profile": "smoke"},
                "code_rev": "665d3f484878f4926625d11760e4363f6f7f4db1",
                "budgets": {
                    "bees": 1,
                    "wall_clock_s": 900,
                    "attempts": 1,
                    "accepted_waves": 1,
                },
                "coordinator": {
                    "stop": {"after": "first dispatch", "driver_exit_confirmed": True},
                    "restart": {"coordinator_restarts": 1, "deadline_s": 1200},
                },
                "internal_path": "/home/owner/private",
                "token": "leakedvalue",
            },
        }

    def list_pending(self, state_dir):
        return [dict(self.request)]

    def owner_capability(self, request_id):
        return "owner-capability-for-" + request_id

    def load_receipt(self, state_dir, request_id):
        if self.existing_receipt is not None:
            return dict(self.existing_receipt)
        raise FakeApprovalError("approval_receipt_missing", "no receipt yet")

    def load_request(self, state_dir, request_id):
        if request_id != self.request["request_id"]:
            raise FakeApprovalError("approval_missing", "no such approval request")
        return dict(self.request)

    def record_decision(self, state_dir, request_id, *, decision, expected_digest, owner, auth_method, owner_capability=None, signing_key=None, now=None):
        self.decisions.append((request_id, decision, expected_digest, owner, auth_method, owner_capability))
        if self.fail:
            code, message = self.fail
            raise FakeApprovalError(code, message)
        if expected_digest != self.request["request_digest"]:
            raise FakeApprovalError("approval_digest_mismatch", "the preview changed")
        if decision not in {"approved", "declined"}:
            raise FakeApprovalError("approval_invalid", "decision must be approved or declined")
        return dict(self.receipt, decision=decision)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    fake = FakeApproval()
    module = types.ModuleType("bundlebees_plugin.approval")
    module.FakeApproval = FakeApproval
    module.ApprovalError = FakeApprovalError
    module.owner_capability = fake.owner_capability
    module.list_pending = fake.list_pending
    module.load_request = fake.load_request
    module.load_receipt = fake.load_receipt
    module.record_decision = fake.record_decision
    monkeypatch.setitem(sys.modules, "bundlebees_plugin.approval", module)
    API.reset_state()
    API.set_data_dir(tmp_path)
    app = FastAPI()
    app.include_router(API.router, prefix="/api/plugins/bundlebees")
    with TestClient(app) as test:
        yield test, fake
    API.reset_state()


def test_approval_page_is_plain_language_and_binds_exact_fields(client):
    http, _fake = client
    response = http.get("/api/plugins/bundlebees/approval/apr-0123456789abcdef")
    assert response.status_code == 200
    page = response.text
    assert "Bundlebees asks for one-time approval" in page
    assert "Approve once" in page
    assert "Decline" in page
    assert "restart-trial-20261115" in page
    assert "mojomast/rizzstack-smoke" in page
    assert "93b02f138b09db25a27fb79995d02ced460e75c1" in page
    assert "smoke-firejail5" in page
    assert "smoke" in page
    assert "smoke/answer.py" in page
    assert "1 bee(s); 900 seconds; 1 attempt(s); 1 accepted wave(s)" in page
    assert "does not approve a merge, release, deployment, publication or final product acceptance" in page
    assert "leakedvalue" not in page
    assert "/home/owner/private" not in page


def test_approvals_list_has_only_public_card_fields(client):
    http, _fake = client
    response = http.get("/api/plugins/bundlebees/approvals")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    row = body["approvals"][0]
    assert row["request_id"] == "apr-0123456789abcdef"
    assert row["job_id"] == "restart-trial-20261115"
    assert "internal_path" not in row
    assert "token" not in str(row)


def test_decision_requires_exact_digest(client):
    http, fake = client
    response = http.post(
        "/api/plugins/bundlebees/approvals/apr-0123456789abcdef/decision",
        json={"decision": "approve", "expected_digest": "wrong"},
    )
    assert response.status_code == 400
    assert len(fake.decisions) == 1
    assert fake.decisions[0][2] == "wrong"
    assert "preview changed" in response.text


def test_decision_rejects_unknown_decision(client):
    http, fake = client
    response = http.post(
        "/api/plugins/bundlebees/approvals/apr-0123456789abcdef/decision",
        json={"decision": "maybe", "expected_digest": "d" * 64},
    )
    assert response.status_code == 400
    assert fake.decisions == []


def test_decision_is_idempotent_for_same_request(client):
    http, fake = client
    payload = {"decision": "approve", "expected_digest": "d" * 64}
    first = http.post("/api/plugins/bundlebees/approvals/apr-0123456789abcdef/decision", json=payload)
    second = http.post("/api/plugins/bundlebees/approvals/apr-0123456789abcdef/decision", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["receipt"] == second.json()["receipt"]
    assert len(fake.decisions) == 2
    assert fake.decisions[0][1] == "approved"
    assert fake.decisions[0][5] == "owner-capability-for-apr-0123456789abcdef"


def test_missing_request_is_not_a_usable_card(client):
    http, _fake = client
    response = http.get("/api/plugins/bundlebees/approval/apr-missing")
    assert response.status_code == 410
    assert "no such approval request" in response.text
    assert "Approve once" not in response.text


def test_expired_decision_is_refused(client):
    http, fake = client
    fake.fail = ("approval_expired", "the approval request has expired")
    response = http.post(
        "/api/plugins/bundlebees/approvals/apr-0123456789abcdef/decision",
        json={"decision": "approve", "expected_digest": "d" * 64},
    )
    assert response.status_code == 410
    assert "expired" in response.text


def test_after_restart_a_settled_receipt_is_not_approvable_again(client):
    http, fake = client
    fake.existing_receipt = dict(fake.receipt)
    response = http.get("/api/plugins/bundlebees/approval/apr-0123456789abcdef")
    assert response.status_code == 200
    assert 'class="approve"' not in response.text
    assert 'class="decline"' not in response.text


def test_an_expired_request_offers_no_buttons(client):
    http, fake = client
    fake.request = dict(fake.request)
    fake.request["expires_at"] = "2000-01-01T00:00:00Z"
    response = http.get("/api/plugins/bundlebees/approval/apr-0123456789abcdef")
    assert response.status_code == 200
    assert "expired" in response.text
    assert 'class="approve"' not in response.text


def test_card_does_not_render_internal_state_from_binding(client):
    page = client[0].get("/api/plugins/bundlebees/approval/apr-0123456789abcdef").text
    assert "result-token" not in page
    assert "authorization" not in page.lower() or "not approve" in page.lower()
