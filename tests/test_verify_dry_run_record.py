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
import sys
import unicodedata
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
DECOY_SKILL_REFERENCE_DOC = SKILL_REFERENCE_DOC
REFERENCE_DOCS = (README_DOC, CHANGELOG_DOC, SKILL_REFERENCE_DOC)

DRY_RUN_SUBJECT_SEPARATORS = (
    "- \u00a0\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d"
)
_DRY_RUN_GUARD_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf"})
_DRY_RUN_GUARD_SPLIT_SOURCE_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})
_DRY_RUN_GUARD_BOUNDARY_MARKER = "\ue000"
_DRY_RUN_GUARD_CC_MARKER = "\ue001"
_DRY_RUN_GUARD_SPLIT_MARKER = _DRY_RUN_GUARD_CC_MARKER
_DRY_RUN_GUARD_SEPARATOR_RUN = re.compile(
    rf"[{re.escape(DRY_RUN_SUBJECT_SEPARATORS)}]+"
)
_DRY_RUN_GUARD_NON_WORD_RUN = re.compile(
    rf"(?:[^\w{re.escape(_DRY_RUN_GUARD_BOUNDARY_MARKER)}"
    rf"{re.escape(_DRY_RUN_GUARD_CC_MARKER)}]|_)+"
)
_DRY_RUN_GUARD_DOT_BOUNDARY = re.compile(r"(?<=\w)\.(?= )")
_DRY_RUN_GUARD_SLASH_BOUNDARY = re.compile(r"(?<=\w)/(?=\w)")
_DRY_RUN_GUARD_SPLIT_NON = re.compile(
    rf"\b(?:n[{re.escape(_DRY_RUN_GUARD_SPLIT_MARKER)}]*on"
    rf"|no[{re.escape(_DRY_RUN_GUARD_SPLIT_MARKER)}]*n)\b",
    re.IGNORECASE,
)


def _dry_run_guard_nfkc_fold_contains_whitespace(character: str) -> bool:
    return any(
        folded.isspace()
        or unicodedata.category(folded)
        in _DRY_RUN_GUARD_SPLIT_SOURCE_CATEGORIES
        for folded in unicodedata.normalize("NFKC", character)
    )


def _map_dry_run_guard_split_sources(text: str) -> str:
    mapped: list[str] = []
    for character in text:
        if character in (
            _DRY_RUN_GUARD_BOUNDARY_MARKER,
            _DRY_RUN_GUARD_CC_MARKER,
        ):
            mapped.append(_DRY_RUN_GUARD_SPLIT_MARKER)
            continue
        if character != " " and (
            unicodedata.category(character)
            in _DRY_RUN_GUARD_SPLIT_SOURCE_CATEGORIES
            or _dry_run_guard_nfkc_fold_contains_whitespace(character)
        ):
            mapped.append(_DRY_RUN_GUARD_SPLIT_MARKER)
            continue
        mapped.append(character)
    return "".join(mapped)


def _normalize_dry_run_guard_text(text: str) -> str:
    text = _map_dry_run_guard_split_sources(text)
    text = _DRY_RUN_GUARD_DOT_BOUNDARY.sub(
        _DRY_RUN_GUARD_BOUNDARY_MARKER, text
    )
    text = _DRY_RUN_GUARD_SLASH_BOUNDARY.sub(
        _DRY_RUN_GUARD_BOUNDARY_MARKER, text
    )
    normalized = unicodedata.normalize("NFKC", text)
    guarded: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category in _DRY_RUN_GUARD_STRIPPED_CATEGORIES:
            if category == "Cc" and character.isspace():
                guarded.append(_DRY_RUN_GUARD_SPLIT_MARKER)
            continue
        guarded.append(character)
    normalized = "".join(guarded)
    normalized = _DRY_RUN_GUARD_NON_WORD_RUN.sub(" ", normalized)
    normalized = _DRY_RUN_GUARD_SPLIT_NON.sub("non", normalized)
    normalized = normalized.replace(_DRY_RUN_GUARD_SPLIT_MARKER, " ")
    normalized = _DRY_RUN_GUARD_SEPARATOR_RUN.sub(" ", normalized)
    normalized = " ".join(normalized.split())
    return " ".join(normalized.split())


class _NormalizedPin:
    def __init__(self, pattern: str, flags: int = 0) -> None:
        self.pattern = pattern
        self._regex = re.compile(pattern, flags)

    def search(self, text: str) -> re.Match[str] | None:
        return self._regex.search(_normalize_dry_run_guard_text(text))


DRY_RUN_NEVER_CANONICAL_PIN = _NormalizedPin(
    r"(?<!non)(?<!non )"
    r"dry\s+runs?\b.{0,240}?"
    r"never write[s]? or replace[s]?\b.{0,80}?canonical record",
    re.IGNORECASE,
)
EXPECTED_SIDE_RECORD_CLAUSES = {
    "README.md": (
        "A dry run writes its preview to `verify-results/dry-run/<attempt_id>.json` "
        "only and never writes or replaces the canonical record"
    ),
    "CHANGELOG.md": (
        "A dry run writes its preview to `verify-results/dry-run/<attempt_id>.json` "
        "only, with `dry_run: true`, and never writes or replaces the canonical record"
    ),
    "verify-and-deploy.md": (
        "A dry run writes its preview to `verify-results/dry-run/<attempt_id>.json` "
        "only and never writes or replaces the canonical record"
    ),
}
METRICS_DISCLOSURE = (
    "Once its lease loads, a dry run also appends a `verify` event to "
    "`metrics/<run_id>.jsonl`."
)
UNQUALIFIED_METRICS_DISCLOSURES = (
    "It also appends a `verify` event to `metrics/<run_id>.jsonl`.",
    "It also appends a `verify` event to metrics/<run_id>.jsonl:",
)
UNQUALIFIED_METRICS_DISCLOSURE_PIN = re.compile(
    r"It also appends a `?verify`?\s+event to\s+`?metrics/<run_id>\.jsonl",
    re.IGNORECASE,
)
OVERBROAD_DRY_RUN_FORMS = (
    "Dry-run evidence is written to",
    "Dry-run evidence goes to",
    "now writes only `verify-results/dry-run/<attempt_id>.json`",
    "writes only `verify-results/dry-run/<attempt_id>.json`",
    "writes only verify-results/dry-run/<attempt_id>.json",
    "Dry runs write only `verify-results/dry-run/<attempt_id>.json`",
    "Dry run writes only `verify-results/dry-run/<attempt_id>.json`",
    "Dry runs write only: `verify-results/dry-run/<attempt_id>.json`",
    "Dry runs write only to `verify-results/dry-run/<attempt_id>.json`",
)
CHANGELOG_CRASH_SPLIT = (
    "A failed replace in `cli._write_verify_result` leaves the previous record "
    "byte-identical and removes the temporary file in a `finally` block. A crash "
    "can leave the temporary file behind: the previous record stays byte-identical "
    "until the atomic rename."
)
CHANGELOG_CLEANUP_SCOPE = (
    "That cleanup covers the canonical and dry-run writes that go through "
    "`cli._write_verify_result`, not the canonical write in "
    "`verify_local._write_record`, which has no `try`/`finally` cleanup."
)
CHANGELOG_ATOMIC_WRITE = (
    "Both record writes serialise the unchanged JSON bytes to a same-directory 0600 "
    "temporary file, fsync it, and `os.replace` it"
)
DRY_RUN_NEVER_CANONICAL_SUBJECTS = (
    "A dry run never writes or replaces the canonical record.",
    "Dry runs never write or replace the canonical record.",
    "A dry\u00a0run never writes or replaces the canonical record.",
    "A dry-run never writes or replaces the canonical record.",
    "A dry\u2010run never writes or replaces the canonical record.",
    "A dry\u2011run never writes or replaces the canonical record.",
    "A dry\u2013run never writes or replaces the canonical record.",
    "A dry\u2014run never writes or replaces the canonical record.",
    "A dry  run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_EXTENDED_SUBJECTS = (
    "A dry\u2012run never writes or replaces the canonical record.",
    "A dry\u2015run never writes or replaces the canonical record.",
    "A dry\u2212run never writes or replaces the canonical record.",
    "A dry\uff0drun never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_CC_SUBJECTS = (
    "A dry\trun never writes or replaces the canonical record.",
    "A dry\nrun never writes or replaces the canonical record.",
    "A dry\rrun never writes or replaces the canonical record.",
    "A dry\vrun never writes or replaces the canonical record.",
    "A dry\frun never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_CC_SUBJECT_IDS = (
    "tab-u0009",
    "line-feed-u000a",
    "carriage-return-u000d",
    "vertical-tab-u000b",
    "form-feed-u000c",
)
DRY_RUN_NEVER_CANONICAL_SENTENCE_BOUNDARY_SUBJECTS = (
    "The canon. Dry run never writes or replaces the canonical record.",
    "Their value is non. Dry run never writes or replaces the canonical record.",
    "Their value is non/dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_SENTENCE_BOUNDARY_IDS = (
    "after-canon-terminator",
    "after-non-terminator",
    "after-non-slash",
)
DRY_RUN_NEVER_CANONICAL_DECOYS = (
    "A non-dry-run never writes or replaces the canonical record.",
    "A non dry run never writes or replaces the canonical record.",
    "A non\u00a0dry run never writes or replaces the canonical record.",
    "A non\u2010dry run never writes or replaces the canonical record.",
    "A non\u2011dry run never writes or replaces the canonical record.",
    "A non\u2013dry run never writes or replaces the canonical record.",
    "A non\u2014dry run never writes or replaces the canonical record.",
    "A non\u2012dry run never writes or replaces the canonical record.",
    "A non\u2015dry run never writes or replaces the canonical record.",
    "A non\u2212dry run never writes or replaces the canonical record.",
    "A non\uff0ddry run never writes or replaces the canonical record.",
    "A non  dry run never writes or replaces the canonical record.",
    "A non\tdry run never writes or replaces the canonical record.",
    "A non   dry run never writes or replaces the canonical record.",
    "A non\u200bdry run never writes or replaces the canonical record.",
    "A non\u00addry run never writes or replaces the canonical record.",
    "A non\u2018dry run never writes or replaces the canonical record.",
    "A non\u2019dry run never writes or replaces the canonical record.",
    "A non\u2043dry run never writes or replaces the canonical record.",
    "A non\ufe58dry run never writes or replaces the canonical record.",
    "A non\ufe63dry run never writes or replaces the canonical record.",
    "A non\uff3edry run never writes or replaces the canonical record.",
    "A non - dry run never writes or replaces the canonical record.",
    "A non- dry run never writes or replaces the canonical record.",
    "A non--dry run never writes or replaces the canonical record.",
    "A non\u2013\u00a0dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_BOUNDARY_DECOYS = (
    "A non!dry run never writes or replaces the canonical record.",
    "A non! dry run never writes or replaces the canonical record.",
    "A non !dry run never writes or replaces the canonical record.",
    "A non?dry run never writes or replaces the canonical record.",
    "A non? dry run never writes or replaces the canonical record.",
    "A non;dry run never writes or replaces the canonical record.",
    "A non; dry run never writes or replaces the canonical record.",
    "A non / dry run never writes or replaces the canonical record.",
    "A non/ dry run never writes or replaces the canonical record.",
    "A non..dry run never writes or replaces the canonical record.",
    "A non./dry run never writes or replaces the canonical record.",
    "A non/.dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_BOUNDARY_DECOY_IDS = (
    "bang-tight",
    "bang-space",
    "space-bang",
    "question-tight",
    "question-space",
    "semicolon-tight",
    "semicolon-space",
    "spaced-slash",
    "slash-space",
    "double-dot",
    "dot-slash",
    "slash-dot",
)
DRY_RUN_NEVER_CANONICAL_SPLIT_DECOYS = (
    "A no\tn dry run never writes or replaces the canonical record.",
    "A n\ton dry run never writes or replaces the canonical record.",
    "A no\nn dry run never writes or replaces the canonical record.",
    "A no\rn dry run never writes or replaces the canonical record.",
    "A no\vn dry run never writes or replaces the canonical record.",
    "A no\fn dry run never writes or replaces the canonical record.",
    "A n\non dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_SPLIT_DECOY_IDS = (
    "no-tab-n",
    "n-tab-on",
    "no-line-feed-n",
    "no-carriage-return-n",
    "no-vertical-tab-n",
    "no-form-feed-n",
    "n-line-feed-on",
)
DRY_RUN_NEVER_CANONICAL_ZS_SPLIT_DECOYS = (
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no　n dry run never writes or replaces the canonical record.",
    "A n　on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A no n dry run never writes or replaces the canonical record.",
    "A n on dry run never writes or replaces the canonical record.",
    "A non dry run never writes or replaces the canonical record.",
    "A non dry run never writes or replaces the canonical record.",
    "A non dry run never writes or replaces the canonical record.",
    "A non dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_ZS_SPLIT_DECOY_IDS = (
    "no-nbsp-u00a0",
    "n-on-nbsp-u00a0",
    "no-ogham-space-u1680",
    "n-on-ogham-space-u1680",
    "no-en-quad-u2000",
    "n-on-en-quad-u2000",
    "no-em-space-u2003",
    "n-on-em-space-u2003",
    "no-thin-space-u2009",
    "n-on-thin-space-u2009",
    "no-figure-space-u2007",
    "n-on-figure-space-u2007",
    "no-narrow-nbsp-u202f",
    "n-on-narrow-nbsp-u202f",
    "no-ideographic-space-u3000",
    "n-on-ideographic-space-u3000",
    "no-line-separator-u2028",
    "n-on-line-separator-u2028",
    "no-paragraph-separator-u2029",
    "n-on-paragraph-separator-u2029",
    "no-literal-u-e000",
    "n-on-literal-u-e000",
    "no-literal-u-e001",
    "n-on-literal-u-e001",
)
DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_CODEPOINTS = (
    "\u00a8",
    "\u00af",
    "\u00b4",
    "\u00b8",
    "\u02d8",
    "\u02d9",
    "\u02da",
    "\u02db",
    "\u02dc",
    "\u02dd",
    "\u037a",
    "\u0384",
    "\u0385",
    "\u1fbd",
    "\u1fbf",
    "\u1fc0",
    "\u1fc1",
    "\u1fcd",
    "\u1fce",
    "\u1fcf",
    "\u1fdd",
    "\u1fde",
    "\u1fdf",
    "\u1fed",
    "\u1fee",
    "\u1ffd",
    "\u1ffe",
    "\u2017",
    "\u203e",
    "\u309b",
    "\u309c",
    "\ufc5e",
    "\ufc5f",
    "\ufc60",
    "\ufc61",
    "\ufc62",
    "\ufc63",
    "\ufdfa",
    "\ufdfb",
    "\ufe49",
    "\ufe4a",
    "\ufe4b",
    "\ufe4c",
    "\ufe70",
    "\ufe72",
    "\ufe74",
    "\ufe76",
    "\ufe78",
    "\ufe7a",
    "\ufe7c",
    "\ufe7e",
    "\uffe3",
)
DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_DECOYS = tuple(
    f"A {left}{codepoint}{right} dry run never writes or replaces the canonical record."
    for codepoint in DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_CODEPOINTS
    for left, right in (("no", "n"), ("n", "on"))
)
DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_DECOY_IDS = tuple(
    f"{shape}-u{ord(codepoint):04x}"
    for codepoint in DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_CODEPOINTS
    for shape in ("no-n", "n-on")
)
DRY_RUN_NEVER_CANONICAL_ZS_SPACE_CODEPOINTS = (
    "\u00a0",
    "\u2000",
    "\u2001",
    "\u2002",
    "\u2003",
    "\u2004",
    "\u2005",
    "\u2006",
    "\u2007",
    "\u2008",
    "\u2009",
    "\u200a",
    "\u202f",
    "\u205f",
    "\u3000",
)
DRY_RUN_NEVER_CANONICAL_ZS_SPACE_DECOYS = tuple(
    f"A {left}{codepoint}{right} dry run never writes or replaces the canonical record."
    for codepoint in DRY_RUN_NEVER_CANONICAL_ZS_SPACE_CODEPOINTS
    for left, right in (("no", "n"), ("n", "on"))
)
DRY_RUN_NEVER_CANONICAL_ZS_SPACE_DECOY_IDS = tuple(
    f"{shape}-u{ord(codepoint):04x}"
    for codepoint in DRY_RUN_NEVER_CANONICAL_ZS_SPACE_CODEPOINTS
    for shape in ("no-n", "n-on")
)
DRY_RUN_NEVER_CANONICAL_NFKC_NON_SPACE_FOLD_CONTROLS = (
    ("\ufb03", "noffin"),
    ("\u00bd", "no1/2n"),
    ("\uff21", "noAn"),
    ("\u2075", "no5n"),
    ("\u33a1", "nom2n"),
)
DRY_RUN_NEVER_CANONICAL_NFKC_NON_SPACE_FOLD_CONTROL_IDS = (
    "ligature-ffi-u-fb03",
    "vulgar-half-u-00bd",
    "fullwidth-a-u-ff21",
    "superscript-five-u-2075",
    "squared-m2-u-33a1",
)
DRY_RUN_NEVER_CANONICAL_LITERAL_SENTINEL_DECOYS = (
    "A non\ue000dry run never writes or replaces the canonical record.",
    "A non\ue000 dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_LITERAL_SENTINEL_DECOY_IDS = (
    "literal-sentinel-tight",
    "literal-sentinel-space",
)
DRY_RUN_NEVER_CANONICAL_NONASCII_SPACE_DECOYS = (
    "A non.\u00a0dry run never writes or replaces the canonical record.",
    "A non.\u2003dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_NONASCII_SPACE_DECOY_IDS = (
    "dot-nbsp",
    "dot-em-space",
)
DRY_RUN_NEVER_CANONICAL_DOT_TAB_DECOYS = (
    "A non.\tdry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_SPLIT_POSITIVES = (
    "Use -n on dry runs; they never write or replace the canonical record.",
    "Pass `-n` on dry runs and they never write or replace the canonical record.",
    "There is no N dry run that never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_SPLIT_POSITIVE_IDS = (
    "hyphen-n-on",
    "backticked-n-on",
    "no-N",
)
DRY_RUN_NEVER_CANONICAL_SPLIT_CONTROLS = (
    "There is no normal dry run that never writes or replaces the canonical record.",
    "There is no new dry run that never writes or replaces the canonical record.",
    "There is no-notch dry run that never writes or replaces the canonical record.",
    "Turn on dry runs; they never write or replace the canonical record.",
    "Run on dry runs; they never write or replace the canonical record.",
    "An on dry run never writes or replaces the canonical record.",
)
DRY_RUN_NEVER_CANONICAL_SPLIT_CONTROL_IDS = (
    "no-normal",
    "no-new",
    "no-notch",
    "turn-on",
    "run-on",
    "an-on",
)
DECOY_CORPUS = (
    "A non-dry-run verify writes `verify-results/<attempt_id>.json` whether it "
    "passes or refuses; it never writes or replaces the dry-run canonical record at "
    "`verify-results/dry-run/<attempt_id>.json`.",
    *DRY_RUN_NEVER_CANONICAL_DECOYS,
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


def _doc_text_without_comment_markers(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    stripped = "\n".join(re.sub(r"^\s*#\s?", "", line) for line in raw.splitlines())
    return " ".join(stripped.split())


def _doc_paragraphs(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", raw) if block.strip()]


def _normalized_overbroad_text(text: str) -> str:
    normalized = text.casefold().replace("`", "").replace("*", "")
    normalized = re.sub(
        r"[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\uff0d]", " ", normalized
    )
    return " ".join(normalized.split())


_DRY_RUN_CELL_ALIASES = ("--dry-run",)
_DRY_RUN_CELL_SUFFIX = re.compile(r"^(?:[\s\W_]|\([^()]*\)|\[[^\[\]]*\])*$")


def _clean_flag_cell(cell: str) -> str:
    cleaned = cell.replace("`", "").replace("*", "").casefold()
    return " ".join(cleaned.split())


def _cell_declares_dry_run(cell: str) -> bool:
    cleaned = _clean_flag_cell(cell)
    if any(
        part.strip() in _DRY_RUN_CELL_ALIASES
        for part in cleaned.split(",")
    ):
        return True
    if not cleaned.startswith("--dry-run"):
        return False
    return bool(_DRY_RUN_CELL_SUFFIX.fullmatch(cleaned[len("--dry-run"):]))


def _cell_is_flag_token(cell: str) -> bool:
    return bool(re.fullmatch(r"-{1,2}[a-z][\w-]*", _clean_flag_cell(cell)))


def _cell_mentions_dry_run_flag(cell: str) -> bool:
    return bool(
        re.search(r"(?<![\w-])--dry-run(?![\w-])", _clean_flag_cell(cell))
    )


def _skill_reference_dry_run_row(document: str) -> str:
    first_cell_rows: list[str] = []
    non_first_cell_rows: list[tuple[str, str]] = []
    for line in document.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = stripped.split("|")[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        if not cells:
            continue
        if _cell_declares_dry_run(cells[0]):
            first_cell_rows.append(line)
        elif any(_cell_mentions_dry_run_flag(cell) for cell in cells[1:]):
            non_first_cell_rows.append((line, cells[0]))
    if not first_cell_rows:
        if any(
            not _cell_is_flag_token(first_cell)
            for _line, first_cell in non_first_cell_rows
        ):
            raise AssertionError(
                "the skill reference has a --dry-run flag row but the flag is not "
                "in the first cell; the --dry-run flag must be in the first cell"
            )
        raise AssertionError(
            "the skill reference is missing its --dry-run flag row"
        )
    assert len(first_cell_rows) == 1, (
        "the skill reference has more than one --dry-run flag row (duplicate)"
    )
    return first_cell_rows[0]


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
            side_record in paragraph and DRY_RUN_NEVER_CANONICAL_PIN.search(paragraph)
            for paragraph in paragraphs
        ), f"{path.name} does not say a dry run never writes or replaces the canonical record"

    @pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
    def test_document_pins_its_exact_dry_run_side_record_clause(self, path: Path) -> None:
        expected = EXPECTED_SIDE_RECORD_CLAUSES[path.name]
        assert expected in _doc_text(path), (
            f"{path.name} does not contain the exact scoped dry-run side-record clause"
        )
        assert any(expected in paragraph for paragraph in _doc_paragraphs(path)), (
            f"{path.name} does not keep the exact scoped dry-run side-record clause "
            "inside one paragraph"
        )

    @pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
    def test_document_states_the_metrics_disclosure(self, path: Path) -> None:
        assert METRICS_DISCLOSURE in _doc_text(path), (
            f"{path.name} does not carry the qualified metrics disclosure"
        )

    @pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
    def test_unqualified_metrics_disclosure_is_absent(self, path: Path) -> None:
        text = _doc_text(path)
        for unqualified in UNQUALIFIED_METRICS_DISCLOSURES:
            assert unqualified not in text, (
                f"{path.name} still carries the unqualified metrics disclosure"
            )
        normalized = _doc_text_without_comment_markers(path)
        assert not UNQUALIFIED_METRICS_DISCLOSURE_PIN.search(normalized), (
            f"{path.name} still carries the unqualified metrics disclosure "
            "across comment markers or line wrapping"
        )
        if path == README_DOC:
            raw = path.read_text(encoding="utf-8")
            assert not (
                "It also appends a `verify`" in raw
                and "event to metrics/<run_id>.jsonl" in raw
            ), "README.md still carries the unqualified metrics sentence in the bash block"

    @pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
    def test_document_rejects_overbroad_dry_run_forms(self, path: Path) -> None:
        text = _normalized_overbroad_text(_doc_text(path))
        for form in OVERBROAD_DRY_RUN_FORMS:
            assert _normalized_overbroad_text(form) not in text, (
                f"{path.name} still uses the overbroad dry-run form: {form}"
            )

    def test_skill_reference_dry_run_flag_row_scopes_the_side_record(self) -> None:
        row = _skill_reference_dry_run_row(
            SKILL_REFERENCE_DOC.read_text(encoding="utf-8")
        )
        assert EXPECTED_SIDE_RECORD_CLAUSES["verify-and-deploy.md"] in row, (
            "the --dry-run flag row does not state the exact scoped dry-run clause"
        )
        assert "Dry-run evidence goes to" not in row, (
            "the --dry-run flag row still carries the overbroad dry-run form"
        )
        normalized_row = _normalized_overbroad_text(row)
        for form in OVERBROAD_DRY_RUN_FORMS:
            assert _normalized_overbroad_text(form) not in normalized_row, (
                f"the --dry-run flag row still carries the overbroad dry-run form: {form}"
            )

    def test_changelog_splits_the_crash_and_failed_replace_sentence(self) -> None:
        text = _doc_text(CHANGELOG_DOC)
        assert CHANGELOG_CRASH_SPLIT in text, (
            "CHANGELOG.md does not split the crash and failed-replace sentence"
        )
        assert CHANGELOG_CLEANUP_SCOPE in text, (
            "CHANGELOG.md does not scope the cleanup to cli._write_verify_result"
        )
        assert CHANGELOG_ATOMIC_WRITE in text, (
            "CHANGELOG.md does not state the atomic write without the chmod conjunct"
        )
        assert "A crash or failed replace" not in text
        assert "no temporary file behind" not in text
        assert "fsync, chmod" not in text

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
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(decoy)


@pytest.mark.parametrize(
    "subject", DRY_RUN_NEVER_CANONICAL_SUBJECTS, ids=range(len(DRY_RUN_NEVER_CANONICAL_SUBJECTS))
)
def test_never_canonical_pin_accepts_the_subject_table(subject: str) -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_EXTENDED_SUBJECTS,
    ids=["figure-dash", "horizontal-bar", "minus-sign", "fullwidth-hyphen"],
)
def test_never_canonical_pin_accepts_the_extended_separator_subjects(
    subject: str,
) -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_CC_SUBJECTS,
    ids=DRY_RUN_NEVER_CANONICAL_CC_SUBJECT_IDS,
)
def test_never_canonical_pin_accepts_cc_separated_subjects(subject: str) -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_SENTENCE_BOUNDARY_SUBJECTS,
    ids=DRY_RUN_NEVER_CANONICAL_SENTENCE_BOUNDARY_IDS,
)
def test_never_canonical_pin_accepts_subjects_after_sentence_boundaries(
    subject: str,
) -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject", DRY_RUN_NEVER_CANONICAL_DECOYS, ids=range(len(DRY_RUN_NEVER_CANONICAL_DECOYS))
)
def test_never_canonical_pin_rejects_the_negation_decoys(subject: str) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_BOUNDARY_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_BOUNDARY_DECOY_IDS,
)
def test_never_canonical_pin_rejects_the_boundary_overreach_decoys(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_SPLIT_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_SPLIT_DECOY_IDS,
)
def test_never_canonical_pin_rejects_the_split_negation_decoys(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_ZS_SPLIT_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_ZS_SPLIT_DECOY_IDS,
)
def test_never_canonical_pin_rejects_non_ascii_whitespace_split_decoys(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_NFKC_SPACE_FOLD_DECOY_IDS,
)
def test_never_canonical_pin_rejects_nfkc_space_fold_decoys(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_ZS_SPACE_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_ZS_SPACE_DECOY_IDS,
)
def test_never_canonical_pin_rejects_zs_space_regression_decoys(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    ("codepoint", "expected"),
    DRY_RUN_NEVER_CANONICAL_NFKC_NON_SPACE_FOLD_CONTROLS,
    ids=DRY_RUN_NEVER_CANONICAL_NFKC_NON_SPACE_FOLD_CONTROL_IDS,
)
def test_never_canonical_split_sources_ignore_non_space_nfkc_folds(
    codepoint: str,
    expected: str,
) -> None:
    subject = f"no{codepoint}n"
    mapped = _map_dry_run_guard_split_sources(subject)
    assert mapped == subject, repr(mapped)
    assert _DRY_RUN_GUARD_SPLIT_MARKER not in mapped
    assert not any(
        folded.isspace()
        or unicodedata.category(folded)
        in _DRY_RUN_GUARD_SPLIT_SOURCE_CATEGORIES
        for folded in unicodedata.normalize("NFKC", codepoint)
    ), f"U+{ord(codepoint):04X} folds to whitespace"
    folded = unicodedata.normalize("NFKC", mapped).replace("\u2044", "/")
    assert folded == expected, repr(folded)
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(
        f"A {subject} dry run never writes or replaces the canonical record."
    ), subject


def test_never_canonical_pin_accepts_a_ligature_fold_subject() -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(
        "A no\ufb03n dry run never writes or replaces the canonical record."
    )


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_LITERAL_SENTINEL_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_LITERAL_SENTINEL_DECOY_IDS,
)
def test_never_canonical_pin_rejects_literal_boundary_sentinel_decoys(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_NONASCII_SPACE_DECOYS,
    ids=DRY_RUN_NEVER_CANONICAL_NONASCII_SPACE_DECOY_IDS,
)
def test_never_canonical_pin_rejects_a_dot_before_non_ascii_space(
    subject: str,
) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_DOT_TAB_DECOYS,
    ids=("dot-tab",),
)
def test_never_canonical_pin_rejects_a_dot_before_tab(subject: str) -> None:
    assert not DRY_RUN_NEVER_CANONICAL_PIN.search(subject), repr(subject)


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_SPLIT_POSITIVES,
    ids=DRY_RUN_NEVER_CANONICAL_SPLIT_POSITIVE_IDS,
)
def test_never_canonical_pin_accepts_split_negation_positives(
    subject: str,
) -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize(
    "subject",
    DRY_RUN_NEVER_CANONICAL_SPLIT_CONTROLS,
    ids=DRY_RUN_NEVER_CANONICAL_SPLIT_CONTROL_IDS,
)
def test_never_canonical_pin_accepts_split_negation_controls(
    subject: str,
) -> None:
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(subject), subject


@pytest.mark.parametrize("path", REFERENCE_DOCS, ids=lambda path: path.name)
def test_shipped_docs_contain_no_literal_guard_markers(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    assert "\ue000" not in raw, f"{path.name} contains a literal U+E000"
    assert "\ue001" not in raw, f"{path.name} contains a literal U+E001"


def test_the_dead_sentence_boundary_constant_is_gone() -> None:
    module = sys.modules[__name__]
    removed_name = "_DRY_RUN_GUARD_" + "SENTENCE_BOUNDARY_CHARACTERS"
    assert not hasattr(module, removed_name)
    source = Path(__file__).read_text(encoding="utf-8")
    assert removed_name not in source


def test_dry_run_decoy_corpus_is_rejected_and_the_changelog_clause_matches() -> None:
    for decoy in DECOY_CORPUS:
        assert not DRY_RUN_NEVER_CANONICAL_PIN.search(decoy), decoy
    assert DRY_RUN_NEVER_CANONICAL_PIN.search(
        EXPECTED_SIDE_RECORD_CLAUSES["CHANGELOG.md"]
    ), "the new CHANGELOG clause must still match the canonical-record pin"


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


_GOOD_DRY_RUN_ROW = (
    "| `--dry-run` | Apply the patch and run tests, then stop before any push. "
    "A dry run writes its preview to `verify-results/dry-run/<attempt_id>.json` "
    "only and never writes or replaces the canonical record. "
    "Cannot be combined with `--commit-local`; the pair is refused rather than interpreted. |"
)


def _run_skill_reference_row_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, document: str
) -> None:
    path = tmp_path / "verify-and-deploy.md"
    path.write_text(document, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SKILL_REFERENCE_DOC", path)
    assert sys.modules[__name__].SKILL_REFERENCE_DOC == path, (
        "the row pin harness must redirect the SKILL_REFERENCE_DOC global that "
        "the shipped assertion reads"
    )
    TestVerifyRecordDocs().test_skill_reference_dry_run_flag_row_scopes_the_side_record()


def _row_with_suffix(suffix: str) -> str:
    return _GOOD_DRY_RUN_ROW[: -len(" |")] + f" {suffix} |"


def _row_with_first_cell(first_cell: str) -> str:
    return _GOOD_DRY_RUN_ROW.replace("| `--dry-run` |", f"|{first_cell}|", 1)


def test_skill_reference_row_pin_accepts_a_reformatted_flag_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _GOOD_DRY_RUN_ROW.replace("| `--dry-run` |", "|  `--dry-run` |", 1)
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_rejects_plural_write_only_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_suffix(
        "Dry runs write only `verify-results/dry-run/<attempt_id>.json`."
    )
    with pytest.raises(AssertionError, match="overbroad"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_rejects_the_evidence_goes_to_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_suffix(
        "Dry-run evidence goes to `verify-results/dry-run/<attempt_id>.json`."
    )
    with pytest.raises(AssertionError, match="overbroad"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_rejects_a_missing_flag_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = (
        "# Verify\n\n"
        "| Flag | Meaning |\n"
        "|---|---|\n"
        "| `--commit-local` | Verify in an isolated worktree. |\n"
    )
    with pytest.raises(AssertionError, match="missing"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, document)


def test_skill_reference_row_pin_rejects_duplicate_flag_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = f"# Verify\n\n{_GOOD_DRY_RUN_ROW}\n{_GOOD_DRY_RUN_ROW}\n"
    with pytest.raises(AssertionError, match="duplicate"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, document)


@pytest.mark.parametrize(
    "suffix",
    [
        "Dry runs write only verify-results/dry-run/<attempt_id>.json.",
        "dry runs write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry  runs write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry-runs write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry runs write only  `verify-results/dry-run/<attempt_id>.json`.",
    ],
    ids=[
        "plural-unbackticked",
        "lowercase",
        "double-space",
        "hyphenated-plural",
        "double-space-before-backtick",
    ],
)
def test_skill_reference_row_pin_rejects_residual_write_only_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    row = _row_with_suffix(suffix)
    with pytest.raises(AssertionError, match="overbroad"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_accepts_a_bold_flag_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_first_cell(" **`--dry-run`** ")
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


@pytest.mark.parametrize("first_cell", ["--dry-run"], ids=["long-alias"])
def test_skill_reference_row_pin_accepts_an_alias_flag_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, first_cell: str
) -> None:
    row = _row_with_first_cell(f" {first_cell} ")
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_accepts_an_uppercase_flag_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_first_cell(" `--Dry-Run` ")
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


@pytest.mark.parametrize(
    "first_cell",
    ["  `--dry-run` ", "\t`--dry-run`\t", "`--dry-run`"],
    ids=["two-spaces", "tab", "zero-spaces"],
)
def test_skill_reference_row_pin_accepts_whitespace_run_flag_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, first_cell: str
) -> None:
    row = _row_with_first_cell(first_cell)
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_reports_a_non_first_cell_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = (
        "# Verify\n\n"
        "| Flag | Meaning |\n"
        "|---|---|\n"
        "| Apply and run tests | `--dry-run` stops before any push. |\n"
    )
    with pytest.raises(AssertionError, match="first cell"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, document)


def test_skill_reference_row_pin_negative_control_decoy_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "verify-and-deploy.md"
    row = _row_with_suffix(
        "Dry runs write only `verify-results/dry-run/<attempt_id>.json`."
    )
    path.write_text(f"# Verify\n\n{row}\n", encoding="utf-8")
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "DECOY_SKILL_REFERENCE_DOC", path)
    assert module.DECOY_SKILL_REFERENCE_DOC == path
    assert module.SKILL_REFERENCE_DOC != path
    with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
        with pytest.raises(AssertionError, match="overbroad"):
            TestVerifyRecordDocs().test_skill_reference_dry_run_flag_row_scopes_the_side_record()


def _first_cell_of_row(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return ""
    cells = stripped.split("|")[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return cells[0] if cells else ""


def test_shipped_skill_reference_without_its_dry_run_row_reports_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = SKILL_REFERENCE_DOC.read_text(encoding="utf-8")
    lines = document.splitlines()
    kept = [
        line
        for line in lines
        if not _cell_declares_dry_run(_first_cell_of_row(line))
    ]
    assert len(kept) == len(lines) - 1
    with pytest.raises(AssertionError, match="missing"):
        _run_skill_reference_row_pin(
            monkeypatch, tmp_path, "\n".join(kept) + "\n"
        )


@pytest.mark.parametrize(
    "suffix",
    [
        "Dry\u2012runs write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry\u2015runs write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry\uff0druns write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry\u2212runs write only `verify-results/dry-run/<attempt_id>.json`.",
        "Dry runs write only: `verify-results/dry-run/<attempt_id>.json`.",
        "Dry runs write only to `verify-results/dry-run/<attempt_id>.json`.",
    ],
    ids=[
        "figure-dash",
        "horizontal-bar",
        "fullwidth-hyphen",
        "minus-sign",
        "colon-form",
        "write-only-to",
    ],
)
def test_skill_reference_row_pin_rejects_extended_overbroad_forms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, suffix: str
) -> None:
    row = _row_with_suffix(suffix)
    with pytest.raises(AssertionError, match="overbroad"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_rejects_a_bare_short_alias_flag_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_first_cell(" -n ")
    with pytest.raises(AssertionError, match="missing"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_accepts_a_combined_alias_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_first_cell(" `--dry-run`, `-n` ")
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


@pytest.mark.parametrize(
    "first_cell",
    [" `--dry-run` (preview) ", " `--dry-run`, ", " **`--dry-run`** "],
    ids=["parenthetical-note", "trailing-punctuation", "bold-marker"],
)
def test_skill_reference_row_pin_accepts_decorated_flag_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, first_cell: str
) -> None:
    row = _row_with_first_cell(first_cell)
    _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_rejects_a_prose_flag_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_first_cell(" Run with --dry-run to preview ")
    with pytest.raises(AssertionError, match="missing"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")


def test_skill_reference_row_pin_rejects_prose_letters_after_a_flag_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _row_with_first_cell(" `--dry-run` use this to preview ")
    with pytest.raises(AssertionError, match="missing"):
        _run_skill_reference_row_pin(monkeypatch, tmp_path, f"# Verify\n\n{row}\n")

