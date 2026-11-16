"""The coordinator action queue records, claims and fences — and nothing else.

Every case runs against a fresh temporary state directory and a fixed clock.
The queue under test only ever reads and writes its own JSON records: it never
executes a command, never calls a model, and never dispatches work. Execution
stays with the caller, and an AST guard at the end of this file proves that.

Fencing follows the classic Kleppmann lease-and-fence design: every claim mints
a monotonically increasing durable fencing value, a caller must present the
value it was minted for every mutation, and a paused owner that comes back
after another owner adopted the item is refused with a precise stale refusal.
"""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bundlebees_plugin import coordinator_actions as ca
from bundlebees_plugin import faults

BASE = 1_800_000_000.0


def clock(step: float = 0.0):
    value = [BASE]

    def tick() -> float:
        value[0] += step
        return value[0]

    return tick


def enqueue(state: Path, action_id: str, **kwargs):
    kwargs.setdefault("action_key", f"key-{action_id}")
    kwargs.setdefault("kind", "note")
    return ca.enqueue(state, action_id, now=BASE, **kwargs)


def claim(state: Path, action_id: str, owner: str = "bee-a", **kwargs):
    kwargs.setdefault("ttl_s", 900.0)
    return ca.claim(state, action_id, owner, now=BASE, **kwargs)


def record_path(state: Path, action_id: str) -> Path:
    return ca.action_path(state, action_id)


def stored(state: Path, action_id: str) -> dict:
    return json.loads(record_path(state, action_id).read_text(encoding="utf-8"))


def error_of(call):
    try:
        call()
    except ca.ActionError as exc:
        return exc
    raise AssertionError("expected an ActionError")


# ---------------------------------------------------------------------------
# durable per-item records over the existing state dir
# ---------------------------------------------------------------------------


def test_enqueue_writes_a_private_per_item_record_under_the_existing_state_dir(tmp_path: Path) -> None:
    record = enqueue(tmp_path, "act_a", action_key="merge-1", kind="merge", repo="org/a")
    assert record["schema"] == "bundlebees.coordinator-actions/1"
    assert record["action_id"] == "act_a"
    assert record["action_key"] == "merge-1"
    assert record["kind"] == "merge"
    assert record["repo"] == "org/a"
    assert record["status"] == "queued"
    assert record["fence_seq"] == 0
    assert record["owner"] == ""
    assert record["adopted_from"] is None
    assert record["result"] is None
    assert record["created_at"] == record["updated_at"]

    directory = tmp_path / "coordinator" / "actions"
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    path = record_path(tmp_path, "act_a")
    assert path.name == "act_a.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == ca.load(tmp_path, "act_a")


def test_enqueue_replay_by_action_key_returns_the_original_record_unchanged(tmp_path: Path) -> None:
    first = enqueue(tmp_path, "act_a", action_key="merge-1", payload={"pr": 7})
    replay = enqueue(tmp_path, "act_a", action_key="merge-1", payload={"pr": 999})
    assert replay == first
    assert replay["payload"] == {"pr": 7}
    assert sorted(p.name for p in (tmp_path / "coordinator" / "actions").glob("*.json")) == ["act_a.json"]
    assert stored(tmp_path, "act_a") == first


def test_enqueue_refuses_an_action_key_held_by_a_different_action(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", action_key="merge-1")
    exc = error_of(lambda: enqueue(tmp_path, "act_b", action_key="merge-1"))
    assert exc.code == "action_key_conflict"
    assert "act_a" in exc.message
    assert ca.load(tmp_path, "act_b") is None


def test_enqueue_refuses_redefining_an_existing_action_id(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", action_key="merge-1")
    exc = error_of(lambda: enqueue(tmp_path, "act_a", action_key="merge-2"))
    assert exc.code == "action_id_conflict"
    assert "merge-1" in exc.message


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"action_key": "k", "kind": "note"}, "invalid_action_id"),
        ({"action_key": "", "kind": "note"}, "invalid_action_key"),
        ({"action_key": "bad\nkey", "kind": "note"}, "invalid_action_key"),
        ({"action_key": "k", "kind": ""}, "invalid_kind"),
        ({"action_key": "k", "kind": "merge drop"}, "invalid_kind"),
        ({"action_key": "k", "kind": "note", "repo": "org/a\nb"}, "invalid_repo"),
        ({"action_key": "k", "kind": "note", "repo": "org/a", "target": "x\ny"}, "invalid_target"),
    ],
)
def test_enqueue_refuses_unsafe_ids_labels_and_shapes(tmp_path: Path, kwargs: dict, code: str) -> None:
    bad_id = "../escape" if code == "invalid_action_id" else "act_a"
    exc = error_of(lambda: enqueue(tmp_path, bad_id, **kwargs))
    assert exc.code == code, exc.message


def test_enqueue_refuses_a_payload_that_is_not_json_serializable(tmp_path: Path) -> None:
    exc = error_of(lambda: enqueue(tmp_path, "act_a", payload={"bad": object()}))
    assert exc.code == "payload_invalid"
    assert ca.load(tmp_path, "act_a") is None


def test_load_and_list_refuse_corrupt_records_and_list_is_sorted(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_c")
    enqueue(tmp_path, "act_a")
    enqueue(tmp_path, "act_b")
    assert [item["action_id"] for item in ca.list_actions(tmp_path)] == ["act_a", "act_b", "act_c"]

    record_path(tmp_path, "act_b").write_text("{not json", encoding="utf-8")
    exc = error_of(lambda: ca.load(tmp_path, "act_b"))
    assert exc.code == "state_corrupt"
    assert "act_b" in exc.message
    assert error_of(lambda: ca.list_actions(tmp_path)).code == "state_corrupt"


# ---------------------------------------------------------------------------
# atomic per-item claims and monotonic durable fencing values
# ---------------------------------------------------------------------------


def test_claim_is_atomic_under_a_thread_race_exactly_one_winner(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", kind="merge", repo="org/a")
    outcomes: list[tuple[str, str]] = []

    def attempt(index: int) -> None:
        try:
            claim(tmp_path, "act_a", owner=f"bee-{index}")
            outcomes.append(("won", f"bee-{index}"))
        except ca.ActionError as exc:
            outcomes.append((exc.code, f"bee-{index}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for index in range(8):
            pool.submit(attempt, index)
    wins = [owner for code, owner in outcomes if code == "won"]
    losses = [code for code, _ in outcomes if code != "won"]
    assert len(wins) == 1
    assert losses == ["claim_active"] * 7
    assert stored(tmp_path, "act_a")["owner"] == wins[0]
    assert ca.fence_value(tmp_path) == 1


def test_claim_mints_strictly_increasing_fence_seq_values_per_state_dir(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    enqueue(tmp_path, "act_b")
    enqueue(tmp_path, "act_c")
    first = claim(tmp_path, "act_a")
    assert first["fence_seq"] == 1
    second = claim(tmp_path, "act_b")
    assert second["fence_seq"] == 2

    ca.complete(tmp_path, "act_a", fence_seq=first["fence_seq"], now=BASE)
    third = claim(tmp_path, "act_c")
    assert third["fence_seq"] == 3

    other = tmp_path / "second-coordinator-state"
    enqueue(other, "act_a")
    assert claim(other, "act_a")["fence_seq"] == 1
    assert ca.fence_value(other) == 1
    assert ca.fence_value(tmp_path) == 3


def test_claim_refuses_unknown_and_done_actions(tmp_path: Path) -> None:
    assert error_of(lambda: claim(tmp_path, "act_missing")).code == "unknown_action"
    enqueue(tmp_path, "act_a")
    done = claim(tmp_path, "act_a")
    ca.complete(tmp_path, "act_a", fence_seq=done["fence_seq"], now=BASE)
    assert error_of(lambda: claim(tmp_path, "act_a", owner="bee-b")).code == "action_done"
    assert error_of(lambda: ca.claim(tmp_path, "act_a", "bee-b", now=BASE)).code == "action_done"


def test_claim_stamps_owner_lease_expiry_and_timestamps(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    record = ca.claim(tmp_path, "act_a", "bee-a", ttl_s=100.0, now=BASE)
    assert record["status"] == "claimed"
    assert record["owner"] == "bee-a"
    assert record["lease_expires_at"] == BASE + 100.0
    assert record["claimed_at"].endswith("Z")
    assert record["adopted_from"] is None
    assert stored(tmp_path, "act_a") == record


def test_claim_refuses_an_active_claim_naming_owner_and_expiry(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    claim(tmp_path, "act_a", owner="bee-a", ttl_s=100.0)
    exc = error_of(lambda: claim(tmp_path, "act_a", owner="bee-b"))
    assert exc.code == "claim_active"
    assert "bee-a" in exc.message
    assert "1800000100" in exc.message


def test_claim_refuses_an_expired_claim_and_points_at_adoption(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    claim(tmp_path, "act_a", owner="bee-a", ttl_s=60.0)
    exc = error_of(lambda: ca.claim(tmp_path, "act_a", "bee-b", now=BASE + 61.0))
    assert exc.code == "claim_expired"
    assert "adopt" in (exc.message + exc.fix).lower()


# ---------------------------------------------------------------------------
# stale-owner adoption
# ---------------------------------------------------------------------------


def test_adopt_takes_over_an_expired_claim_and_mints_a_higher_fence(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    stale = claim(tmp_path, "act_a", owner="bee-a", ttl_s=60.0)
    adopted = ca.adopt(tmp_path, "act_a", "bee-b", now=BASE + 61.0)
    assert adopted["status"] == "claimed"
    assert adopted["owner"] == "bee-b"
    assert adopted["adopted_from"] == "bee-a"
    assert adopted["fence_seq"] == stale["fence_seq"] + 1
    assert adopted["lease_expires_at"] == BASE + 61.0 + 900.0
    assert stored(tmp_path, "act_a") == adopted


def test_adopt_refuses_an_active_claim(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    claim(tmp_path, "act_a", owner="bee-a", ttl_s=900.0)
    assert error_of(lambda: ca.adopt(tmp_path, "act_a", "bee-b", now=BASE)).code == "claim_active"


def test_adopt_refuses_an_item_that_was_never_claimed(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    assert error_of(lambda: ca.adopt(tmp_path, "act_a", "bee-a", now=BASE)).code == "nothing_to_adopt"


def test_renew_extends_the_lease_only_for_the_current_fence(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    live = claim(tmp_path, "act_a", owner="bee-a", ttl_s=100.0)
    renewed = ca.renew(tmp_path, "act_a", fence_seq=live["fence_seq"], ttl_s=50.0, now=BASE + 10.0)
    assert renewed["lease_expires_at"] == BASE + 60.0
    assert renewed["owner"] == "bee-a"
    assert stored(tmp_path, "act_a") == renewed

    exc = error_of(lambda: ca.renew(tmp_path, "act_a", fence_seq=0, ttl_s=50.0, now=BASE + 11.0))
    assert exc.code == "stale_fence"
    assert stored(tmp_path, "act_a")["lease_expires_at"] == BASE + 60.0


# ---------------------------------------------------------------------------
# precise stale refusals
# ---------------------------------------------------------------------------


def test_complete_requires_the_current_fence_seq(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    live = claim(tmp_path, "act_a", owner="bee-a")
    exc = error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=0, now=BASE))
    assert exc.code == "stale_fence"
    assert exc.message.count("0") >= 1 and "1" in exc.message
    assert stored(tmp_path, "act_a")["status"] == "claimed"

    assert error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=None, now=BASE)).code == "fence_invalid"
    assert error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq="1", now=BASE)).code == "fence_invalid"


def test_an_adopted_owner_refuses_the_paused_owner_with_a_precise_stale_refusal(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", kind="merge", repo="org/a")
    stale = claim(tmp_path, "act_a", owner="bee-a", ttl_s=60.0)
    ca.adopt(tmp_path, "act_a", "bee-b", now=BASE + 61.0)

    exc = error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=stale["fence_seq"], now=BASE + 62.0))
    assert exc.code == "stale_fence"
    assert str(stale["fence_seq"]) in exc.message
    assert "2" in exc.message
    assert "bee-b" in exc.message
    after = stored(tmp_path, "act_a")
    assert after["status"] == "claimed" and after["owner"] == "bee-b" and after["fence_seq"] == 2


def test_complete_after_lease_expiry_without_adoption_succeeds_on_the_current_fence(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    live = claim(tmp_path, "act_a", owner="bee-a", ttl_s=60.0)
    done = ca.complete(tmp_path, "act_a", fence_seq=live["fence_seq"], now=BASE + 600.0)
    assert done["status"] == "done"
    assert done["result"] is None
    assert done["lease_expires_at"] is None


def test_a_future_fence_and_a_foreign_fence_are_mismatches_not_stale(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    enqueue(tmp_path, "act_b")
    first = claim(tmp_path, "act_a")
    second = claim(tmp_path, "act_b")

    exc = error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=first["fence_seq"] + 9, now=BASE))
    assert exc.code == "fence_mismatch"
    exc = error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=second["fence_seq"], now=BASE))
    assert exc.code == "fence_mismatch"
    assert stored(tmp_path, "act_a")["status"] == "claimed"

    done = ca.complete(tmp_path, "act_a", fence_seq=first["fence_seq"], now=BASE)
    assert done["status"] == "done"


def test_complete_and_renew_refuse_unknown_queued_and_done_states(tmp_path: Path) -> None:
    assert error_of(lambda: ca.complete(tmp_path, "act_missing", fence_seq=1, now=BASE)).code == "unknown_action"
    enqueue(tmp_path, "act_a")
    assert error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=1, now=BASE)).code == "not_claimed"
    assert error_of(lambda: ca.renew(tmp_path, "act_a", fence_seq=1, ttl_s=60.0, now=BASE)).code == "not_claimed"
    live = claim(tmp_path, "act_a")
    ca.complete(tmp_path, "act_a", fence_seq=live["fence_seq"], now=BASE)
    assert error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=live["fence_seq"], now=BASE)).code == "action_done"
    assert error_of(lambda: ca.renew(tmp_path, "act_a", fence_seq=live["fence_seq"], ttl_s=60.0, now=BASE)).code == "action_done"


def test_complete_refuses_a_result_that_is_not_json_serializable(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    live = claim(tmp_path, "act_a")
    exc = error_of(lambda: ca.complete(tmp_path, "act_a", fence_seq=live["fence_seq"], result=[object()], now=BASE))
    assert exc.code == "result_invalid"
    assert stored(tmp_path, "act_a")["status"] == "claimed"


# ---------------------------------------------------------------------------
# durable fencing values survive a crash between mint and claim
# ---------------------------------------------------------------------------


def test_fence_values_survive_a_crash_between_mint_and_claim(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    enqueue(tmp_path, "act_b")

    def crash(fence_seq: int, **_):
        raise RuntimeError(f"simulated crash after minting {fence_seq}")

    handle = faults.register("coordinator_actions_fence_allocated", crash)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            claim(tmp_path, "act_a")
        assert ca.fence_value(tmp_path) == 1
        assert ca.load(tmp_path, "act_a")["status"] == "queued"
    finally:
        faults.unregister(handle and "coordinator_actions_fence_allocated" or "")

    second = claim(tmp_path, "act_b")
    assert second["fence_seq"] == 2
    assert json.loads(ca.fence_path(tmp_path).read_text(encoding="utf-8"))["fence_seq"] == 2


def test_the_fault_checkpoint_is_inert_without_a_registered_hook(tmp_path: Path) -> None:
    assert faults.registered() == ()
    enqueue(tmp_path, "act_a")
    assert claim(tmp_path, "act_a")["fence_seq"] == 1


def test_a_corrupt_fence_counter_is_refused_instead_of_reused(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    ca.fence_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    ca.fence_path(tmp_path).write_text("{corrupt", encoding="utf-8")
    assert error_of(lambda: ca.fence_value(tmp_path)).code == "fence_counter_corrupt"
    exc = error_of(lambda: claim(tmp_path, "act_a"))
    assert exc.code == "fence_counter_corrupt"
    assert ca.load(tmp_path, "act_a")["status"] == "queued"


# ---------------------------------------------------------------------------
# idempotent replay burns nothing
# ---------------------------------------------------------------------------


def test_replay_burns_no_fence_and_touches_no_lease(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", kind="merge", repo="org/a")
    live = claim(tmp_path, "act_a", owner="bee-a")
    replay = enqueue(tmp_path, "act_a", action_key="key-act_a", kind="merge", repo="org/a")
    assert replay == live
    assert ca.fence_value(tmp_path) == 1
    assert len(ca.list_actions(tmp_path)) == 1
    assert stored(tmp_path, "act_a") == live


# ---------------------------------------------------------------------------
# per-repo serialization for overlapping merge and deploy leases only
# ---------------------------------------------------------------------------


def test_a_merge_blocks_an_overlapping_deploy_on_the_same_repo(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_merge", kind="merge", repo="org/a")
    enqueue(tmp_path, "act_deploy", kind="deploy", repo="org/a")
    claim(tmp_path, "act_merge", owner="bee-a")
    assert ca.fence_value(tmp_path) == 1

    exc = error_of(lambda: claim(tmp_path, "act_deploy", owner="bee-b"))
    assert exc.code == "lease_conflict"
    assert "act_merge" in exc.message and "merge" in exc.message
    assert ca.load(tmp_path, "act_deploy")["status"] == "queued"
    assert ca.fence_value(tmp_path) == 1


def test_two_merges_on_one_repo_serialize(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_first", kind="merge", repo="org/a")
    enqueue(tmp_path, "act_second", kind="merge", repo="org/a")
    claim(tmp_path, "act_first")
    assert error_of(lambda: claim(tmp_path, "act_second")).code == "lease_conflict"


def test_actions_outside_the_merge_and_deploy_family_never_serialize(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_note_1", kind="note", repo="org/a")
    enqueue(tmp_path, "act_note_2", kind="note", repo="org/a")
    enqueue(tmp_path, "act_merge", kind="merge", repo="org/a")
    claim(tmp_path, "act_note_1")
    claim(tmp_path, "act_note_2")
    claim(tmp_path, "act_merge")

    enqueue(tmp_path, "act_note_3", kind="note", repo="org/a")
    assert claim(tmp_path, "act_note_3", owner="bee-d")["status"] == "claimed"


def test_non_overlapping_targets_and_other_repos_do_not_serialize(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_merge_x", kind="merge", repo="org/a", target="feature/x")
    enqueue(tmp_path, "act_deploy_docs", kind="deploy", repo="org/a", target="docs/site")
    enqueue(tmp_path, "act_merge_other", kind="merge", repo="org/b")
    claim(tmp_path, "act_merge_x")
    claim(tmp_path, "act_deploy_docs")
    claim(tmp_path, "act_merge_other")

    enqueue(tmp_path, "act_deploy_x", kind="deploy", repo="org/a", target="feature/x")
    assert error_of(lambda: claim(tmp_path, "act_deploy_x", owner="bee-d")).code == "lease_conflict"

    enqueue(tmp_path, "act_deploy_stage", kind="deploy", repo="org/a", target="staging")
    assert claim(tmp_path, "act_deploy_stage", owner="bee-e")["status"] == "claimed"


def test_completion_and_expiry_release_the_repo_lease(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_merge", kind="merge", repo="org/a")
    enqueue(tmp_path, "act_deploy", kind="deploy", repo="org/a")
    live = claim(tmp_path, "act_merge", owner="bee-a", ttl_s=60.0)
    assert error_of(lambda: claim(tmp_path, "act_deploy", owner="bee-b")).code == "lease_conflict"

    ca.complete(tmp_path, "act_merge", fence_seq=live["fence_seq"], now=BASE)
    assert claim(tmp_path, "act_deploy", owner="bee-b")["status"] == "claimed"


def test_an_expired_claim_holds_no_repo_lease(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_merge", kind="merge", repo="org/a")
    enqueue(tmp_path, "act_deploy", kind="deploy", repo="org/a")
    claim(tmp_path, "act_merge", owner="bee-a", ttl_s=60.0)
    assert ca.claim(tmp_path, "act_deploy", "bee-b", now=BASE + 61.0)["status"] == "claimed"


# ---------------------------------------------------------------------------
# the queue never executes anything
# ---------------------------------------------------------------------------


def test_the_queue_module_never_executes_anything() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ca))
    imported: set[str] = set()
    names: set[str] = set()
    attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "bundlebees_plugin." if node.level else ""
            imported.add(f"{prefix}{node.module or ''}")
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)

    allowed_imports = {
        "__future__",
        "json",
        "os",
        "re",
        "time",
        "uuid",
        "fcntl",
        "contextlib",
        "datetime",
        "pathlib",
        "typing",
        "importlib",
        "bundlebees_plugin.security",
    }
    for module in imported:
        assert module in allowed_imports, module

    forbidden_calls = {
        "system",
        "popen",
        "Popen",
        "exec",
        "eval",
        "compile",
        "__import__",
        "spawn",
        "spawnv",
        "fork",
        "forkpty",
        "startfile",
        "execv",
        "execve",
        "execvp",
        "kill",
        "killpg",
        "terminate",
        "getoutput",
        "getstatusoutput",
        "check_call",
        "check_output",
        "run",
        "run_command",
        "dispatch",
        "invoke",
        "launch",
    }
    assert not (names & forbidden_calls), sorted(names & forbidden_calls)

    # re.compile is bookkeeping, so attribute access is judged on its own list
    # that leaves the dynamic-evaluation builtins to the bare-name check above.
    forbidden_attrs = forbidden_calls - {"exec", "eval", "compile", "__import__"}
    assert not (attrs & forbidden_attrs), sorted(attrs & forbidden_attrs)

    public = {name for name in dir(ca) if not name.startswith("_")}
    for verb in ("run", "dispatch", "execute", "spawn", "invoke", "start", "launch"):
        assert verb not in public, verb
