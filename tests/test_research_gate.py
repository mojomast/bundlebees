"""RESEARCH-BEFORE-RETRY is mechanical, not a prompt rule.

These tests pin the executable rule: a failed verify (or the same failure
signature in the same lineage) enqueues exactly one research action and one
research work item; a retry is refused until accepted findings exist; an
environment-problem verdict lands in the gate-watcher decision queue and never
becomes a retry; and an unavailable queue fails closed. The durable stores are
the existing coordinator action queue, work scheduler and gate-watcher
backlog — this module adds evidence, not another queue.
"""

from __future__ import annotations

import json
from pathlib import Path

from bundlebees_plugin import coordinator_actions as ca
from bundlebees_plugin import gate_watcher as gw
from bundlebees_plugin import research_gate as rg
from bundlebees_plugin import tools
from bundlebees_plugin import verify_workers
from bundlebees_plugin import work_scheduler as ws
from bundlebees_plugin.security import contains_address

WF = "c" * 40
REV = "a" * 40


def failure(**over):
    base = {
        "kind": "verify",
        "code": "tests_failed",
        "message": "/home/one/repo/tests/test_alpha.py::test_one failed at 2026-11-18T10:00:00Z\nassert 1 == 2",
        "failing_tests": ["tests/test_alpha.py::test_one"],
        "attempt_id": "att-first",
        "versions": {"tool": "bundlebees-0.2", "runtime": "python-3.11"},
        "repo": "org/repo",
    }
    base.update(over)
    return base


def research_actions(state: Path):
    return [item for item in ca.list_actions(state) if item["kind"] == "research"]


def research_items(state: Path):
    return [item for item in ws.list_items(state) if item["kind"] == "research"]


def direct_actions(state: Path, action_id: str):
    path = ca.action_path(state, action_id)
    return path.read_bytes()


def retry_brief(**over):
    brief = {
        "objective": "retry the failed work with the researched fix",
        "source_rev": REV,
        "assigned_files": ["pkg/x.py"],
        "acceptance": {"criteria": ["the failing test passes"]},
        "retry_of": "att-first",
    }
    brief.update(over)
    return brief


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def projects(self):
        return []

    def attempts(self):
        return []

    def create(self, brief, **kwargs):
        self.calls.append(dict(brief))
        return {"attempt_id": "att-retry", "state": "queued"}


def findings(fid: str, verdict: str = "patch-bug"):
    return {
        "schema": "bundlebees.research-findings/1",
        "failure_id": fid,
        "sources": [{"kind": "failing-test", "ref": "tests/test_alpha.py::test_one"}],
        "rejected_alternatives": ["retry without a fix (duplicate failure)"],
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# AC-1: one failure -> exactly one research action and one research work item
# ---------------------------------------------------------------------------


def test_a_fixture_failure_enqueues_exactly_one_research_action_and_work_item(
    tmp_path: Path,
) -> None:
    result = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)

    assert result["ok"] is True
    assert result["created"] is True
    actions = research_actions(tmp_path)
    items = research_items(tmp_path)
    assert len(actions) == 1
    assert len(items) == 1

    action = actions[0]
    item = items[0]
    assert action["kind"] == ca.RESEARCH_KIND == "research"
    assert action["action_key"] == "research:" + result["research_key"]
    assert action["payload"]["failure_id"] == result["failure_id"]
    assert action["payload"]["signature"] == result["signature"]
    assert action["payload"]["versions"] == {
        "runtime": "python-3.11",
        "tool": "bundlebees-0.2",
    }
    assert item["kind"] == "research"
    assert result["failure_id"] in item["title"]
    assert result["signature"] in item["title"]

    blob = json.dumps({"action": action, "item": item}, sort_keys=True)
    assert not contains_address(blob), blob


# ---------------------------------------------------------------------------
# AC-2: replay is byte-identical; a new lineage is a new item
# ---------------------------------------------------------------------------


def test_duplicate_failure_is_a_no_op_and_a_new_lineage_enqueues_one(tmp_path: Path) -> None:
    first = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    action_bytes = direct_actions(tmp_path, first["action"]["action_id"])
    queue_bytes = ws.queue_path(tmp_path).read_bytes()

    replay = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)

    assert replay["created"] is False
    assert replay["action"] == first["action"]
    assert replay["work_item"] == first["work_item"]
    assert len(research_actions(tmp_path)) == 1
    assert len(research_items(tmp_path)) == 1
    assert direct_actions(tmp_path, first["action"]["action_id"]) == action_bytes
    assert ws.queue_path(tmp_path).read_bytes() == queue_bytes

    other = rg.enqueue_research(
        tmp_path,
        failure(attempt_id="att-second"),
        repo="org/repo",
        workflow_ref=WF,
    )
    assert other["created"] is True
    assert other["signature"] == first["signature"]
    assert other["research_key"] != first["research_key"]
    assert len(research_actions(tmp_path)) == 2
    assert len(research_items(tmp_path)) == 2


def test_signature_excludes_time_attempt_run_and_absolute_path_noise(tmp_path: Path) -> None:
    one = failure(
        attempt_id="att-one",
        message=(
            "2026-11-18T10:00:00Z /home/alice/repo/tests/test_alpha.py::test_one "
            "att-one run-9 failed\nassert 1 == 2"
        ),
    )
    two = failure(
        attempt_id="att-two",
        message=(
            "2027-01-01T01:02:03Z /tmp/other/repo/tests/test_alpha.py::test_one "
            "att-two run-77 failed\nassert 1 == 2"
        ),
    )
    assert rg.failure_signature(one) == rg.failure_signature(two)


# ---------------------------------------------------------------------------
# AC-3: _dispatch refuses blind retries, allows accepted findings
# ---------------------------------------------------------------------------


def test_dispatch_refuses_a_retry_until_accepted_findings_exist(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    client = FakeClient()

    refused = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))
    assert refused["ok"] is False
    assert refused["code"] == "research_required"
    assert queued["failure_id"] in refused["error"]
    assert client.calls == []

    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    allowed = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))
    assert allowed["ok"] is True
    assert len(client.calls) == 1


def test_a_brief_carrying_a_failure_id_is_gated_too(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    client = FakeClient()
    brief = retry_brief()
    brief.pop("retry_of")
    brief["failure_id"] = queued["failure_id"]

    refused = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))
    assert refused["code"] == "research_required"
    assert client.calls == []

    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    assert json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))["ok"] is True


# ---------------------------------------------------------------------------
# AC-4: environment-problem goes to the decision queue, never a retry
# ---------------------------------------------------------------------------


def test_environment_problem_lands_in_the_decision_queue_and_never_retries(
    tmp_path: Path,
) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)

    routed = rg.route_verdict(tmp_path, queued["failure_id"], "environment-problem")
    assert routed["ok"] is True
    assert routed["retry_allowed"] is False
    backlog = gw.load_backlog(tmp_path)
    assert any(slice_["id"] == routed["slice_id"] for slice_ in backlog["slices"])
    assert any(
        decision["slice_id"] == routed["slice_id"]
        for decision in backlog["decisions"]
    )
    assert [a for a in ca.list_actions(tmp_path) if a["kind"] == "retry"] == []

    rg.record_findings(tmp_path, findings(queued["failure_id"], verdict="patch-bug"))
    client = FakeClient()
    refused = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))
    assert refused["ok"] is False
    assert refused["code"] == "environment_problem"
    assert client.calls == []


def test_patch_bug_records_repair_intent_and_unknown_stays_blocked(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)

    repair = rg.route_verdict(tmp_path, queued["failure_id"], "patch-bug")
    assert repair["ok"] is True and repair["retry_allowed"] is True
    assert repair["repair_action"]["kind"] == "repair"

    unknown = rg.route_verdict(tmp_path, queued["failure_id"], "unknown")
    assert unknown["ok"] is True and unknown["retry_allowed"] is False
    rg.record_findings(tmp_path, findings(queued["failure_id"], verdict="unknown"))
    refused = json.loads(tools._dispatch(FakeClient(), {"brief": retry_brief()}, tmp_path))
    assert refused["code"] == "research_inconclusive"


# ---------------------------------------------------------------------------
# AC-5: queue unavailability and unreadable research state fail closed
# ---------------------------------------------------------------------------


def test_queue_unavailability_is_a_typed_refusal_not_a_silent_success(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "state-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")

    result = rg.enqueue_research(blocked, failure(), repo="org/repo", workflow_ref=WF)

    assert result["ok"] is False
    assert result["code"] == "queue_unavailable"
    assert result["failure_id"]


def test_unreadable_findings_fail_closed_and_do_not_dispatch(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    path = rg.findings_path(tmp_path, queued["failure_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    client = FakeClient()
    refused = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))
    assert refused["ok"] is False
    assert refused["code"] == "research_required"
    assert client.calls == []


def test_invalid_findings_do_not_unlock_the_retry(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(
        tmp_path,
        {
            "failure_id": queued["failure_id"],
            "sources": [],
            "rejected_alternatives": [],
            "verdict": "maybe",
        },
    )
    problems = rg.validate_findings(
        json.loads(rg.findings_path(tmp_path, queued["failure_id"]).read_text()),
        queued["failure_id"],
    )
    assert problems
    refused = json.loads(tools._dispatch(FakeClient(), {"brief": retry_brief()}, tmp_path))
    assert refused["code"] == "research_required"


# ---------------------------------------------------------------------------
# Trigger: publish_verify_result enqueues research without changing its record
# ---------------------------------------------------------------------------


def _claimed_verify(state: Path, attempt_id: str) -> None:
    ca.enqueue(
        state,
        verify_workers.verify_action_id(attempt_id),
        action_key=verify_workers.verify_action_key(attempt_id),
        kind="verify",
        workflow_ref=WF,
        payload={
            "repo": "org/repo",
            "tests": ["tests/test_alpha.py::test_one"],
        },
    )
    ca.claim(state, verify_workers.verify_action_id(attempt_id), "verify-bee", now=1000.0)


def test_publish_failure_triggers_exactly_one_research_item_and_keeps_the_record(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _claimed_verify(state, "att-first")
    produced = {
        "ok": False,
        "fence_seq": 1,
        "code": "tests_failed",
        "message": "tests/test_alpha.py::test_one failed\nassert 1 == 2",
        "fix": "patch the bug",
        "versions": {"tool": "bundlebees-0.2", "runtime": "python-3.11"},
    }

    published = verify_workers.publish_verify_result(state, "att-first", produced, now=1001.0)
    assert published["ok"] is False
    assert published["result"]["code"] == "tests_failed"
    assert len(research_actions(state)) == 1
    assert len(research_items(state)) == 1

    duplicate = rg.enqueue_research(
        state,
        {
            "kind": "verify",
            "code": "tests_failed",
            "message": produced["message"],
            "attempt_id": "att-first",
            "failing_tests": ["tests/test_alpha.py::test_one"],
            "versions": produced["versions"],
            "repo": "org/repo",
        },
        repo="org/repo",
        workflow_ref=WF,
    )
    assert duplicate["created"] is False
    assert len(research_actions(state)) == 1
    assert len(research_items(state)) == 1
    stored = ca.load(state, verify_workers.verify_action_id("att-first"))
    assert stored["result"]["ok"] is False
    assert stored["result"]["code"] == "tests_failed"


def test_a_research_enqueue_error_never_escapes_or_rewrites_the_verify_record(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    _claimed_verify(state, "att-first")

    def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(rg, "enqueue_research", boom)
    published = verify_workers.publish_verify_result(
        state,
        "att-first",
        {
            "ok": False,
            "fence_seq": 1,
            "code": "tests_failed",
            "message": "tests/test_alpha.py::test_one failed",
        },
        now=1001.0,
    )

    assert published["ok"] is False
    stored = ca.load(state, verify_workers.verify_action_id("att-first"))
    assert stored["result"]["ok"] is False
    # record_failure still marked the failure as needing research
    marker = rg.failure_path(state, "att-first")
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["needs_research"] is True
