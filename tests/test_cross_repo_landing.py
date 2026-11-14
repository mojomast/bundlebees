"""Cross-repository proof: hyvv producer bytes land through bundlebees.

The event is built by the real ``launcher.build_landed_event`` from the pinned
hyvv checkout (``HYVV_SRC``), validated with the real canonical validator from
the pinned workflow checkout (``WORKFLOW_SRC/tools/bee_landed.py``), then fed
through :func:`landing_bridge.consume_once` with a real coordinator lease.

Nothing under ``bundlebees_plugin`` is stubbed.  When a checkout is missing the
module fails (never skips) under ``CI=1``; outside CI it skips because there is
no honest producer or validator to exercise.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from bundlebees_plugin import bee_events, event_consumer, landing_bridge, leases

HYVV_SRC = os.environ.get("HYVV_SRC", "").strip()
WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "").strip()

SUBSCRIBER = "coordinator"
FEATURE = "BEE-CROSS-1"
JOB = "job_cross_0001"
TASK = "task_cross_0001"
ATTEMPT = "att_cross_0001"
SOURCE_REV = "a" * 40
FILES = ["src/cross_repo.py", "tests/test_cross_repo.py"]
COORDINATOR_ID = "coordinator-1"
COORDINATOR_SESSION = "session-1"
ARTIFACT = b"cross-repo-producer-artifact\n"
CFG = {"launcher_id": "worker-1", "model": "deepseek-v4.1-flash"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _ci_required() -> bool:
    return (
        _truthy(os.environ.get("CI"))
        or _truthy(os.environ.get("REQUIRE_HYVV"))
        or _truthy(os.environ.get("REQUIRE_WORKFLOW"))
    )


def _missing_checkouts() -> list[str]:
    missing: list[str] = []
    if not HYVV_SRC or not (Path(HYVV_SRC) / "launcher.py").is_file():
        missing.append("HYVV_SRC (a pinned hyvv checkout with launcher.py)")
    if not WORKFLOW_SRC:
        missing.append("WORKFLOW_SRC (a pinned workflow checkout)")
    else:
        root = Path(WORKFLOW_SRC)
        if not (root / "tools" / "bee_landed.py").is_file():
            missing.append("WORKFLOW_SRC/tools/bee_landed.py")
        if not (root / "schemas" / "bee-landed.schema.json").is_file():
            missing.append("WORKFLOW_SRC/schemas/bee-landed.schema.json")
    return missing


@pytest.fixture(scope="module", autouse=True)
def _pinned_checkouts():
    missing = _missing_checkouts()
    if not missing:
        return
    message = "cross-repo landing proof requires " + "; ".join(missing)
    if _ci_required():
        pytest.fail(message + " (CI=1 fails, never skips)")
    pytest.skip(message)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def producers(_pinned_checkouts):
    hyvv = _load_module("cross_repo_hyvv_launcher", Path(HYVV_SRC) / "launcher.py")
    canonical = _load_module(
        "cross_repo_bee_landed", Path(WORKFLOW_SRC) / "tools" / "bee_landed.py"
    )
    if not callable(getattr(hyvv, "build_landed_event", None)):
        pytest.fail("HYVV_SRC/launcher.py has no build_landed_event")
    if not callable(getattr(canonical, "validate", None)):
        pytest.fail("WORKFLOW_SRC/tools/bee_landed.py has no validate")
    return SimpleNamespace(hyvv=hyvv, canonical=canonical)


def _lease_brief() -> dict:
    return {
        "source_rev": SOURCE_REV,
        "assigned_files": list(FILES),
        "acceptance": ["the canonical landing validates and is handled once"],
        "branch": "feat/cross-repo-landing",
    }


def _attempt(
    lease_record: dict | None,
    *,
    lease_sha256: str | None = None,
    assigned_files: list[str] | None = None,
    coordinator: dict | None = None,
    transition: str | None = None,
    state: str = "succeeded",
) -> dict:
    if lease_sha256 is None:
        lease_sha256 = (
            lease_record["brief_sha256"] if lease_record is not None else "e" * 64
        )
    brief = {
        "objective": "land one terminal attempt across the hyvv/bundlebees boundary",
        "feature_id": FEATURE,
        "job_id": JOB,
        "task_id": TASK,
        "source_rev": SOURCE_REV,
        "coordinator": dict(
            coordinator or {"id": COORDINATOR_ID, "session": COORDINATOR_SESSION}
        ),
        "assigned_files": list(FILES if assigned_files is None else assigned_files),
        "lease_sha256": lease_sha256,
        "acceptance": ["the canonical landing validates and is handled once"],
    }
    if transition is not None:
        brief["terminal_transition_id"] = transition
    return {
        "attempt_id": ATTEMPT,
        "kind": "opencode",
        "brief": brief,
        "state": state,
        "detail": "",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_000.0,
        "model": "deepseek-v4.1-flash",
    }


def _result_file(root: Path) -> Path:
    path = root / "attempt.result.json"
    path.write_bytes(ARTIFACT)
    return path


def _build(producers, attempt: dict, result_path: Path) -> dict:
    return producers.hyvv.build_landed_event(attempt, CFG, result_path)


def _write(producers, outbox: Path, event: dict) -> Path:
    return producers.hyvv.write_landed_event(outbox, event)


def _write_named(outbox: Path, name: str, event: dict) -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(
        json.dumps(event, sort_keys=True, indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _state(tmp_path: Path, *, record_lease: bool = True) -> tuple[Path, dict | None]:
    state = tmp_path / "state"
    lease = leases.record_lease(state, ATTEMPT, _lease_brief()) if record_lease else None
    return state, lease


def _outbox_dir(tmp_path: Path) -> Path:
    return tmp_path / "hyvv-outbox"


def _events_outbox(state: Path) -> Path:
    return state / "events" / "outbox"


def _inbox(state: Path, identity: str = SUBSCRIBER) -> Path:
    return state / "events" / "inbox" / identity


def _json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _subscribe(state: Path, target: dict | None = None) -> None:
    bee_events.subscribe(
        state,
        target or {"identity": SUBSCRIBER, "file": "inbox.jsonl"},
    )


def test_real_hyvv_event_validates_lands_delivers_and_consumes_once(tmp_path, producers):
    state, lease = _state(tmp_path)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)
    attempt = _attempt(lease)
    event = _build(producers, attempt, result_path)

    assert lease["mac"]
    producers.canonical.validate(event)
    assert event["data"]["schema"] == bee_events.SCHEMA
    assert event["data"]["attempt_id"] == ATTEMPT
    assert event["data"]["lease"]["sha256"] == lease["brief_sha256"]
    assert event["id"] == bee_events.event_id_for(ATTEMPT, "succeeded", "succeeded")

    written = _write(producers, source, event)
    assert written.name == f"{event['id']}.json"
    assert json.loads(written.read_text(encoding="utf-8")) == event

    _subscribe(state)
    result = landing_bridge.consume_once(state, source)
    assert result["processed"] == 1
    assert result["failed"] == 0
    assert result["pending"] == 0
    assert result["sources"][0]["identities"] == [
        {"attempt_id": ATTEMPT, "job_id": JOB, "records": ["lease"]}
    ]

    stored_path = _events_outbox(state) / f"{event['id']}.json"
    assert stored_path.is_file()
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    producers.canonical.validate(stored)
    assert stored == event

    inbox_path = _inbox(state) / f"{event['id']}.json"
    assert inbox_path.is_file()
    inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
    assert inbox["event_id"] == event["id"]
    assert inbox["event"]["data"]["coordinator"]["identity"] == COORDINATOR_ID

    assert not (source / written.name).exists()
    assert (source / landing_bridge.ACK_DIR / written.name).is_file()
    acks = list((source / landing_bridge.ACK_DIR / ".acks").glob("*.ack.json"))
    assert len(acks) == 1
    assert json.loads(acks[0].read_text(encoding="utf-8"))["event_ids"] == [event["id"]]

    handled: list[dict] = []

    def handler(payload: dict) -> dict:
        handled.append(payload)
        return {"accepted": event["id"]}

    first = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="cross-repo-consumer"
    )
    assert first is not None
    assert first["status"] == "acked"
    assert first["duplicate"] is False
    assert len(handled) == 1
    assert handled[0] == event

    ack_path = state / "events" / "acks" / SUBSCRIBER / f"{event['id']}.json"
    assert ack_path.is_file()
    ack = json.loads(ack_path.read_text(encoding="utf-8"))
    assert ack["state"] == "acked"
    assert ack["event_id"] == event["id"]


def test_mismatched_lease_digest_and_files_are_refused(tmp_path, producers):
    state, lease = _state(tmp_path)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)

    bad_digest = _build(
        producers,
        _attempt(lease, lease_sha256="0" * 64, transition="tr-wrong-digest"),
        result_path,
    )
    bad_files = _build(
        producers,
        _attempt(
            lease,
            assigned_files=["src/other.py"],
            transition="tr-wrong-files",
        ),
        result_path,
    )
    _write(producers, source, bad_digest)
    _write(producers, source, bad_files)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 0
    assert result["failed"] == 2
    assert {failure["error"] for failure in result["failures"]} == {"lease_mismatch"}
    assert _json_files(_events_outbox(state)) == []
    assert (source / f"{bad_digest['id']}.json").exists()
    assert (source / f"{bad_files['id']}.json").exists()


def test_forged_coordinator_identity_cannot_replace_the_authoritative_landing(
    tmp_path, producers
):
    state, lease = _state(tmp_path)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)
    event = _build(producers, _attempt(lease), result_path)
    _write(producers, source, event)
    _subscribe(state)

    first = landing_bridge.consume_once(state, source)
    assert first["processed"] == 1
    authoritative = _events_outbox(state) / f"{event['id']}.json"
    before = authoritative.read_bytes()

    forged = _build(
        producers,
        _attempt(
            lease,
            coordinator={"id": "intruder-coordinator", "session": "intruder-session"},
        ),
        result_path,
    )
    assert forged["id"] == event["id"]
    assert forged["data"]["coordinator"]["identity"] == "intruder-coordinator"
    producers.canonical.validate(forged)
    forged_path = _write_named(source, "forged-coordinator.json", forged)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "event_conflict"
    assert authoritative.read_bytes() == before
    assert json.loads(authoritative.read_text(encoding="utf-8"))["data"]["coordinator"][
        "identity"
    ] == COORDINATOR_ID
    assert forged_path.exists()


def test_absent_source_metadata_is_refused(tmp_path, producers):
    state, _lease = _state(tmp_path, record_lease=False)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)
    event = _build(producers, _attempt(None), result_path)
    producers.canonical.validate(event)
    _write(producers, source, event)

    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "identity_missing"
    assert _json_files(_events_outbox(state)) == []
    assert (source / f"{event['id']}.json").exists()


def test_duplicate_delivery_does_not_call_the_handler_twice(tmp_path, producers):
    state, lease = _state(tmp_path)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)
    event = _build(producers, _attempt(lease), result_path)
    _write(producers, source, event)
    _subscribe(state)
    assert landing_bridge.consume_once(state, source)["processed"] == 1

    inbox_path = _inbox(state) / f"{event['id']}.json"
    delivery_bytes = inbox_path.read_bytes()
    handled: list[dict] = []

    def handler(payload: dict) -> dict:
        handled.append(payload)
        return {"accepted": True}

    first = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="cross-repo-consumer"
    )
    assert first is not None and first["status"] == "acked"
    assert len(handled) == 1

    (_inbox(state) / "zzz-duplicate-delivery.json").write_bytes(delivery_bytes)
    duplicate = event_consumer.consume_once(
        state, SUBSCRIBER, handler, consumer="cross-repo-consumer"
    )

    assert duplicate is not None
    assert duplicate["duplicate"] is True
    assert duplicate["status"] == "duplicate"
    assert len(handled) == 1
    acks = list((state / "events" / "acks" / SUBSCRIBER).glob("*.json"))
    assert len(acks) == 1


def test_tampered_event_is_refused(tmp_path, producers):
    state, lease = _state(tmp_path)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)
    event = _build(producers, _attempt(lease), result_path)
    event["data"]["state"] = "verified"

    with pytest.raises(producers.canonical.BeeLandedError):
        producers.canonical.validate(event)

    _write(producers, source, event)
    result = landing_bridge.consume_once(state, source)

    assert result["processed"] == 0
    assert result["failed"] == 1
    assert result["failures"][0]["error"] == "event_schema_invalid"
    assert _json_files(_events_outbox(state)) == []
    assert (source / f"{event['id']}.json").exists()


def test_failed_delivery_is_retried_after_the_subscriber_is_fixed(tmp_path, producers):
    state, lease = _state(tmp_path)
    source = _outbox_dir(tmp_path)
    result_path = _result_file(tmp_path)
    event = _build(producers, _attempt(lease), result_path)
    _write(producers, source, event)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    try:
        _subscribe(
            state,
            {
                "identity": SUBSCRIBER,
                "url": f"http://127.0.0.1:{port}/inbox",
                "secret": "cross-repo-retry-secret",
            },
        )
        first = landing_bridge.consume_once(state, source)
    finally:
        listener.close()

    assert first["processed"] == 0
    assert first["failed"] == 1
    assert first["failures"][0]["error"] == "delivery_pending"
    assert (_events_outbox(state) / f"{event['id']}.json").is_file()
    assert (source / f"{event['id']}.json").exists()
    assert not (_inbox(state) / f"{event['id']}.json").exists()

    assert bee_events.unsubscribe(state, SUBSCRIBER) is True
    _subscribe(state)

    second = landing_bridge.consume_once(state, source)

    assert second["processed"] == 1
    assert second["failed"] == 0
    assert second["pending"] == 0
    assert (_inbox(state) / f"{event['id']}.json").is_file()
    assert not (source / f"{event['id']}.json").exists()
    assert (source / landing_bridge.ACK_DIR / f"{event['id']}.json").is_file()
