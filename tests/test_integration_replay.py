"""An accepted wave is recoverable only from intact, matching local evidence."""

import json

import pytest

from bundlebees_plugin import integrate_job as ij
from tests.test_integrate_job import _text_slice, git, repo_with_base


@pytest.fixture
def run(tmp_path):
    repo, base = repo_with_base(tmp_path)
    state = tmp_path / "state"
    job = _text_slice(repo, state, base, "job_a", "att_a", "verified")
    calls = []

    def suite(workdir, argv):
        calls.append((git(workdir, "rev-parse", "HEAD").stdout.strip(), argv))
        return {"exit_code": 0}

    def integrate(*argv):
        return ij.integrate(state, job, repo, suite_command=argv or ["fixture"], runner=suite)

    return integrate, calls, state, repo, base


def history(state):
    return {p.relative_to(state): p.read_bytes() for p in state.rglob("*.json")}


def ref_sha(repo, ref):
    return git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()


@pytest.mark.parametrize("damage", [
    "absent-sidecar", "unreadable-sidecar", "missing-digest", "bad-digest", "identity-body", "missing-wave",
    "legacy", "wrong-wave", "attempt-list", "missing-suite", "exit-false", "timeout", "blocked", "problems",
    "head-mismatch", "tree-mismatch", "absent-commit", "missing-ancestor", "wrong-attempt-owner",
    "unbound-result", "record-digest", "sidecar-link",
])
def test_unproven_recovery_leaves_evidence_and_ref_untouched(run, damage):
    integrate, calls, state, repo, base = run
    first = integrate()
    ref = first["integration_ref"]
    git(repo, "update-ref", "-d", ref)
    wave = state / "waves/job_a.w1.json"
    sidecar = state / "wave-identities/job_a.w1.json"
    record, stored = map(json.loads, (wave.read_bytes(), sidecar.read_bytes()))
    materialized = stored["materialized"]
    if damage == "wrong-wave":
        record["wave_id"] = "job_b.w1"
    elif damage == "attempt-list":
        record["attempts"].append("att_extra")
    elif damage == "missing-suite":
        del record["suite"]
    elif damage == "exit-false":
        record["suite"]["exit_code"] = False
    elif damage == "timeout":
        record["suite"]["timed_out"] = True
    elif damage == "blocked":
        record["suite"]["blocking"] = ["contract"]
    elif damage == "problems":
        record["problems"] = {"att_a": "not verified"}
    elif damage == "head-mismatch":
        record["head_sha"] = base
    elif damage == "tree-mismatch":
        materialized["source_tree_sha256"] = "f" * 64
    elif damage == "absent-commit":
        record["head_sha"] = materialized["head_sha"] = "f" * 40
    elif damage == "missing-ancestor":
        record["head_sha"] = materialized["head_sha"] = base
        materialized["source_tree_sha256"] = stored["identity"]["source_tree_sha256"]
    elif damage == "missing-digest":
        del stored["identity_digest"]
    elif damage == "bad-digest":
        stored["identity_digest"] = "f" * 64
    elif damage == "identity-body":
        stored["identity"]["command"] = ["other"]
    elif damage == "wrong-attempt-owner":
        verify = state / "verify-results/att_a.json"
        verify.write_text(json.dumps(dict(json.loads(verify.read_bytes()), attempt_id="att_b")))
    # Internally consistent fixture hashes must not mask semantic gate failures.
    if damage != "record-digest":
        materialized["record_sha256"] = ij._record_digest(record)
    else:
        record["at"] = "changed"
    if "identity_digest" in stored:
        stored["binding_sha256"] = ij._binding_digest(stored["identity_digest"], materialized)
    if damage == "unbound-result":
        del stored["binding_sha256"], materialized["previous_ref"], materialized["record_sha256"]
    wave.write_text(json.dumps(record))
    sidecar.write_text(json.dumps(stored))
    if damage == "absent-sidecar":
        sidecar.unlink()
    elif damage == "unreadable-sidecar":
        sidecar.write_bytes(b"{damaged")
    elif damage == "sidecar-link":
        moved = sidecar.rename(state / "copy.json")
        sidecar.symlink_to(moved)
    elif damage == "missing-wave":
        wave.unlink()
    elif damage == "legacy":
        wave.rename(wave.with_name("w1.json"))
        sidecar.unlink()

    before = history(state)
    with pytest.raises(ij.IntegrateError) as refused:
        integrate()
    assert refused.value.code == "wave_reconciliation_required"
    assert not ref_sha(repo, ref) and len(calls) == 1 and history(state) == before


def test_changed_request_rechecks_derived_slot_and_preserves_both_receipts(run):
    integrate, calls, state, repo, _ = run
    first = integrate()
    second = integrate("changed")
    assert second["ok"] and not second["recovered"] and len(calls) == 2
    assert integrate("changed")["recovered"] and len(calls) == 2
    ref = first["integration_ref"]
    git(repo, "update-ref", "-d", ref)
    # A valid sidecar for the *original* identity next to the derived record
    # cannot be reported as the changed request. Keep both for reconciliation.
    derived = second["record"]["wave_id"]
    path = state / f"wave-identities/{derived}.json"
    stored = json.loads(path.read_bytes())
    original = json.loads((state / "wave-identities/job_a.w1.json").read_bytes())
    for field in ("identity", "identity_digest"):
        stored[field] = original[field]
    stored["binding_sha256"] = ij._binding_digest(stored["identity_digest"], stored["materialized"])
    path.write_text(json.dumps(stored))
    before = history(state)
    with pytest.raises(ij.IntegrateError) as refused:
        integrate("changed")
    assert refused.value.code == "wave_reconciliation_required"
    assert not ref_sha(repo, ref) and history(state) == before and len(calls) == 2


def test_only_the_interrupted_ref_update_can_be_replayed(run):
    integrate, calls, state, repo, base = run
    first = integrate()
    ref, head = first["integration_ref"], first["record"]["head_sha"]
    before = history(state)
    git(repo, "update-ref", "-d", ref)
    assert integrate()["recovered"] and ref_sha(repo, ref) == head
    assert integrate()["recovered"] and history(state) == before

    later = git(repo, "commit-tree", f"{head}^{{tree}}", "-p", head, "-m", "later wave").stdout.strip()
    for stale in (base, later):
        git(repo, "update-ref", ref, stale)
        with pytest.raises(ij.IntegrateError) as refused:
            integrate()
        assert refused.value.code == "wave_reconciliation_required"
        assert ref_sha(repo, ref) == stale and history(state) == before
    assert len(calls) == 1


def test_first_publication_refuses_concurrent_ref_update(run, monkeypatch):
    integrate, calls, state, repo, base = run
    ref = ij.integration_ref("job_a")

    def advance(_name, **_kwargs):
        git(repo, "update-ref", ref, base)

    monkeypatch.setattr(ij, "_checkpoint", advance)
    with pytest.raises(ij.IntegrateError) as refused:
        integrate()
    assert refused.value.code == "wave_reconciliation_required"
    before = history(state)
    with pytest.raises(ij.IntegrateError):
        integrate()
    assert ref_sha(repo, ref) == base and history(state) == before and len(calls) == 1
