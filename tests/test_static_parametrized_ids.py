"""RED for wave A follow-up A1b-FUP2: static ids cover parametrized GREEN ids.

``acdd_cli._collect_manifest`` falls back to syntax-tree collection when an
approved test file cannot be imported at ``source_rev``: those ids are recorded
as ``path::test_name`` with ``methods[path] == "static"``. A pytest GREEN of the
same file reports ``path::test_name[param]`` for every parameter, so the exact
id match in ``tdd.check_collected_ids`` refused a legitimate run: the bare id
looked missing and every parameter looked extra.

These tests freeze a real pinned contract whose approved file is parametrized
and only statically collectable at ``source_rev``, then drive the expanded
GREEN through ``check_collected_ids``, ``check_green``, ``record_green``,
``integrate_preflight``, the CLI ``verify`` path and ``verify_and_commit``.

The exact-id rule stays fail-closed everywhere else: a parametrized id that was
not statically collected is still an extra, duplicates still refuse, a skipped
or collection-stage case still refuses, an approved id with no executed case
still refuses, and an unknown id offered as static is refused instead of
widening the rule. The one documented residual is that a partial
parametrization is accepted - freeze time cannot know the parameter count - and
``test_a_partial_parametrization_is_the_documented_residual`` pins that
boundary on purpose.

Everything is offline: temporary git repos, a fake collector result, a fake
launcher result and an injected test runner.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import acdd_cli, cli, leases, sandbox, security, tdd, verify_local
from bundlebees_plugin import contract as acdd
from bundlebees_plugin.brief import validate_brief
from tests.fakes import FakeCtx, load_plugin

CID = "A1b-fup2-fixture"
SRC = "src/width.py"
TEST_FILE = "tests/test_static_param.py"
SCALES = TEST_FILE + "::test_width_scales"
PLAIN = TEST_FILE + "::test_width_default"
CHILD_A = SCALES + "[2]"
CHILD_B = SCALES + "[3]"
STRAY = TEST_FILE + "::test_width_extra[2]"
BRANCH = "feat/param-ids"
ATTEMPT = "att-param"
IMPL_SHA = "b" * 40

TEST_SOURCE = '''import pytest

from src.width import width


@pytest.mark.parametrize("multiplier", [2, 3])
def test_width_scales(multiplier):
    assert width(multiplier) == 4 * multiplier


def test_width_default():
    assert width(1) == 4
'''

GOOD_PATCH = """diff --git a/src/width.py b/src/width.py
new file mode 100644
--- /dev/null
+++ b/src/width.py
@@ -0,0 +1,2 @@
+def width(multiplier):
+    return 4 * multiplier
"""


# --------------------------------------------------------------------------- fixtures


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env=security.child_env(),
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose base commit holds the approved test file, unimportable at that rev."""
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")
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
        "title": "width scales under the statically collected tests",
        "outcome": {"summary": "width multiplies", "observable_behavior": ["width(m) == 4 * m"]},
        "interfaces": [{"name": "width", "kind": "function", "change": "add"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "the module", "when": "the scaled test runs",
             "then": "it passes", "verification": "test", "tests": [SCALES]},
            {"id": "AC-002", "given": "the module", "when": "the default test runs",
             "then": "it passes", "verification": "test", "tests": [PLAIN]},
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


def _manifest(contract: dict, sha: str, repo: Path, ids: list[str], method: str = "static") -> dict:
    return {
        "collection_version": tdd.COLLECTION_VERSION,
        "contract_id": str(contract["id"]),
        "contract_sha256": sha,
        "source_rev": str(contract["source_rev"]),
        "test_fingerprints": tdd.fingerprint_files(repo, [TEST_FILE]),
        "methods": {TEST_FILE: method},
        "collected_ids": list(ids),
        "resolved": {"AC-001": {SCALES: [SCALES]}, "AC-002": {PLAIN: [PLAIN]}},
        "environment": {"isolation": "fake", "runner": "pytest --collect-only (fixture)"},
        "at": "2026-11-25T00:00:00Z",
    }


def _pin_state(
    state: Path, repo: Path, base: str, *, ids: list[str] | None = None, method: str = "static"
) -> tuple[dict, str, bytes]:
    """Freeze one contract bound to a signed collection manifest (the real pin shape)."""
    selected = list(ids or [PLAIN, SCALES])
    contract = _contract(base)
    sha = acdd.contract_sha256(contract)
    key = leases.evidence_key(state)
    manifest = _manifest(contract, sha, repo, selected, method)
    _path, digest = tdd.write_collection(state, manifest, key)
    frozen = acdd.freeze_contract(
        contract,
        approved_by="Kyle Durepos (fixture)",
        approved_at="2026-11-25T00:00:00Z",
        test_collection={"manifest_sha256": digest, "source_rev": base, "collected": len(selected)},
    )
    assert acdd.check_frozen(frozen) == sha
    return frozen, sha, key


def _cases(*ids: str, outcome: str = "passed", stage: str = "call") -> list[dict]:
    return [{"id": node, "outcome": outcome, "stage": stage} for node in ids]


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
        test_first=True, now="2026-11-25T00:00:00Z",
    )
    tdd.write_record(state, record, leases.evidence_key(state))
    return record


def _seed_red(state: Path, repo: Path, base: str, *, frozen: dict, sha: str) -> dict:
    cases = _cases(PLAIN, SCALES, outcome="failed")
    for case in cases:
        case.update(error_type="AssertionError", message="assert 1 == 8")
    return _record(state, "red", base, frozen=frozen, sha=sha, repo=repo,
                   cases=cases, exit_code=1)


def _lease(state: Path, base: str, frozen: dict) -> dict:
    brief = {
        "objective": "add width",
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


def _param_runner(drop_default: bool = False):
    """Injected commit-local runner: behaviour follows the patched source."""

    def runner(workdir: Path, argv) -> dict:
        fixed = (Path(workdir) / SRC).is_file()
        ids = [CHILD_A, CHILD_B, PLAIN]
        if drop_default:
            ids = [CHILD_A, CHILD_B]
        if not fixed:
            cases = _cases(PLAIN, SCALES, outcome="failed")
            for case in cases:
                case.update(error_type="AssertionError", message="assert 1 == 8")
            return {"backend": "fake", "exit_code": 1, "timed_out": False,
                    "output_tail": "", "junit": {"tests": 2, "failures": 2, "errors": 0, "skipped": 0},
                    "cases": cases}
        return {"backend": "fake", "exit_code": 0, "timed_out": False, "output_tail": "",
                "junit": {"tests": len(ids), "failures": 0, "errors": 0, "skipped": 0},
                "cases": _cases(*ids)}

    return runner


def _commit_local(state: Path, repo: Path, base: str, frozen: dict,
                  runner) -> dict:
    lease_record = _lease(state, base, frozen)
    preflight = lambda files: acdd_cli.verify_preflight(lease_record, state, list(files))
    return verify_local.verify_and_commit(
        state, repo, ATTEMPT, patch=GOOD_PATCH, declared_files=[SRC],
        base_rev=base, tests=[TEST_FILE], gitleaks=None, require_gitleaks=False,
        runner=runner, acdd_preflight=preflight, contract_declared=True,
    )


# ------------------------------------------------------- freeze-time static collection


def test_collect_manifest_marks_a_parametrized_static_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The premise: the unimportable parametrized file is recorded statically, unexpanded."""
    repo, base = _repo(tmp_path)
    contract = _contract(base)
    sha = acdd.contract_sha256(contract)

    def fake_collect(workdir, argv, **kwargs):
        return {
            "backend": "fake",
            "exit_code": 2,
            "timed_out": False,
            "output_tail": "E   ModuleNotFoundError: No module named 'src.width'",
            "collected": {
                "exit_code": 2,
                "ids": [],
                "errors": [{"nodeid": TEST_FILE, "error_type": "ModuleNotFoundError"}],
            },
        }

    monkeypatch.setattr(sandbox, "run_in_sandbox", fake_collect)
    ctx = _cli_ctx(FakeClient(_worker_result(base)))
    manifest = acdd_cli._collect_manifest(ctx, contract, sha, repo)
    assert manifest["methods"] == {TEST_FILE: "static"}
    assert manifest["collected_ids"] == [PLAIN, SCALES]
    assert manifest["resolved"] == {"AC-001": {SCALES: [SCALES]}, "AC-002": {PLAIN: [PLAIN]}}


# --------------------------------------------------------------- the shared selection loader


def test_frozen_selection_reports_the_static_ids(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    preflight = {"contract": frozen, "sha": sha, "key": key}

    assert tdd.frozen_approved_selection(state, preflight) == ([PLAIN, SCALES], [PLAIN, SCALES])
    assert tdd.frozen_approved_ids(state, preflight) == [PLAIN, SCALES]

    unpinned = acdd.freeze_contract(
        _contract(base), approved_by="Kyle Durepos (fixture)", approved_at="2026-11-25T00:00:00Z"
    )
    unpinned_state = {"contract": unpinned, "sha": acdd.check_frozen(unpinned), "key": key}
    assert tdd.frozen_approved_selection(state, unpinned_state) is None
    assert tdd.frozen_approved_ids(state, unpinned_state) is None


def test_frozen_selection_of_a_pytest_collected_pin_has_no_static_ids(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base, method="pytest")

    assert tdd.frozen_approved_selection(state, {"contract": frozen, "sha": sha, "key": key}) == (
        [PLAIN, SCALES], []
    )


# --------------------------------------------------------- check_collected_ids (the rule)


def test_check_collected_ids_accepts_parametrized_children_of_a_static_id():
    assert tdd.check_collected_ids([SCALES], _cases(CHILD_A, CHILD_B), static_ids=[SCALES]) is None
    assert tdd.check_collected_ids(
        [PLAIN, SCALES], _cases(CHILD_A, CHILD_B, PLAIN), static_ids=[PLAIN, SCALES]
    ) is None


def test_check_collected_ids_requires_a_case_for_every_approved_id():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([PLAIN, SCALES], _cases(CHILD_A), static_ids=[PLAIN, SCALES])
    assert caught.value.code == "tests_mismatch"
    assert f"missing ids: {PLAIN}" in caught.value.error

    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([SCALES], [], static_ids=[SCALES])
    assert caught.value.code == "tests_mismatch"
    assert SCALES in caught.value.error


def test_check_collected_ids_refuses_children_when_the_id_is_not_static():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([SCALES], _cases(CHILD_A))
    assert caught.value.code == "tests_mismatch"
    assert f"missing ids: {SCALES}" in caught.value.error
    assert f"extra ids: {CHILD_A}" in caught.value.error


def test_check_collected_ids_refuses_an_extra_parametrized_case():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([SCALES], _cases(CHILD_A, CHILD_B, STRAY), static_ids=[SCALES])
    assert caught.value.code == "tests_mismatch"
    assert f"extra ids: {STRAY}" in caught.value.error


def test_check_collected_ids_refuses_a_duplicate_parametrized_case():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([SCALES], _cases(CHILD_A, CHILD_A), static_ids=[SCALES])
    assert caught.value.code == "tests_mismatch"
    assert f"duplicate run ids: {CHILD_A}" in caught.value.error


def test_check_collected_ids_refuses_unexecuted_parametrized_cases():
    shapes = [
        _cases(CHILD_B, outcome="skipped"),
        _cases(CHILD_B, outcome="xfailed"),
        _cases(CHILD_B, stage="collection"),
    ]
    for extra in shapes:
        with pytest.raises(tdd.TddError) as caught:
            tdd.check_collected_ids([SCALES], _cases(CHILD_A) + extra, static_ids=[SCALES])
        assert caught.value.code == "tests_mismatch"
        assert CHILD_B in caught.value.error


def test_check_collected_ids_refuses_an_unknown_static_id():
    with pytest.raises(tdd.TddError) as caught:
        tdd.check_collected_ids([PLAIN], _cases(PLAIN), static_ids=[SCALES])
    assert caught.value.code == "tests_mismatch"
    assert SCALES in caught.value.error


def test_a_partial_parametrization_is_the_documented_residual():
    """Freeze time cannot know the parameter count; one executed child is a pass.

    Every other exact-id property still holds, so the residual is only the
    *number* of parameters - never an unrun approved id, an extra file or an
    unexecuted case.
    """
    assert tdd.check_collected_ids([SCALES], _cases(CHILD_A), static_ids=[SCALES]) is None


# --------------------------------------------------------------------- check_green


def test_check_green_expands_only_with_the_static_ids(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    green = tdd.make_record(
        frozen, sha, phase="green", revision=IMPL_SHA,
        test_fingerprints=tdd.fingerprint_files(repo, [TEST_FILE]),
        command=["python", "-m", "pytest", TEST_FILE],
        result=_result(exit_code=0, cases=_cases(CHILD_A, CHILD_B, PLAIN)),
        produced_by={"role": "verifier", "name": "bundlebees verify"},
        test_first=True, now="2026-11-25T00:00:00Z",
    )
    approved = [PLAIN, SCALES]
    tdd.check_green(frozen, sha, red, green, approved_ids=approved, static_ids=approved)

    with pytest.raises(tdd.TddError) as caught:
        tdd.check_green(frozen, sha, red, green, approved_ids=approved)
    assert caught.value.code == "tests_mismatch"
    assert f"missing ids: {SCALES}" in caught.value.error


# --------------------------------------------------------------- acdd_cli.record_green


def test_record_green_accepts_the_expanded_green(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)

    evidence = {"backend": "fake", **_result(exit_code=0, cases=_cases(CHILD_A, CHILD_B, PLAIN))}
    written = acdd_cli.record_green(state, _acdd_state(frozen, sha, red, key), repo, evidence, IMPL_SHA)
    assert written["phase"] == "green"
    (green,) = [record for record in _records(state) if record["phase"] == "green"]
    tdd.check_green(frozen, sha, red, green, approved_ids=[PLAIN, SCALES], static_ids=[PLAIN, SCALES])


def test_record_green_still_refuses_an_unrun_approved_id(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    before = [record["phase"] for record in _records(state)]

    evidence = {"backend": "fake", **_result(exit_code=0, cases=_cases(CHILD_A, CHILD_B))}
    with pytest.raises(cli.CliError) as caught:
        acdd_cli.record_green(state, _acdd_state(frozen, sha, red, key), repo, evidence, IMPL_SHA)
    assert caught.value.code == "tests_mismatch"
    assert PLAIN in caught.value.error
    assert [record["phase"] for record in _records(state)] == before


# --------------------------------------------------------------- integrate_preflight


def test_integrate_preflight_accepts_the_expanded_green(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _lease(state, base, frozen)
    _record(state, "green", IMPL_SHA, frozen=frozen, sha=sha, repo=repo,
            cases=_cases(CHILD_A, CHILD_B, PLAIN), exit_code=0)

    (item,) = acdd_cli.integrate_preflight(state, [ATTEMPT])
    assert item["contract"]["id"] == CID
    assert item["sha"] == sha


# --------------------------------------------------------------- the CLI verify path


def test_cli_verify_records_the_expanded_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)
    _lease(state, base, frozen)
    _fake_sandbox(monkeypatch, _cases(CHILD_A, CHILD_B, PLAIN))

    rc = _verify(_cli_ctx(FakeClient(_worker_result(base))), repo, state, "--dry-run")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "green: valid" in out
    (green,) = [record for record in _records(state) if record["phase"] == "green"]
    tdd.check_green(frozen, sha, red, green, approved_ids=[PLAIN, SCALES], static_ids=[PLAIN, SCALES])


# --------------------------------------------------------------- verify_and_commit


def test_verify_and_commit_persists_the_expanded_green(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    red = _seed_red(state, repo, base, frozen=frozen, sha=sha)

    record = _commit_local(state, repo, base, frozen, _param_runner())
    assert record["ok"] is True
    (green,) = [item for item in _records(state) if item["phase"] == "green"]
    assert {case["id"] for case in green["result"]["cases"]} == {CHILD_A, CHILD_B, PLAIN}
    tdd.check_green(frozen, sha, red, green, approved_ids=[PLAIN, SCALES], static_ids=[PLAIN, SCALES])


def test_verify_and_commit_still_refuses_an_unrun_approved_id(tmp_path: Path):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen, sha, key = _pin_state(state, repo, base)
    _seed_red(state, repo, base, frozen=frozen, sha=sha)

    with pytest.raises(verify_local.VerifyError) as caught:
        _commit_local(state, repo, base, frozen, _param_runner(drop_default=True))
    assert caught.value.code == "tests_mismatch"
    assert [item["phase"] for item in _records(state)] == ["red"]
