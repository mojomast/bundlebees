"""Approved specification: verify records carry repo, slice, PR and contract provenance.

The merge lane treats a verify record as evidence only for the slice and
contract it was written for: the record's ``repo``, ``slice`` and
``contract_sha256`` must equal the frozen base lease that classified the pull
request, and a ``pr`` it names must be the pull request under test.  The writer
therefore stamps the binding trio from dispatch-time truth and the pull request
whenever one is known, and refuses to write a slice record it cannot bind -- a
half-attributed record reads as evidence while belonging to nothing.

These tests are the approved specification.  The implementation is
``bundlebees_plugin/verify_provenance.py`` plus the provenance plumbing in
``bundlebees_plugin/cli.py``; a slice record is written through
``cli._write_verify_result(..., provenance=..., require_provenance=True)``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import cli, contract, verify_provenance
from bundlebees_plugin.cli import CliError

REPO = "mojomast/bundlebees"
DIGEST = "58560a1db87bf7e5cdf8690e31301a3bfbb9bf28fb41cb302d42d034730ad9af"


def contract_body(**overrides):
    """A minimal valid acdd/1 contract body for the writer slice."""
    body = {
        "contract_version": "acdd/1",
        "id": "verify-writer",
        "title": "Verify records carry repo, slice, PR and contract provenance",
        "outcome": {
            "summary": "The verify writer stamps repo, slice, PR and contract on every record.",
            "observable_behavior": [
                "a slice record names the repository, slice, pull request and contract it proves",
                "a record the writer cannot attribute is not written",
            ],
        },
        "interfaces": [
            {
                "name": "bundlebees_plugin.verify_provenance.stamp",
                "kind": "function",
                "change": "add",
                "notes": "Validate and attach the four provenance fields.",
            }
        ],
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "statement": "a verify record carries the repo, slice, PR and contract digest it proves",
                "verification": "test",
                "tests": ["tests/test_verify_writer_provenance.py::test_stamp_adds_all_four_fields_normalised"],
            }
        ],
        "scope": {
            "assigned_files": ["bundlebees_plugin/verify_provenance.py"],
            "test_files": ["tests/test_verify_writer_provenance.py"],
        },
        "source_rev": "0" * 40,
        "tdd": {"applicable": True, "test_first": True},
        "verification": {
            "independent": True,
            "commands": ["python3 -m pytest -q tests/test_verify_writer_provenance.py"],
        },
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    body.update(overrides)
    return body


def frozen(**overrides):
    return contract.freeze_contract(
        contract_body(**overrides),
        approved_by="Kyle (owner), typed approval in chat, 2026-11-26",
        approved_at="2026-11-26T00:00:00Z",
    )


def lease(contract_document, **brief_extra):
    """A lease record shaped like ``leases.record_lease`` writes it."""
    brief = {
        "objective": "stamp provenance on verify records",
        "source_rev": "0" * 40,
        "assigned_files": ["bundlebees_plugin/verify_provenance.py"],
        "acceptance": {
            "criteria": ["a record names what it proves"],
            "tests": ["tests/test_verify_writer_provenance.py"],
        },
        "contract": contract_document,
        "repo": REPO,
    }
    brief.update(brief_extra)
    return {"attempt_id": "att_1", "brief": brief}


def provenance(**overrides):
    values = {"repo": REPO, "slice": "verify-writer", "pr": 317, "contract_sha256": DIGEST}
    values.update(overrides)
    return values


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# AC-001: the record carries all four fields
# ---------------------------------------------------------------------------


def test_stamp_adds_all_four_fields_normalised():
    stamped = verify_provenance.stamp(
        {"attempt_id": "att_1"},
        {"repo": "MojoMast/BundleBees.git", "slice": "verify-writer", "pr": "316", "contract_sha256": DIGEST.upper()},
    )
    assert stamped["attempt_id"] == "att_1"
    assert stamped["repo"] == REPO
    assert stamped["slice"] == "verify-writer"
    assert stamped["pr"] == 316
    assert stamped["contract_sha256"] == DIGEST


@pytest.mark.parametrize("field", verify_provenance.BINDING_FIELDS)
def test_stamp_refuses_a_mapping_missing_each_required_field(field):
    incomplete = provenance()
    incomplete.pop(field)
    with pytest.raises(verify_provenance.ProvenanceError) as exc:
        verify_provenance.stamp({}, incomplete)
    assert exc.value.code == "verify_provenance_missing"
    assert field in exc.value.error


def test_stamp_omits_an_unknown_pull_request_and_refuses_an_invalid_one():
    binding = provenance()
    binding.pop("pr")
    stamped = verify_provenance.stamp({}, binding)
    assert "pr" not in stamped
    assert stamped["repo"] == REPO
    for value in (0, -3, "pr"):
        with pytest.raises(verify_provenance.ProvenanceError) as exc:
            verify_provenance.stamp({}, provenance(pr=value))
        assert exc.value.code == "verify_provenance_missing"


# ---------------------------------------------------------------------------
# AC-002: each field is validated, not copied
# ---------------------------------------------------------------------------


def test_repo_slug_accepts_a_slug_and_refuses_a_path_or_bare_name():
    assert verify_provenance.repo_slug("MojoMast/BundleBees") == REPO
    assert verify_provenance.repo_slug("mojomast/bundlebees.git") == REPO
    for value in ("", "bundlebees", "/tmp/remote.git", "../x/y", "a/b/c"):
        with pytest.raises(verify_provenance.ProvenanceError) as exc:
            verify_provenance.repo_slug(value)
        assert exc.value.code == "verify_provenance_missing"


def test_pull_url_parsing_returns_the_number_and_refuses_other_urls():
    assert verify_provenance.pr_from_url("https://github.com/mojomast/bundlebees/pull/316") == 316
    assert verify_provenance.pr_from_url("https://github.com/mojomast/bundlebees/tree/main") is None
    assert verify_provenance.pr_from_url("") is None
    assert verify_provenance.pr_from_delivery({"pr_url": "https://github.com/mojomast/bundlebees/pull/83"}) == 83
    assert verify_provenance.pr_from_delivery({}) is None
    assert verify_provenance.pr_from_delivery(None) is None


def test_pull_number_refuses_a_boolean_a_zero_and_a_word():
    assert verify_provenance.pull_number("316") == 316
    for value in (True, 0, -1, "pr", "", None):
        with pytest.raises(verify_provenance.ProvenanceError) as exc:
            verify_provenance.pull_number(value)
        assert exc.value.code == "verify_provenance_missing"


def test_contract_digest_refuses_a_short_or_non_hex_digest():
    assert verify_provenance.contract_digest(DIGEST.upper()) == DIGEST
    for value in ("", "58560a1d", "z" * 64, None):
        with pytest.raises(verify_provenance.ProvenanceError) as exc:
            verify_provenance.contract_digest(value)
        assert exc.value.code == "verify_provenance_missing"


def test_slice_name_refuses_a_path_shaped_name():
    assert verify_provenance.slice_name("verify-writer") == "verify-writer"
    for value in ("", "../slice", "a/b", "-leading"):
        with pytest.raises(verify_provenance.ProvenanceError) as exc:
            verify_provenance.slice_name(value)
        assert exc.value.code == "verify_provenance_missing"


# ---------------------------------------------------------------------------
# AC-003: provenance is bound to the frozen contract in the lease
# ---------------------------------------------------------------------------


def test_from_lease_binds_the_slice_and_contract_hash():
    document = frozen()
    observed = verify_provenance.from_lease(lease(document), repo=None, pr="316")
    assert observed["repo"] == REPO
    assert observed["slice"] == "verify-writer"
    assert observed["contract_sha256"] == document["review"]["contract_sha256"]
    assert observed["pr"] == 316


def test_from_lease_refuses_a_lease_without_a_contract():
    lease_doc = lease(frozen())
    lease_doc["brief"].pop("contract")
    with pytest.raises(verify_provenance.ProvenanceError) as exc:
        verify_provenance.from_lease(lease_doc, repo=None, pr=1)
    assert exc.value.code == "verify_provenance_missing"
    assert "contract" in exc.value.error


def test_from_lease_refuses_a_contract_edited_after_freeze():
    document = frozen()
    document["title"] = "changed after the owner approved it"
    with pytest.raises(verify_provenance.ProvenanceError) as exc:
        verify_provenance.from_lease(lease(document), repo=None, pr=1)
    assert exc.value.code == "verify_provenance_invalid"


def test_from_lease_refuses_a_brief_slice_that_disagrees_with_the_contract():
    with pytest.raises(verify_provenance.ProvenanceError) as exc:
        verify_provenance.from_lease(lease(frozen(), slice="wave-c-c3"), repo=None, pr=1)
    assert exc.value.code == "verify_provenance_invalid"


def test_from_lease_refuses_a_brief_without_a_repo_and_no_origin():
    lease_doc = lease(frozen(), repo=None)
    with pytest.raises(verify_provenance.ProvenanceError) as exc:
        verify_provenance.from_lease(lease_doc, repo=None, pr=1)
    assert exc.value.code == "verify_provenance_missing"


def test_with_pr_adds_the_number_and_leaves_it_out_when_unknown():
    base = verify_provenance.from_lease(lease(frozen()), repo=None)
    unknown = verify_provenance.with_pr(base, None)
    assert "pr" not in unknown
    assert [unknown[field] for field in verify_provenance.BINDING_FIELDS] == [
        REPO,
        "verify-writer",
        base["contract_sha256"],
    ]
    complete = verify_provenance.with_pr(base, 317)
    assert [complete[field] for field in verify_provenance.FIELDS] == [
        REPO,
        "verify-writer",
        317,
        base["contract_sha256"],
    ]


# ---------------------------------------------------------------------------
# AC-004: the writer stamps the record, and refuses what it cannot attribute
# ---------------------------------------------------------------------------


def test_the_written_record_carries_provenance_and_survives_scrub(tmp_path):
    target = cli._write_verify_result(
        tmp_path,
        "att_1",
        ok=True,
        sha="a" * 40,
        branch="feat/verify-writer",
        files=["bundlebees_plugin/verify_provenance.py"],
        tests=["tests/test_verify_writer_provenance.py"],
        provenance=provenance(),
        require_provenance=True,
    )
    payload = json.loads(Path(target).read_text(encoding="utf-8"))
    assert payload["repo"] == REPO
    assert payload["slice"] == "verify-writer"
    assert payload["pr"] == 317
    # scrub() cannot tell a 64-hex digest from a key, so the writer restores it.
    assert payload["contract_sha256"] == DIGEST


def test_the_writer_refuses_a_slice_record_without_provenance(tmp_path):
    with pytest.raises(CliError) as exc:
        cli._write_verify_result(
            tmp_path,
            "att_1",
            ok=True,
            sha="a" * 40,
            branch="feat/verify-writer",
            files=[],
            tests=[],
            require_provenance=True,
        )
    assert exc.value.code == "verify_provenance_missing"
    assert not (tmp_path / "verify-results" / "att_1.json").exists()


def test_a_pr_less_slice_record_carries_the_binding(tmp_path):
    target = cli._write_verify_result(
        tmp_path,
        "att_1",
        ok=True,
        sha="c" * 40,
        branch="feat/verify-writer",
        files=["bundlebees_plugin/cli.py"],
        tests=["tests/test_verify_writer_provenance.py"],
        provenance={"repo": REPO, "slice": "verify-writer", "contract_sha256": DIGEST},
        require_provenance=True,
    )
    payload = json.loads(Path(target).read_text(encoding="utf-8"))
    assert payload["repo"] == REPO
    assert payload["slice"] == "verify-writer"
    assert payload["contract_sha256"] == DIGEST
    assert "pr" not in payload


def test_a_legacy_record_is_written_without_provenance_fields(tmp_path):
    target = cli._write_verify_result(
        tmp_path,
        "att_1",
        ok=False,
        sha=None,
        branch="feat/legacy",
        files=[],
        tests=[],
        code="no_tests",
    )
    payload = json.loads(Path(target).read_text(encoding="utf-8"))
    for field in verify_provenance.FIELDS:
        assert field not in payload


# ---------------------------------------------------------------------------
# AC-005: the CLI resolves the PR from dispatch-time state, never the worker
# ---------------------------------------------------------------------------


def test_the_cli_resolves_the_pr_from_the_delivery_record(tmp_path):
    resolved = cli._verify_record_provenance(
        lease(frozen()),
        tmp_path,
        delivery={"pr_url": "https://github.com/mojomast/bundlebees/pull/83"},
    )
    assert resolved["pr"] == 83
    assert resolved["repo"] == REPO


def test_the_cli_prefers_an_explicit_pull_request_over_the_delivery(tmp_path):
    resolved = cli._verify_record_provenance(
        lease(frozen()),
        tmp_path,
        pr=310,
        delivery={"pr_url": "https://github.com/mojomast/bundlebees/pull/83"},
    )
    assert resolved["pr"] == 310


def test_the_cli_falls_back_to_the_lease_brief_for_the_pull_request(tmp_path):
    resolved = cli._verify_record_provenance(lease(frozen(), pull_request=70), tmp_path)
    assert resolved["pr"] == 70


def test_the_cli_writes_the_binding_without_a_pull_request(tmp_path):
    resolved = cli._verify_record_provenance(lease(frozen()), tmp_path)
    assert "pr" not in resolved
    assert resolved["repo"] == REPO
    assert resolved["slice"] == "verify-writer"
    assert len(resolved["contract_sha256"]) == 64


def test_the_cli_falls_back_to_the_origin_slug_for_the_repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    git(work, "remote", "add", "origin", "https://github.com/MojoMast/BundleBees.git")
    resolved = cli._verify_record_provenance(lease(frozen(), repo=None), work, pr=1)
    assert resolved["repo"] == REPO
