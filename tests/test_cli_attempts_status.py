"""Read-only ``hermes bundlebees attempts`` and ``status`` CLI views.

Both commands answer from the plugin's local attempt store -- leases, verify
results and accepted wave records -- with no network and no credential
values. The pair's only secret-store interaction is ``status``'s single
launcher-token presence probe: ``client.token_configured()`` asks Hermes's
``agent.secret_scope.get_secret`` for ``BUNDLEBEES_LAUNCHER_TOKEN`` to fill
``health.credential_configured``. The probe reads presence only, never a
value, never emits one, and fails closed when the scope is unavailable.
The lookalike value below is assembled from fragments so no source line
resembles a real credential, while still exercising the scrub path end to
end in the output assertions.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from bundlebees_plugin import agent_consumer, attribution, cli, grants, leases
from bundlebees_plugin.brief import validate_brief
from tests.fakes import FakeCtx, load_plugin, secret_scope


def _stamp(minutes_ago: int) -> str:
    """An ISO-8601 Zulu stamp relative to the wall clock.

    These fixtures used to be frozen literals. That silently aged: once the
    literals fell outside ``cli.ATTEMPT_TTL_S`` the "dispatched" fixtures
    became "expired" and the read-only views changed meaning. Relative stamps
    keep the fixtures describing the situation they are named for.
    """
    moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


CREATED_A = _stamp(60)
CREATED_B = _stamp(50)
VERIFIED_AT = _stamp(40)
ACCEPTED_AT = _stamp(30)

ROW_FIELDS = {
    "attempt_id",
    "state",
    "branch",
    "model",
    "route",
    "attributed_model",
    "project",
    "job_id",
    "repo",
    "created_at",
    "updated_at",
}

# Assembled from fragments: no source line here carries a credential-shaped
# literal, yet the finished value still trips the scrubber on its way out.
LOOKALIKE = "sk" + "-" + "a" * 32


def _ctx(settings: dict | None = None) -> FakeCtx:
    plugin = load_plugin()
    ctx = FakeCtx(settings=settings)
    plugin.register(ctx)
    return ctx


def _invoke(ctx: FakeCtx, argv: list[str]) -> int:
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    args = parser.parse_args(argv)
    return record["handler_fn"](args)


def _lease(
    state: Path,
    attempt_id: str,
    *,
    now: str,
    branch: str | None = None,
    project: str | None = None,
    model: str | None = None,
    job_id: str | None = None,
    repo: str | None = None,
) -> dict:
    brief = {
        "objective": f"work on {attempt_id}",
        "source_rev": "b" * 40,
        "branch": branch or f"feat/{attempt_id}",
        "assigned_files": ["src/a.py"],
        "acceptance": {"criteria": ["sample passes"]},
    }
    if project is not None:
        brief["project"] = project
    if model is not None:
        brief["model"] = model
    if job_id is not None:
        brief["job_id"] = job_id
    if repo is not None:
        brief["repo"] = repo
    return leases.record_lease(state, attempt_id, validate_brief(brief), now=now)


def _attribution_intent(
    state: Path,
    attempt_id: str,
    *,
    route: str | None = "zoo",
    model: str | None = "zoo:glm-5.3-flash",
    job_id: str = "job-attributed",
    project: str = "proj-attributed",
) -> dict:
    intent = attribution.intent_for(
        endpoint="http://127.0.0.1:47922",
        worker_id="worker-1",
        ledger_chain="chain-1",
        job_id=job_id,
        project=project,
        contract="contract-1",
        revision="b" * 40,
    )
    if route is not None:
        intent["route"] = route
    if model is not None:
        intent["model"] = model
    record = attribution.record_intent(state, intent)
    attribution.bind_attempt(state, record["intent_id"], attempt_id)
    return record


def _verify_result(state: Path, attempt_id: str, *, ok: bool, at: str) -> None:
    path = state / "verify-results" / f"{attempt_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "ok": ok,
                "sha": None,
                "branch": f"feat/{attempt_id}",
                "files": ["src/a.py"],
                "tests": [],
                "at": at,
                "evidence": {},
            }
        ),
        encoding="utf-8",
    )


def _completion_payload(
    attempt_id: str,
    *,
    recorded_at: str,
    state_name: str = "succeeded",
    **overrides,
) -> dict:
    record = {
        "schema": agent_consumer.TOOL_COMPLETION_SCHEMA,
        "attempt_id": attempt_id,
        "state": state_name,
        "recorded_at": recorded_at,
    }
    record.update(overrides)
    return record


def _write_completion(state: Path, attempt_id: str, payload) -> None:
    path = state / "tool-completions" / f"{attempt_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (bytes, bytearray)):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


def _accepted_wave(state: Path, wave_id: str, attempt_id: str, *, at: str) -> None:
    path = state / "waves" / f"{wave_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "wave_id": wave_id,
                "accepted": True,
                "head_sha": "c" * 40,
                "attempts": [attempt_id],
                "problems": {},
                "suite": {},
                "regressions": 0,
                "at": at,
            }
        ),
        encoding="utf-8",
    )


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_cli_adds_the_read_only_views():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)

    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)

    args = parser.parse_args(["attempts", "--state-dir", "/tmp/nowhere"])
    assert args.command == "attempts"
    args = parser.parse_args(["status"])
    assert args.command == "status"
    assert args.state_dir is None


# ---------------------------------------------------------------------------
# attempts
# ---------------------------------------------------------------------------


def test_attempts_lists_the_local_store_as_json(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(
        state, "att-b", now=CREATED_B,
        branch="feat/second", project="proj-b", model="model-two",
    )
    _lease(
        state, "att-a", now=CREATED_A,
        branch="feat/first", project="proj-a", model="model-one",
    )

    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    rows = payload["attempts"]
    assert [row["attempt_id"] for row in rows] == ["att-a", "att-b"]
    row = rows[0]
    assert set(row) == ROW_FIELDS
    assert row["state"] == "dispatched"
    assert row["branch"] == "feat/first"
    assert row["model"] == "model-one"
    assert row["project"] == "proj-a"
    assert row["created_at"] == CREATED_A
    assert row["updated_at"] == CREATED_A
    assert rows[1]["state"] == "dispatched"
    assert rows[1]["branch"] == "feat/second"
    assert rows[1]["project"] == "proj-b"
    assert rows[1]["model"] == "model-two"


def test_attempts_on_an_empty_store_yields_an_empty_list(tmp_path: Path, capsys):
    missing = tmp_path / "state"
    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(missing)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload == {"attempts": []}

    empty = tmp_path / "empty"
    empty.mkdir()
    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(empty)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload == {"attempts": []}


def test_attempts_state_reflects_local_evidence(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-accepted", now=CREATED_B)
    _accepted_wave(state, "w1", "att-accepted", at=ACCEPTED_AT)
    _lease(state, "att-verified", now=CREATED_A)
    _verify_result(state, "att-verified", ok=True, at=VERIFIED_AT)
    _lease(state, "att-failed", now=CREATED_A)
    _verify_result(state, "att-failed", ok=False, at=VERIFIED_AT)
    _lease(state, "att-fresh", now=CREATED_A)

    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    rows = {row["attempt_id"]: row for row in payload["attempts"]}
    assert set(rows) == {"att-accepted", "att-verified", "att-failed", "att-fresh"}
    assert rows["att-accepted"]["state"] == "accepted"
    assert rows["att-verified"]["state"] == "verified"
    assert rows["att-failed"]["state"] == "failed"
    assert rows["att-fresh"]["state"] == "dispatched"
    assert rows["att-verified"]["updated_at"] == VERIFIED_AT
    assert rows["att-accepted"]["updated_at"] == ACCEPTED_AT
    assert rows["att-fresh"]["updated_at"] == CREATED_A


def test_attempts_scrub_a_lookalike_before_printing(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-clean", now=CREATED_A, project="proj-clean")
    _lease(state, "att-marked", now=CREATED_B, project=LOOKALIKE)

    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(state)])
    out = capsys.readouterr().out

    assert rc == 0
    assert LOOKALIKE not in out
    assert "sk-" not in out
    rows = {row["attempt_id"]: row for row in json.loads(out)["attempts"]}
    assert set(rows) == {"att-clean", "att-marked"}
    assert rows["att-clean"]["project"] == "proj-clean"
    assert rows["att-marked"]["project"] == "[redacted]"


def test_a_secret_shaped_attempt_id_keeps_its_scrubbed_form(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, LOOKALIKE, now=CREATED_A, branch="feat/lookalike-id")
    _lease(state, "att-clean", now=CREATED_B)

    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(state)])
    out = capsys.readouterr().out

    assert rc == 0
    assert LOOKALIKE not in out
    rows = {row["attempt_id"]: row for row in json.loads(out)["attempts"]}
    assert set(rows) == {"[redacted]", "att-clean"}
    assert rows["[redacted]"]["state"] == "dispatched"
    assert rows["[redacted]"]["branch"] == "feat/lookalike-id"


def test_a_tampered_lease_is_skipped_without_failing_the_view(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-intact", now=CREATED_A)
    _lease(state, "att-edited", now=CREATED_B)
    lease_file = state / "leases" / "att-edited.json"
    lease_file.chmod(0o600)
    record = json.loads(lease_file.read_text(encoding="utf-8"))
    record.pop("mac")
    lease_file.write_text(json.dumps(record), encoding="utf-8")

    rc = _invoke(_ctx(), ["attempts", "--state-dir", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert [row["attempt_id"] for row in payload["attempts"]] == ["att-intact"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_counts_in_flight_work_and_the_bee_cap(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-accepted", now=CREATED_A)
    _accepted_wave(state, "w1", "att-accepted", at=ACCEPTED_AT)
    _lease(state, "att-verified", now=CREATED_A)
    _verify_result(state, "att-verified", ok=True, at=VERIFIED_AT)
    _lease(state, "att-failed", now=CREATED_A)
    _verify_result(state, "att-failed", ok=False, at=VERIFIED_AT)
    _lease(state, "att-fresh", now=CREATED_B)

    rc = _invoke(_ctx(), ["status", "--state-dir", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    # Only the dispatched attempt is in flight. att-verified has a verify
    # result: the bee finished and the result awaits acceptance, so it is not
    # work in progress (104 such rows were being reported as active bees).
    assert payload["in_flight"] == 1
    assert payload["in_flight_attempts"] == ["att-fresh"]
    assert payload["bee_cap"] == grants.GLOBAL_MAX_BEES


def test_status_on_an_empty_store_is_not_an_error(tmp_path: Path, capsys):
    state = tmp_path / "state"

    rc = _invoke(_ctx(), ["status", "--state-dir", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["in_flight"] == 0
    assert payload["in_flight_attempts"] == []
    assert payload["bee_cap"] == grants.GLOBAL_MAX_BEES


def test_status_reports_health_without_credentials(tmp_path: Path, capsys):
    state = tmp_path / "state"

    rc = _invoke(_ctx(), ["status", "--state-dir", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    health = payload["health"]
    assert health["adapter"] == "orbit-loopback"
    assert health["launcher_url_configured"] is False
    assert health["credential_configured"] is False


def test_status_reports_a_configured_credential_without_its_value(tmp_path: Path, capsys):
    state = tmp_path / "state"
    settings = {"adapter": "launcher", "launcher_url": "configured-elsewhere"}

    with secret_scope({cli.TOKEN_SECRET_NAME: LOOKALIKE}):
        rc = _invoke(_ctx(settings), ["status", "--state-dir", str(state)])
        out = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(out)
    health = payload["health"]
    assert health["adapter"] == "launcher"
    assert health["launcher_url_configured"] is True
    assert health["credential_configured"] is True
    assert LOOKALIKE not in out


# ---------------------------------------------------------------------------
# Dispatch identity travels with the row
# ---------------------------------------------------------------------------


def test_attempt_row_carries_the_job_the_brief_was_dispatched_for(tmp_path: Path, capsys):
    """The fleet view cannot say which job an attempt belongs to without this.

    The lease brief pins the dispatch target, but the attempt row used to drop
    it: the Hive ledger rendered "Job unknown" for every row, the investigation
    panel could not link an attempt to its job, and Rails had no key to bind to.
    """
    state = tmp_path / "state"
    _lease(state, "att-job", now=CREATED_A, job_id="rizzstack-m2-canary-2")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["job_id"] == "rizzstack-m2-canary-2"
    # A brief that names no repo leaves it null: project is NOT a repo slug and
    # substituting it would invent a repository that was never dispatched.
    assert row["repo"] is None


def test_attempt_row_without_a_job_stays_null(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-nojob", now=CREATED_A, project="proj-a")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["job_id"] is None
    assert row["repo"] is None


def test_attempt_row_carries_an_explicit_repo(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-repo", now=CREATED_A, job_id="job-x", repo="mojomast/bundlebees")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["repo"] == "mojomast/bundlebees"


# ---------------------------------------------------------------------------
# A dispatch with no recorded outcome is not in flight forever
# ---------------------------------------------------------------------------

ANCIENT = "2020-01-01T00:00:00Z"


def _now_stamp() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_attempt_without_an_outcome_past_the_ttl_is_expired_and_not_in_flight(
    tmp_path: Path, capsys
):
    state = tmp_path / "state"
    _lease(state, "att-old", now=ANCIENT, project="proj-a")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "expired"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 0
    assert status["in_flight_attempts"] == []


def test_attempt_dispatched_moments_ago_is_still_in_flight(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-now", now=_now_stamp(), project="proj-a")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "dispatched"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 1
    assert status["in_flight_attempts"] == ["att-now"]


def test_a_recorded_outcome_is_never_expired_away(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-verified", now=ANCIENT, project="proj-a")
    _verify_result(state, "att-verified", ok=True, at=ANCIENT)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "verified"

    # Its outcome is recorded, so it is not in flight and must not be expired.
    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == 0


def test_a_failed_outcome_past_the_ttl_stays_failed(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-failed", now=ANCIENT, project="proj-a")
    _verify_result(state, "att-failed", ok=False, at=ANCIENT)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "failed"


# ---------------------------------------------------------------------------
# Terminal tool completions are local evidence, not an outcome claim
# ---------------------------------------------------------------------------

COMPLETION_AT = _stamp(20)


def test_a_failed_completion_is_failed_and_not_in_flight(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-live", now=CREATED_B)
    _lease(state, "att-done", now=CREATED_A)
    _write_completion(
        state,
        "att-done",
        _completion_payload("att-done", recorded_at=COMPLETION_AT, state_name="failed"),
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    rows = {row["attempt_id"]: row for row in json.loads(capsys.readouterr().out)["attempts"]}
    assert rows["att-done"]["state"] == "failed"
    assert rows["att-done"]["updated_at"] == COMPLETION_AT
    assert rows["att-live"]["state"] == "dispatched"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 1
    assert status["in_flight_attempts"] == ["att-live"]


def test_a_succeeded_completion_is_unverified_and_not_in_flight(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-unverified", now=CREATED_A)
    _write_completion(
        state,
        "att-unverified",
        _completion_payload("att-unverified", recorded_at=COMPLETION_AT),
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "unverified"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 0
    assert status["in_flight_attempts"] == []


def test_a_completion_naming_a_different_attempt_is_ignored(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-own", now=CREATED_A)
    _write_completion(
        state,
        "att-own",
        _completion_payload(
            "att-other", recorded_at=COMPLETION_AT, state_name="failed"
        ),
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "dispatched"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 1
    assert status["in_flight_attempts"] == ["att-own"]


def test_tool_completion_rejects_a_traversal_attempt_id(tmp_path: Path):
    state = tmp_path / "state"
    (state / "tool-completions").mkdir(parents=True)
    outside = _completion_payload("../outside", recorded_at=COMPLETION_AT)
    (state / "outside.json").write_text(json.dumps(outside), encoding="utf-8")

    assert cli._tool_completion(state, "../outside") is None

    inside = _completion_payload("att-inside", recorded_at=COMPLETION_AT)
    _write_completion(state, "att-inside", inside)
    assert cli._tool_completion(state, "att-inside") == inside


def test_tool_completion_rejects_a_regex_valid_dotdot_attempt_id(tmp_path: Path):
    state = tmp_path / "state"
    payload = _completion_payload("a..b", recorded_at=COMPLETION_AT)
    _write_completion(state, "a..b", payload)

    assert cli._tool_completion(state, "a..b") is None


def test_tool_completion_rejects_an_absolute_attempt_id(tmp_path: Path):
    state = tmp_path / "state"
    abs_id = str(tmp_path / "abs-outside")
    payload = _completion_payload(abs_id, recorded_at=COMPLETION_AT)
    (tmp_path / "abs-outside.json").write_text(json.dumps(payload), encoding="utf-8")

    assert cli._tool_completion(state, abs_id) is None


def test_tool_completion_refuses_a_symlinked_record(tmp_path: Path):
    state = tmp_path / "state"
    completions = state / "tool-completions"
    completions.mkdir(parents=True)
    payload = _completion_payload("att_symlink", recorded_at=COMPLETION_AT)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(payload), encoding="utf-8")

    target = json.loads(outside.read_text(encoding="utf-8"))
    assert target["schema"] == agent_consumer.TOOL_COMPLETION_SCHEMA
    assert target["attempt_id"] == "att_symlink"

    (completions / "att_symlink.json").symlink_to(outside)
    assert cli._tool_completion(state, "att_symlink") is None

    control = _completion_payload("att_symlink_ok", recorded_at=COMPLETION_AT)
    _write_completion(state, "att_symlink_ok", control)
    assert cli._tool_completion(state, "att_symlink_ok") == control


def test_tool_completion_refuses_a_fifo_without_blocking(tmp_path: Path):
    state = tmp_path / "state"
    completions = state / "tool-completions"
    completions.mkdir(parents=True)
    path = completions / "att_fifo.json"
    os.mkfifo(path)
    payload = _completion_payload("att_fifo", recorded_at=COMPLETION_AT)
    encoded = json.dumps(payload).encode()

    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    keeper = None
    try:
        os.write(fd, encoded)
        assert json.loads(os.read(fd, len(encoded)).decode("utf-8")) == payload
        os.write(fd, encoded)

        # Keep the buffered evidence alive while the writer is closed, so the
        # read path cannot fall back on a writer to avoid opening the FIFO.
        keeper = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        os.close(fd)
        fd = None

        result: dict[str, object] = {}

        def call() -> None:
            result["value"] = cli._tool_completion(state, "att_fifo")

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(2.0)

        assert not thread.is_alive()
        assert result["value"] is None
    finally:
        if fd is not None:
            os.close(fd)
        if keeper is not None:
            os.close(keeper)


def test_tool_completion_refuses_a_directory(tmp_path: Path):
    state = tmp_path / "state"
    (state / "tool-completions" / "att_dir.json").mkdir(parents=True)

    assert cli._tool_completion(state, "att_dir") is None


def test_tool_completion_refuses_a_symlinked_completions_directory(
    tmp_path: Path, capsys
):
    state = tmp_path / "state"
    _lease(state, "att_symdir", now=CREATED_A)
    outside = tmp_path / "outside-completions"
    outside.mkdir()
    payload = _completion_payload("att_symdir", recorded_at=COMPLETION_AT)
    (outside / "att_symdir.json").write_text(json.dumps(payload), encoding="utf-8")
    (state / "tool-completions").symlink_to(outside, target_is_directory=True)

    assert cli._tool_completion(state, "att_symdir") is None

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "dispatched"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 1
    assert status["in_flight_attempts"] == ["att_symdir"]

    (state / "tool-completions").unlink()
    (state / "tool-completions").mkdir()
    control = _completion_payload("att_symdir_ok", recorded_at=COMPLETION_AT)
    _write_completion(state, "att_symdir_ok", control)
    assert cli._tool_completion(state, "att_symdir_ok") == control


def test_tool_completion_refuses_a_hardlinked_record(tmp_path: Path):
    state = tmp_path / "state"
    completions = state / "tool-completions"
    completions.mkdir(parents=True)
    payload = _completion_payload("att_hardlink", recorded_at=COMPLETION_AT)
    source = tmp_path / "hardlink-source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    os.link(source, completions / "att_hardlink.json")

    assert cli._tool_completion(state, "att_hardlink") is None

    control = _completion_payload("att_hardlink_ok", recorded_at=COMPLETION_AT)
    _write_completion(state, "att_hardlink_ok", control)
    assert cli._tool_completion(state, "att_hardlink_ok") == control


def _padded_completion(payload: dict, size: int) -> bytes:
    encoded = json.dumps(payload).encode()
    assert len(encoded) <= size
    return encoded + b" " * (size - len(encoded))


def test_tool_completion_accepts_exactly_65536_bytes_and_refuses_65537(
    tmp_path: Path,
):
    state = tmp_path / "state"
    payload = _completion_payload("att_bound", recorded_at=COMPLETION_AT)
    raw = _padded_completion(payload, 65536)
    assert len(raw) == 65536
    _write_completion(state, "att_bound", raw)
    assert cli._tool_completion(state, "att_bound") == payload

    _write_completion(state, "att_bound", raw + b" ")
    assert cli._tool_completion(state, "att_bound") is None


def test_tool_completion_post_read_bound_is_pinned(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    payload = _completion_payload("att_post_bound", recorded_at=COMPLETION_AT)
    _write_completion(state, "att_post_bound", payload)
    oversized = _padded_completion(payload, 70000)

    def fake_read(fd: int, size: int) -> bytes:
        return oversized

    monkeypatch.setattr(cli.os, "read", fake_read)
    assert cli._tool_completion(state, "att_post_bound") is None


def test_tool_completion_pre_read_bound_is_pinned(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    payload = _completion_payload("att_pre_bound", recorded_at=COMPLETION_AT)
    encoded = json.dumps(payload).encode()
    raw = encoded + b" " * (65537 - len(encoded))
    assert len(raw) == 65537
    _write_completion(state, "att_pre_bound", raw)

    def fake_read(fd: int, size: int) -> bytes:
        return encoded

    monkeypatch.setattr(cli.os, "read", fake_read)
    assert cli._tool_completion(state, "att_pre_bound") is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{not json", id="bad-json"),
        pytest.param(
            _completion_payload("att-bad", recorded_at=COMPLETION_AT, state_name="running"),
            id="unknown-state",
        ),
        pytest.param(b"[]", id="non-mapping"),
        pytest.param(b"\xff\xfe{}\xff", id="invalid-utf8"),
        pytest.param(
            ("[" * 10000 + "]" * 10000).encode(), id="deeply-nested-json"
        ),
        pytest.param(
            ("[" * 100000 + "]" * 100000).encode(),
            id="deeply-nested-oversized-json",
        ),
        pytest.param(
            json.dumps(
                _completion_payload(
                    "att-bad", recorded_at=COMPLETION_AT, padding="x" * 70000
                )
            ).encode(),
            id="oversized-file",
        ),
        pytest.param(
            {
                "attempt_id": "att-bad",
                "state": "succeeded",
                "recorded_at": COMPLETION_AT,
            },
            id="missing-schema",
        ),
        pytest.param(
            _completion_payload(
                "att-bad",
                recorded_at=COMPLETION_AT,
                schema="rizzstack.tool-completion/2",
            ),
            id="mismatched-schema",
        ),
        pytest.param(
            _completion_payload(
                "att-bad", recorded_at=COMPLETION_AT[:-1] + ".500Z"
            ),
            id="fractional-stamp",
        ),
    ],
)
def test_a_malformed_completion_is_ignored_and_the_row_stays_dispatched(
    tmp_path: Path, capsys, payload
):
    state = tmp_path / "state"
    _lease(state, "att-bad", now=CREATED_A)
    _write_completion(state, "att-bad", payload)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "dispatched"
    assert row["updated_at"] == CREATED_A

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 1
    assert status["in_flight_attempts"] == ["att-bad"]


def test_a_verify_record_outranks_a_failed_completion(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-verified", now=CREATED_A)
    _write_completion(
        state,
        "att-verified",
        _completion_payload("att-verified", recorded_at=_stamp(50), state_name="failed"),
    )
    _verify_result(state, "att-verified", ok=True, at=VERIFIED_AT)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "verified"
    assert row["updated_at"] == VERIFIED_AT

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == 0


def test_a_terminal_completion_keeps_an_ancient_lease_out_of_expired(
    tmp_path: Path, capsys
):
    state = tmp_path / "state"
    _lease(state, "att-old", now=ANCIENT)
    completion_at = _stamp(1)
    _write_completion(
        state,
        "att-old",
        _completion_payload("att-old", recorded_at=completion_at),
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] != "expired"
    assert row["state"] == "unverified"
    assert row["updated_at"] == completion_at

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == 0


def test_a_completion_older_than_the_ttl_outranks_expiry(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-old", now=ANCIENT)
    completion_at = _stamp(2880)
    _write_completion(
        state,
        "att-old",
        _completion_payload("att-old", recorded_at=completion_at),
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "unverified"
    assert row["updated_at"] == completion_at

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == 0


@pytest.mark.parametrize("completion_state", ["cancelled", "interrupted"])
def test_cancelled_and_interrupted_completions_are_failed(
    tmp_path: Path, capsys, completion_state: str
):
    state = tmp_path / "state"
    _lease(state, "att-terminal", now=CREATED_A)
    _write_completion(
        state,
        "att-terminal",
        _completion_payload(
            "att-terminal", recorded_at=COMPLETION_AT, state_name=completion_state
        ),
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "failed"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == 0


# ---------------------------------------------------------------------------
# Dispatch route comes only from a verified attribution intent
# ---------------------------------------------------------------------------


def test_a_verified_attribution_reports_its_route_and_model(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-routed", now=CREATED_A)
    _attribution_intent(state, "att-routed", route="zoo", model="zoo:glm-5.3-flash")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["route"] == "zoo"
    assert row["attributed_model"] == "zoo:glm-5.3-flash"


def test_a_missing_attribution_record_leaves_both_null(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-unattributed", now=CREATED_A, model="brief-model")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    # The brief's model is not a route and never becomes the attributed model.
    assert row["model"] == "brief-model"
    assert row["route"] is None
    assert row["attributed_model"] is None


def test_a_tampered_attribution_leaves_both_null(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-tampered", now=CREATED_A)
    record = _attribution_intent(state, "att-tampered", route="zoo", model="zoo:glm-5.3-flash")
    path = state / "attribution" / f"{record['intent_id']}.json"
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["route"] = "go"
    tampered["model"] = "go:other"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["route"] is None
    assert row["attributed_model"] is None


def test_an_adopted_attempt_leaves_both_null(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-adopted", now=CREATED_A)
    _attribution_intent(state, "att-other", route="zoo", model="zoo:glm-5.3-flash")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["route"] is None
    assert row["attributed_model"] is None


def test_an_ambiguous_attribution_leaves_both_null(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-ambiguous", now=CREATED_A)
    _attribution_intent(state, "att-ambiguous", route="zoo", model="zoo:glm-5.3-flash")
    _attribution_intent(state, "att-ambiguous", route="go", model="go:other")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]

    assert row["route"] is None
    assert row["attributed_model"] is None


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_the_views_leave_the_store_byte_for_byte_unchanged(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-a", now=CREATED_A)
    _verify_result(state, "att-a", ok=True, at=VERIFIED_AT)
    _accepted_wave(state, "w1", "att-a", at=ACCEPTED_AT)
    _attribution_intent(state, "att-a")
    _write_completion(
        state,
        "att-a",
        _completion_payload("att-a", recorded_at=COMPLETION_AT),
    )
    before = _snapshot(state)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    capsys.readouterr()

    assert _snapshot(state) == before
