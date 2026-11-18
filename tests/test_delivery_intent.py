"""Verify must record the delivery outbox row before it pushes.

Incident (2026-11-18, att_c5e878f31c054c1d): every ``bundlebees verify`` printed
``delivery: intent not recorded: name 'base' is not defined`` because the intent
call referenced a name that did not exist in the verify scope. The push still
happened, so the durable outbox row the ``deliver`` sweep depends on was never
written and the PR obligation was silently dropped.

These tests drive the real verify command (local git repo + local bare remote)
and assert the outbox contract:

  * an intent is recorded with ``base_sha`` equal to the lease ``source_rev``
    (the PR base is always ``main``; the record carries the verified base);
  * after the push the record is ``pushed`` and the eager relay runs, or it
    stays ``pushed`` for the ``deliver`` sweep when the relay fails.

Run: python -m pytest tests/test_delivery_intent.py -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import delivery
from tests.test_cli import (
    ATTEMPT_ID,
    BRANCH,
    IN_LEASE_PATCH,
    FakeClient,
    _ctx,
    _invoke,
    _lease,
    _repo,
    _result,
    _verify_argv,
)


def _git(repo: Path, *args: str):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _intent_record(state: Path) -> dict:
    path = state / "deliveries" / f"{ATTEMPT_ID}.json"
    assert path.is_file(), "verify did not write the durable delivery intent"
    return json.loads(path.read_text(encoding="utf-8"))


def test_verify_records_the_intent_with_the_lease_source_rev(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    repo, remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    relayed: list[tuple] = []

    def fake_open_pr(repo_name, branch, title, body="", *args, **kwargs):
        relayed.append((repo_name, branch, title, body))
        return "https://github.com/example/repo/pull/1", True

    # The eager relay is the only piece replaced: git push/test/commit stay real.
    monkeypatch.setattr(delivery, "open_pr_idempotent", fake_open_pr)

    rc = _invoke(_ctx(client), _verify_argv(repo, state))
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "intent not recorded" not in out

    payload = json.loads((state / "verify-results" / f"{ATTEMPT_ID}.json").read_text())
    record = _intent_record(state)

    assert record["base_sha"] == base_sha, "the intent base must be the lease source_rev"
    assert record["state"] == delivery.STATE_PR_OPEN
    assert record["pushed_sha"] == payload["sha"]
    assert record["branch"] == BRANCH
    # The feature branch reached the remote; main never moved.
    assert _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip() == payload["sha"]
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == base_sha
    assert len(relayed) == 1
    assert relayed[0][0] == record["repo"]
    assert relayed[0][1] == BRANCH


def test_a_failed_eager_relay_leaves_a_pushed_record_for_the_sweep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    def fail_open_pr(*args, **kwargs):
        raise delivery.DeliveryError("gh_create_failed", "boom: no network")

    monkeypatch.setattr(delivery, "open_pr_idempotent", fail_open_pr)

    rc = _invoke(_ctx(client), _verify_argv(repo, state))
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "intent not recorded" not in out
    assert "pr: pending" in out

    record = _intent_record(state)
    assert record["state"] == delivery.STATE_PUSHED
    assert record["base_sha"] == base_sha
    assert record["pr_url"] is None
    pending = delivery.pending(state)
    assert [row["attempt_id"] for row in pending] == [ATTEMPT_ID]
    assert delivery.next_step(pending[0]) == "open_pr"
