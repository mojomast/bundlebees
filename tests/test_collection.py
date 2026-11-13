"""Review C1: every AC test reference must resolve to a collected test id.

Before a contract is frozen, bundlebees collects the approved tests at
``source_rev`` (``pytest --collect-only`` in the sandbox, bounded) and stores a
signed test-collection manifest bound to the contract hash, ``source_rev`` and
the test-file fingerprints. A typo, a renamed test or a whole-file reference is
refused, as is a syntax error in an approved test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import leases, tdd
from bundlebees_plugin import contract as acdd
from tests.test_cli_acdd import (
    APPROVED_TEST, CID, TEST_ID, _contract, _ctx, _frozen, _invoke, _repo, _write,
)


def _freeze(tmp_path, contract, *extra):
    ctx = _ctx()
    src = _write(tmp_path / "draft.json", contract)
    out = tmp_path / "frozen.json"
    rc = _invoke(ctx, ["contract", "freeze", str(src), "--approved-by", "Kyle", "--out", str(out), *extra])
    return rc, out


def _criteria(tests):
    return [{"id": "AC-001", "given": "g", "when": "w", "then": "t", "verification": "test", "tests": tests}]


def _manifest(state, frozen):
    sha = acdd.check_frozen(frozen)
    return tdd.load_collection(state, str(frozen["id"]), sha, leases.evidence_key(state))


def test_freeze_collects_and_binds_a_signed_manifest(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    rc, out = _freeze(tmp_path, _contract(base), "--workdir", str(repo), "--state-dir", str(state))
    text = capsys.readouterr().out
    assert rc == 0, text
    assert "every AC reference resolved" in text
    frozen = json.loads(out.read_text())
    sha = acdd.check_frozen(frozen)
    manifest = _manifest(state, frozen)
    assert manifest["source_rev"] == base
    assert TEST_ID in manifest["collected_ids"]
    assert manifest["resolved"]["AC-001"][TEST_ID] == [TEST_ID]
    assert manifest["methods"] == {APPROVED_TEST: "pytest"}
    assert set(manifest["test_fingerprints"]) == {APPROVED_TEST}
    assert frozen["review"]["test_collection"]["manifest_sha256"] == tdd.collection_digest(manifest)
    assert frozen["review"]["test_collection"]["collected"] == len(manifest["collected_ids"])


def test_freeze_refuses_a_reference_that_matches_no_collected_test(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    bad = _contract(base, acceptance_criteria=_criteria([APPROVED_TEST + "::test_value_is_tow"]))
    rc, out = _freeze(tmp_path, bad, "--workdir", str(repo), "--state-dir", str(tmp_path / "s"))
    text = capsys.readouterr().out
    assert rc != 0 and "test_unresolved" in text and "matches no collected test id" in text
    assert not out.exists()


def test_freeze_refuses_a_whole_file_reference(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    bad = _contract(base, acceptance_criteria=_criteria([APPROVED_TEST]))
    rc, out = _freeze(tmp_path, bad, "--workdir", str(repo), "--state-dir", str(tmp_path / "s"))
    text = capsys.readouterr().out
    assert rc != 0 and "test_unresolved" in text and "whole file" in text
    assert not out.exists()


def test_freeze_without_a_workdir_refuses_instead_of_skipping_collection(tmp_path, capsys):
    _, base = _repo(tmp_path)
    rc, out = _freeze(tmp_path, _contract(base), "--state-dir", str(tmp_path / "s"))
    assert rc != 0 and "collection_required" in capsys.readouterr().out
    assert not out.exists()


def test_tdd_not_applicable_contracts_need_no_collection(tmp_path, capsys):
    _, base = _repo(tmp_path)
    plain = _contract(base, tdd={"applicable": False, "reason": "docs only"}, evidence_required=["review"],
                      acceptance_criteria=[{"id": "AC-001", "statement": "docs read well", "verification": "review"}])
    rc, out = _freeze(tmp_path, plain)
    assert rc == 0, capsys.readouterr().out
    assert "test_collection" not in json.loads(out.read_text())["review"]


def test_a_syntax_error_in_an_approved_test_is_refused(tmp_path, capsys):
    repo, base = _repo(tmp_path, "def test_broken(:\n    pass\n")
    tests = [APPROVED_TEST + "::test_broken"]
    rc, out = _freeze(tmp_path, _contract(base, acceptance_criteria=_criteria(tests)),
                      "--workdir", str(repo), "--state-dir", str(tmp_path / "s"))
    text = capsys.readouterr().out
    assert rc != 0 and "tests_uncollectable" in text
    assert not out.exists()


def test_freeze_refuses_more_than_the_collected_id_limit(tmp_path, monkeypatch, capsys):
    from bundlebees_plugin import acdd_cli

    repo, base = _repo(tmp_path)
    monkeypatch.setattr(acdd_cli, "_MAX_COLLECTED_IDS", 0)
    rc, out = _freeze(tmp_path, _contract(base), "--workdir", str(repo), "--state-dir", str(tmp_path / "s"))
    assert rc != 0 and "tests_uncollectable" in capsys.readouterr().out
    assert not out.exists()


@pytest.mark.parametrize(
    "ref, want",
    [
        ("t.py::test_a", ["t.py::test_a"]),
        ("t.py::test_p", ["t.py::test_p[1]", "t.py::test_p[2]"]),
        ("t.py::C", ["t.py::C::test_x"]),
        ("t.py", []),
        ("t.py::test_", []),
    ],
)
def test_resolve_exact_parametrized_and_class_children_only(ref, want):
    ids = ["t.py::test_a", "t.py::test_ab", "t.py::test_p[1]", "t.py::test_p[2]", "t.py::C::test_x"]
    assert tdd.resolve_test_ref(ref, ids) == want


def test_a_tampered_manifest_is_detected(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    rc, out = _freeze(tmp_path, _contract(base), "--workdir", str(repo), "--state-dir", str(state))
    assert rc == 0, capsys.readouterr().out
    frozen = json.loads(out.read_text())
    sha = acdd.check_frozen(frozen)
    path = tdd.collection_path(state, str(frozen["id"]), sha)
    data = json.loads(path.read_text())
    data["collected_ids"].append(APPROVED_TEST + "::test_injected")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(tdd.TddError) as caught:
        tdd.load_collection(state, str(frozen["id"]), sha, leases.evidence_key(state))
    assert caught.value.code == "evidence_tampered"


def test_signed_records_and_frozen_contracts_match_the_schemas(tmp_path, capsys):
    """Frozen contracts with test_collection and signed records validate."""
    jsonschema = pytest.importorskip("jsonschema")
    schemas = Path(__file__).resolve().parents[1] / "bundlebees_plugin" / "schemas"
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    rc, out = _freeze(tmp_path, _contract(base), "--workdir", str(repo), "--state-dir", str(state))
    assert rc == 0, capsys.readouterr().out
    frozen = json.loads(out.read_text())
    jsonschema.Draft202012Validator(json.loads((schemas / "contract.schema.json").read_text())).validate(frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(out), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    evidence_schema = json.loads((schemas / "evidence.schema.json").read_text())
    for record in tdd.load_records(state, CID, leases.evidence_key(state)):
        body = {k: v for k, v in record.items() if not k.startswith("_")}
        jsonschema.Draft202012Validator(evidence_schema).validate(body)
