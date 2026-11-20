"""Literal output for structurally validated verify values.

``_print`` scrubs every line, and its long-key rule redacts any 32+ character
identifier run carrying a digit. A valid branch and the verify-result path are
structural evidence, not credentials, so those exact values have a narrow
literal path. Everything untrusted -- worker text, captured output, errors,
and any path outside the verify state directory -- keeps full redaction.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bundlebees_plugin import cli, verify_local
from tests.test_cli import (
    IN_LEASE_PATCH,
    FakeClient,
    _ctx,
    _git,
    _invoke,
    _lease,
    _repo,
    _result,
)

BRANCH = "fix/verify-node-targets-" + "2026-11-18"
ATTEMPT_ID = "att_" + "0123456789abcdef" * 2
COMMIT = "a1" * 20

ADDRESS_DECOYS = ("10.0.0.1", "feat/10.0.0.1")
HIGH_ENTROPY_DECOYS = (
    "feat/" + "a1" * 20,
    "feat/" + "0123456789abcdef" * 2,
    "feat/" + "A1" * 16,
)
DECOY_BRANCHES = ADDRESS_DECOYS + HIGH_ENTROPY_DECOYS
KEY_SHAPE_DECOYS = (
    "feat/550e8400-e29b-41d4-a716-446655440000",
    "550e8400-e29b-41d4-a716-446655440000",
    "feat/0123456789abcdef-0123456789abcdef",
    "feat/0123456789abcdef_0123456789abcdef",
    "fix/DEADBEEFDEADBEEF-deadbeefdeadbeef",
)
LITERAL_CONTROLS = (
    "fix/verify-node-targets-2026-11-18",
    "feat/snapshot-decisions-section",
    "fix/verify-literal-redaction-parity",
    "release/2026-11-20",
    "v1.2.3",
)


def _verify_argv(repo, state) -> list[str]:
    return [
        "verify",
        ATTEMPT_ID,
        "--workdir",
        str(repo),
        "--branch",
        BRANCH,
        "--state-dir",
        str(state),
    ]


def test_verify_dry_run_prints_validated_branch_and_path_literally(
    tmp_path, capsys
):
    repo, _remote, base_sha = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base_sha)
    state = tmp_path / "state"
    _lease(state, base_sha, attempt_id=ATTEMPT_ID, branch=BRANCH)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha, branch=BRANCH))

    rc = _invoke(_ctx(client), _verify_argv(repo, state) + ["--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert f"branch: {BRANCH}" in out
    target = (state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json").resolve()
    assert f"verify result: {target}" in out
    assert not (state / "verify-results" / f"{ATTEMPT_ID}.json").exists()

    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["branch"] == BRANCH
    assert record["dry_run"] is True

    rc = _invoke(_ctx(FakeClient()), ["attempts", "--state-dir", str(state)])
    rows = json.loads(capsys.readouterr().out)["attempts"]
    row = next(item for item in rows if item["attempt_id"] == ATTEMPT_ID)
    assert row["branch"] == BRANCH


def test_verify_record_never_reinserts_an_invalid_branch(tmp_path, capsys):
    state = tmp_path / "state"
    bad_branch = "feat/../verify-node-targets-2026-11-18-abcd1234"

    target = cli._write_verify_result(
        state, "att-bad", ok=True, sha=None, branch=bad_branch, files=[], tests=[]
    )
    record = json.loads(target.read_text(encoding="utf-8"))
    assert record["branch"] != bad_branch
    assert "[redacted]" in record["branch"]

    _lease(state, "b" * 40, attempt_id="att-bad", branch=bad_branch)
    rc = _invoke(_ctx(FakeClient()), ["attempts", "--state-dir", str(state)])
    rows = json.loads(capsys.readouterr().out)["attempts"]
    row = next(item for item in rows if item["attempt_id"] == "att-bad")
    assert row["branch"] != bad_branch
    assert "[redacted]" in row["branch"]


def test_commit_local_prints_validated_commit_digest(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    client = SimpleNamespace(
        result_envelope=lambda attempt_id: {
            "attempt_id": attempt_id,
            "patch": {
                "diff": IN_LEASE_PATCH,
                "files": [{"path": "src/a.py"}],
            },
            "source": {"repo_rev": "b" * 40},
        }
    )
    ctx = _ctx(client)
    args = SimpleNamespace(
        attempt_id=ATTEMPT_ID,
        state_dir=str(state),
        workdir=str(repo),
        tests=["tests/test_sample.py"],
    )

    monkeypatch.setattr(
        verify_local,
        "verify_and_commit",
        lambda *a, **k: {
            "sha": COMMIT,
            "branch": BRANCH,
            "files": ["src/a.py"],
        },
    )

    rc = cli._cmd_verify_commit_local(ctx, args)
    out = capsys.readouterr().out

    assert rc == 0
    assert f"commit: {COMMIT}" in out
    assert f"branch: {BRANCH}" in out


def test_untrusted_text_and_outside_paths_stay_redacted(tmp_path, capsys):
    state = tmp_path / "state"
    lookalike = "sk-" + "a" * 32
    cli._print(f"worker said: {lookalike}")
    out = capsys.readouterr().out
    assert lookalike not in out
    assert "[redacted]" in out

    outside = tmp_path / "outside" / ("verify-node-targets-" + "2026-11-18-abcd1234")
    cli._print_state_path("verify result", outside, state)
    out = capsys.readouterr().out
    assert str(outside) not in out
    assert "[redacted]" in out

    assert lookalike not in cli._tail(f"test output: {lookalike}")


@pytest.mark.parametrize("branch", DECOY_BRANCHES)
def test_literal_branch_rejects_address_and_long_run_shapes(branch):
    assert cli._literal_branch(branch) is None


@pytest.mark.parametrize("branch", DECOY_BRANCHES)
def test_print_branch_redacts_address_and_long_run_shapes(branch, capsys):
    cli._print_branch(branch)
    out = capsys.readouterr().out

    raw_suffix = branch.rsplit("/", 1)[-1]
    assert branch not in out
    assert raw_suffix not in out
    marker = "[redacted-ip]" if branch in ADDRESS_DECOYS else "[redacted]"
    assert marker in out


@pytest.mark.parametrize("branch", KEY_SHAPE_DECOYS)
def test_literal_branch_rejects_key_shapes(branch):
    assert cli._literal_branch(branch) is None


@pytest.mark.parametrize("branch", KEY_SHAPE_DECOYS)
def test_print_branch_redacts_key_shapes(branch, capsys):
    cli._print_branch(branch)
    out = capsys.readouterr().out

    raw_suffix = branch.rsplit("/", 1)[-1]
    assert branch not in out
    assert raw_suffix not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("branch", LITERAL_CONTROLS)
def test_literal_branch_keeps_ordinary_branch_names(branch, capsys):
    assert cli._literal_branch(branch) == branch

    cli._print_branch(branch)
    assert capsys.readouterr().out == f"branch: {branch}\n"


def test_literal_branch_keeps_issue_number_branch(capsys):
    branch = "feat/issue-1234"
    assert cli._literal_branch(branch) == branch

    cli._print_branch(branch)
    assert capsys.readouterr().out == f"branch: {branch}\n"


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("10.0.0.1", "[redacted-ip]"),
        ("fix/verify-node-targets-2026-11-18", "fix/verify-node-targets-2026-11-18"),
    ],
)
def test_attempts_row_applies_branch_shape_gate(tmp_path, capsys, branch, expected):
    state = tmp_path / "state"
    _lease(state, "b" * 40, attempt_id="att-shape", branch=branch)

    rc = _invoke(_ctx(FakeClient()), ["attempts", "--state-dir", str(state)])
    out = capsys.readouterr().out

    assert rc == 0, out
    rows = json.loads(out)["attempts"]
    row = next(item for item in rows if item["attempt_id"] == "att-shape")
    assert row["branch"] == expected


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        (
            "feat/550e8400-e29b-41d4-a716-446655440000",
            "[redacted]",
        ),
        ("fix/verify-node-targets-2026-11-18", "fix/verify-node-targets-2026-11-18"),
    ],
)
def test_attempts_row_redacts_key_shaped_branch(tmp_path, capsys, branch, expected):
    state = tmp_path / "state"
    _lease(state, "b" * 40, attempt_id="att-key", branch=branch)

    rc = _invoke(_ctx(FakeClient()), ["attempts", "--state-dir", str(state)])
    out = capsys.readouterr().out

    assert rc == 0, out
    rows = json.loads(out)["attempts"]
    row = next(item for item in rows if item["attempt_id"] == "att-key")
    assert row["branch"] == expected
