"""Read-only ``hermes bundlebees attempts`` and ``status`` CLI views.

Both commands answer from the plugin's local attempt store -- leases, verify
results and accepted wave records -- with no network and no configured
credential. The lookalike value below is assembled from fragments so no source
line resembles a real credential, while still exercising the scrub path end to
end in the output assertions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from bundlebees_plugin import cli, grants, leases
from bundlebees_plugin.brief import validate_brief
from tests.fakes import FakeCtx, load_plugin, secret_scope

CREATED_A = "2026-11-16T09:00:00Z"
CREATED_B = "2026-11-16T10:00:00Z"
VERIFIED_AT = "2026-11-16T11:00:00Z"
ACCEPTED_AT = "2026-11-16T12:00:00Z"

ROW_FIELDS = {
    "attempt_id",
    "state",
    "branch",
    "model",
    "project",
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
    return leases.record_lease(state, attempt_id, validate_brief(brief), now=now)


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
    assert payload["in_flight"] == 2
    assert sorted(payload["in_flight_attempts"]) == ["att-fresh", "att-verified"]
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
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_the_views_leave_the_store_byte_for_byte_unchanged(tmp_path: Path, capsys):
    state = tmp_path / "state"
    _lease(state, "att-a", now=CREATED_A)
    _verify_result(state, "att-a", ok=True, at=VERIFIED_AT)
    _accepted_wave(state, "w1", "att-a", at=ACCEPTED_AT)
    before = _snapshot(state)

    assert _invoke(_ctx(), ["attempts", "--state-dir", str(state)]) == 0
    assert _invoke(_ctx(), ["status", "--state-dir", str(state)]) == 0
    capsys.readouterr()

    assert _snapshot(state) == before
