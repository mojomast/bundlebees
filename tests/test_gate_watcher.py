"""The gate watcher turns planned work into queued work without a coordinator.

Every case runs in a fresh temporary root against ``<root>/plans/backlog.json``.
No model, network, launcher or token is involved: the world is a fake adapter
answering the exact probe surface the watcher requires, and the dispatcher is a
fake that records the payloads it is handed.

The red-first cases map to the owner directive: gate evaluation per type,
auto-dispatch on gate clear, owner-gate routing, budget and idempotence, and
the seeded backlog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import brief as brief_mod
from bundlebees_plugin import gate_watcher as gw

BASE = 1_790_000_000
GO_SWITCH = "2026-11-29T00:00:00Z"
REV_A = "a" * 40
REV_B = "b" * 40
REV_C = "c" * 40


def clock(step: float = 0.0):
    value = [BASE]

    def tick() -> float:
        value[0] += step
        return value[0]

    return tick


def backlog_doc(root: Path) -> dict:
    return json.loads(gw.backlog_path(root).read_text(encoding="utf-8"))


def slice_doc(root: Path, slice_id: str) -> dict:
    for item in backlog_doc(root)["slices"]:
        if item["id"] == slice_id:
            return item
    raise AssertionError(f"no slice {slice_id!r} in the backlog")


def make_brief(objective="Do the slice work", rev=REV_A, files=("src/one.py",)):
    return {
        "objective": objective,
        "source_rev": rev,
        "assigned_files": list(files),
        "acceptance": {"criteria": ["tests pass"], "tests": ["tests/test_one.py"]},
    }


class FakeWorld:
    """The exact probe surface the watcher requires; nothing more."""

    def __init__(
        self,
        prs: dict[int, str] | None = None,
        attempts: dict[str, str] | None = None,
        facts: dict[str, str] | None = None,
        leases: dict[str, list[str]] | None = None,
        bees: int = 0,
    ) -> None:
        self.prs = dict(prs or {})
        self.attempts = dict(attempts or {})
        self.facts = dict(facts or {})
        self.leases = dict(leases or {})
        self.bees = bees

    def pr_state(self, pr: int) -> str | None:
        return self.prs.get(int(pr))

    def attempt_state(self, attempt_id: str) -> str | None:
        return self.attempts.get(str(attempt_id))

    def fact_state(self, fact: str) -> str | None:
        return self.facts.get(str(fact))

    def active_bee_leases(self) -> dict[str, list[str]]:
        return dict(self.leases)

    def active_bee_count(self) -> int:
        return self.bees


class FakeDispatcher:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.payloads: list[dict] = []
        self.fail_for = set(fail_for or ())

    def dispatch(self, payload: dict) -> dict:
        slice_id = str(payload.get("kanban_task_id") or "")
        if slice_id in self.fail_for:
            raise gw.GateWatcherError(
                "dispatch_refused", f"the launcher refused {slice_id!r}"
            )
        attempt_id = f"att_{slice_id}"
        self.payloads.append(dict(payload))
        return {"attempt_id": attempt_id, "state": "running", "task_id": slice_id}


# ---------------------------------------------------------------------------
# seeding: the known planned slices land with their campaign-record gates
# ---------------------------------------------------------------------------


def test_seed_backlog_writes_schema_versioned_document(tmp_path: Path) -> None:
    doc = gw.seed_backlog(tmp_path, now=BASE)
    assert doc["schema"] == gw.SCHEMA
    path = gw.backlog_path(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == gw.SCHEMA
    assert raw["budget"] == {"lanes": gw.MAX_LANES, "bees": gw.MAX_BEES}
    ids = [item["id"] for item in raw["slices"]]
    assert ids == [
        "F14",
        "F16",
        "F18",
        "F19",
        "F5",
        "M2",
        "OV-A",
        "R02",
        "R03",
        "go-switch",
        "ov0-spike",
        "wave6-closeout",
    ]


def test_seed_backlog_is_create_only(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, now=BASE)
    with pytest.raises(gw.GateWatcherError) as err:
        gw.seed_backlog(tmp_path, now=BASE)
    assert err.value.code == "backlog_exists"


def test_seeded_gates_match_the_campaign_record(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, now=BASE)
    f16 = slice_doc(tmp_path, "F16")
    assert f16["gates"] == [{"type": "pr_merged", "pr": 44}]
    assert f16["lane"] == "features"
    for slice_id in ("F18", "F19"):
        item = slice_doc(tmp_path, slice_id)
        assert item["depends_on"] == ["F16"]
        assert item["gates"] == [{"type": "none"}]
    ov = slice_doc(tmp_path, "OV-A")
    assert ov["gates"] == [{"type": "attempt_succeeded", "slice": "ov0-spike"}]
    assert ov["depends_on"] == ["ov0-spike"]
    for slice_id in ("R02", "R03"):
        item = slice_doc(tmp_path, slice_id)
        assert item["gates"] == [{"type": "pr_merged", "pr": 40}]
        assert item["lane"] == "rails"
    f5 = slice_doc(tmp_path, "F5")
    assert f5["gates"] == [{"type": "attempt_succeeded", "fact": "F2"}]
    m2 = slice_doc(tmp_path, "M2")
    assert m2["gates"] == [{"type": "attempt_succeeded", "fact": "F2"}]
    assert m2["lane"] == "canary"
    f14 = slice_doc(tmp_path, "F14")
    assert f14["gates"] == [{"type": "attempt_succeeded", "fact": "M1"}]
    switch = slice_doc(tmp_path, "go-switch")
    assert switch["gates"] == [{"type": "date", "on": GO_SWITCH}]
    assert switch["lane"] == "release"
    closeout = slice_doc(tmp_path, "wave6-closeout")
    assert closeout["gates"] == [
        {
            "type": "owner_decision",
            "question": "Close out Wave 6 and prepare the campaign closeout report?",
        }
    ]
    assert closeout["status"] == "planned"
    for item in backlog_doc(tmp_path)["slices"]:
        assert item["status"] == "planned"
        brief_mod.validate_brief(item["brief"])
        assert item["project"]


def test_seed_backlog_lanes_fit_the_budget(tmp_path: Path) -> None:
    doc = gw.seed_backlog(tmp_path, now=BASE)
    lanes = {item["lane"] for item in doc["slices"]}
    assert gw.MIN_LANES <= len(lanes) <= gw.MAX_LANES


def test_seed_backlog_validates_and_refuses_bad_slices(tmp_path: Path) -> None:
    with pytest.raises(gw.GateWatcherError) as err:
        gw.seed_backlog(
            tmp_path,
            slices=[
                {
                    "id": "bad-gate",
                    "title": "Bad",
                    "project": "bundlebees",
                    "lane": "features",
                    "priority": 5,
                    "gates": [{"type": "pr_merged", "pr": "forty-four"}],
                    "brief": make_brief(),
                }
            ],
            now=BASE,
        )
    assert err.value.code == "gate_invalid"


# ---------------------------------------------------------------------------
# gate evaluation per type
# ---------------------------------------------------------------------------


def test_gate_pr_merged_states(tmp_path: Path) -> None:
    gate = {"type": "pr_merged", "pr": 44}
    assert gw.evaluate_gate(gate, world=FakeWorld(prs={44: "merged"}), now=BASE) == (
        "cleared",
        {"pr": 44, "state": "merged"},
    )
    state, evidence = gw.evaluate_gate(
        gate, world=FakeWorld(prs={44: "open"}), now=BASE
    )
    assert state == "waiting"
    assert evidence == {"pr": 44, "state": "open"}
    state, evidence = gw.evaluate_gate(
        gate, world=FakeWorld(prs={44: "closed"}), now=BASE
    )
    assert state == "stale"
    assert evidence["state"] == "closed"
    state, _ = gw.evaluate_gate(gate, world=FakeWorld(), now=BASE)
    assert state == "waiting"


def test_gate_attempt_succeeded_states(tmp_path: Path) -> None:
    by_attempt = {"type": "attempt_succeeded", "attempt": "att_1"}
    assert gw.evaluate_gate(
        by_attempt, world=FakeWorld(attempts={"att_1": "succeeded"}), now=BASE
    )[0] == "cleared"
    state, _ = gw.evaluate_gate(
        by_attempt, world=FakeWorld(attempts={"att_1": "failed"}), now=BASE
    )
    assert state == "stale"
    state, _ = gw.evaluate_gate(
        by_attempt, world=FakeWorld(attempts={"att_1": "running"}), now=BASE
    )
    assert state == "waiting"

    by_fact = {"type": "attempt_succeeded", "fact": "F2"}
    assert gw.evaluate_gate(
        by_fact, world=FakeWorld(facts={"F2": "landed"}), now=BASE
    ) == ("cleared", {"fact": "F2", "state": "landed"})
    state, _ = gw.evaluate_gate(by_fact, world=FakeWorld(facts={"F2": "failed"}), now=BASE)
    assert state == "stale"

    by_slice = {"type": "attempt_succeeded", "slice": "ov0-spike"}
    world = FakeWorld(attempts={"att_ov0-spike": "succeeded"})
    assert gw.evaluate_gate(
        by_slice, world=world, now=BASE, slices={"ov0-spike": {"attempt_id": "att_ov0-spike"}}
    )[0] == "cleared"
    state, _ = gw.evaluate_gate(
        by_slice, world=world, now=BASE, slices={"ov0-spike": {"attempt_id": None}}
    )
    assert state == "waiting"


def test_gate_date_and_none_and_owner(tmp_path: Path) -> None:
    before = BASE
    after = 1_796_000_000
    gate = {"type": "date", "on": GO_SWITCH}
    assert gw.evaluate_gate(gate, world=FakeWorld(), now=before)[0] == "waiting"
    state, evidence = gw.evaluate_gate(gate, world=FakeWorld(), now=after)
    assert state == "cleared"
    assert evidence == {"on": GO_SWITCH, "now": gw.iso(after)}

    assert gw.evaluate_gate({"type": "none"}, world=FakeWorld(), now=BASE) == (
        "cleared",
        {},
    )

    state, evidence = gw.evaluate_gate(
        {"type": "owner_decision"}, world=FakeWorld(), now=BASE
    )
    assert state == "owner_required"
    assert evidence == {"type": "owner_decision"}
    state, _ = gw.evaluate_gate(
        {"type": "owner_decision"},
        world=FakeWorld(),
        now=BASE,
        decisions={"wave6-closeout": {"decision": "approve"}},
        slice_id="wave6-closeout",
    )
    assert state == "cleared"
    state, evidence = gw.evaluate_gate(
        {"type": "owner_decision"},
        world=FakeWorld(),
        now=BASE,
        decisions={"wave6-closeout": {"decision": "decline"}},
        slice_id="wave6-closeout",
    )
    assert state == "stale"
    assert evidence["reason"] == "owner_declined"


# ---------------------------------------------------------------------------
# the beat: planned -> queued -> dispatched without coordinator attention
# ---------------------------------------------------------------------------


def seed_one(tmp_path: Path, slice_id: str = "F5") -> dict:
    return {
        "id": slice_id,
        "title": "Feature F5",
        "project": "orbitdesktop",
        "lane": "features",
        "priority": 5,
        "gates": [{"type": "attempt_succeeded", "fact": "F2"}],
        "brief": make_brief(objective="Build F5", rev=REV_A),
    }


def test_beat_queues_and_dispatches_on_gate_clear(tmp_path: Path) -> None:
    gw.seed_backlog(
        tmp_path,
        slices=[seed_one(tmp_path, "F5")],
        now=BASE,
    )
    world = FakeWorld(facts={"F2": "landed"})
    dispatcher = FakeDispatcher()
    report = gw.run_beat(
        tmp_path, world=world, dispatcher=dispatcher, now=clock(1)
    )
    assert report["queued"] == ["F5"]
    assert report["dispatched"] == [{"id": "F5", "attempt_id": "att_F5"}]
    assert len(dispatcher.payloads) == 1
    payload = dispatcher.payloads[0]
    assert payload["project"] == "orbitdesktop"
    assert payload["kanban_task_id"] == "F5"
    brief_mod.validate_brief(payload["brief"])
    record = slice_doc(tmp_path, "F5")
    assert record["status"] == "dispatched"
    assert record["attempt_id"] == "att_F5"
    kinds = [(item["from"], item["to"]) for item in record["transitions"]]
    assert kinds == [("planned", "queued"), ("queued", "dispatched")]
    queued = record["transitions"][0]
    assert queued["evidence"]["gates"] == [
        {"type": "attempt_succeeded", "fact": "F2", "state": "landed"}
    ]
    log = backlog_doc(tmp_path)["transitions"]
    assert [item["slice"] for item in log] == ["F5", "F5"]


def test_beat_waits_while_the_gate_is_unclear(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=[seed_one(tmp_path, "F5")], now=BASE)
    report = gw.run_beat(
        tmp_path, world=FakeWorld(facts={"F2": "running"}), dispatcher=FakeDispatcher(), now=BASE
    )
    assert report["queued"] == []
    assert report["dispatched"] == []
    assert slice_doc(tmp_path, "F5")["status"] == "planned"


def test_beat_is_idempotent_and_never_redispatches(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=[seed_one(tmp_path, "F5")], now=BASE)
    world = FakeWorld(facts={"F2": "landed"})
    dispatcher = FakeDispatcher()
    gw.run_beat(tmp_path, world=world, dispatcher=dispatcher, now=clock(1))
    before = gw.backlog_path(tmp_path).read_text(encoding="utf-8")
    report = gw.run_beat(tmp_path, world=world, dispatcher=dispatcher, now=clock(2))
    assert report["queued"] == []
    assert report["dispatched"] == []
    assert report["changed"] is False
    assert gw.backlog_path(tmp_path).read_text(encoding="utf-8") == before
    assert len(dispatcher.payloads) == 1


def test_beat_dispatch_failure_stays_queued_and_retries(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=[seed_one(tmp_path, "F5")], now=BASE)
    failing = FakeDispatcher(fail_for={"F5"})
    world = FakeWorld(facts={"F2": "landed"})
    report = gw.run_beat(tmp_path, world=world, dispatcher=failing, now=clock(1))
    assert report["queued"] == ["F5"]
    assert report["dispatched"] == []
    assert report["dispatch_errors"] == [
        {"id": "F5", "code": "dispatch_refused"}
    ]
    record = slice_doc(tmp_path, "F5")
    assert record["status"] == "queued"
    assert record["last_dispatch_error"]["code"] == "dispatch_refused"

    working = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=world, dispatcher=working, now=clock(2))
    assert report["dispatched"] == [{"id": "F5", "attempt_id": "att_F5"}]
    assert slice_doc(tmp_path, "F5")["last_dispatch_error"] is None


def test_beat_queues_even_when_the_bee_budget_is_spent(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=[seed_one(tmp_path, "F5")], now=BASE)
    world = FakeWorld(facts={"F2": "landed"}, bees=gw.MAX_BEES)
    report = gw.run_beat(tmp_path, world=world, dispatcher=FakeDispatcher(), now=BASE)
    assert report["queued"] == ["F5"]
    assert report["dispatched"] == []
    assert report["skipped_budget"] == [{"id": "F5", "reason": "bee_cap"}]
    assert slice_doc(tmp_path, "F5")["status"] == "queued"


# ---------------------------------------------------------------------------
# owner-gated slices never dispatch; they route to the decision queue
# ---------------------------------------------------------------------------


def test_owner_gated_slice_routes_and_never_dispatches(tmp_path: Path) -> None:
    slice_record = seed_one(tmp_path, "wave6-closeout")
    slice_record["gates"] = [{"type": "owner_decision", "question": "close wave 6?"}]
    gw.seed_backlog(tmp_path, slices=[slice_record], now=BASE)
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=clock(1))
    assert report["routed_owner"] == ["wave6-closeout"]
    assert report["dispatched"] == []
    assert dispatcher.payloads == []
    record = slice_doc(tmp_path, "wave6-closeout")
    assert record["status"] == "blocked-owner"
    assert record["owner_request"] == {"question": "close wave 6?", "requested_at": gw.iso(BASE + 1)}
    assert record["transitions"][0]["to"] == "blocked-owner"

    # A second beat does not re-route or duplicate the request.
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=clock(2))
    assert report["routed_owner"] == []
    assert len(slice_doc(tmp_path, "wave6-closeout")["transitions"]) == 1


def test_owner_decision_queues_and_then_dispatches(tmp_path: Path) -> None:
    slice_record = seed_one(tmp_path, "wave6-closeout")
    slice_record["gates"] = [{"type": "owner_decision"}]
    gw.seed_backlog(tmp_path, slices=[slice_record], now=BASE)
    gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=FakeDispatcher(), now=clock(1))
    gw.record_owner_decision(
        tmp_path, "wave6-closeout", decision="approve", by="owner", now=clock(2)
    )
    decisions = backlog_doc(tmp_path)["decisions"]
    assert decisions == [
        {
            "slice_id": "wave6-closeout",
            "decision": "approve",
            "by": "owner",
            "note": "",
            "at": gw.iso(BASE + 2),
        }
    ]
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=clock(3))
    assert report["queued"] == ["wave6-closeout"]
    assert report["dispatched"] == [{"id": "wave6-closeout", "attempt_id": "att_wave6-closeout"}]
    record = slice_doc(tmp_path, "wave6-closeout")
    kinds = [(item["from"], item["to"]) for item in record["transitions"]]
    assert kinds == [
        ("planned", "blocked-owner"),
        ("blocked-owner", "queued"),
        ("queued", "dispatched"),
    ]
    assert record["transitions"][1]["evidence"]["decision"] == "approve"


def test_owner_decline_never_dispatches(tmp_path: Path) -> None:
    slice_record = seed_one(tmp_path, "wave6-closeout")
    slice_record["gates"] = [{"type": "owner_decision"}]
    gw.seed_backlog(tmp_path, slices=[slice_record], now=BASE)
    gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=FakeDispatcher(), now=clock(1))
    gw.record_owner_decision(
        tmp_path, "wave6-closeout", decision="decline", by="owner", note="not yet", now=clock(2)
    )
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=clock(3))
    assert report["dispatched"] == []
    assert report["declined"] == ["wave6-closeout"]
    assert slice_doc(tmp_path, "wave6-closeout")["status"] == "blocked-owner"

    with pytest.raises(gw.GateWatcherError) as err:
        gw.record_owner_decision(
            tmp_path, "wave6-closeout", decision="maybe", by="owner", now=clock(4)
        )
    assert err.value.code == "decision_invalid"


# ---------------------------------------------------------------------------
# stale gates mark the slice blocked with a reason
# ---------------------------------------------------------------------------


def test_stale_pr_gate_blocks_with_reason_and_recovers(tmp_path: Path) -> None:
    slice_record = seed_one(tmp_path, "F16")
    slice_record["gates"] = [{"type": "pr_merged", "pr": 44}]
    gw.seed_backlog(tmp_path, slices=[slice_record], now=BASE)
    report = gw.run_beat(
        tmp_path, world=FakeWorld(prs={44: "closed"}), dispatcher=FakeDispatcher(), now=clock(1)
    )
    assert report["stale"] == [
        {"id": "F16", "reason": "pr_closed_unmerged", "gate": "pr_merged"}
    ]
    record = slice_doc(tmp_path, "F16")
    assert record["status"] == "planned"
    assert record["blocked"]["reason"] == "pr_closed_unmerged"
    assert record["blocked"]["gate"] == "pr_merged"

    # The PR reopens: the block clears and normal waiting resumes.
    gw.run_beat(
        tmp_path, world=FakeWorld(prs={44: "open"}), dispatcher=FakeDispatcher(), now=clock(2)
    )
    record = slice_doc(tmp_path, "F16")
    assert record["blocked"] is None

    # And a later merge queues and dispatches it.
    gw.run_beat(
        tmp_path, world=FakeWorld(prs={44: "merged"}), dispatcher=FakeDispatcher(), now=clock(3)
    )
    record = slice_doc(tmp_path, "F16")
    assert record["status"] == "dispatched"


def test_failed_attempt_gate_blocks_with_reason(tmp_path: Path) -> None:
    slice_record = seed_one(tmp_path, "OV-A")
    slice_record["gates"] = [{"type": "attempt_succeeded", "attempt": "att_ov0"}]
    gw.seed_backlog(tmp_path, slices=[slice_record], now=BASE)
    report = gw.run_beat(
        tmp_path,
        world=FakeWorld(attempts={"att_ov0": "failed"}),
        dispatcher=FakeDispatcher(),
        now=clock(1),
    )
    assert report["stale"] == [
        {"id": "OV-A", "reason": "attempt_failed", "gate": "attempt_succeeded"}
    ]
    assert slice_doc(tmp_path, "OV-A")["blocked"]["reason"] == "attempt_failed"


# ---------------------------------------------------------------------------
# lane budget, bee cap and lease rules
# ---------------------------------------------------------------------------


def many_slices(count: int, lane_prefix: str = "lane") -> list[dict]:
    slices = []
    for index in range(count):
        slices.append(
            {
                "id": f"s{index}",
                "title": f"Slice {index}",
                "project": "bundlebees",
                "lane": f"{lane_prefix}-{index}",
                "priority": 5,
                "gates": [{"type": "none"}],
                "brief": make_brief(
                    objective=f"Slice {index}", rev=REV_A, files=(f"src/slice_{index}.py",)
                ),
            }
        )
    return slices


def test_lane_budget_caps_distinct_lanes(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=many_slices(9), now=BASE)
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=BASE)
    assert len(report["dispatched"]) == gw.MAX_LANES
    assert len(dispatcher.payloads) == gw.MAX_LANES
    assert {item["id"] for item in report["skipped_budget"]} == {
        "s7",
        "s8",
    }
    assert all(
        item["reason"] == "lane_budget" for item in report["skipped_budget"]
    )


def test_bee_cap_counts_live_bees_plus_queued(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=many_slices(3), now=BASE)
    world = FakeWorld(bees=9)
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=world, dispatcher=dispatcher, now=BASE)
    assert [item["id"] for item in report["dispatched"]] == ["s0"]
    assert report["skipped_budget"] == [
        {"id": "s1", "reason": "bee_cap"},
        {"id": "s2", "reason": "bee_cap"},
    ]


def test_lease_conflict_blocks_dispatch(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=many_slices(2), now=BASE)
    world = FakeWorld(leases={"bee-1": ["src/slice_0.py", "src/slice_1.py"]})
    report = gw.run_beat(tmp_path, world=world, dispatcher=FakeDispatcher(), now=BASE)
    assert report["dispatched"] == []
    assert report["lease_skipped"] == [
        {"id": "s0", "reason": "lease_conflict"},
        {"id": "s1", "reason": "lease_conflict"},
    ]
    assert all(
        slice_doc(tmp_path, slice_id)["status"] == "planned"
        for slice_id in ("s0", "s1")
    )


def test_two_slices_in_one_lane_share_the_budget_and_leases(tmp_path: Path) -> None:
    first = seed_one(tmp_path, "a-one")
    first["gates"] = [{"type": "none"}]
    second = seed_one(tmp_path, "a-two")
    second["gates"] = [{"type": "none"}]
    second["brief"] = make_brief(objective="Build a-two", rev=REV_B, files=("src/two.py",))
    gw.seed_backlog(tmp_path, slices=[first, second], now=BASE)
    dispatcher = FakeDispatcher()
    report = gw.run_beat(
        tmp_path,
        world=FakeWorld(bees=9),
        dispatcher=dispatcher,
        now=BASE,
        max_bees=10,
    )
    assert report["dispatched"] == [{"id": "a-one", "attempt_id": "att_a-one"}]
    assert report["skipped_budget"] == [{"id": "a-two", "reason": "bee_cap"}]


def test_priority_and_created_order_break_ties(tmp_path: Path) -> None:
    first = seed_one(tmp_path, "low")
    first["priority"] = 9
    first["gates"] = [{"type": "none"}]
    first["brief"] = make_brief(objective="Build low", rev=REV_A, files=("src/low.py",))
    second = seed_one(tmp_path, "high")
    second["priority"] = 1
    second["gates"] = [{"type": "none"}]
    second["brief"] = make_brief(objective="Build high", rev=REV_B, files=("src/high.py",))
    gw.seed_backlog(tmp_path, slices=[first, second], now=BASE)
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=FakeDispatcher(), now=BASE)
    assert [item["id"] for item in report["dispatched"]] == ["high", "low"]


# ---------------------------------------------------------------------------
# depends_on: a slice waits until its dependency slice is dispatched
# ---------------------------------------------------------------------------


def test_depends_on_waits_for_the_dependency_slice(tmp_path: Path) -> None:
    lead = seed_one(tmp_path, "F16")
    lead["gates"] = [{"type": "none"}]
    follower = seed_one(tmp_path, "F18")
    follower["gates"] = [{"type": "none"}]
    follower["brief"] = make_brief(objective="Build F18", rev=REV_B, files=("src/f18.py",))
    follower["depends_on"] = ["F16"]
    gw.seed_backlog(tmp_path, slices=[lead, follower], now=BASE)
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=BASE)
    assert report["dispatched"] == [{"id": "F16", "attempt_id": "att_F16"}]
    assert report["waiting"] == ["F18"]
    report = gw.run_beat(
        tmp_path,
        world=FakeWorld(attempts={"att_F16": "succeeded"}),
        dispatcher=dispatcher,
        now=BASE,
    )
    assert report["dispatched"] == [{"id": "F18", "attempt_id": "att_F18"}]


def test_unknown_dependency_never_clears(tmp_path: Path) -> None:
    follower = seed_one(tmp_path, "F18")
    follower["depends_on"] = ["ghost"]
    gw.seed_backlog(tmp_path, slices=[follower], now=BASE)
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=FakeDispatcher(), now=BASE)
    assert report["dispatched"] == []
    assert report["waiting"] == ["F18"]


# ---------------------------------------------------------------------------
# the whole seeded backlog beats end to end
# ---------------------------------------------------------------------------


def test_seeded_backlog_beat_routes_and_waits(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, now=BASE)
    dispatcher = FakeDispatcher()
    report = gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=dispatcher, now=clock(1))
    assert report["queued"] == ["ov0-spike"]
    assert report["dispatched"] == [{"id": "ov0-spike", "attempt_id": "att_ov0-spike"}]
    assert report["routed_owner"] == ["wave6-closeout"]
    assert slice_doc(tmp_path, "F16")["status"] == "planned"

    # The spike lands, PR #44 and #40 merge, F2 and M1 land, and the date
    # passes: every remaining external gate clears and the lanes fill within
    # the budget. F18/F19 still wait for F16's attempt to land.
    world = FakeWorld(
        prs={44: "merged", 40: "merged"},
        facts={"F2": "landed", "M1": "landed"},
        attempts={"att_ov0-spike": "succeeded"},
    )
    report = gw.run_beat(
        tmp_path, world=world, dispatcher=dispatcher, now=1_796_000_000
    )
    dispatched = {item["id"] for item in report["dispatched"]}
    assert dispatched == {
        "F16",
        "OV-A",
        "R02",
        "R03",
        "F5",
        "M2",
        "F14",
        "go-switch",
    }
    assert slice_doc(tmp_path, "F18")["status"] == "planned"

    # F16's attempt lands: its dependents queue and dispatch in the same lane.
    world.attempts["att_F16"] = "succeeded"
    report = gw.run_beat(
        tmp_path, world=world, dispatcher=dispatcher, now=1_796_100_000
    )
    assert {item["id"] for item in report["dispatched"]} == {"F18", "F19"}
    assert slice_doc(tmp_path, "wave6-closeout")["status"] == "blocked-owner"


def test_run_beat_refuses_an_incompatible_world(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=[seed_one(tmp_path, "F5")], now=BASE)

    class Bad:
        pass

    with pytest.raises(gw.GateWatcherError) as err:
        gw.run_beat(tmp_path, world=Bad(), dispatcher=FakeDispatcher(), now=BASE)
    assert err.value.code == "world_incompatible"
    with pytest.raises(gw.GateWatcherError) as err:
        gw.run_beat(tmp_path, world=FakeWorld(), dispatcher=object(), now=BASE)
    assert err.value.code == "dispatcher_incompatible"


def test_budget_validation_stays_in_the_owner_envelope(tmp_path: Path) -> None:
    gw.seed_backlog(tmp_path, slices=[seed_one(tmp_path, "F5")], now=BASE)
    with pytest.raises(gw.GateWatcherError) as err:
        gw.run_beat(
            tmp_path,
            world=FakeWorld(),
            dispatcher=FakeDispatcher(),
            now=BASE,
            max_lanes=4,
        )
    assert err.value.code == "budget_invalid"
    with pytest.raises(gw.GateWatcherError) as err:
        gw.run_beat(
            tmp_path,
            world=FakeWorld(),
            dispatcher=FakeDispatcher(),
            now=BASE,
            max_bees=11,
        )
    assert err.value.code == "budget_invalid"
