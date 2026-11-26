"""The attach step forwards findings for the gated failure, not a stray block.

WA-A5B attached the accepted findings artifact to the dispatched brief. Its
independent review found that a caller-supplied block short-circuits the attach
before the gated failure id is resolved: the research gate validates the state
artifact, ignores a supplied block whose ``failure_id`` differs, and the
mismatched block is then forwarded to the launcher. These tests pin the attach
step's own decisions (which id wins, what a supplied block has to match) and the
fail-closed branches the end-to-end suite cannot reach because
``research_gate.require_findings`` refuses before the helper runs.
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


def test_a_supplied_block_for_another_failure_never_reaches_the_launcher(
    tmp_path: Path,
) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    artifact = rg.record_findings(tmp_path, findings(queued["failure_id"]))
    stale = findings("some-other-id")
    brief = retry_brief(failure_id=queued["failure_id"], findings=stale)
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is True
    assert len(client.calls) == 1
    assert client.calls[0]["findings"] == artifact
    assert client.calls[0]["findings"]["failure_id"] == queued["failure_id"]


def test_a_stray_block_does_not_survive_a_retry_of_only_brief(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    artifact = rg.record_findings(tmp_path, findings(queued["failure_id"]))
    brief = retry_brief(findings=findings("some-other-id"))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is True
    assert client.calls[0]["findings"] == artifact


def test_a_supplied_block_for_the_gated_failure_is_still_preserved(tmp_path: Path) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    direct = findings(queued["failure_id"])
    direct["summary"] = "the caller supplied this accepted block"

    attached = tools._attach_accepted_findings(
        {"failure_id": queued["failure_id"], "findings": direct}, tmp_path
    )

    assert isinstance(attached, dict)
    assert attached["findings"] is direct


def test_a_missing_artifact_is_refused_rather_than_dispatched(tmp_path: Path) -> None:
    outcome = tools._attach_accepted_findings({"failure_id": "att-no-artifact"}, tmp_path)

    assert isinstance(outcome, str)
    payload = json.loads(outcome)
    assert payload["ok"] is False
    assert payload["code"] == "research_required"


def test_a_corrupt_artifact_is_refused_rather_than_dispatched(tmp_path: Path) -> None:
    path = rg.findings_path(tmp_path, "att-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    outcome = tools._attach_accepted_findings({"failure_id": "att-corrupt"}, tmp_path)

    assert isinstance(outcome, str)
    payload = json.loads(outcome)
    assert payload["ok"] is False
    assert payload["code"] == "research_corrupt"


def test_a_direct_block_without_a_gated_id_is_forwarded_untouched() -> None:
    direct = findings("some-other-id")

    attached = tools._attach_accepted_findings({"findings": direct}, None)

    assert isinstance(attached, dict)
    assert attached["findings"] is direct
