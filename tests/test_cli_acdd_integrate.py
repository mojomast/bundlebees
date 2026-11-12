"""Approved acceptance tests for contract-aware ``integrate`` (written first, RED).

When a verified attempt's lease carries a contract, integrate must require a
valid signed GREEN for it, then record a signed ``integration`` record for the
combined-branch suite, pass or fail. The acceptance report only says accepted
when red, green and integration are all valid.
"""

from __future__ import annotations

import json

from tests.test_cli_acdd import (
    ATTEMPT_ID, BRANCH, CID, GOOD_PATCH, FakeClient, _ctx, _frozen, _git, _invoke,
    _lease, _records, _repo, _result, _write,
)


def _contract_with_integration(base: str) -> dict:
    return _frozen(base, evidence_required=["red", "green", "integration"],
                   verification={"independent": True, "integration": True})


def _verified(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _contract_with_integration(base)
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    _git(repo, "checkout", BRANCH)
    rc = _invoke(_ctx(FakeClient(_result(GOOD_PATCH, base))),
                 ["verify", ATTEMPT_ID, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc == 0, out
    return repo, state, path


def _integrate(repo, state) -> int:
    return _invoke(_ctx(), ["integrate", "--wave", "w1", "--attempts", ATTEMPT_ID,
                            "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state)])


def test_integrate_records_signed_integration_evidence(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "accepted: yes" in out
    assert "integration: valid" in out
    phases = [r["phase"] for r in _records(state)]
    assert phases == ["red", "green", "integration"]
    integration = _records(state)[-1]
    assert integration["classification"] == "passed"
    assert integration["revision"] == _git(repo, "rev-parse", "HEAD")
    assert integration["produced_by"]["role"] == "verifier"

    rc = _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["phases"] == {"red": "valid", "green": "valid", "integration": "valid"}
    assert report["accepted"] is True


def test_integrate_failure_is_recorded_and_not_accepted(tmp_path, capsys):
    repo, state, path = _verified(tmp_path, capsys)
    (repo / "tests" / "test_combined.py").write_text("def test_combined():\n    assert False\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "combined regression")
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    assert "accepted: no" in out
    integration = _records(state)[-1]
    assert integration["phase"] == "integration"
    assert integration["classification"] != "passed"

    _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["phases"]["integration"] == "integration_failed"
    assert report["accepted"] is False


def test_integrate_without_green_for_a_contract_attempt_is_refused(tmp_path, capsys):
    repo, state, _path = _verified(tmp_path, capsys)
    # Simulate a lost GREEN record by moving it aside (load_records reads only *.json).
    evidence_dir = state / "evidence" / CID
    for record in sorted(evidence_dir.iterdir()):
        if record.name.endswith("-green.json"):
            record.rename(record.with_name(record.name + ".moved"))
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    assert "green_failed" in out or "green_missing" in out
