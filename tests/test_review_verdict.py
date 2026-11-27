"""Verdict validation and posting (program: automated outside review, Phase 1.4).

The coordinator validates a review bee's ``rizzstack.review-verdict/1`` before anything is
posted: the schema, the binding to the packet (repo/pr/head), the head's stability since
dispatch, the model-family independence, and the blocking evidence. ``post_mode`` is
``auto_reject`` for now: a ``changes_requested`` verdict is posted, an ``approve`` is queued
for the owner's confirmation, and ``needs_owner`` posts nothing.

Run: python -m pytest tests/test_review_verdict.py -q
"""

from __future__ import annotations

import pytest

from bundlebees_plugin.review_verdict import (
    VerdictError,
    check_binding,
    post_verdict,
    record_line,
    render_review,
    validate_verdict,
)

HEAD = "1a1d032f98691fb4121d5b9673b36562c77a5627"
PACKET = {"repo": "mojomast/rizzstack", "pr": 41, "head_sha": HEAD, "author_model_family": "unknown"}
REVIEWER = {"identity": "rizzstack-reviewer", "model": "k3.1-pro", "model_family": "kimi"}


def _verdict(**overrides):
    value = {
        "verdict": "approve",
        "repo": "mojomast/rizzstack",
        "pr": 41,
        "head_sha": HEAD,
        "reviewer": dict(REVIEWER),
        "review_types": ["general"],
        "checked": ["read the diff", "ran the tests"],
        "findings": [],
        "owner_decisions": [],
    }
    value.update(overrides)
    return value


def test_schema_invalid_refused():
    with pytest.raises(VerdictError) as caught:
        validate_verdict({"verdict": "approve"})
    assert caught.value.code == "verdict_schema_invalid"


def test_approve_with_a_blocking_finding_is_inconsistent():
    finding = {"severity": "blocking", "title": "gate bypass", "failure_scenario": "x -> y"}
    with pytest.raises(VerdictError) as caught:
        validate_verdict(_verdict(findings=[finding]))
    assert caught.value.code == "verdict_inconsistent"


def test_changes_requested_needs_blocking_evidence():
    with pytest.raises(VerdictError) as caught:
        validate_verdict(_verdict(verdict="changes_requested"))
    assert caught.value.code == "verdict_inconsistent"

    finding = {"severity": "blocking", "title": "gate bypass"}
    with pytest.raises(VerdictError) as caught:
        validate_verdict(_verdict(verdict="changes_requested", findings=[finding]))
    assert caught.value.code == "verdict_inconsistent"


def test_needs_owner_names_a_decision():
    with pytest.raises(VerdictError) as caught:
        validate_verdict(_verdict(verdict="needs_owner"))
    assert caught.value.code == "verdict_inconsistent"
    validate_verdict(_verdict(verdict="needs_owner", owner_decisions=["allow the lease widen"]))


def test_head_moved_refused():
    with pytest.raises(VerdictError) as caught:
        check_binding(_verdict(), PACKET, "f" * 40)
    assert caught.value.code == "head_moved"


def test_repo_or_pr_mismatch_refused():
    with pytest.raises(VerdictError) as caught:
        check_binding(_verdict(repo="mojomast/other"), PACKET, HEAD)
    assert caught.value.code == "verdict_binding_mismatch"
    with pytest.raises(VerdictError) as caught:
        check_binding(_verdict(pr=42), PACKET, HEAD)
    assert caught.value.code == "verdict_binding_mismatch"


def test_independence_rechecked():
    packet = dict(PACKET, author_model_family="kimi")
    with pytest.raises(VerdictError) as caught:
        check_binding(_verdict(), packet, HEAD)
    assert caught.value.code == "review_not_independent"


def test_render_approve_line_is_canonical():
    line = render_review(_verdict()).splitlines()[0]
    assert line == (
        "APPROVE (independent review — rizzstack-reviewer, model family kimi) at " + HEAD
    )


def test_render_changes_line_names_the_first_blocking_title():
    finding = {"severity": "blocking", "title": "gate bypass", "failure_scenario": "x -> y"}
    line = render_review(_verdict(verdict="changes_requested", findings=[finding])).splitlines()[0]
    assert line == (
        "CHANGES REQUESTED (independent review — rizzstack-reviewer, model family kimi: "
        "gate bypass) at " + HEAD
    )


def test_needs_owner_renders_nothing():
    assert render_review(_verdict(verdict="needs_owner", owner_decisions=["decide x"])) is None


def test_auto_reject_posts_changes_requested():
    finding = {"severity": "blocking", "title": "gate bypass", "failure_scenario": "x -> y"}
    verdict = _verdict(verdict="changes_requested", findings=[finding])
    posted = []
    result = post_verdict(verdict, lambda v, body: posted.append((v, body)), mode="auto_reject")
    assert result == {"posted": True, "queued": False}
    assert posted and posted[0][1].startswith("CHANGES REQUESTED")


def test_approve_is_queued_not_posted():
    posted = []
    result = post_verdict(_verdict(), lambda v, body: posted.append((v, body)), mode="auto_reject")
    assert result == {"posted": False, "queued": True}
    assert posted == []


def test_needs_owner_posts_nothing():
    verdict = _verdict(verdict="needs_owner", owner_decisions=["decide x"])
    result = post_verdict(verdict, lambda v, body: pytest.fail("posted"), mode="auto_reject")
    assert result == {"posted": False, "queued": False}


def test_one_record_line_per_verdict():
    line = record_line(_verdict(), attempt_id="att_abc", status="posted")
    assert line == f"mojomast/rizzstack#41 {HEAD} approve kimi att_abc posted"
