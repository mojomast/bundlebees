"""Standing repo-grant behavior: human enrollment, auto-authorization, refusals."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from bundlebees_plugin import grants as G
from bundlebees_plugin import job as jobs
from bundlebees_plugin import job_cli, leases

REPO = "mojomast/rizzstack-smoke"
SHA = "93b02f138b09db25a27fb79995d02ced460e75c1"


def repo_grant(**overrides) -> G.Grant:
    body = dict(
        grant_id=G.repo_grant_id(REPO),
        approved_by="kyle",
        approved_at="2026-11-14T00:00:00Z",
        expires_at=G.parse_expiry("never"),
        repos=(REPO,),
        hosts=("*",),
        profiles=("*",),
        task_scopes=(),
        limits=G.Limits(max_bees=3, max_jobs=None, max_concurrent_jobs=12,
                        wall_clock_s=3600, budget_units=None),
        statement="A standing grant for the disposable rizzstack-smoke repository.",
        unrestricted_scope=True,
    )
    body.update(overrides)
    return G.Grant(**body)


def tasks(count: int = 1, *, wall_clock_s: int = 900):
    return [
        {
            "task_id": f"T{index + 1}",
            "objective": "Create smoke/answer.py returning 42.",
            "lease": ["smoke/answer.py"],
            "acceptance": {"criteria": ["answer() returns 42"], "tests": ["tests/test_smoke.py"]},
            "wall_clock_s": wall_clock_s,
            "provision": {"repo": REPO, "source_rev": SHA, "profile": "smoke",
                          "project": "rizzstack-smoke", "contract_sha256": "aa03cce9"},
        }
        for index in range(count)
    ]


class GrantFixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.key = leases._ensure_key(self.state)
        self.store = G.GrantStore(self.state, key=self.key, tty=lambda: True)

    def close(self):
        self.tmp.cleanup()


@pytest.fixture()
def grant_state():
    fixture = GrantFixture()
    try:
        yield fixture
    finally:
        fixture.close()


def test_repo_grant_requires_a_real_tty_and_statement(grant_state):
    store = G.GrantStore(grant_state.state, key=grant_state.key, tty=lambda: False)
    with pytest.raises(G.GrantError) as exc:
        store.enroll(repo_grant(), statement="A standing grant for smoke.")
    assert exc.value.code == "interactive_required"
    assert not (grant_state.state / "grants").exists()

    with pytest.raises(G.GrantError) as exc:
        grant_state.store.enroll(repo_grant(), statement="")
    assert exc.value.code == "statement_required"


def test_repo_grant_lists_and_revokes_immediately(grant_state):
    grant = repo_grant()
    grant_state.store.enroll(grant, statement=grant.statement)

    listed = grant_state.store.list()
    assert [item.grant_id for item in listed] == [grant.grant_id]
    assert listed[0].statement == grant.statement

    grant_state.store.revoke_repo(REPO, statement="stop new jobs")
    with pytest.raises(jobs.JobError) as exc:
        jobs.authorize_from_repo_grant(grant_state.state, job_id="job_revoked", tasks=tasks())
    assert exc.value.code == "grant_revoked"


def test_expired_and_tampered_grants_fail_closed(grant_state):
    grant_state.store.enroll(repo_grant(expires_at=time.time() + 5), statement="short")
    later = G.GrantStore(grant_state.state, key=grant_state.key, tty=lambda: True,
                         now=lambda: time.time() + 10)
    with pytest.raises(G.GrantError) as exc:
        later.find_active_for_repo(REPO)
    assert exc.value.code == "grant_expired"

    path = grant_state.state / "grants" / f"{G.repo_grant_id(REPO)}.json"
    body = json.loads(path.read_text())
    body["response"] = "tampered"
    path.write_text(json.dumps(body))
    with pytest.raises(G.GrantError) as exc:
        grant_state.store.list()
    assert exc.value.code == "grant_unreadable"


def test_auto_authorization_records_grant_evidence(grant_state):
    grant = repo_grant()
    grant_state.store.enroll(grant, statement=grant.statement)

    auth = jobs.authorize_from_repo_grant(grant_state.state, job_id="job_granted", tasks=tasks())
    assert auth["authority"] == "repo-grant"
    assert auth["grant_id"] == grant.grant_id
    assert auth["grant_statement"] == grant.statement
    assert auth["grant_approved_at"] == grant.approved_at
    assert auth["by"] == f"repo-grant:{grant.grant_id}"
    assert set(auth["gates"]) >= {"merge", "force_push", "release", "publish", "tag", "deploy"}

    job = jobs.create_job(grant_state.state, job_id="job_granted", tasks=tasks())
    assert job["authorization"]["authority"] == "repo-grant"
    assert job["authorization"]["grant_id"] == grant.grant_id
    assert job["authorization"]["grant_statement"] == grant.statement
    assert job["authorization"]["grant"]["approved_at"] == grant.approved_at
    assert jobs.load_authorization(grant_state.state, "job_granted")["job_id"] == "job_granted"


def test_without_a_grant_job_creation_is_refused(grant_state):
    with pytest.raises(jobs.JobError) as exc:
        jobs.authorize_from_repo_grant(grant_state.state, job_id="job_no_grant", tasks=tasks())
    assert exc.value.code == "grant_missing"


def test_grant_limits_reject_excess_bees_and_wall_clock(grant_state):
    grant_state.store.enroll(repo_grant(), statement="bounded grant")

    with pytest.raises(jobs.JobError) as exc:
        jobs.authorize_from_repo_grant(grant_state.state, job_id="job_too_many", tasks=tasks(4))
    assert exc.value.code == "bees_not_granted"

    with pytest.raises(jobs.JobError) as exc:
        jobs.authorize_from_repo_grant(grant_state.state, job_id="job_too_long", tasks=tasks(wall_clock_s=3601))
    assert exc.value.code == "wall_clock_not_granted"


def test_cli_job_create_auto_authorizes_from_grant(grant_state, capsys, tmp_path):
    grant = repo_grant()
    grant_state.store.enroll(grant, statement=grant.statement)
    task_file = tmp_path / "tasks.json"
    task_file.write_text(json.dumps(tasks()), encoding="utf-8")
    args = SimpleNamespace(job_handler="create", job_id="job_cli_grant", tasks=str(task_file))
    assert job_cli.cmd_job(None, args, state_dir=grant_state.state, client_factory=lambda: None) == 0
    job = jobs.load_job(grant_state.state, "job_cli_grant")
    assert job["authorization"]["authority"] == "repo-grant"
    assert job["authorization"]["grant_id"] == grant.grant_id
    assert capsys.readouterr().out.strip()


def test_cli_repo_grant_is_human_only(grant_state, monkeypatch, capsys):
    monkeypatch.setattr("bundlebees_plugin.grants.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("bundlebees_plugin.grants.sys.stdout.isatty", lambda: False)
    args = SimpleNamespace(repo_handler="grant", repo=REPO, max_bees=3, wall_clock_max=3600,
                           expires="never", statement="Standing approval for smoke work.",
                           state_dir=str(grant_state.state))
    assert job_cli.cmd_repo(None, args, state_dir=grant_state.state) == 1
    assert "interactive_required" in capsys.readouterr().out

    monkeypatch.setattr("bundlebees_plugin.grants.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("bundlebees_plugin.grants.sys.stdout.isatty", lambda: True)
    assert job_cli.cmd_repo(None, args, state_dir=grant_state.state) == 0
    assert grant_state.store.list()[0].repos == (REPO,)


def test_cli_repo_list_and_grant_use_the_shared_state(grant_state):
    args = SimpleNamespace(repo_handler="list", json=True, state_dir=str(grant_state.state))
    assert job_cli.cmd_repo(None, args, state_dir=grant_state.state) == 0

    # The CLI write path must pass the same TTY gate; grant through the store
    # here and verify the read-only command sees it.
    grant = repo_grant()
    grant_state.store.enroll(grant, statement=grant.statement)
    assert job_cli.cmd_repo(None, args, state_dir=grant_state.state) == 0


def test_agent_cannot_enroll_or_revoke(grant_state):
    agent_store = G.GrantStore(grant_state.state, key=grant_state.key, tty=lambda: False)
    with pytest.raises(G.GrantError) as exc:
        agent_store.enroll(repo_grant(), statement="agent must not enroll")
    assert exc.value.code == "interactive_required"

    grant_state.store.enroll(repo_grant(), statement="human enrolls")
    with pytest.raises(G.GrantError) as exc:
        agent_store.revoke_repo(REPO, statement="agent must not revoke")
    assert exc.value.code == "interactive_required"
