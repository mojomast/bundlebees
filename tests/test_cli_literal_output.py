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
INTERPRETER = "/opt/toolchains/python-3.14.9+20261021-linux-x64/bin/python3.14"
FOREIGN_INTERPRETER = "/opt/toolchains/python-3.13.7+b4c1d2e3-linux-x64/bin/python3.13"
PYTEST_FLAGS = (
    "-B",
    "-m",
    "pytest",
    "-p",
    "no:cacheprovider",
    "-q",
    "--junitxml={evidence}/junit.xml",
)

ADDRESS_DECOYS = ("10.0.0.1", "feat/10.0.0.1")
HIGH_ENTROPY_DECOYS = (
    "feat/" + "a1" * 20,
    "feat/" + "0123456789abcdef" * 2,
    "feat/" + "A1" * 16,
)
DECOY_BRANCHES = ADDRESS_DECOYS + HIGH_ENTROPY_DECOYS
LITERAL_CONTROLS = (
    "fix/verify-node-targets-2026-11-18",
    "feat/snapshot-decisions-section",
    "fix/verify-literal-redaction-parity",
    "release/2026-11-20",
    "v1.2.3",
)
KEY_SHAPE_BRANCHES = (
    "feat/0123456789abcdef0123456789abcdef-0123456789abcdef",
    "feat/0123456789abcdef0123456789abcdef-DEADBEEFDEADBEEF",
    "feat/0123456789abcdef0123456789abcdef-e29b-41d4-a716-446655440000",
    "feat/0123456789abcdef0123456789abcdef_0123456789abcdef",
)
KEY_SHAPE_FRAGMENTS = (
    "0123456789abcdef0123456789abcdef",
    "0123456789abcdef",
    "0123456789abcdef01234567",
    "DEADBEEFDEADBEEF",
)
KEY_SHAPE_DECOYS = (
    "550e8400-e29b-41d4-a716-446655440000",
    "feat/550e8400-e29b-41d4-a716-446655440000",
    "feat/0123456789abcdef-0123456789abcdef",
    "feat/0123456789abcdef_0123456789abcdef",
    "fix/DEADBEEFDEADBEEF-deadbeefdeadbeef",
)
KEY_SHAPE_LEAK_BRANCHES = (
    "feat/550e8400-e29b-41d4-a716-446655440000",
    "fix/DEADBEEFDEADBEEF-deadbeefdeadbeef",
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


@pytest.mark.parametrize("branch", KEY_SHAPE_LEAK_BRANCHES)
def test_verify_record_redacts_key_shaped_branch(tmp_path, branch):
    state = tmp_path / "state"
    target = cli._write_verify_result(
        state, "att-key", ok=True, sha=None, branch=branch, files=[], tests=[]
    )
    record = json.loads(target.read_text(encoding="utf-8"))

    assert record["branch"] == "[redacted]"
    assert "DEADBEEFDEADBEEF" not in record["branch"]
    assert "deadbeefdeadbeef" not in record["branch"]


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


@pytest.mark.parametrize("branch", LITERAL_CONTROLS)
def test_literal_branch_keeps_ordinary_branch_names(branch, capsys):
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


@pytest.mark.parametrize("branch", KEY_SHAPE_LEAK_BRANCHES)
def test_attempts_row_redacts_key_shaped_branch(tmp_path, capsys, branch):
    state = tmp_path / "state"
    _lease(state, "b" * 40, attempt_id="att-key", branch=branch)

    rc = _invoke(_ctx(FakeClient()), ["attempts", "--state-dir", str(state)])
    out = capsys.readouterr().out

    assert rc == 0, out
    rows = json.loads(out)["attempts"]
    row = next(item for item in rows if item["attempt_id"] == "att-key")
    assert row["branch"] == "[redacted]"


@pytest.mark.parametrize("branch", KEY_SHAPE_BRANCHES)
def test_print_branch_scrubs_before_key_shape_substitution(branch, capsys):
    cli._print_branch(branch)
    out = capsys.readouterr().out

    assert branch not in out
    assert out == "branch: [redacted]\n"
    for fragment in KEY_SHAPE_FRAGMENTS:
        assert fragment not in out


@pytest.mark.parametrize("branch", KEY_SHAPE_BRANCHES)
def test_literal_branch_rejects_whole_key_shape_runs(branch):
    assert cli._literal_branch(branch) is None


@pytest.mark.parametrize("branch", KEY_SHAPE_DECOYS)
def test_literal_branch_rejects_key_shapes(branch):
    assert cli._literal_branch(branch) is None


@pytest.mark.parametrize("branch", KEY_SHAPE_DECOYS)
def test_print_branch_redacts_key_shapes(branch, capsys):
    cli._print_branch(branch)
    out = capsys.readouterr().out

    assert branch not in out
    assert branch.rsplit("/", 1)[-1] not in out
    assert "[redacted]" in out
    if branch.startswith("fix/"):
        assert out == "branch: fix/[redacted]\n"


@pytest.mark.parametrize("branch", LITERAL_CONTROLS + ("feat/issue-1234",))
def test_key_shape_guard_keeps_literal_controls(branch, capsys):
    assert cli._literal_branch(branch) == branch

    cli._print_branch(branch)
    assert capsys.readouterr().out == f"branch: {branch}\n"


@pytest.mark.parametrize(
    ("value", "collapse", "expected"),
    [
        pytest.param(None, False, None, id="null"),
        pytest.param(None, True, None, id="null-collapse"),
        pytest.param("", False, "", id="empty"),
        pytest.param("", True, "", id="empty-collapse"),
        pytest.param("   ", False, "   ", id="whitespace"),
        pytest.param("   ", True, "   ", id="whitespace-collapse"),
    ],
)
def test_display_branch_preserves_null_and_empty(value, collapse, expected):
    result = cli._display_branch(value, collapse=collapse)

    if expected is None:
        assert result is None
    else:
        assert result == expected
    assert result != "[redacted]"


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        pytest.param(None, None, id="null"),
        pytest.param("", "", id="empty"),
    ],
)
def test_verify_record_preserves_null_and_empty_branch(tmp_path, branch, expected):
    state = tmp_path / "state"

    target = cli._write_verify_result(
        state,
        "att-null",
        ok=False,
        sha=None,
        branch=branch,
        files=[],
        tests=[],
        code="sandbox_env_missing",
        error="x",
    )
    record = json.loads(target.read_text(encoding="utf-8"))

    if expected is None:
        assert record["branch"] is None
    else:
        assert record["branch"] == expected
    assert record["branch"] != "[redacted]"


def _write_argv_record(tmp_path, tests, text):
    state = tmp_path / "state"
    target = cli._write_verify_result(
        state,
        "att-argv",
        ok=True,
        sha=None,
        branch="fix/verify-record-argv-literal",
        files=[],
        tests=tests,
        evidence={"test_argv": text},
    )
    return json.loads(target.read_text(encoding="utf-8"))


def test_verify_record_keeps_cli_built_pytest_argv_literal(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    text = " ".join([INTERPRETER, *PYTEST_FLAGS, *tests])

    record = _write_argv_record(tmp_path, tests, text)

    assert record["evidence"]["test_argv"] == text
    assert INTERPRETER in record["evidence"]["test_argv"]


def test_verify_record_keeps_node_argv_literal(tmp_path):
    tests = ["tests/" + "a1" * 20 + ".test.mjs", "tests/b.test.mjs"]
    text = " ".join(["node", "--test", *tests])

    record = _write_argv_record(tmp_path, tests, text)

    assert record["evidence"]["test_argv"] == text
    assert tests[0] in record["evidence"]["test_argv"]


def test_verify_record_still_scrubs_a_non_template_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    text = INTERPRETER + " -m pytest tests/test_sample.py"

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_truncated_template(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/" + "a1" * 1050 + ".py"]
    text = " ".join([INTERPRETER, *PYTEST_FLAGS, *tests])

    assert len(text) > 2000
    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_foreign_interpreter_pytest_argv(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    text = " ".join([FOREIGN_INTERPRETER, *PYTEST_FLAGS, *tests])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_pytest_argv_with_trailing_tokens(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    text = " ".join([INTERPRETER, *PYTEST_FLAGS, *tests, "--maxfail=1"])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_node_argv_with_an_extra_flag(tmp_path):
    tests = ["tests/" + "a1" * 20 + ".test.mjs", "tests/b.test.mjs"]
    text = " ".join(["node", "--test", "--experimental-strip-types", *tests])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_node_argv_missing_a_target(tmp_path):
    tests = ["tests/" + "a1" * 20 + ".test.mjs", "tests/b.test.mjs"]
    text = " ".join(["node", "--test", tests[0]])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_space_containing_target(
    tmp_path, monkeypatch
):
    """Pin the documented fail-closed fallback for review-bb-pr228 F1(a).

    Change this only when the match learns to preserve a space-containing
    target.
    """

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/" + "a1" * 20 + " b.py"]
    text = " ".join([INTERPRETER, *PYTEST_FLAGS, *tests])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_an_acdd_subset_argv(tmp_path, monkeypatch):
    """Pin the review-bb-pr228 F1(b) ACDD selected-subset fallback."""

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py", "tests/data.json"]
    text = " ".join([INTERPRETER, *PYTEST_FLAGS, "tests/test_sample.py"])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_a_node_argv_with_a_trailing_token(tmp_path):
    """Pin review-bb-pr229 F1: a trailing node token must never be echoed literally."""

    tests = ["tests/" + "a1" * 20 + ".test.mjs", "tests/b.test.mjs"]
    text = " ".join(
        ["node", "--test", *tests, "--report=9f2c7d1e5a3b4c6d8e0f1a2b3c4d5e6f"]
    )

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_an_interpreter_sharing_only_the_final_suffix(
    tmp_path, monkeypatch
):
    """Pin review-bb-pr229 F2: only exact equality with sys.executable may restore the argv literally."""

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    suffix_path = (
        "/home/clanker01/.ssh/9f2c7d1e5a3b4c6d8e0f1a2b3c4d5e6f/bin/python3.14"
    )
    text = " ".join([suffix_path, *PYTEST_FLAGS, *tests])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_verify_record_still_scrubs_an_interpreter_sharing_only_the_basename(
    tmp_path, monkeypatch
):
    """Pin review-bb-pr229 F2/N3: only exact sys.executable equality restores the argv literally."""

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    basename_path = (
        "/home/clanker01/.ssh/9f2c7d1e5a3b4c6d8e0f1a2b3c4d5e6f/python3.14"
    )
    text = " ".join([basename_path, *PYTEST_FLAGS, *tests])

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


_NEAR_MISS_RUN = "9f2c7d1e5a3b4c6d8e0f1a2b3c4d5e6f"
NEAR_MISS_INTERPRETERS = (
    pytest.param(
        INTERPRETER + "/" + _NEAR_MISS_RUN + "/python3.14",
        id="interpreter-with-suffix",
    ),
    pytest.param(
        INTERPRETER.rsplit("/", 1)[0]
        + "/"
        + _NEAR_MISS_RUN
        + "/python3.14",
        id="sibling-in-same-directory",
    ),
    pytest.param(
        INTERPRETER.rsplit("/", 1)[0] + "/" + _NEAR_MISS_RUN,
        id="same-directory-different-basename",
    ),
    pytest.param(
        "/opt/evil/" + _NEAR_MISS_RUN + "/bin/python3.14",
        id="foreign-prefix-short-suffix",
    ),
    pytest.param(
        "/etc/" + _NEAR_MISS_RUN + "/python3.14",
        id="foreign-directory-same-basename",
    ),
)


@pytest.mark.parametrize("interpreter", NEAR_MISS_INTERPRETERS)
def test_trusted_argv_rejects_every_near_miss_interpreter_shape(
    tmp_path, monkeypatch, interpreter
):
    """Pin review-bb-pr230 F1 near-miss interpreter shapes as a class.

    The five rows cover an appended suffix, a sibling basename in the same
    directory, a same-directory high-entropy basename, a foreign prefix
    sharing the /bin/python3.14 suffix, and a foreign directory sharing the
    basename. A relaxation of any shape must not restore the argv literally.
    """

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    text = " ".join([interpreter, *PYTEST_FLAGS, *tests])

    assert cli._trusted_test_argv({"test_argv": text}, tests) is None

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


def test_trusted_argv_rejects_an_exact_interpreter_with_a_leading_token(
    tmp_path, monkeypatch
):
    """Pin review-bb-pr230 F1 C4b: an exact interpreter plus a leading token is not a tail-slice match."""

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py"]
    text = " ".join([INTERPRETER, _NEAR_MISS_RUN, *PYTEST_FLAGS, *tests])

    assert cli._trusted_test_argv({"test_argv": text}, tests) is None

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


RESIDUAL_ARGV_SHAPES = (
    pytest.param(
        ["tests/a.test.mjs"],
        "/home/clanker01/.ssh/" + _NEAR_MISS_RUN + "/node --test tests/a.test.mjs",
        id="node-arm-path-basename",
    ),
    pytest.param(
        ["tests/a.test.mjs"],
        "/home/clanker01/.ssh/" + _NEAR_MISS_RUN + " node --test tests/a.test.mjs",
        id="node-arm-path-leading-token",
    ),
    pytest.param(
        ["tests/test_sample.py"],
        " ".join(
            [
                INTERPRETER,
                *PYTEST_FLAGS,
                "/home/clanker01/.ssh/" + _NEAR_MISS_RUN,
                "tests/test_sample.py",
            ]
        ),
        id="pytest-arm-middle-token",
    ),
    pytest.param(
        ["tests/test_sample.py"],
        " ".join(
            [
                INTERPRETER,
                *PYTEST_FLAGS[:6],
                "--junitxml=/home/clanker01/.ssh/" + _NEAR_MISS_RUN + "/junit.xml",
                "tests/test_sample.py",
            ]
        ),
        id="pytest-arm-foreign-junitxml",
    ),
    pytest.param(
        ["tests/test_sample.py"],
        " ".join(
            [
                INTERPRETER,
                *PYTEST_FLAGS,
                "/home/clanker01/.ssh/" + _NEAR_MISS_RUN + "/test_sample.py",
            ]
        ),
        id="pytest-arm-target-last-component",
    ),
)


@pytest.mark.parametrize("tests, text", RESIDUAL_ARGV_SHAPES)
def test_trusted_argv_rejects_residual_widened_shapes(
    tmp_path, monkeypatch, tests, text
):
    """Pin review-bb-pr231 F1 measured survivors, one row per widening.

    R1r: node arm matches tokens[0] by basename plus an exact tail.
    R2r: node arm matches a tail slice with a leading token.
    R3: pytest arm matches the fixed flags as a prefix and the targets as a
    suffix with a foreign token in the middle.
    R5r: pytest arm accepts any --junitxml= value.
    R6r: pytest arm compares the target's last path component (its basename);
    parent-directory comparison is a separate shape, pinned by the sweep below.

    A relaxed match for any row must keep the argv out of the trusted literal
    path, so no widened shape may be restored verbatim.
    """

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)

    assert cli._trusted_test_argv({"test_argv": text}, tests) is None

    record = _write_argv_record(tmp_path, tests, text)
    argv_text = record["evidence"]["test_argv"]

    assert argv_text != text
    assert "[redacted]" in argv_text


_NEAR_MISS_DIR = "/home/clanker01/.ssh/" + _NEAR_MISS_RUN


def _argv_sweep_cases():
    templates = (
        (
            "pytest",
            [INTERPRETER, *PYTEST_FLAGS, "tests/test_sample.py"],
            ["tests/test_sample.py"],
        ),
        (
            "node",
            ["node", "--test", "tests/a.test.mjs"],
            ["tests/a.test.mjs"],
        ),
    )
    cases = []
    for arm, template, tests in templates:
        for index, token in enumerate(template):
            for kind, mutated in (
                ("foreign-prefix", _NEAR_MISS_DIR + "/" + token.lstrip("/")),
                ("appended-component", token + "/" + _NEAR_MISS_RUN),
            ):
                tokens = list(template)
                tokens[index] = mutated
                cases.append(
                    pytest.param(
                        tests,
                        template,
                        tokens,
                        id=f"{arm}-{index}-{kind}",
                    )
                )
            if "/" in token:
                tokens = list(template)
                tokens[index] = token.rsplit("/", 1)[0] + "/" + _NEAR_MISS_RUN
                cases.append(
                    pytest.param(
                        tests,
                        template,
                        tokens,
                        id=f"{arm}-{index}-same-dir-foreign-base",
                    )
                )
            cases.append(
                pytest.param(
                    tests,
                    template,
                    [
                        *template[:index],
                        _NEAR_MISS_DIR,
                        *template[index:],
                    ],
                    id=f"{arm}-{index}-inserted-before",
                )
            )
            cases.append(
                pytest.param(
                    tests,
                    template,
                    [
                        *template[: index + 1],
                        _NEAR_MISS_DIR,
                        *template[index + 1 :],
                    ],
                    id=f"{arm}-{index}-inserted-after",
                )
            )
    cases.append(
        pytest.param(
            ["tests/test_sample.py"],
            [INTERPRETER, *PYTEST_FLAGS, "tests/test_sample.py"],
            [
                INTERPRETER,
                "--junitxml=" + _NEAR_MISS_DIR + "/junit.xml",
                *PYTEST_FLAGS,
                "tests/test_sample.py",
            ],
            id="pytest-1-leading-option",
        )
    )
    return tuple(cases)


ARGV_SWEEP_CASES = _argv_sweep_cases()


@pytest.mark.parametrize("tests, template, tokens", ARGV_SWEEP_CASES)
def test_trusted_argv_rejects_a_one_token_mutation_at_every_position(
    tmp_path, monkeypatch, tests, template, tokens
):
    """Sweep one-token mutations across every position of both templates.

    The earlier tables pin named shapes; this sweeps every position of the
    pytest and node templates so the enumerated single-token mutations fail
    here. A finite sweep bounds only the mutations it enumerates.
    """

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)

    assert tokens != template
    text = " ".join(tokens)

    assert cli._trusted_test_argv({"test_argv": text}, tests) is None

    record = _write_argv_record(tmp_path, tests, text)
    assert record["evidence"]["test_argv"] != text
    assert "[redacted]" in record["evidence"]["test_argv"]


ARGV_SWEEP_POSITIVE_CASES = (
    pytest.param(
        ["tests/test_sample.py"],
        [INTERPRETER, *PYTEST_FLAGS, "tests/test_sample.py"],
        id="pytest-exact",
    ),
    pytest.param(
        ["tests/a.test.mjs"],
        ["node", "--test", "tests/a.test.mjs"],
        id="node-exact",
    ),
)


@pytest.mark.parametrize("tests, tokens", ARGV_SWEEP_POSITIVE_CASES)
def test_trusted_argv_sweep_positive_controls_restore_exact_templates(
    monkeypatch, tests, tokens
):
    """An always-None guard must fail: the exact templates still restore."""

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)

    text = " ".join(tokens)
    assert cli._trusted_test_argv({"test_argv": text}, tests) == text


RESIDUAL_TWO_TARGET_AND_NORMALISED_SHAPES = (
    pytest.param(
        ["tests/test_sample.py", "tests/test_sample_second.py"],
        [
            INTERPRETER,
            *PYTEST_FLAGS,
            "tests/test_sample.py",
            _NEAR_MISS_DIR + "/test_sample_second.py",
        ],
        id="pytest-second-target-foreign-prefix",
    ),
    pytest.param(
        ["tests/test_sample.py", "tests/test_sample_second.py"],
        [
            INTERPRETER,
            *PYTEST_FLAGS,
            _NEAR_MISS_DIR + "/test_sample.py",
            "tests/test_sample_second.py",
        ],
        id="pytest-first-target-foreign-prefix",
    ),
    pytest.param(
        ["tests/a.test.mjs", "tests/b.test.mjs"],
        ["node", "--test", "tests/a.test.mjs", _NEAR_MISS_DIR + "/b.test.mjs"],
        id="node-second-target-foreign-prefix",
    ),
    pytest.param(
        ["tests/a.test.mjs", "tests/b.test.mjs"],
        ["node", "--test", _NEAR_MISS_DIR + "/a.test.mjs", "tests/b.test.mjs"],
        id="node-first-target-foreign-prefix",
    ),
    pytest.param(
        ["tests/test_sample.py"],
        [INTERPRETER, *PYTEST_FLAGS, "tests/" + _NEAR_MISS_RUN + "/../test_sample.py"],
        id="pytest-normpath-parent-segment",
    ),
    pytest.param(
        ["tests/test_sample.py"],
        [INTERPRETER, *PYTEST_FLAGS, "./tests/" + _NEAR_MISS_RUN + "/../test_sample.py"],
        id="pytest-dot-prefix",
    ),
    pytest.param(
        ["tests/test_sample.py"],
        [INTERPRETER, *PYTEST_FLAGS, "tests/./" + _NEAR_MISS_RUN + "/../test_sample.py"],
        id="pytest-dot-middle-segment",
    ),
    pytest.param(
        ["tests/a.test.mjs"],
        ["node", "--test", "tests/" + _NEAR_MISS_RUN + "/../a.test.mjs"],
        id="node-normpath-parent-segment",
    ),
    pytest.param(
        ["tests/a.test.mjs"],
        ["node", "--test", "./tests/" + _NEAR_MISS_RUN + "/../a.test.mjs"],
        id="node-dot-prefix",
    ),
    pytest.param(
        ["tests/a.test.mjs"],
        ["node", "--test", "tests/./" + _NEAR_MISS_RUN + "/../a.test.mjs"],
        id="node-dot-middle-segment",
    ),
)


@pytest.mark.parametrize("tests, tokens", RESIDUAL_TWO_TARGET_AND_NORMALISED_SHAPES)
def test_trusted_argv_rejects_two_target_and_normalised_widenings(
    tmp_path, monkeypatch, tests, tokens
):
    """Pin review-bb-pr233 F1's two measured widening families.

    S1/S2 compare only one of several targets (first-only and last-only
    variants), so a two-target ``tests`` list can restore a foreign target.
    S3/S4 compare lexical normalisations, so a ``.`` or ``..`` segment that
    normalises onto the exact target is restored with its foreign component
    intact. These rows pass on the base guard; mutant-kill is the
    discrimination. A finite table bounds only the shapes it enumerates.
    """

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)

    text = " ".join(tokens)
    assert cli._trusted_test_argv({"test_argv": text}, tests) is None

    record = _write_argv_record(tmp_path, tests, text)
    assert record["evidence"]["test_argv"] != text
    assert "[redacted]" in record["evidence"]["test_argv"]


def test_trusted_argv_two_target_exact_template_still_restores(monkeypatch):
    """A guard that refuses multi-target templates must fail here."""

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)
    tests = ["tests/test_sample.py", "tests/test_sample_second.py"]
    text = " ".join([INTERPRETER, *PYTEST_FLAGS, *tests])

    assert cli._trusted_test_argv({"test_argv": text}, tests) == text

RESIDUAL_MULTI_TARGET_CONJUNCTION_SHAPES = (
    pytest.param(
        ["tests/test_sample.py", "tests/test_sample_second.py"],
        [
            INTERPRETER,
            *PYTEST_FLAGS,
            "tests/test_sample.py",
            "tests/" + _NEAR_MISS_RUN + ".py",
        ],
        id="pytest-two-target-same-dir-foreign-basename",
    ),
    pytest.param(
        ["tests/a.test.mjs", "tests/b.test.mjs"],
        ["node", "--test", "tests/a.test.mjs", "tests/" + _NEAR_MISS_RUN + ".mjs"],
        id="node-two-target-same-dir-foreign-basename",
    ),
    pytest.param(
        ["tests/a.py", "tests/b.py", "tests/c.py"],
        [
            INTERPRETER,
            *PYTEST_FLAGS,
            "tests/a.py",
            _NEAR_MISS_DIR + "/foreign.py",
            "tests/c.py",
        ],
        id="pytest-three-target-foreign-middle",
    ),
    pytest.param(
        ["tests/a.test.mjs", "tests/b.test.mjs", "tests/c.test.mjs"],
        [
            "node",
            "--test",
            "tests/a.test.mjs",
            _NEAR_MISS_DIR + "/foreign.mjs",
            "tests/c.test.mjs",
        ],
        id="node-three-target-foreign-middle",
    ),
)

@pytest.mark.parametrize("tests, tokens", RESIDUAL_MULTI_TARGET_CONJUNCTION_SHAPES)
def test_trusted_argv_rejects_multi_target_conjunction_widenings(
    tmp_path, monkeypatch, tests, tokens
):
    """Pin review-bb-pr234 F1/F2's two measured conjunction families.

    F1: a parent-only comparison over several targets sees every dirname as
    `tests`, so a same-directory foreign basename carrying a 32-char run is
    restored. F2: a first+last conjunction over three targets ignores the
    middle token, so a foreign middle path is restored. These rows pass on
    the base guard; mutant-kill is the discrimination. A finite table bounds
    only the shapes it enumerates.
    """

    monkeypatch.setattr(cli.sys, "executable", INTERPRETER)

    text = " ".join(tokens)
    assert cli._trusted_test_argv({"test_argv": text}, tests) is None

    record = _write_argv_record(tmp_path, tests, text)
    assert record["evidence"]["test_argv"] != text
    assert "[redacted]" in record["evidence"]["test_argv"]
