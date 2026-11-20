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
from pathlib import Path

from bundlebees_plugin import attribution, cli, grants, leases
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


def _completion(
    state: Path,
    attempt_id: str,
    *,
    state_name: str = "succeeded",
    at: str | None = None,
    recorded_attempt_id: str | None = None,
) -> None:
    path = state / "tool-completions" / f"{attempt_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "rizzstack.tool-completion/1",
                "attempt_id": recorded_attempt_id or attempt_id,
                "state": state_name,
                "branch": f"feat/{attempt_id}",
                "feature_id": "feature-a",
                "event_id": "event-a",
                "recorded_at": at or _stamp(20),
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
# A terminal tool-completion is local evidence, not in-flight work
# ---------------------------------------------------------------------------


def test_a_failed_completion_is_failed_and_not_in_flight(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-done", now=_now_stamp(), project="proj-a")
    _completion(state, "att-done", state_name="failed", at=_now_stamp())
    _lease(state, "att-live", now=_now_stamp(), project="proj-b")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    rows = {row["attempt_id"]: row for row in json.loads(capsys.readouterr().out)["attempts"]}
    assert rows["att-done"]["state"] == "failed"
    assert rows["att-live"]["state"] == "dispatched"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 1
    assert status["in_flight_attempts"] == ["att-live"]


def test_a_succeeded_completion_is_unverified_and_not_in_flight(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-succeeded", now=_now_stamp(), project="proj-a")
    _completion(state, "att-succeeded", state_name="succeeded", at=_now_stamp())

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "unverified"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["in_flight"] == 0
    assert status["in_flight_attempts"] == []


def test_a_completion_for_a_different_attempt_is_ignored(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-mismatch", now=_now_stamp(), project="proj-a")
    _completion(
        state,
        "att-mismatch",
        state_name="failed",
        at=_now_stamp(),
        recorded_attempt_id="att-other",
    )

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "dispatched"

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == 1


def test_a_malformed_completion_is_ignored_and_the_view_succeeds(tmp_path: Path, capsys):
    state = tmp_path / "state"
    names = ("att-bad-json", "att-unknown-state", "att-not-mapping")
    for name in names:
        _lease(state, name, now=_now_stamp(), project="proj-a")
    root = state / "tool-completions"
    root.mkdir(parents=True, exist_ok=True)
    (root / "att-bad-json.json").write_text("{not json", encoding="utf-8")
    _completion(state, "att-unknown-state", state_name="running", at=_now_stamp())
    (root / "att-not-mapping.json").write_text(json.dumps([1, 2]), encoding="utf-8")

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    rows = {row["attempt_id"]: row for row in json.loads(capsys.readouterr().out)["attempts"]}
    assert set(rows) == set(names)
    assert {row["state"] for row in rows.values()} == {"dispatched"}

    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    assert json.loads(capsys.readouterr().out)["in_flight"] == len(names)


def test_a_verify_result_outranks_a_failed_completion(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-relater", now=_now_stamp(), project="proj-a")
    _completion(state, "att-relater", state_name="failed", at=_stamp(30))
    _verify_result(state, "att-relater", ok=True, at=_stamp(10))

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "verified"


def test_an_ancient_lease_with_a_fresh_completion_is_not_expired(tmp_path: Path, capsys):
    state = tmp_path / "state"
    fresh = _now_stamp()
    _lease(state, "att-ancient", now=ANCIENT, project="proj-a")
    _completion(state, "att-ancient", state_name="succeeded", at=fresh)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    row = json.loads(capsys.readouterr().out)["attempts"][0]
    assert row["state"] == "unverified"
    assert row["updated_at"] == fresh


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
    _completion(state, "att-a", state_name="failed", at=VERIFIED_AT)
    before = _snapshot(state)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    capsys.readouterr()

    assert _snapshot(state) == before
