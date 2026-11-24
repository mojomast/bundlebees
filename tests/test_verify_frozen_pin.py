"""RED for wave A follow-up A1b-FUP1: the frozen id pin must gate every GREEN.

WA-A1B taught ``tdd.check_green`` to demand exactly the frozen collected ids,
but the two callers that *record* the GREEN evidence - ``acdd_cli.record_green``
(the ``hermes bundlebees verify`` path) and ``acdd_cli.integrate_preflight`` -
never passed the pin, and the checked record ``verify_local._check_frozen_green``
builds for the commit-local lane was thrown away instead of signed. A partial
GREEN therefore still certified a slice through the verify path.

These tests drive one real frozen pin (a signed collection manifest bound by
``review.test_collection``) through the recording surface, the CLI verify
command, ``integrate_preflight`` and ``verify_and_commit``. The negative test
``test_unwired_check_green_accepts_the_partial_run`` documents why the wiring
matters: the bare ``check_green`` call without ``approved_ids`` still accepts a
partial run, so every recorder must pass the pin.

They also close the cheap review findings from the same PR: a skipped/xfailed
case must not count as an executed approved run, a failing GREEN must report
``green_failed`` before ``tests_mismatch``, the JUnit case parser must not stop
at 2,000 ids below the 20,000-id collection bound, and a non-numeric JUnit
counter on the commit-local lane must raise ``VerifyError`` instead of leaking
``ValueError``. Everything is offline: temporary git repos, a local bare
remote, a fake launcher result and an injected test runner.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import acdd_cli, cli, leases, security, tdd, verify_local
from bundlebees_plugin import contract as acdd
from bundlebees_plugin.brief import validate_brief
from tests.fakes import FakeCtx, load_plugin

CID = "A1B-fup1-fixture"
SRC = "src/a.py"
TEST_FILE = "tests/test_pin.py"
T1 = "tests/test_pin.py::test_first_value"
T2 = "tests/test_pin.py::test_second_value"
BRANCH = "feat/pin"
ATTEMPT = "att-pin"
IMPL_SHA = "b" * 40

TEST_SOURCE = '''from pathlib import Path


def _value():
    ns = {}
    exec((Path(__file__).resolve().parents[1] / "src" / "a.py").read_text(), ns)
    return ns["value"]


def test_first_value():
    assert _value() == 2


def test_second_value():
    assert _value() == 2
'''

GOOD_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


# --------------------------------------------------------------------------- fixtures


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env=security.child_env(),
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose base commit already holds the two approved (failing) tests."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")
    (repo / "src").mkdir()
    (repo / SRC).write_text("value = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / TEST_FILE).write_text(TEST_SOURCE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "approved tests (RED)")
    base = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True,
        env=security.child_env(),
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "branch", BRANCH)
    _git(repo, "push", "-u", "origin", BRANCH)
    return repo, base


def _contract(source_rev: str, **overrides) -> dict:
    data = {
        "contract_version": "acdd/1",
        "id": CID,
        "title": "value stays two under the pinned tests",
        "outcome": {"summary": "value is 2", "observable_behavior": ["src.a.value == 2"]},
        "interfaces": [{"name": "value", "kind": "other", "change": "modify"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "the module", "when": "the first test runs",
             "then": "it passes", "verification": "test", "tests": [T1]},
            {"id": "AC-002", "given": "the module", "when": "the second test runs",
             "then": "it passes", "verification": "test", "tests": [T2]},
        ],
        "scope": {"assigned_files": [SRC], "test_files": [TEST_FILE], "prohibited": []},
        "source_rev": source_rev,
        "tdd": {"applicable": True, "test_first": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "tag", "release", "deploy", "publish"]},
    }
    data.update(overrides)
    return data


def _manifest(contract: dict, sha: str, repo: Path, ids: list[str]) -> dict:
    return {
        "collection_version": tdd.COLLECTION_VERSION,
        "contract_id": str(contract["id"]),
        "contract_sha256": sha,
        "source_rev": str(contract["source_rev"]),
        "test_fingerprints": tdd.fingerprint_files(repo, [TEST_FILE]),
        "methods": {TEST_FILE: "pytest"},
        "collected_ids": list(ids),
        "resolved": {"AC-001": {T1: [T1]}, "AC-002": {T2: [T2]}},
        "environment": {"isolation": "fake", "runner": "pytest --collect-only (fixture)"},
        "at": "2026-11-24T20:00:00Z",
    }


def _pin_state(
    state: Path, repo: Path, base: str, *, ids: list[str] | None = None, **overrides
) -> tuple[dict, str, bytes]:
    """Freeze one contract bound to a signed collection manifest (the real pin shape)."""
    ids = list(ids or [T1, T2])
    contract = _contract(base, **overrides)
    sha = acdd.contract_sha256(contract)
    key = leases.evidence_key(state)
    manifest = _manifest(contract, sha, repo, ids)
    _path, digest = tdd.write_collection(state, manifest, key)
    frozen = acdd.freeze_contract(
        contract,
        approved_by="Kyle Durepos (fixture)",
        approved_at="2026-11-24T20:00:00Z",
        test_collection={"manifest_sha256": digest, "source_rev": base, "collected": len(ids)},
    )
    assert acdd.check_frozen(frozen) == sha
    return frozen, sha, key


def _cases(*ids: str, outcome: str = "passed") -> list[dict]:
    return [{"id": node, "outcome": outcome, "stage": "call"} for node in ids]


def _result(*, exit_code: int, cases: list[dict]) -> dict:
    failing = sum(case["outcome"] in ("failed", "error") for case in cases)
    skipped = sum(case["outcome"] == "skipped" for case in cases)
    return {
        "exit_code": exit_code,
        "tests": len(cases),
        "failures": failing,
        "errors": 0,
        "skipped": skipped,
        "timed_out": False,
        "cases": cases,
    }


def _record(state: Path, phase: str, revision: str, *, frozen: dict, sha: str,
            repo: Path, cases: list[dict], exit_code: int) -> dict:
    record = tdd.make_record(
        frozen, sha, phase=phase, revision=revision,
        test_fingerprints=tdd.fingerprint_files(repo, [TEST_FILE]),
        command=["python", "-m", "pytest", TEST_FILE],
        result=_result(exit_code=exit_code, cases=cases),
        produced_by={"role": "verifier", "name": "bundlebees verify"},
        test_first=True, now="2026-11-24T20:00:00Z",
    )
    tdd.write_record(state, record, leases.evidence_key(state))
    return record


def _seed_red(state: Path, repo: Path, base: str, *, frozen: dict, sha: str) -> dict:
    cases = _cases(T1, T2, outcome="failed")
    for case in cases:
        case.update(error_type="AssertionError", message="assert 1 == 2")
    return _record(state, "red", base, frozen=frozen, sha=sha, repo=repo,
                   cases=cases, exit_code=1)


def _lease(state: Path, base: str, frozen: dict) -> dict:
    brief = {
        "objective": "make value two",
        "source_rev": base,
        "branch": BRANCH,
        "assigned_files": [SRC],
        "acceptance": {"criteria": ["AC-001", "AC-002"], "tests": [TEST_FILE]},
        "contract": frozen,
    }
    return leases.record_lease(state, ATTEMPT, validate_brief(brief))


def _records(state: Path) -> list[dict]:
    return tdd.load_records(state, CID, leases.evidence_key(state))


def _acdd_state(frozen: dict, sha: str, red: dict, key: bytes) -> dict:
    return {"contract": frozen, "sha": sha, "red": red, "key": key, "tests": [TEST_FILE]}


class FakeClient:
    def __init__(self, result: dict) -> None:
        self.result_data = result

    def result(self, attempt_id: str) -> dict:
        return dict(self.result_data)

    def get_raw(self, attempt_id: str) -> dict:
        return {"result": dict(self.result_data)}


def _cli_ctx(client: FakeClient) -> FakeCtx:
    plugin = load_plugin()
    ctx = FakeCtx(settings={"verify_allow_unsafe_local": True})
    ctx.bundlebees_client = client
    plugin.register(ctx)
    return ctx


def _invoke(ctx: FakeCtx, argv: list[str]) -> int:
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    return record["handler_fn"](parser.parse_args(argv))


def _verify(ctx: FakeCtx, repo: Path, state: Path, *extra: str) -> int:
    return _invoke(ctx, ["verify", ATTEMPT, "--workdir", str(repo), "--branch", BRANCH,
                         "--state-dir", str(state), *extra])


def _worker_result(base: str) -> dict:
    return {
        "attempt_id": ATTEMPT,
        "branch": BRANCH,
        "patch": GOOD_PATCH,
        "base_rev": base,
        "brief": {"branch": BRANCH, "assigned_files": [SRC], "tests": [TEST_FILE]},
    }


def _fake_sandbox(monkeypatch: pytest.MonkeyPatch, cases: list[dict], *, exit_code: int = 0) -> None:
    def fake_run(workdir, argv, **kwargs):
        return {
            "backend": "fake",
            "exit_code": exit_code,
            "timed_out": False,
            "output_tail": "",
            "junit": {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0},
            "cases": cases,
        }

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", fake_run)


def _content_runner(partial: bool = False, bad_junit: bool = False):
    """Injected commit-local runner: behaviour follows the patched source."""

    def runner(workdir: Path, argv) -> dict:
        fixed = (Path(workdir) / SRC).read_text(encoding="utf-8") == "value = 2\n"
        ids = [T1] if (partial and fixed) else [T1, T2]
        outcome = "passed" if fixed else "failed"
        cases = _cases(*ids, outcome=outcome)
        if not fixed:
            for case in cases:
                case.update(error_type="AssertionError", message="assert 1 == 2")
        junit = {"tests": len(cases), "failures": 0 if fixed else len(cases),
                 "errors": 0, "skipped": 0}
        if bad_junit and fixed:
            junit = {"tests": "many", "failures": 0, "errors": 0, "skipped": 0}
        return {
            "backend": "fake",
            "exit_code": 0 if fixed else 1,
            "timed_out": False,
            "output_tail": "",
            "junit": junit,
            "cases": cases,
        }

    return runner


def _commit_local(state: Path, repo: Path, base: str, frozen: dict, sha: str,
                  runner, *, declared: list[str] | None = None) -> dict:
    lease_record = _lease(state, base, frozen)
    preflight = lambda files: acdd_cli.verify_preflight(lease_record, state, list(files))
    return verify_local.verify_and_commit(
        state, repo, ATTEMPT, patch=GOOD_PATCH, declared_files=declared or [SRC],
        base_rev=base, tests=[TEST_FILE], gitleaks=None, require_gitleaks=False,
        runner=runner, acdd_preflight=preflight, contract_declared=True,
    )


# --------------------------------------------------------------- the shared pin loader


def test_frozen_approved_ids_loads_the_pinned_selection(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)

    assert tdd.frozen_approved_ids(state, {"contract": frozen, "sha": sha, "key": key}) == [T1, T2]

    unpinned = acdd.freeze_contract(
        _contract(base), approved_by="Kyle Durepos (fixture)",
        approved_at="2026-11-24T20:00:00Z",
    )
    assert tdd.frozen_approved_ids(
        state, {"contract": unpinned, "sha": acdd.check_frozen(unpinned), "key": key}
    ) is None


def test_frozen_approved_ids_refuses_a_tampered_pin(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)

    # Re-sign a manifest that no longer matches the digest the contract pinned.
    tampered = _manifest(_contract(base), sha, repo, [T1])
    tdd.write_collection(state, tampered, key)
    with pytest.raises(tdd.TddError) as caught:
        tdd.frozen_approved_ids(state, {"contract": frozen, "sha": sha, "key": key})
    assert caught.value.code == "evidence_tampered"

    # An empty selected set is a refusal too, never a pass.
    empty = _manifest(_contract(base), sha, repo, [])
    _path, empty_digest = tdd.write_collection(state, empty, key)
    empty_frozen = acdd.freeze_contract(
        _contract(base), approved_by="Kyle Durepos (fixture)",
        approved_at="2026-11-24T20:00:00Z",
        test_collection={"manifest_sha256": empty_digest, "source_rev": base, "collected": 0},
    )
    with pytest.raises(tdd.TddError) as caught:
        tdd.frozen_approved_ids(
            state, {"contract": empty_frozen, "sha": sha, "key": key}
        )
    assert caught.value.code == "tests_mismatch"


# --------------------------------------------------------------- acdd_cli.record_green


def test_record_green_refuses_a_partial_run_and_persists_nothing(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    before = [record["phase"] for record in _records(state)]

    evidence = {"backend": "fake", **_result(exit_code=0, cases=_cases(T1))}
    with pytest.raises(cli.CliError) as caught:
        acdd_cli.record_green(state, _acdd_state(frozen, sha, red, key), repo, evidence, IMPL_SHA)
    assert caught.value.code == "tests_mismatch"
    assert T2 in caught.value.error
    assert [record["phase"] for record in _records(state)] == before


def test_record_green_persists_the_exact_frozen_run(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)

    evidence = {"backend": "fake", **_result(exit_code=0, cases=_cases(T1, T2))}
    written = acdd_cli.record_green(
        state, _acdd_state(frozen, sha, red, key), repo, evidence, IMPL_SHA
    )
    assert written["phase"] == "green"
    (green,) = [record for record in _records(state) if record["phase"] == "green"]
    tdd.check_green(frozen, sha, red, green, approved_ids=[T1, T2])


# --------------------------------------------------------------- integrate_preflight


def test_integrate_preflight_refuses_a_stored_partial_green(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _lease(state, base, frozen)
    # A GREEN signed before the pin was enforced (or by a caller that forgot it).
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(T1), exit_code=0)

    with pytest.raises(cli.CliError) as caught:
        acdd_cli.integrate_preflight(state, [ATTEMPT])
    assert caught.value.code == "tests_mismatch"
    assert T2 in caught.value.error


def test_integrate_preflight_accepts_a_stored_exact_green(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _lease(state, base, frozen)
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(T1, T2), exit_code=0)

    (item,) = acdd_cli.integrate_preflight(state, [ATTEMPT])
    assert item["contract"]["id"] == CID
    assert item["sha"] == sha


# --------------------------------------------------------------- the CLI verify path


def test_cli_verify_refuses_a_partial_green_for_a_pinned_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _lease(state, base, frozen)
    _fake_sandbox(monkeypatch, _cases(T1))

    rc = _verify(_cli_ctx(FakeClient(_worker_result(base))), repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    assert "tests_mismatch" in out
    assert [record["phase"] for record in _records(state)] == ["red"]
    assert _git(repo, "rev-parse", f"origin/{BRANCH}") == base


def test_cli_verify_records_the_exact_frozen_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _lease(state, base, frozen)
    _fake_sandbox(monkeypatch, _cases(T1, T2))

    rc = _verify(_cli_ctx(FakeClient(_worker_result(base))), repo, state, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "green: valid" in out
    red = next(record for record in _records(state) if record["phase"] == "red")
    (green,) = [record for record in _records(state) if record["phase"] == "green"]
    tdd.check_green(frozen, sha, red, green, approved_ids=[T1, T2])


# --------------------------------------------------------------- verify_and_commit


def test_verify_and_commit_persists_the_checked_green_record(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)

    record = _commit_local(state, repo, base, frozen, sha, _content_runner())
    assert record["ok"] is True
    (green,) = [item for item in _records(state) if item["phase"] == "green"]
    assert green["revision"] == record["sha"]
    assert {case["id"] for case in green["result"]["cases"]} == {T1, T2}
    tdd.check_green(frozen, sha, red, green, approved_ids=[T1, T2])


def test_verify_and_commit_refuses_a_partial_green(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)

    with pytest.raises(verify_local.VerifyError) as caught:
        _commit_local(state, repo, base, frozen, sha, _content_runner(partial=True))
    assert caught.value.code == "tests_mismatch"
    assert [item["phase"] for item in _records(state)] == ["red"]


def test_verify_and_commit_reports_a_non_numeric_junit_counter(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)

    with pytest.raises(verify_local.VerifyError) as caught:
        _commit_local(state, repo, base, frozen, sha, _content_runner(bad_junit=True))
    assert caught.value.code == "evidence_invalid"


# --------------------------------------------------------------- cheap review findings


def test_check_collected_ids_refuses_a_skipped_case():
    for outcome in ("skipped", "xfailed"):
        cases = _cases(T1, outcome=outcome) + _cases(T2)
        with pytest.raises(tdd.TddError) as caught:
            tdd.check_collected_ids([T1, T2], cases)
        assert caught.value.code == "tests_mismatch"
        assert T1 in caught.value.error


def test_check_green_reports_green_failed_before_tests_mismatch(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    green = tdd.make_record(
        frozen, sha, phase="green", revision=IMPL_SHA,
        test_fingerprints=tdd.fingerprint_files(repo, [TEST_FILE]),
        command=["python", "-m", "pytest", TEST_FILE],
        result=_result(exit_code=1, cases=_cases(T1, outcome="failed")),
        produced_by={"role": "verifier", "name": "bundlebees verify"},
        test_first=True, now="2026-11-24T20:00:00Z",
    )
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_green(frozen, sha, red, green, approved_ids=[T1, T2])
    assert caught.value.code == "green_failed"


def test_parse_junit_cases_does_not_stop_at_two_thousand(tmp_path: Path):
    path = tmp_path / "junit.xml"
    body = "".join(
        f'<testcase classname="tests.test_big" name="test_{index}"/>'
        for index in range(2100)
    )
    path.write_text(
        f'<testsuites><testsuite tests="2100">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    cases = tdd.parse_junit_cases(path)
    assert len(cases) == 2100


def test_unwired_check_green_accepts_the_partial_run(tmp_path: Path):
    """The pre-fix behaviour the wiring exists to close: no pin, no id check."""
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    green = tdd.make_record(
        frozen, sha, phase="green", revision=IMPL_SHA,
        test_fingerprints=tdd.fingerprint_files(repo, [TEST_FILE]),
        command=["python", "-m", "pytest", TEST_FILE],
        result=_result(exit_code=0, cases=_cases(T1)),
        produced_by={"role": "verifier", "name": "bundlebees verify"},
        test_first=True, now="2026-11-24T20:00:00Z",
    )
    assert tdd.check_green(frozen, sha, red, green) is green
