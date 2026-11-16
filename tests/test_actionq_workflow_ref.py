"""Every durable action-queue record is pinned to its workflow protocol commit.

The owner's convention of Nov 13: workflow is a dependency of execution, not
merely a source of inspiration. The queue therefore carries ``workflow_ref`` —
the exact 40-hex workflow protocol commit under which the action was
authorized and will be executed — on every record it appends, refuses an
append that declares no pin (``missing_workflow_ref``), refuses a pin that is
not a full commit (``invalid_workflow_ref``), refuses to mutate a stored
record that does not carry the field at all, and carries the pin untouched
through claim, adoption, renewal and completion so verify and review evidence
binds to the protocol revision the action actually ran under.

Every case runs against a fresh temporary state directory and a fixed clock,
matching the conventions of ``tests/test_coordinator_actions.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import coordinator_actions as ca

BASE = 1_800_000_000.0
PIN = "a" * 40
OTHER_PIN = "b" * 40


def enqueue(state: Path, action_id: str, **kwargs):
    kwargs.setdefault("action_key", f"key-{action_id}")
    kwargs.setdefault("kind", "note")
    return ca.enqueue(state, action_id, now=BASE, **kwargs)


def stored(state: Path, action_id: str) -> dict:
    return json.loads(ca.action_path(state, action_id).read_text(encoding="utf-8"))


def error_of(call):
    try:
        call()
    except ca.ActionError as exc:
        return exc
    raise AssertionError("expected an ActionError")


def write_legacy_record(state: Path, action_id: str, **overrides) -> dict:
    """The pre-convention record shape: a valid record with no workflow_ref."""

    record = {
        "schema": ca.SCHEMA,
        "action_id": action_id,
        "action_key": f"key-{action_id}",
        "kind": "merge",
        "repo": "org/a",
        "target": "",
        "payload": {},
        "status": "queued",
        "fence_seq": 0,
        "owner": "",
        "claimed_at": None,
        "lease_expires_at": None,
        "adopted_from": None,
        "result": None,
        "created_at": "2027-01-15T07:55:00Z",
        "updated_at": "2027-01-15T07:55:00Z",
    }
    record.update(overrides)
    path = ca.action_path(state, action_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# the record shape carries the pin
# ---------------------------------------------------------------------------


def test_enqueue_stamps_the_pinned_workflow_ref_into_the_record(tmp_path: Path) -> None:
    record = enqueue(tmp_path, "act_a", workflow_ref=PIN.upper())
    assert record["workflow_ref"] == PIN
    assert stored(tmp_path, "act_a")["workflow_ref"] == PIN
    assert ca.load(tmp_path, "act_a")["workflow_ref"] == PIN


def test_every_record_carries_the_field_even_when_the_caller_pins_nothing(tmp_path: Path) -> None:
    record = enqueue(tmp_path, "act_a")
    assert "workflow_ref" in record
    assert record["workflow_ref"] == ""
    assert stored(tmp_path, "act_a")["workflow_ref"] == ""


# ---------------------------------------------------------------------------
# the validator refuses appends without the pin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ref", [None, ""])
def test_enqueue_refuses_an_append_that_declares_no_pin(tmp_path: Path, ref) -> None:
    exc = error_of(
        lambda: ca.enqueue(tmp_path, "act_a", action_key="k", kind="note", workflow_ref=ref, now=BASE)
    )
    assert exc.code == "missing_workflow_ref"
    assert ca.load(tmp_path, "act_a") is None
    assert ca.fence_value(tmp_path) == 0


@pytest.mark.parametrize("ref", ["abc", "a" * 39, "z" * 40, 123, ["a" * 40]])
def test_enqueue_refuses_a_pin_that_is_not_a_full_commit(tmp_path: Path, ref) -> None:
    exc = error_of(
        lambda: ca.enqueue(tmp_path, "act_a", action_key="k", kind="note", workflow_ref=ref, now=BASE)
    )
    assert exc.code == "invalid_workflow_ref"
    assert ca.load(tmp_path, "act_a") is None


def test_a_refused_append_writes_nothing_and_burns_no_fence(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_before")
    assert ca.fence_value(tmp_path) == 0
    error_of(
        lambda: ca.enqueue(tmp_path, "act_a", action_key="k", kind="note", workflow_ref=None, now=BASE)
    )
    assert sorted(p.name for p in (tmp_path / "coordinator" / "actions").glob("*.json")) == [
        "act_before.json"
    ]
    assert ca.fence_value(tmp_path) == 0


# ---------------------------------------------------------------------------
# replay matches on the pin too
# ---------------------------------------------------------------------------


def test_replay_returns_the_pinned_record_untouched_and_refuses_a_different_pin(tmp_path: Path) -> None:
    first = enqueue(tmp_path, "act_a", action_key="merge-1", workflow_ref=PIN, payload={"pr": 7})
    replay = enqueue(tmp_path, "act_a", action_key="merge-1", workflow_ref=PIN, payload={"pr": 999})
    assert replay == first
    assert replay["workflow_ref"] == PIN
    assert replay["payload"] == {"pr": 7}

    exc = error_of(
        lambda: enqueue(tmp_path, "act_a", action_key="merge-1", workflow_ref=OTHER_PIN)
    )
    assert exc.code == "action_id_conflict"
    assert stored(tmp_path, "act_a")["workflow_ref"] == PIN

    # a replay that declares no pin is not the pinned shape; it is refused
    # instead of silently re-enqueueing the action under no protocol.
    exc = error_of(lambda: enqueue(tmp_path, "act_a", action_key="merge-1"))
    assert exc.code == "action_id_conflict"
    assert stored(tmp_path, "act_a")["workflow_ref"] == PIN


# ---------------------------------------------------------------------------
# the pin is preserved and surfaced through claim, renewal, adoption, completion
# ---------------------------------------------------------------------------


def test_claim_carries_the_pin_forward(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", kind="merge", repo="org/a", workflow_ref=PIN)
    claimed = ca.claim(tmp_path, "act_a", "bee-a", now=BASE)
    assert claimed["workflow_ref"] == PIN
    assert claimed["fence_seq"] == 1
    assert stored(tmp_path, "act_a")["workflow_ref"] == PIN


def test_the_pin_survives_renewal_adoption_and_completion_next_to_the_result(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a", kind="merge", repo="org/a", workflow_ref=PIN)
    claimed = ca.claim(tmp_path, "act_a", "bee-a", ttl_s=60.0, now=BASE)
    renewed = ca.renew(tmp_path, "act_a", fence_seq=claimed["fence_seq"], ttl_s=60.0, now=BASE + 10.0)
    assert renewed["workflow_ref"] == PIN
    adopted = ca.adopt(tmp_path, "act_a", "bee-b", now=BASE + 200.0)
    assert adopted["workflow_ref"] == PIN
    assert adopted["adopted_from"] == "bee-a"
    done = ca.complete(
        tmp_path, "act_a", fence_seq=adopted["fence_seq"], result={"verdict": "GREEN"}, now=BASE + 300.0
    )
    assert done["status"] == "done"
    assert done["workflow_ref"] == PIN
    assert done["result"] == {"verdict": "GREEN"}

    final = stored(tmp_path, "act_a")
    assert final["workflow_ref"] == PIN
    assert final["result"] == {"verdict": "GREEN"}


def test_a_legacy_empty_pin_is_carried_through_the_whole_path(tmp_path: Path) -> None:
    enqueue(tmp_path, "act_a")
    claimed = ca.claim(tmp_path, "act_a", "bee-a", now=BASE)
    assert claimed["workflow_ref"] == ""
    done = ca.complete(tmp_path, "act_a", fence_seq=claimed["fence_seq"], now=BASE + 10.0)
    assert done["workflow_ref"] == ""
    assert stored(tmp_path, "act_a")["workflow_ref"] == ""


# ---------------------------------------------------------------------------
# a stored record without the field is refused, not perpetuated
# ---------------------------------------------------------------------------


def test_a_record_without_the_field_is_refused_at_every_mutation(tmp_path: Path) -> None:
    write_legacy_record(tmp_path, "act_legacy")
    before = ca.action_path(tmp_path, "act_legacy").read_bytes()
    assert ca.load(tmp_path, "act_legacy")["status"] == "queued"  # reads stay permissive

    exc = error_of(lambda: ca.claim(tmp_path, "act_legacy", "bee-a", now=BASE))
    assert exc.code == "missing_workflow_ref"
    assert ca.action_path(tmp_path, "act_legacy").read_bytes() == before
    assert ca.fence_value(tmp_path) == 0  # a refused claim mints no fencing value

    exc = error_of(lambda: ca.adopt(tmp_path, "act_legacy", "bee-b", now=BASE))
    assert exc.code == "missing_workflow_ref"
    assert ca.action_path(tmp_path, "act_legacy").read_bytes() == before


def test_a_claimed_legacy_record_is_refused_at_renewal_and_completion(tmp_path: Path) -> None:
    write_legacy_record(
        tmp_path,
        "act_legacy",
        status="claimed",
        owner="bee-x",
        fence_seq=5,
        claimed_at="2027-01-15T07:56:00Z",
        lease_expires_at=BASE - 1.0,
    )
    before = ca.action_path(tmp_path, "act_legacy").read_bytes()

    exc = error_of(lambda: ca.renew(tmp_path, "act_legacy", fence_seq=5, ttl_s=60.0, now=BASE))
    assert exc.code == "missing_workflow_ref"
    assert ca.action_path(tmp_path, "act_legacy").read_bytes() == before

    exc = error_of(lambda: ca.adopt(tmp_path, "act_legacy", "bee-b", now=BASE))
    assert exc.code == "missing_workflow_ref"
    assert ca.action_path(tmp_path, "act_legacy").read_bytes() == before

    exc = error_of(lambda: ca.complete(tmp_path, "act_legacy", fence_seq=5, now=BASE))
    assert exc.code == "missing_workflow_ref"
    assert ca.action_path(tmp_path, "act_legacy").read_bytes() == before
    assert ca.fence_value(tmp_path) == 0
