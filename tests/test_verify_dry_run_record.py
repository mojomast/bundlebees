"""A dry run previews a verification; it never replaces the official record.

Every case drives the real ``cli._cmd_verify`` with a temporary lease/worktree,
a fake client and a hermetic sandbox runner. A non-dry-run ``verify
<attempt_id>`` writes the canonical ``verify-results/<attempt>.json`` on green
or refusal; a dry run writes only ``verify-results/dry-run/<attempt>.json``,
and ``--commit-local`` writes the canonical record only when it commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pytest

from bundlebees_plugin import cli
from tests.test_cli import (
    ATTEMPT_ID,
    BRANCH,
    IN_LEASE_PATCH,
    OUT_OF_LEASE_PATCH,
    FakeClient,
    _ctx,
    _git,
    _invoke,
    _lease,
    _repo,
    _result,
    _verify_argv,
)

OFFICIAL_SHA = "a" * 40
OFFICIAL_BRANCH = "feat/official"
OFFICIAL_AT = "2026-01-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _no_payload_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    monkeypatch.delenv("HYVV_SRC", raising=False)


@pytest.fixture
def green_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(workdir: Path, argv: list[str], **kwargs):
        return {
            "backend": "fake",
            "exit_code": 0,
            "timed_out": False,
            "output_tail": "",
        }

    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", fake_run)


def _official_record() -> dict:
    return {
        "attempt_id": ATTEMPT_ID,
        "ok": True,
        "sha": OFFICIAL_SHA,
        "branch": OFFICIAL_BRANCH,
        "files": ["src/a.py"],
        "tests": ["tests/test_sample.py"],
        "at": OFFICIAL_AT,
        "evidence": {"source_rev": "b" * 40},
    }


def _seed_official(state: Path) -> Path:
    path = state / "verify-results" / f"{ATTEMPT_ID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_official_record(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _side_record(state: Path) -> Path:
    return state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(path: Path) -> tuple[str, int]:
    stat = path.stat()
    return _sha256(path), stat.st_mtime_ns


def test_dry_run_green_leaves_the_official_record_byte_identical(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    official = _seed_official(state)
    before_sha, before_mtime = _snapshot(official)
    before_top_level = sorted(p.name for p in (state / "verify-results").glob("*.json"))
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    assert _sha256(official) == before_sha
    assert official.stat().st_mtime_ns == before_mtime
    saved = json.loads(official.read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert saved["sha"] == OFFICIAL_SHA
    assert saved["branch"] == OFFICIAL_BRANCH

    side = _side_record(state)
    payload = json.loads(side.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["ok"] is True
    assert payload["sha"] is None
    assert payload["attempt_id"] == ATTEMPT_ID
    assert payload["evidence"]["exit_code"] == 0
    assert sorted(p.name for p in (state / "verify-results").glob("*.json")) == before_top_level
    assert sorted(p.name for p in side.parent.glob("*")) == [side.name]


def test_dry_run_refusal_leaves_the_official_record_byte_identical(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    official = _seed_official(state)
    before_sha, before_mtime = _snapshot(official)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert _sha256(official) == before_sha
    assert official.stat().st_mtime_ns == before_mtime
    saved = json.loads(official.read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert saved["sha"] == OFFICIAL_SHA

    payload = json.loads(_side_record(state).read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["ok"] is False
    assert payload["code"] == "scope_violation"
    assert isinstance(payload["error"], str) and payload["error"]


def test_dry_run_does_not_create_a_canonical_record(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    assert not (state / "verify-results" / f"{ATTEMPT_ID}.json").exists()
    side = _side_record(state)
    assert side.exists()
    assert json.loads(side.read_text(encoding="utf-8"))["dry_run"] is True


def test_refused_dry_run_does_not_create_a_canonical_record(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc != 0
    assert not (state / "verify-results" / f"{ATTEMPT_ID}.json").exists()
    assert json.loads(_side_record(state).read_text(encoding="utf-8"))["ok"] is False


def test_repeated_dry_runs_write_one_side_record_without_temp_files(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))
    argv = _verify_argv(repo, state, "--dry-run")

    assert _invoke(_ctx(client), argv) == 0
    first = json.loads(_side_record(state).read_text(encoding="utf-8"))
    _git(repo, "reset", "--hard", base_sha)
    assert _invoke(_ctx(client), argv) == 0
    second = json.loads(_side_record(state).read_text(encoding="utf-8"))

    side_dir = _side_record(state).parent
    assert sorted(p.name for p in side_dir.iterdir()) == [f"{ATTEMPT_ID}.json"]
    assert set(first) == set(second) == {
        "attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence", "dry_run",
    }
    assert second["dry_run"] is True
    assert second["sha"] is None


def test_official_verify_keeps_the_canonical_shape(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc == 0
    canonical = state / "verify-results" / f"{ATTEMPT_ID}.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert set(payload) == {"attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence"}
    assert "dry_run" not in payload
    assert payload["ok"] is True
    assert re.fullmatch(r"[0-9a-f]{40}", payload["sha"] or "")
    assert payload["branch"] == BRANCH
    assert not _side_record(state).exists()


def test_official_refusal_keeps_the_canonical_shape(
    tmp_path: Path, green_sandbox: None
):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc != 0
    canonical = state / "verify-results" / f"{ATTEMPT_ID}.json"
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert set(payload) == {
        "attempt_id", "ok", "sha", "branch", "files", "tests", "at", "evidence", "code", "error",
    }
    assert "dry_run" not in payload
    assert payload["ok"] is False
    assert payload["code"] == "scope_violation"
    assert not _side_record(state).exists()


def test_non_dry_run_refusal_replaces_a_green_canonical_record(tmp_path: Path):
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    official = _seed_official(state)
    seeded = json.loads(official.read_text(encoding="utf-8"))
    assert seeded["ok"] is True
    assert seeded["sha"] == OFFICIAL_SHA

    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state))

    assert rc != 0
    payload = json.loads(official.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["code"] == "scope_violation"
    assert isinstance(payload["error"], str) and payload["error"]
    assert payload["sha"] is None
    assert "dry_run" not in payload
    assert not _side_record(state).exists()


DOCS_ROOT = Path(__file__).resolve().parent.parent
README_DOC = DOCS_ROOT / "README.md"
CHANGELOG_DOC = DOCS_ROOT / "CHANGELOG.md"
SKILL_REFERENCE_DOC = (
    DOCS_ROOT / "skills" / "bundlebees-operator" / "references" / "verify-and-deploy.md"
)
REFERENCE_DOCS = (README_DOC, CHANGELOG_DOC, SKILL_REFERENCE_DOC)

STANDALONE_DRY_RUN = (
    r"(?<!non[ \-\u2010-\u2015])"
    r"(?<!non [\-\u2010-\u2015])"
    r"(?<!non[ ][\-\u2010-\u2015] )"
    r"(?<!non[\-\u2010-\u2015] )"
    r"(?<!non\t )"
    r"(?<!non  )"
    r"(?<!non\t)"
    r"dry[ -]runs?\b"
)
SIDE_RECORD_PATH = r"verify-results/dry-run/<attempt_id>\.json"
SIDE_RECORD_PATH_TEXT = "verify-results/dry-run/<attempt_id>.json"
SIDE_RECORD_GAP_FORBIDDEN = (
    r"(?:nothing|never|unless|when|after|before|if|no\s+longer|not"
    r"|dry[ -]runs?|writes?)"
)
DRY_RUN_SIDE_RECORD_PIN = re.compile(
    rf"{STANDALONE_DRY_RUN}\b"
    rf"(?:\s+now)?\s+writes?\s+"
    rf"(?:(?!{SIDE_RECORD_GAP_FORBIDDEN}\b|`?{SIDE_RECORD_PATH})[^.;]){{0,40}}?"
    rf"`?{SIDE_RECORD_PATH}`?(?:\s+only\b)?",
    re.IGNORECASE,
)

BASE_STANDALONE_DRY_RUN = r"(?<!non-)dry[- ]run"
BASE_DRY_RUN_SIDE_RECORD_PIN = re.compile(
    rf"{BASE_STANDALONE_DRY_RUN}\b[^.]{{0,200}}?(?:writes only|only)"
    rf"[^.]{{0,200}}?verify-results/dry-run/<attempt_id>\.json"
    rf"|{BASE_STANDALONE_DRY_RUN}\b[^.]{{0,200}}?verify-results/dry-run/<attempt_id>\.json"
    rf"[^.]{{0,80}}?\bonly\b",
    re.IGNORECASE,
)
DRY_RUN_NEVER_CANONICAL_PIN = re.compile(
    rf"{STANDALONE_DRY_RUN}\b.{{0,240}}?never writes or replaces"
    rf".{{0,80}}?canonical record",
    re.IGNORECASE,
)
COMMIT_LOCAL_EXCEPTION_PINS = (
    re.compile(r"`?verify --commit-local`? is the exception", re.IGNORECASE),
    re.compile(r"writes? the canonical record only when it commits", re.IGNORECASE),
    re.compile(r"commit-local refusal writes no record", re.IGNORECASE),
    re.compile(
        r"leaves? (?:any )?earlier (?:one|canonical record) in place",
        re.IGNORECASE,
    ),
)
SKILL_REFUSAL_KEYS_PIN = re.compile(
    r"(?:refusal|refused|refuses)[^.]{0,80}(?:adds?|includes?|appends?)"
    r"[^.]{0,60}`?code`?[^.]{0,30}`?error`?",
    re.IGNORECASE,
)
GREEN_RECORD_KEYS = (
    "attempt_id",
    "ok",
    "sha",
    "branch",
    "files",
    "tests",
    "at",
    "evidence",
)


def _doc_text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _doc_paragraphs(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", raw) if block.strip()]


class TestVerifyRecordDocs:
    def test_no_document_claims_only_a_committing_verify(self) -> None:
        for path in REFERENCE_DOCS:
            assert "only a committing verify" not in _doc_text(path).lower(), path

    @pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
    def test_document_states_the_refusal_truth(self, path: Path) -> None:
        text = _doc_text(path)
        assert re.search(
            r"non-dry-run verify writes\s+`?verify-results/<attempt_id>\.json`?",
            text,
            re.IGNORECASE,
        ), f"{path.name} does not say a non-dry-run verify writes the canonical record"
        assert re.search(
            r"refus\w*.{0,160}ok:\s*false.{0,160}code.{0,80}error",
            text,
            re.IGNORECASE,
        ), f"{path.name} does not record ok: false with a code and error on refusal"
        assert re.search(
            r"refus\w*.{0,240}replaces?.{0,80}canonical record",
            text,
            re.IGNORECASE,
        ), f"{path.name} does not say a refusal replaces the canonical record"
        paragraphs = _doc_paragraphs(path)
        side_record = "verify-results/dry-run/<attempt_id>.json"
        assert any(
            side_record in paragraph and DRY_RUN_SIDE_RECORD_PIN.search(paragraph)
            for paragraph in paragraphs
        ), f"{path.name} does not name the dry-run side record as the only dry-run output"
        assert any(
            side_record in paragraph and DRY_RUN_NEVER_CANONICAL_PIN.search(paragraph)
            for paragraph in paragraphs
        ), f"{path.name} does not say a dry run never writes or replaces the canonical record"

    @pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
    def test_document_states_the_commit_local_exception(self, path: Path) -> None:
        text = _doc_text(path)
        missing = [pin.pattern for pin in COMMIT_LOCAL_EXCEPTION_PINS if not pin.search(text)]
        assert not missing, (
            f"{path.name} does not say a commit-local refusal writes no record "
            f"and leaves any earlier one in place: {missing}"
        )

    def test_skill_reference_enumerates_the_refusal_keys(self) -> None:
        paragraphs = _doc_paragraphs(SKILL_REFERENCE_DOC)
        shape = [
            paragraph
            for paragraph in paragraphs
            if "Verify writes `verify-results/<attempt_id>.json`" in paragraph
        ]
        assert len(shape) == 1, "the skill reference must have one record-shape paragraph"
        for key in GREEN_RECORD_KEYS:
            assert f"`{key}`" in shape[0], f"the record shape does not name {key}"
        assert SKILL_REFUSAL_KEYS_PIN.search(shape[0]), (
            "the record shape does not explicitly add code and error for a refusal"
        )


def test_dry_run_pins_reject_a_non_dry_run_only_decoy() -> None:
    decoy = (
        "A non-dry-run verify writes `verify-results/<attempt_id>.json` whether it "
        "passes or refuses; it never writes or replaces the dry-run canonical record at "
        "`verify-results/dry-run/<attempt_id>.json`."
    )
    assert not DRY_RUN_SIDE_RECORD_PIN.search(decoy)
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(decoy)


SUBJECT_CASES = (
    ("singular-and-plural-space", "A dry run and Dry runs are standalone subjects.", True),
    ("singular-and-plural-hyphen", "A dry-run and Dry-runs are standalone subjects.", True),
    ("non-space", "A non dry run verify writes a record.", False),
    ("non-hyphen", "A non-dry-run verify writes a record.", False),
    ("non-en-dash", "A non\u2013dry-run verify writes a record.", False),
    ("non-em-dash", "A non\u2014dry-run verify writes a record.", False),
    ("non-spaced-hyphen", "A non - dry run verify writes a record.", False),
    ("non-spaced-en-dash", "A non \u2013 dry run verify writes a record.", False),
    ("non-spaced-em-dash", "A non \u2014 dry run verify writes a record.", False),
    ("non-figure-dash", "A non\u2010dry-run verify writes a record.", False),
    ("non-non-breaking-hyphen", "A non\u2011dry-run verify writes a record.", False),
    ("non-space-before-en-dash", "A non \u2013dry-run verify writes a record.", False),
    ("non-em-dash-before-space", "A non\u2014 dry run verify writes a record.", False),
    ("non-double-space", "A non  dry run verify writes a record.", False),
)


@pytest.mark.parametrize(
    "name, text, expected",
    SUBJECT_CASES,
    ids=[case[0] for case in SUBJECT_CASES],
)
def test_standalone_dry_run_subject_table(
    name: str, text: str, expected: bool
) -> None:
    assert bool(re.search(STANDALONE_DRY_RUN, text, re.IGNORECASE)) is expected


REVIEWER_DECOYS = (
    (
        "nothing_and_a_second_writer",
        "A dry run writes nothing, and a committing verify writes "
        "verify-results/dry-run/<attempt_id>.json.",
    ),
    (
        "does_not_write",
        "A dry run doesn't write verify-results/dry-run/<attempt_id>.json.",
    ),
    (
        "will_not_write",
        "A dry run won't write verify-results/dry-run/<attempt_id>.json.",
    ),
    (
        "fails_to_write",
        "A dry run fails to write verify-results/dry-run/<attempt_id>.json.",
    ),
    (
        "no_longer_writes",
        "A dry run no longer writes verify-results/dry-run/<attempt_id>.json.",
    ),
    (
        "previews_what_a_committing_verify_writes",
        "A dry run previews the output that a committing verify writes to "
        "verify-results/dry-run/<attempt_id>.json.",
    ),
    (
        "shows_nothing_unless_the_operator_writes",
        "A dry run shows nothing unless the operator writes "
        "verify-results/dry-run/<attempt_id>.json.",
    ),
)


@pytest.mark.parametrize(
    "name, decoy",
    REVIEWER_DECOYS,
    ids=[case[0] for case in REVIEWER_DECOYS],
)
def test_side_record_pin_rejects_a_reviewer_decoy(name: str, decoy: str) -> None:
    assert not DRY_RUN_SIDE_RECORD_PIN.search(decoy)


def test_side_record_pin_rejects_the_prints_only_decoy() -> None:
    decoy = (
        "A dry run only prints and never writes "
        "verify-results/dry-run/<attempt_id>.json."
    )
    assert not DRY_RUN_SIDE_RECORD_PIN.search(decoy)


def test_side_record_pin_rejects_the_committing_verify_decoy() -> None:
    decoy = (
        "A dry run only previews; only a committing verify writes "
        "verify-results/dry-run/<attempt_id>.json."
    )
    assert not DRY_RUN_SIDE_RECORD_PIN.search(decoy)
    assert not DRY_RUN_SIDE_RECORD_PIN.search(
        "only a committing verify writes verify-results/dry-run/<attempt_id>.json."
    )


def test_dry_run_never_canonical_pin_rejects_the_dash_decoy() -> None:
    decoy = (
        "A non\u2013dry-run verify never writes or replaces the canonical record."
    )
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(decoy)


SHIPPED_SIDE_RECORD_SENTENCES = (
    "A dry run writes only `verify-results/dry-run/<attempt_id>.json`",
    "A dry run writes its preview to `verify-results/dry-run/<attempt_id>.json` only",
)


def test_shipped_documents_satisfy_the_strengthened_side_record_pin() -> None:
    for path in REFERENCE_DOCS:
        paragraphs = _doc_paragraphs(path)
        assert any(
            SIDE_RECORD_PATH_TEXT in paragraph
            and DRY_RUN_SIDE_RECORD_PIN.search(paragraph)
            for paragraph in paragraphs
        ), f"{path.name} no longer states that a dry run writes the side record"
        for policy in (DRY_RUN_SIDE_RECORD_PIN, DRY_RUN_NEVER_CANONICAL_PIN):
            assert policy.search(_doc_text(path)), f"{path.name} lost a dry-run policy"


def test_new_side_record_pin_is_strictly_stronger_than_the_base_pin() -> None:
    for name, decoy in REVIEWER_DECOYS:
        assert not BASE_DRY_RUN_SIDE_RECORD_PIN.search(decoy), name
        assert not DRY_RUN_SIDE_RECORD_PIN.search(decoy), name

    base_accepts = (
        "A dry run only prints and never writes "
        "verify-results/dry-run/<attempt_id>.json.",
        "A dry run only previews; only a committing verify writes "
        "verify-results/dry-run/<attempt_id>.json.",
    )
    for decoy in base_accepts:
        assert BASE_DRY_RUN_SIDE_RECORD_PIN.search(decoy)
        assert not DRY_RUN_SIDE_RECORD_PIN.search(decoy)

    for sentence in SHIPPED_SIDE_RECORD_SENTENCES:
        assert BASE_DRY_RUN_SIDE_RECORD_PIN.search(sentence)
        assert DRY_RUN_SIDE_RECORD_PIN.search(sentence)


def test_changelog_crash_debris_sentence_is_corrected() -> None:
    text = _doc_text(CHANGELOG_DOC)
    assert not re.search(
        r"crash or failed replace.{0,200}?no temporary file behind",
        text,
        re.IGNORECASE,
    )
    assert re.search(
        r"hard process kill.{0,200}?\.<record>\.\*\.tmp", text, re.IGNORECASE
    )
    assert re.search(r"no reader reads", text, re.IGNORECASE)

    bullet = next(
        paragraph
        for paragraph in _doc_paragraphs(CHANGELOG_DOC)
        if "Verify records refuse uncontained state paths" in paragraph
    )
    for token in (
        "0600",
        "os.replace",
        "byte-identical",
        "failed write or replace",
        "finally",
    ):
        assert token in bullet, f"the crash-durability bullet no longer documents {token}"
    assert "cli._write_verify_result" in bullet
    assert "verify_local._write_record" in bullet
    assert "commit-local" in bullet
    assert re.search(r"says nothing about|does not cover", bullet, re.IGNORECASE)


def _verify_parser():
    parser = argparse.ArgumentParser()
    _ctx(FakeClient()).cli_commands["bundlebees"]["setup_fn"](parser)
    return parser


def test_parser_refuses_commit_local_with_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _verify_parser().parse_args(
            ["verify", ATTEMPT_ID, "--commit-local", "--dry-run"]
        )

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--commit-local" in err
    assert "--dry-run" in err

    with pytest.raises(SystemExit) as help_exit:
        _verify_parser().parse_args(["verify", ATTEMPT_ID, "--help"])
    assert help_exit.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--commit-local | --dry-run" in help_text


def test_commit_local_flag_still_parses_alone() -> None:
    args = _verify_parser().parse_args(["verify", ATTEMPT_ID, "--commit-local"])

    assert args.command == "verify"
    assert args.commit_local is True
    assert args.dry_run is False


def test_dry_run_flag_still_parses_alone() -> None:
    args = _verify_parser().parse_args(["verify", ATTEMPT_ID, "--dry-run"])

    assert args.command == "verify"
    assert args.commit_local is False
    assert args.dry_run is True


def test_dispatch_refuses_a_namespace_with_both_flags_before_any_client_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    client = FakeClient()
    args = argparse.Namespace(
        command="verify",
        attempt_id=ATTEMPT_ID,
        workdir=str(tmp_path),
        branch=BRANCH,
        tests=["tests/test_sample.py"],
        state_dir=str(state),
        kanban_comment=False,
        commit_local=True,
        dry_run=True,
    )

    rc = cli._dispatch(_ctx(client), args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "invalid_argument" in out
    assert "--commit-local" in out
    assert "--dry-run" in out
    assert client.calls == []
    assert not (state / "verify-results").exists()


def test_flags_alone_keep_their_existing_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin dispatch routing; existing tests own the record-shape guarantees.

    ``test_dry_run_green_leaves_the_official_record_byte_identical`` and
    ``test_dry_run_does_not_create_a_canonical_record`` already prove that a
    dry run alone never writes or replaces the canonical record, so this test
    only pins that the two flags alone still take their former paths.
    """

    parser = _verify_parser()
    calls: list[str] = []

    def fake_commit_local(ctx, args):
        calls.append("commit_local")
        return 0

    def fake_verify(ctx, args):
        calls.append("dry_run" if args.dry_run else "commit")
        return 0

    monkeypatch.setattr(cli, "_cmd_verify_commit_local", fake_commit_local)
    monkeypatch.setattr(cli, "_cmd_verify", fake_verify)
    ctx = _ctx(FakeClient())
    state_dir = str(tmp_path / "state")

    commit_args = parser.parse_args(
        ["verify", ATTEMPT_ID, "--commit-local", "--state-dir", state_dir]
    )
    assert cli._dispatch(ctx, commit_args) == 0
    assert calls == ["commit_local"]

    dry_args = parser.parse_args(
        ["verify", ATTEMPT_ID, "--dry-run", "--state-dir", state_dir]
    )
    assert cli._dispatch(ctx, dry_args) == 0
    assert calls == ["commit_local", "dry_run"]


def _plant_outside(state: Path, tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    results = state / "verify-results"
    results.mkdir(parents=True, exist_ok=True)
    return outside


def _plant_dry_run_symlink(state: Path, tmp_path: Path) -> Path:
    outside = _plant_outside(state, tmp_path)
    try:
        (state / "verify-results" / "dry-run").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    return outside


def test_green_dry_run_refuses_a_symlinked_side_record_directory(
    tmp_path: Path, green_sandbox: None, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    outside = _plant_dry_run_symlink(state, tmp_path)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert rc != 0
    assert "state_dir_uncontained" in output
    assert "internal_error" not in output
    assert not (state / "verify-results" / f"{ATTEMPT_ID}.json").exists()
    assert list(outside.iterdir()) == []
    assert not _side_record(state).exists()


def test_refused_dry_run_warns_when_the_side_record_is_uncontained(
    tmp_path: Path, green_sandbox: None, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    official = _seed_official(state)
    before_sha, before_mtime = _snapshot(official)
    outside = _plant_dry_run_symlink(state, tmp_path)
    client = FakeClient(_result(OUT_OF_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    captured = capsys.readouterr()
    warnings = [line for line in captured.err.splitlines() if line.startswith("warning:")]
    assert rc != 0
    assert "scope_violation" in captured.out
    assert "internal_error" not in captured.out
    assert len(warnings) == 1
    assert "state_dir_uncontained" in warnings[0]
    assert "NOT written" in warnings[0]
    assert _sha256(official) == before_sha
    assert official.stat().st_mtime_ns == before_mtime
    assert list(outside.iterdir()) == []
    assert not _side_record(state).exists()


def test_atomic_record_write_keeps_the_previous_target_when_replace_fails(
    tmp_path: Path, green_sandbox: None, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    side = _side_record(state)
    side.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"attempt_id": "att-1", "ok": true, "seeded": true}\n'
    side.write_bytes(original)
    before_sha, before_mtime = _snapshot(side)

    def fail_replace(src, dst):
        raise OSError("replace refused")

    monkeypatch.setattr(cli.os, "replace", fail_replace)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert rc != 0
    assert "internal_error" in output or "replace refused" in output
    assert _sha256(side) == before_sha
    assert side.stat().st_mtime_ns == before_mtime
    assert side.read_bytes() == original
    assert sorted(p.name for p in side.parent.iterdir()) == [side.name]


@pytest.mark.parametrize("dry_run", [False, True], ids=["canonical", "side"])
def test_containment_refusal_is_typed_for_both_record_paths(
    tmp_path: Path, dry_run: bool
) -> None:
    state = tmp_path / "state"
    outside = _plant_outside(state, tmp_path)
    try:
        if dry_run:
            (state / "verify-results" / "dry-run").symlink_to(outside, target_is_directory=True)
            relative = f"verify-results/dry-run/{ATTEMPT_ID}.json"
            escaped = outside / f"{ATTEMPT_ID}.json"
        else:
            (state / "verify-results" / f"{ATTEMPT_ID}.json").symlink_to(
                outside / "escaped.json"
            )
            relative = f"verify-results/{ATTEMPT_ID}.json"
            escaped = outside / "escaped.json"
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(cli.CliError) as excinfo:
        cli._write_verify_result(
            state, ATTEMPT_ID, ok=True, sha=None, branch=BRANCH,
            files=["src/a.py"], tests=["tests/test_sample.py"], dry_run=dry_run,
        )

    assert excinfo.value.code == "state_dir_uncontained"
    assert relative in excinfo.value.error
    assert "state dir" in excinfo.value.error.lower()
    assert "remove or repair" in excinfo.value.fix.lower()
    assert not escaped.exists()
    assert not list(state.rglob("*.tmp"))


def test_successful_verify_records_leave_no_temp_artifacts(
    tmp_path: Path, green_sandbox: None
) -> None:
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))
    argv = _verify_argv(repo, state, "--dry-run")

    assert _invoke(_ctx(client), argv) == 0
    side_dir = _side_record(state).parent
    assert sorted(p.name for p in side_dir.iterdir()) == [f"{ATTEMPT_ID}.json"]
    assert not list(state.rglob("*.tmp"))

    _git(repo, "reset", "--hard", base_sha)
    assert _invoke(_ctx(client), _verify_argv(repo, state)) == 0
    canonical = state / "verify-results" / f"{ATTEMPT_ID}.json"
    assert canonical.exists()
    assert sorted(p.name for p in side_dir.iterdir()) == [f"{ATTEMPT_ID}.json"]
    assert not list(state.rglob("*.tmp"))

