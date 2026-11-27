"""The review pipeline m.trigger (program: automated outside review, Phase 1.5).

On every pull-request head change — a new PR or an updated head — the pipeline builds the
packet and dispatches one review bee, once. The two advisory cron lanes call this and keep
their findings-only identity: they may comment findings, never post APPROVE.

Run: python -m pytest tests/test_review_trigger.py -q
"""

from __future__ import annotations


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.review_trigger")


def test_new_head_dispatches_once():
    m = _load()
    state = {"seen": {}}
    dispatched = []
    prs = {("mojomast/rizzstack", 41): HEAD_A}
    out = m.trigger(state, prs, lambda repo, pr, head: {"repo": repo, "pr": pr, "head": head},
                  lambda brief: dispatched.append(brief) or {"attempt": "att_1"})
    assert len(out) == 1
    assert dispatched == [{"repo": "mojomast/rizzstack", "pr": 41, "head": HEAD_A}]
    assert state["seen"][("mojomast/rizzstack", 41)] == HEAD_A


def test_unchanged_head_does_nothing():
    m = _load()
    state = {"seen": {("mojomast/rizzstack", 41): HEAD_A}}
    out = m.trigger(state, {("mojomast/rizzstack", 41): HEAD_A},
                  lambda *a: {"unexpected": True}, lambda brief: {"attempt": "att_x"})
    assert out == []


def test_updated_head_dispatches_at_the_new_head():
    m = _load()
    state = {"seen": {("mojomast/rizzstack", 41): HEAD_A}}
    dispatched = []
    out = m.trigger(state, {("mojomast/rizzstack", 41): HEAD_B},
                  lambda repo, pr, head: {"head": head},
                  lambda brief: dispatched.append(brief) or {"attempt": "att_2"})
    assert len(out) == 1
    assert dispatched[0]["head"] == HEAD_B
    assert m.heads_to_review({("mojomast/rizzstack", 41): HEAD_A},
                           {("mojomast/rizzstack", 41): HEAD_B}) == [("mojomast/rizzstack", 41, HEAD_B)]


def test_lanes_are_findings_only_and_never_approve():
    m = _load()
    assert m.LANE_KINDS == ("verify_review", "lane_dryrun")
    for kind in m.LANE_KINDS:
        assert m.lane_action(kind) == "findings_only"
        assert m.lane_may_approve(kind) is False


def test_unknown_lane_kind_is_not_silently_allowed():
    m = _load()
    try:
        m.lane_action("something_else")
    except KeyError:
        pass
    else:  # pragma: no cover - a silent fallback would be the bug
        raise AssertionError("an unknown lane kind must not be treated as a reviewer")
