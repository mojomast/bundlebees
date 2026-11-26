"""WA-A5B-FUP2: the retry-findings paths the WA-A5B-FUP1 review left open.

The independent review of PR #311 left three minors, all about which failure's
research a retry is allowed to carry:

1. ``tools._gated_failure_id`` returns ``""`` for a truthy non-string explicit
   ``failure_id``. ``_attach_accepted_findings`` reads that empty string as
   "nothing resolved" and returns the caller's brief untouched, so a DIRECT
   helper call carrying a block for another failure keeps that block. It is not
   reachable through ``_dispatch`` today only because
   ``research_gate.require_findings`` refuses a non-string id first.
2. The ``retry_of``-resolved id path had no pin that a supplied block matching
   that failure is preserved unchanged; AC-003 covered the explicit-id path.
3. A brief carrying both ``failure_id`` and a disagreeing ``retry_of`` is
   accepted whenever both failures have accepted findings, and the explicit
   id's block is attached — the worker retrying B is rendered A's research.

These tests use the real research stores (``enqueue_research`` /
``record_findings``) so every id they assert on is resolved by the same code
the dispatch gate uses. They pin the helper's own decisions and the gate's
both-id rule; nothing here weakens a valid path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import flags
from bundlebees_plugin import research_gate as rg
from bundlebees_plugin import tools

WF = "c" * 40
REV = "a" * 40


@pytest.fixture(autouse=True)
def _attach_enabled(monkeypatch):
    """These pin the enabled attach path; the dark default has its own suite."""

    monkeypatch.setenv(flags.FLAG_ATTACH_FINDINGS, "1")


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


def two_failures(state: Path) -> tuple[dict, dict]:
    """Two distinct recorded failures, each with accepted findings."""
    first = rg.enqueue_research(
        state, failure(attempt_id="att-a"), repo="org/repo", workflow_ref=WF
    )
    second = rg.enqueue_research(
        state,
        failure(
            attempt_id="att-b",
            message="/home/one/repo/tests/test_beta.py::test_two failed\nassert 2 == 3",
            failing_tests=["tests/test_beta.py::test_two"],
        ),
        repo="org/repo",
        workflow_ref=WF,
    )
    assert first["failure_id"] != second["failure_id"]
    rg.record_findings(state, findings(first["failure_id"]))
    rg.record_findings(state, findings(second["failure_id"]))
    return first, second


def retry_brief(**over) -> dict:
    brief = {
        "objective": "retry the failed work with the researched fix",
        "source_rev": REV,
        "assigned_files": ["pkg/x.py"],
        "acceptance": {"criteria": ["the failing test passes"]},
    }
    brief.update(over)
    return brief


# -- finding 1: a truthy non-string id must be refused, never resolved to "" ----


def test_gated_failure_id_refuses_a_truthy_non_string_id(tmp_path: Path) -> None:
    with pytest.raises(rg.ResearchGateError) as caught:
        tools._gated_failure_id({"failure_id": 5}, tmp_path)
    assert caught.value.code == "research_required"
    assert "failure_id" in caught.value.message
    assert "string" in caught.value.message


def test_attach_refuses_a_non_string_id_instead_of_forwarding_the_stray_block(
    tmp_path: Path,
) -> None:
    first, _second = two_failures(tmp_path)
    stray = findings("some-other-id")
    stray["failure_id"] = "some-other-id"

    result = tools._attach_accepted_findings(
        {"failure_id": 5, "findings": stray}, tmp_path
    )

    assert isinstance(result, str), (
        "the helper must refuse a non-string explicit failure_id instead of "
        "returning a brief that still carries the caller's block for another failure"
    )
    envelope = json.loads(result)
    assert envelope["ok"] is False
    assert envelope["code"] == "research_required"
    assert "some-other-id" not in result


# -- finding 2: the retry_of path preserves a matching block (AC-003 parity) ----


def test_a_supplied_block_matching_the_retry_of_failure_is_preserved(
    tmp_path: Path,
) -> None:
    first = rg.enqueue_research(
        tmp_path, failure(attempt_id="att-first"), repo="org/repo", workflow_ref=WF
    )
    rg.record_findings(tmp_path, findings(first["failure_id"]))
    supplied = findings(first["failure_id"])
    supplied["summary"] = "the caller supplied this accepted block"
    brief = retry_brief(retry_of="att-first", findings=supplied)

    attached = tools._attach_accepted_findings(dict(brief), tmp_path)

    assert isinstance(attached, dict), attached
    assert attached["findings"] is supplied, (
        "a supplied block that names the retry_of-resolved failure is preserved "
        "byte-identically, exactly like the explicit-id path"
    )
    assert attached["findings"] == supplied


# -- finding 3: both ids must agree before anything is attached ----------------


def test_a_brief_whose_explicit_and_retry_of_ids_disagree_is_refused(
    tmp_path: Path,
) -> None:
    first, second = two_failures(tmp_path)
    brief = retry_brief(
        failure_id=first["failure_id"],
        retry_of="att-b",
        findings=findings(first["failure_id"]),
    )

    refusal = rg.require_findings(tmp_path, brief)

    assert refusal is not None, "a contradictory brief must fail closed"
    assert refusal["ok"] is False
    assert refusal["code"] == "research_required"
    assert first["failure_id"] in refusal["error"]
    assert second["failure_id"] in refusal["error"]


def test_dispatch_refuses_the_disagreeing_brief_before_any_launcher_call(
    tmp_path: Path,
) -> None:
    first, second = two_failures(tmp_path)
    brief = retry_brief(
        failure_id=first["failure_id"],
        retry_of="att-b",
        findings=findings(first["failure_id"]),
    )
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is False, result
    assert result["code"] == "research_required"
    assert first["failure_id"] in result["error"]
    assert second["failure_id"] in result["error"]
    assert client.calls == [], "nothing may reach the launcher for a contradictory brief"


def test_a_brief_whose_explicit_and_retry_of_ids_agree_is_accepted(
    tmp_path: Path,
) -> None:
    first = rg.enqueue_research(
        tmp_path, failure(attempt_id="att-a"), repo="org/repo", workflow_ref=WF
    )
    rg.record_findings(tmp_path, findings(first["failure_id"]))
    supplied = findings(first["failure_id"])
    brief = retry_brief(
        failure_id=first["failure_id"], retry_of="att-a", findings=supplied
    )

    assert rg.require_findings(tmp_path, brief) is None

    attached = tools._attach_accepted_findings(dict(brief), tmp_path)

    assert isinstance(attached, dict), attached
    assert attached["findings"] is supplied
