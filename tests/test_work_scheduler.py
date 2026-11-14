"""The coordinator work scheduler is durable, deterministic and agent-agnostic.

Every case runs in a fresh temporary state directory against
``<state>/coordinator/work.json``. No model, bee, adapter or network is
involved: the scheduler only reads and writes its own queue.
"""

from __future__ import annotations

import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bundlebees_plugin import work_scheduler as ws

BASE = 1_800_000_000.0


def clock(step: float = 0.0):
    value = [BASE]

    def tick() -> float:
        value[0] += step
        return value[0]

    return tick


def add(state: Path, work_id: str, **kwargs):
    return ws.add_item(state, work_id=work_id, now=BASE, **kwargs)


def queue_doc(state: Path) -> dict:
    return json.loads(ws.queue_path(state).read_text(encoding="utf-8"))


def write_queue_doc(state: Path, doc: dict) -> None:
    ws.queue_path(state).parent.mkdir(parents=True, exist_ok=True)
    ws.queue_path(state).write_text(json.dumps(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# create-only intake and deterministic listing
# ---------------------------------------------------------------------------


def test_add_item_is_create_only(tmp_path: Path) -> None:
    first = add(tmp_path, "ws_a", title="A", kind="research", priority=1)
    assert first["status"] == "pending"
    assert first["owner"] == ""
    assert first["checkpoint"] is None
    assert first["created_at"] == first["updated_at"]

    replay = add(tmp_path, "ws_a", title="A", kind="research", priority=1)
    assert replay == first

    with pytest.raises(ws.SchedulerError) as excinfo:
        add(tmp_path, "ws_a", title="changed", kind="research", priority=1)
    assert excinfo.value.code == "work_exists"

    assert [item["work_id"] for item in ws.list_items(tmp_path)] == ["ws_a"]


def test_add_item_validates_kind_priority_and_dependencies(tmp_path: Path) -> None:
    with pytest.raises(ws.SchedulerError) as bad_kind:
        add(tmp_path, "ws_a", kind="prompt")
    assert bad_kind.value.code == "kind_invalid"

    with pytest.raises(ws.SchedulerError) as bad_priority:
        add(tmp_path, "ws_a", priority=10)
    assert bad_priority.value.code == "priority_invalid"

    with pytest.raises(ws.SchedulerError) as self_dep:
        add(tmp_path, "ws_a", depends_on=["ws_a"])
    assert self_dep.value.code == "dependency_invalid"

    with pytest.raises(ws.SchedulerError) as bad_id:
        add(tmp_path, "../escape")
    assert bad_id.value.code == "identifier_invalid"


def test_list_items_is_deterministic_by_created_at(tmp_path: Path) -> None:
    ws.add_item(tmp_path, work_id="ws_b", title="B", kind="feature", now=BASE + 20)
    ws.add_item(tmp_path, work_id="ws_a", title="A", kind="feature", now=BASE + 10)
    ws.add_item(tmp_path, work_id="ws_c", title="C", kind="feature", now=BASE + 30)

    ordered = [item["work_id"] for item in ws.list_items(tmp_path)]
    assert ordered == ["ws_a", "ws_b", "ws_c"]
    assert ordered == [item["work_id"] for item in ws.list_items(tmp_path)]


def test_queue_file_is_sorted_atomic_json(tmp_path: Path) -> None:
    add(tmp_path, "ws_z", kind="feature")
    add(tmp_path, "ws_a", kind="feature")

    doc = queue_doc(tmp_path)
    assert doc["schema"] == ws.SCHEMA
    assert [item["work_id"] for item in doc["items"]] == ["ws_a", "ws_z"]
    raw = ws.queue_path(tmp_path).read_text(encoding="utf-8")
    assert raw == json.dumps(doc, sort_keys=True, indent=2) + "\n"


# ---------------------------------------------------------------------------
# dependencies and lease conflicts
# ---------------------------------------------------------------------------


def test_dependency_blocks_ready_until_done(tmp_path: Path) -> None:
    add(tmp_path, "ws_dep", kind="feature")
    add(tmp_path, "ws_child", kind="spec", depends_on=["ws_dep"])
    add(tmp_path, "ws_other", kind="docs")

    ready = [item["work_id"] for item in ws.ready_items(tmp_path)]
    assert ready == ["ws_dep", "ws_other"]
    assert "ws_child" not in ready

    ws.claim(tmp_path, "ws_dep", "bee-1", "wt-1", now=BASE)
    assert [item["work_id"] for item in ws.ready_items(tmp_path)] == ["ws_other"]
    ws.complete(tmp_path, "ws_dep", now=BASE)

    ready = [item["work_id"] for item in ws.ready_items(tmp_path)]
    assert "ws_child" in ready


def test_unknown_dependency_is_never_ready(tmp_path: Path) -> None:
    add(tmp_path, "ws_child", kind="spec", depends_on=["ws_missing"])
    assert ws.ready_items(tmp_path) == []


def test_ready_items_respect_external_bee_leases(tmp_path: Path) -> None:
    add(tmp_path, "ws_conflict", kind="spec", lease_files=["src/api.py"])
    add(tmp_path, "ws_free", kind="spec", lease_files=["src/ui.py"])

    busy = {"bee-1": ["src/api.py"], "bee-2": ["docs/readme.md"]}
    assert [item["work_id"] for item in ws.ready_items(tmp_path, busy)] == ["ws_free"]
    assert [item["work_id"] for item in ws.ready_items(tmp_path, {"bee-1": ["src/api.py"]})] == ["ws_free"]
    assert [item["work_id"] for item in ws.ready_items(tmp_path, ["src/api.py"])] == ["ws_free"]
    assert {item["work_id"] for item in ws.ready_items(tmp_path, {})} == {
        "ws_conflict",
        "ws_free",
    }


def test_claimed_lease_conflicts_are_respected(tmp_path: Path) -> None:
    add(tmp_path, "ws_one", kind="feature", lease_files=["src/api.py"])
    add(tmp_path, "ws_two", kind="spec", lease_files=["src/api.py"])
    add(tmp_path, "ws_three", kind="spec", lease_files=["src/other.py"])

    ws.claim(tmp_path, "ws_one", "bee-1", "wt-1", now=BASE)
    assert [item["work_id"] for item in ws.ready_items(tmp_path)] == ["ws_three"]

    with pytest.raises(ws.SchedulerError) as conflict:
        ws.claim(tmp_path, "ws_two", "bee-2", "wt-2", now=BASE)
    assert conflict.value.code == "lease_conflict"

    ws.claim(tmp_path, "ws_three", "bee-2", "wt-2", now=BASE)


def test_claim_requires_pending_and_satisfied_dependencies(tmp_path: Path) -> None:
    add(tmp_path, "ws_dep", kind="feature")
    add(tmp_path, "ws_child", kind="spec", depends_on=["ws_dep"])

    with pytest.raises(ws.SchedulerError) as blocked:
        ws.claim(tmp_path, "ws_child", "bee-1", "wt-1", now=BASE)
    assert blocked.value.code == "dependency_blocked"

    ws.claim(tmp_path, "ws_dep", "bee-1", "wt-1", now=BASE)
    ws.complete(tmp_path, "ws_dep", now=BASE)
    with pytest.raises(ws.SchedulerError) as done:
        ws.claim(tmp_path, "ws_dep", "bee-2", "wt-2", now=BASE)
    assert done.value.code == "work_done"

    with pytest.raises(ws.SchedulerError) as missing:
        ws.claim(tmp_path, "ws_none", "bee-1", "wt-1", now=BASE)
    assert missing.value.code == "work_not_found"


# ---------------------------------------------------------------------------
# deterministic ordering
# ---------------------------------------------------------------------------


def test_ready_order_is_priority_then_created_at(tmp_path: Path) -> None:
    ws.add_item(tmp_path, work_id="ws_late", kind="spec", priority=5, now=BASE + 30)
    ws.add_item(tmp_path, work_id="ws_urgent", kind="spec", priority=0, now=BASE + 40)
    ws.add_item(tmp_path, work_id="ws_early", kind="spec", priority=5, now=BASE + 10)
    ws.add_item(tmp_path, work_id="ws_last", kind="spec", priority=9, now=BASE)

    assert [item["work_id"] for item in ws.ready_items(tmp_path)] == [
        "ws_urgent",
        "ws_early",
        "ws_late",
        "ws_last",
    ]


def test_ties_break_by_work_id(tmp_path: Path) -> None:
    add(tmp_path, "ws_c", kind="spec", priority=3)
    add(tmp_path, "ws_a", kind="spec", priority=3)
    add(tmp_path, "ws_b", kind="spec", priority=3)
    assert [item["work_id"] for item in ws.ready_items(tmp_path)] == [
        "ws_a",
        "ws_b",
        "ws_c",
    ]


def test_identical_operations_are_byte_deterministic(tmp_path: Path) -> None:
    for state in (tmp_path / "one", tmp_path / "two"):
        add(state, "ws_a", kind="spec", priority=2, lease_files=["src/a.py"])
        add(state, "ws_b", kind="feature", depends_on=["ws_a"], lease_files=["src/b.py"])
        ws.claim(state, "ws_a", "bee-1", "wt", now=BASE)
        ws.checkpoint(state, "ws_a", "tests green", ["pytest -q"], now=BASE)
    assert ws.queue_path(tmp_path / "one").read_bytes() == ws.queue_path(
        tmp_path / "two"
    ).read_bytes()


# ---------------------------------------------------------------------------
# claim, ownership, capacity and races
# ---------------------------------------------------------------------------


def test_claim_sets_single_owner_and_is_idempotent(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="feature")
    claimed = ws.claim(tmp_path, "ws_a", "bee-1", "wt-1", now=BASE)
    assert claimed["status"] == "claimed"
    assert claimed["owner"] == "bee-1"
    assert claimed["worktree"] == "wt-1"

    again = ws.claim(tmp_path, "ws_a", "bee-1", "wt-1", now=BASE + 1)
    assert again["owner"] == "bee-1"

    with pytest.raises(ws.SchedulerError) as other:
        ws.claim(tmp_path, "ws_a", "bee-2", "wt-2", now=BASE)
    assert other.value.code == "work_not_pending"


def test_one_active_item_per_owner(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="spec", lease_files=["src/a.py"])
    add(tmp_path, "ws_b", kind="spec", lease_files=["src/b.py"])

    ws.claim(tmp_path, "ws_a", "bee-1", "wt-1", now=BASE)
    with pytest.raises(ws.SchedulerError) as busy:
        ws.claim(tmp_path, "ws_b", "bee-1", "wt-2", now=BASE)
    assert busy.value.code == "owner_busy"

    ws.claim(tmp_path, "ws_b", "bee-2", "wt-2", now=BASE)


def test_claim_race_has_exactly_one_winner(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="feature")
    start = threading.Barrier(8)

    def attempt(index: int) -> str:
        start.wait(timeout=5)
        try:
            ws.claim(tmp_path, "ws_a", f"bee-{index}", f"wt-{index}", now=BASE)
        except ws.SchedulerError as exc:
            return exc.code
        return "won"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))

    assert outcomes.count("won") == 1
    assert outcomes.count("work_not_pending") == 7
    stored = ws.list_items(tmp_path)[0]
    assert stored["status"] == "claimed"
    assert stored["owner"].startswith("bee-")


def test_bounded_concurrency_and_reserved_landing_slot(tmp_path: Path) -> None:
    add(tmp_path, "ws_plan", kind="spec", lease_files=["src/plan.py"])
    add(tmp_path, "ws_ship", kind="feature", lease_files=["src/ship.py"])

    ws.claim(tmp_path, "ws_plan", "bee-1", "wt", now=BASE, max_active=2)
    add(tmp_path, "ws_plan2", kind="spec", lease_files=["src/plan2.py"])
    with pytest.raises(ws.SchedulerError) as reserved:
        ws.claim(tmp_path, "ws_plan2", "bee-2", "wt", now=BASE, max_active=2)
    assert reserved.value.code == "capacity_exhausted"

    ws.claim(tmp_path, "ws_ship", "bee-3", "wt", now=BASE, max_active=2)
    add(tmp_path, "ws_ship2", kind="feature", lease_files=["src/ship2.py"])
    with pytest.raises(ws.SchedulerError) as full:
        ws.claim(tmp_path, "ws_ship2", "bee-4", "wt", now=BASE, max_active=2)
    assert full.value.code == "capacity_exhausted"


def test_backpressure_clears_after_completion(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="feature", lease_files=["src/a.py"])
    add(tmp_path, "ws_b", kind="feature", lease_files=["src/b.py"])

    ws.claim(tmp_path, "ws_a", "bee-1", "wt", now=BASE, max_active=1)
    with pytest.raises(ws.SchedulerError) as full:
        ws.claim(tmp_path, "ws_b", "bee-2", "wt", now=BASE, max_active=1)
    assert full.value.code == "capacity_exhausted"

    ws.complete(tmp_path, "ws_a", now=BASE)
    assert ws.claim(tmp_path, "ws_b", "bee-2", "wt", now=BASE, max_active=1)["status"] == "claimed"


# ---------------------------------------------------------------------------
# durable checkpoint / release / complete / resume
# ---------------------------------------------------------------------------


def test_checkpoint_is_durable_and_requires_a_claim(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="spec")
    with pytest.raises(ws.SchedulerError) as unclaimed:
        ws.checkpoint(tmp_path, "ws_a", "halfway", [], now=BASE)
    assert unclaimed.value.code == "work_not_claimed"

    ws.claim(tmp_path, "ws_a", "bee-1", "wt", now=BASE)
    with pytest.raises(ws.SchedulerError) as no_note:
        ws.checkpoint(tmp_path, "ws_a", "  ", [], now=BASE)
    assert no_note.value.code == "checkpoint_invalid"

    ws.checkpoint(tmp_path, "ws_a", "halfway", ["pytest -q: 12 passed"], now=BASE + 5)
    stored = queue_doc(tmp_path)["items"][0]
    assert stored["checkpoint"] == {
        "note": "halfway",
        "evidence": ["pytest -q: 12 passed"],
        "at": "2027-01-15T08:00:05Z",
    }
    assert stored["updated_at"] == "2027-01-15T08:00:05Z"


def test_checkpoint_resume_survives_module_restart(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="spec")
    ws.claim(tmp_path, "ws_a", "bee-1", "wt-1", now=BASE)
    ws.checkpoint(tmp_path, "ws_a", "safe boundary", ["evidence"], now=BASE)

    reloaded = importlib.reload(ws)
    resumed = reloaded.resume(tmp_path, "ws_a", now=BASE + 60)
    assert resumed["status"] == "claimed"
    assert resumed["owner"] == "bee-1"
    assert resumed["checkpoint"]["note"] == "safe boundary"
    assert resumed["checkpoint"]["evidence"] == ["evidence"]
    assert resumed["resumed"] is False
    assert resumed["reason"] == "owner_still_holds"

    reloaded.complete(tmp_path, "ws_a", now=BASE + 61)
    assert reloaded.list_items(tmp_path)[0]["status"] == "done"


def test_resume_requires_a_checkpoint(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="spec")
    with pytest.raises(ws.SchedulerError) as missing:
        ws.resume(tmp_path, "ws_a", now=BASE)
    assert missing.value.code == "no_checkpoint"


def test_resume_unblocks_blocked_item_after_dependency_lands(tmp_path: Path) -> None:
    add(tmp_path, "ws_dep", kind="feature")
    add(tmp_path, "ws_a", kind="spec", lease_files=["src/a.py"])
    ws.claim(tmp_path, "ws_a", "bee-1", "wt", now=BASE)
    ws.checkpoint(tmp_path, "ws_a", "waiting on dep", [], now=BASE)
    ws.release(tmp_path, "ws_a", owner="bee-1", now=BASE)

    doc = queue_doc(tmp_path)
    for item in doc["items"]:
        if item["work_id"] == "ws_a":
            item["status"] = "blocked"
            item["depends_on"] = ["ws_dep"]
            item["owner"] = ""
            item["worktree"] = ""
    write_queue_doc(tmp_path, doc)

    blocked = ws.resume(tmp_path, "ws_a", now=BASE + 1)
    assert blocked["resumed"] is False
    assert blocked["reason"] == "dependency_blocked"

    ws.claim(tmp_path, "ws_dep", "bee-2", "wt", now=BASE + 2)
    ws.complete(tmp_path, "ws_dep", now=BASE + 3)
    unblocked = ws.resume(tmp_path, "ws_a", now=BASE + 4)
    assert unblocked["resumed"] is True
    assert unblocked["status"] == "pending"
    assert unblocked["reason"] == "unblocked"
    assert unblocked["checkpoint"]["note"] == "waiting on dep"


def test_release_returns_to_pending_and_keeps_checkpoint(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="spec")
    ws.claim(tmp_path, "ws_a", "bee-1", "wt", now=BASE)
    ws.checkpoint(tmp_path, "ws_a", "safe", [], now=BASE)

    with pytest.raises(ws.SchedulerError) as not_owner:
        ws.release(tmp_path, "ws_a", owner="bee-2", now=BASE)
    assert not_owner.value.code == "not_owner"

    released = ws.release(tmp_path, "ws_a", owner="bee-1", now=BASE + 1)
    assert released["status"] == "pending"
    assert released["owner"] == ""
    assert released["worktree"] == ""
    assert released["checkpoint"]["note"] == "safe"

    again = ws.claim(tmp_path, "ws_a", "bee-2", "wt-2", now=BASE + 2)
    assert again["owner"] == "bee-2"


def test_complete_frees_owner_and_owner_is_enforced(tmp_path: Path) -> None:
    add(tmp_path, "ws_a", kind="feature")
    ws.claim(tmp_path, "ws_a", "bee-1", "wt", now=BASE)

    with pytest.raises(ws.SchedulerError) as not_owner:
        ws.complete(tmp_path, "ws_a", owner="bee-2", now=BASE)
    assert not_owner.value.code == "not_owner"

    done = ws.complete(tmp_path, "ws_a", owner="bee-1", now=BASE + 1)
    assert done["status"] == "done"
    assert done["owner"] == ""
    assert done["worktree"] == ""
    assert ws.complete(tmp_path, "ws_a", now=BASE + 2)["status"] == "done"


# ---------------------------------------------------------------------------
# event preemption and idle
# ---------------------------------------------------------------------------


def fill_for_landing(state: Path, *, checkpoint_plan: bool) -> None:
    add(state, "ws_plan", kind="spec", lease_files=["src/plan.py"])
    add(state, "ws_ship", kind="feature", lease_files=["src/ship.py"])
    ws.claim(state, "ws_plan", "bee-plan", "wt-plan", now=BASE, max_active=2)
    if checkpoint_plan:
        ws.checkpoint(state, "ws_plan", "between steps", ["unit green"], now=BASE)
    ws.claim(state, "ws_ship", "bee-ship", "wt-ship", now=BASE, max_active=2)


def test_landing_event_preempts_at_a_safe_checkpoint_without_mutating(tmp_path: Path) -> None:
    fill_for_landing(tmp_path, checkpoint_plan=True)
    before = ws.queue_path(tmp_path).read_bytes()

    event = {"kind": "landing", "lease_files": ["src/plan.py"], "max_active": 2}
    action = ws.preempt_for_event(tmp_path, event, now=BASE + 5)

    assert action["action"] == "checkpoint"
    assert action["work_id"] == "ws_plan"
    assert action["owner"] == "bee-plan"
    assert action["checkpoint"]["note"] == "between steps"
    assert action["then"] == "release"
    assert action["safe"] is True
    assert action["interrupts_non_idempotent"] is False
    assert ws.queue_path(tmp_path).read_bytes() == before


def test_landing_event_never_interrupts_an_uncheckpointed_operation(tmp_path: Path) -> None:
    fill_for_landing(tmp_path, checkpoint_plan=False)
    action = ws.preempt_for_event(
        tmp_path,
        {"kind": "landing", "lease_files": ["src/plan.py"], "max_active": 2},
        now=BASE,
    )
    assert action["action"] == "none"
    assert action["reason"] == "no_safe_checkpoint"


def test_non_landing_event_never_preempts(tmp_path: Path) -> None:
    fill_for_landing(tmp_path, checkpoint_plan=True)
    action = ws.preempt_for_event(tmp_path, {"kind": "planning"}, now=BASE)
    assert action["action"] == "none"
    assert action["reason"] == "event_not_landing"


def test_landing_event_with_free_capacity_needs_no_preemption(tmp_path: Path) -> None:
    add(tmp_path, "ws_plan", kind="spec")
    ws.claim(tmp_path, "ws_plan", "bee-1", "wt", now=BASE)
    ws.checkpoint(tmp_path, "ws_plan", "safe", [], now=BASE)
    action = ws.preempt_for_event(tmp_path, {"kind": "landing"}, now=BASE)
    assert action == {
        "action": "none",
        "reason": "capacity_available",
        "event": {"kind": "landing"},
        "active": 1,
        "capacity": ws.MAX_ACTIVE,
        "at": "2027-01-15T08:00:00Z",
    }


def test_prefers_overlapping_and_least_urgent_victim(tmp_path: Path) -> None:
    add(tmp_path, "ws_high", kind="spec", priority=1, lease_files=["src/a.py"])
    add(tmp_path, "ws_low", kind="spec", priority=8, lease_files=["src/b.py"])
    ws.claim(tmp_path, "ws_high", "bee-1", "wt", now=BASE, max_active=3)
    ws.checkpoint(tmp_path, "ws_high", "high safe", [], now=BASE)
    ws.claim(tmp_path, "ws_low", "bee-2", "wt", now=BASE, max_active=3)
    ws.checkpoint(tmp_path, "ws_low", "low safe", [], now=BASE)

    action = ws.preempt_for_event(
        tmp_path,
        {"kind": "landing", "lease_files": ["src/b.py"], "max_active": 2},
        now=BASE,
    )
    assert action["action"] == "checkpoint"
    assert action["work_id"] == "ws_low"

    action = ws.preempt_for_event(
        tmp_path, {"kind": "landing", "max_active": 2}, now=BASE
    )
    assert action["work_id"] == "ws_low"


def test_sleep_when_idle_is_a_noop_signal(tmp_path: Path) -> None:
    idle = ws.sleep_when_idle([])
    assert idle == {
        "sleep": True,
        "count": 0,
        "work_ids": [],
        "reason": "no_eligible_work",
    }

    add(tmp_path, "ws_a", kind="spec")
    busy = ws.sleep_when_idle(ws.ready_items(tmp_path))
    assert busy["sleep"] is False
    assert busy["count"] == 1
    assert busy["work_ids"] == ["ws_a"]
    assert busy["reason"] == "work_available"

    assert ws.sleep_when_idle(None)["sleep"] is True


def test_fairness_multiple_owners_get_distinct_work(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        add(tmp_path, f"ws_{name}", kind="spec", lease_files=[f"src/{name}.py"])

    order = [item["work_id"] for item in ws.ready_items(tmp_path)]
    assert order == ["ws_a", "ws_b", "ws_c"]
    assert order == [item["work_id"] for item in ws.ready_items(tmp_path)]

    ws.claim(tmp_path, "ws_a", "bee-1", "wt", now=BASE)
    ws.claim(tmp_path, "ws_b", "bee-2", "wt", now=BASE)
    ws.claim(tmp_path, "ws_c", "bee-3", "wt", now=BASE)
    assert {item["owner"] for item in ws.list_items(tmp_path)} == {"bee-1", "bee-2", "bee-3"}

    ws.release(tmp_path, "ws_b", owner="bee-2", now=BASE)
    assert ws.claim(tmp_path, "ws_b", "bee-4", "wt", now=BASE)["owner"] == "bee-4"


# ---------------------------------------------------------------------------
# the scheduler is not an agent
# ---------------------------------------------------------------------------


def test_scheduler_makes_no_model_or_bee_calls() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ws))
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)

    forbidden_modules = (
        "openai",
        "anthropic",
        "litellm",
        "transformers",
        "subprocess",
        "requests",
        "httpx",
        "bundlebees_plugin.client",
        "bundlebees_plugin.adapters",
        "bundlebees_plugin.tools",
        "bundlebees_plugin.job",
        "bundlebees_plugin.agent_work",
        "bundlebees_plugin.hermes_adapter",
    )
    for module in imported:
        assert not any(module == bad or module.startswith(bad + ".") for bad in forbidden_modules), module

    for forbidden_call in ("dispatch", "invoke_bee", "call_model", "spawn", "system"):
        assert forbidden_call not in names
    public = {name for name in dir(ws) if not name.startswith("_")}
    for forbidden_name in ("dispatch", "invoke_bee", "call_model", "spawn"):
        assert forbidden_name not in public
