"""A retry brief carries its accepted findings to the launcher.

The research gate proves accepted findings exist before a retry is allowed,
but the launcher renders ``brief.findings`` into the worker prompt. Without an
attach step the proof stays in the state directory and the bee retries blind.
These tests pin the brief that ``client.create`` actually receives.
"""

from __future__ import annotations

import json
from pathlib import Path

from bundlebees_plugin import research_gate as rg
from bundlebees_plugin import tools

WF = "c" * 40
REV = "a" * 40


def failure(**over):
    base = {
        "kind": "verify",
        "code": "tests_failed",
        "message": "/home/one/repo/tests/test_alpha.py::test_one failed\nassert 1 == 2",
        "failing_tests": ["tests/test_alpha.py::test_one"],
        "attempt_id": "att-first",
        "versions": {"tool": "bundlebees-0.2", "runtime": "python-3.11"},
        "repo": "org/repo",
    }
    base.update(over)
    return base


def findings(fid: str, verdict: str = "patch-bug") -> dict:
    return {
        "schema": "bundlebees.research-findings/1",
        "failure_id": fid,
        "sources": [{"kind": "failing-test", "ref": "tests/test_alpha.py::test_one"}],
        "rejected_alternatives": ["retry without a fix (duplicate failure)"],
        "verdict": verdict,
        "summary": "the parser drops the trailing comma",
    }


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


def retry_brief(**over) -> dict:
    brief = {
        "objective": "retry the failed work with the researched fix",
        "source_rev": REV,
        "assigned_files": ["pkg/x.py"],
        "acceptance": {"criteria": ["the failing test passes"]},
        "retry_of": "att-first",
    }
    brief.update(over)
    return brief


def test_a_retry_brief_carries_the_accepted_findings_to_the_launcher(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    artifact = rg.record_findings(tmp_path, findings(queued["failure_id"]))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is True
    assert len(client.calls) == 1
    assert client.calls[0]["findings"] == artifact


def test_an_explicit_failure_id_also_carries_its_findings(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    artifact = rg.record_findings(tmp_path, findings(queued["failure_id"]))
    brief = retry_brief()
    brief.pop("retry_of")
    brief["failure_id"] = queued["failure_id"]
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is True
    assert client.calls[0]["findings"] == artifact


def test_a_direct_findings_block_is_not_replaced_by_the_state_copy(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    direct = findings(queued["failure_id"])
    direct["summary"] = "the caller supplied this accepted block"
    brief = retry_brief(failure_id=queued["failure_id"], findings=direct)
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is True
    assert client.calls[0]["findings"] == direct


def test_a_brief_without_a_gated_failure_carries_no_findings(tmp_path: Path) -> None:
    brief = retry_brief()
    brief.pop("retry_of")
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is True
    assert "findings" not in client.calls[0]


def test_the_caller_brief_is_not_mutated_by_the_attach(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    brief = retry_brief()
    client = FakeClient()

    json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert "findings" not in brief
