"""Verify-worker bees: per-attempt verification as dispatchable queue actions.

The F6 coordinator action queue already owns durable, fenced, at-most-once
actions. These tests pin the verify lane built on it: a verify-worker bee
claims a ``verify:<attempt>`` item, runs the SAME verifier the coordinator ran
in-beat (``verify_local.verify_and_commit`` under the configured runner, the
same test commands and the same scanner), and returns signed evidence the
coordinator checks before the merge gate reads the record. Shadow mode runs
both paths on a bounded set, records the comparison and alarms on divergence;
the default stays in-beat.

Fixture hygiene is deliberate: every synthetic digest-shaped value is a run of
one repeated letter, so the format assertions hold while nothing in this file
resembles a credential.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import (
    coordinator_actions,
    job as jobs,
    leases,
    queue as feature_queue,
    sandbox,
    verify_local,
    verify_workers,
)

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]

WORKFLOW_A = "a" * 40
WORKFLOW_B = "b" * 40
WORKER_KEY = b"verify-worker-signing-key-for-tests-only"
OTHER_KEY = b"another-signing-key-for-tests-only-ok"
OWNER = "verify-bee-1"
OTHER_OWNER = "verify-bee-2"

TEST_FILE = "tests/test_newmod.py"
NEW_FILES = {
    "pkg/newmod.py": "def double(x):\n    return x * 2\n",
    TEST_FILE: "from pkg.newmod import double\n\ndef test_double():\n    assert double(2) == 4\n",
}
MORE_FILES = {
    "pkg/other.py": "def triple(x):\n    return x * 3\n",
    "tests/test_other.py": "from pkg.other import triple\n\ndef test_triple():\n    assert triple(2) == 6\n",
}
MORE_TESTS = ["tests/test_other.py"]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "existing.py").write_text("VALUE = 1\n")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text(
        "from pkg.existing import VALUE\n\ndef test_value():\n    assert VALUE == 1\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD").stdout.strip()


def make_patch(repo: Path, base: str, files: dict[str, str]) -> str:
    git(repo, "checkout", "-q", "-b", "work", base)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    patch = git(repo, "diff", "--cached", base).stdout
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "work")
    return patch


def commit_files(repo: Path, base: str, files: dict[str, str], message: str) -> str:
    git(repo, "checkout", "-q", "-b", "work", base)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "work")
    return sha


def sandboxed(workdir, argv):
    """The documented test runner for hosts without a strong isolation backend."""

    return sandbox.run_in_sandbox(workdir, list(argv), allow_unsafe_local=True, timeout=120)


def make_scanner(tmp_path: Path) -> str:
    stub = tmp_path / "scan-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return str(stub)


def brief(base: str, files=None) -> dict:
    return {
        "objective": "add a helper",
        "source_rev": base,
        "assigned_files": list(files if files is not None else NEW_FILES),
        "acceptance": {"criteria": ["double"], "tests": [TEST_FILE]},
    }


def request(base: str, patch: str, scanner: str, *, attempt: str = "att_w1",
            workflow: str = WORKFLOW_A, files=None, tests=None, job: str = "job_w") -> dict:
    return verify_workers.build_request(
        attempt_id=attempt,
        source_rev=base,
        workflow_ref=workflow,
        patch=patch,
        declared_files=list(files if files is not None else NEW_FILES),
        tests=list(tests or [TEST_FILE]),
        suite_command=SUITE,
        gitleaks=scanner,
        job_id=job,
        repo="bundlebees/smoke",
    )


def flow(tmp_path: Path, files=None):
    """A complete dispatch fixture: repo, lease, pinned patch, enqueued action."""

    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    scanner = make_scanner(tmp_path)
    patch = make_patch(repo, base, files or NEW_FILES)
    leases.record_lease(state, "att_w1", brief(base, files or NEW_FILES))
    req = request(base, patch, scanner, files=files or NEW_FILES)
    verify_workers.enqueue_verify_action(state, req)
    return repo, state, base, patch, scanner, req


def synthetic_record(**overrides) -> dict:
    record = {
        "attempt_id": "att_c1",
        "ok": True,
        "sha": "c" * 40,
        "branch": "bb-verify/att_c1",
        "base_rev": "a" * 40,
        "files": ["pkg/x.py"],
        "tests": ["tests/test_x.py"],
        "red": {"exit_code": 1, "timed_out": False},
        "suite": {"exit_code": 0, "timed_out": False},
        "gitleaks": {"exit_code": 0},
        "mode": "commit-local",
        "patch_sha256": "e" * 64,
        "execution_identity": {},
        "resumed": False,
        "test_cache": {},
        "at": "2026-01-01T00:00:00Z",
    }
    record.update(overrides)
    return record


class EnqueueAndBinding:
    __test__ = True

    def test_the_item_carries_the_binding_and_replays_idempotently(self, tmp_path):
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        first = verify_workers.enqueue_verify_action(
            state, request("a" * 40, "patch text", scanner), check_lease=False
        )

        assert first["kind"] == "verify"
        assert first["action_key"] == "verify:att_w1"
        assert first["workflow_ref"] == WORKFLOW_A
        assert first["status"] == "queued"
        payload = first["payload"]
        assert payload["attempt_id"] == "att_w1"
        assert payload["source_rev"] == "a" * 40
        assert payload["workflow_ref"] == WORKFLOW_A
        assert payload["patch"] == "patch text"
        assert payload["patch_sha256"]

        replay = verify_workers.enqueue_verify_action(
            state, request("a" * 40, "patch text", scanner), check_lease=False
        )
        assert replay["action_id"] == first["action_id"]
        assert replay["created_at"] == first["created_at"]

        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.enqueue_verify_action(
                state, request("a" * 40, "different patch", scanner), check_lease=False
            )
        assert caught.value.code == "verify_request_changed"

    def test_the_pin_is_required_and_the_rev_must_be_a_full_rev(self):
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.build_request(
                attempt_id="att_w1",
                source_rev="a" * 40,
                workflow_ref="",
                patch="patch text",
                declared_files=["pkg/x.py"],
                tests=[TEST_FILE],
                gitleaks="scanner",
            )
        assert caught.value.code == "missing_workflow_ref"

        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.build_request(
                attempt_id="att_w1",
                source_rev="abc",
                workflow_ref=WORKFLOW_A,
                patch="patch text",
                declared_files=["pkg/x.py"],
                tests=[TEST_FILE],
                gitleaks="scanner",
            )
        assert caught.value.code == "source_rev_invalid"

        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.build_request(
                attempt_id="att_w1",
                source_rev="a" * 40,
                workflow_ref=WORKFLOW_A,
                patch="patch text",
                declared_files=["pkg/x.py"],
                tests=[],
                gitleaks="scanner",
            )
        assert caught.value.code == "tests_required"

        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.build_request(
                attempt_id="att_w1",
                source_rev="a" * 40,
                workflow_ref=WORKFLOW_A,
                patch="patch text",
                declared_files=["pkg/x.py"],
                tests=[TEST_FILE],
                gitleaks="",
            )
        assert caught.value.code == "security_tool_missing"

    def test_verify_items_do_not_serialize_per_repo(self, tmp_path):
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        verify_workers.enqueue_verify_action(
            state, request("a" * 40, "patch one", scanner, attempt="att_p1"), check_lease=False
        )
        verify_workers.enqueue_verify_action(
            state, request("b" * 40, "patch two", scanner, attempt="att_p2", workflow=WORKFLOW_B),
            check_lease=False,
        )

        first = coordinator_actions.claim(state, "verify-att_p1", "bee-one", now=1000.0)
        second = coordinator_actions.claim(state, "verify-att_p2", "bee-two", now=1000.0)

        assert first["status"] == "claimed" and first["fence_seq"] == 1
        assert second["status"] == "claimed" and second["fence_seq"] == 2


class WorkerEvidence:
    __test__ = True

    def test_worker_evidence_satisfies_the_existing_merge_gate(self, tmp_path):
        repo, state, base, patch, scanner, req = flow(tmp_path)
        produced = verify_workers.produce_verify_evidence(
            state, repo, "att_w1", owner=OWNER, key=WORKER_KEY, runner=sandboxed
        )
        assert produced["ok"] is True
        accepted = verify_workers.accept_verify_evidence(
            state, repo, "att_w1", produced["evidence"], key=WORKER_KEY
        )
        assert accepted["ok"] is True and accepted["replayed"] is False

        record = verify_local.read_record(state, "att_w1")
        assert set(record) == set(verify_workers.RECORD_FIELDS)
        assert record["ok"] is True
        assert len(record["sha"]) == 40
        assert record["base_rev"] == base
        assert record["gitleaks"]["exit_code"] == 0
        assert record["suite"]["exit_code"] == 0
        assert record["red"]["exit_code"] != 0
        # The merge gate reads exactly this record: queue.job_accepted_reasons
        # and integrate_job's not_verified check must both accept it.
        assert record["files"] == sorted(NEW_FILES)
        assert verify_local.reprove_commit(repo, record)["ok"] is True

    def test_the_worker_record_matches_the_in_beat_record_shape(self, tmp_path):
        repo, state, base, patch, scanner, req = flow(tmp_path)
        verify_workers.produce_verify_evidence(
            state, repo, "att_w1", owner=OWNER, key=WORKER_KEY, runner=sandboxed
        )
        # A direct in-beat run of the same verifier for a sibling attempt: the
        # records must be indistinguishable in shape and semantics.
        patch_b = make_patch(repo, base, MORE_FILES)
        leases.record_lease(state, "att_w2", brief(base, MORE_FILES))
        in_beat = verify_local.verify_and_commit(
            state, repo, "att_w2",
            patch=patch_b,
            declared_files=list(MORE_FILES),
            base_rev=base,
            tests=MORE_TESTS,
            suite_command=SUITE,
            gitleaks=scanner,
            runner=sandboxed,
        )
        worker_record = verify_local.read_record(state, "att_w1")
        assert set(worker_record) == set(in_beat)
        assert worker_record["mode"] == in_beat["mode"] == "commit-local"
        assert worker_record["gitleaks"]["exit_code"] == in_beat["gitleaks"]["exit_code"] == 0

    def test_the_full_job_gate_accepts_a_worker_verified_attempt(self, tmp_path):
        repo, state, base, patch, scanner, req = flow(tmp_path)
        jobs.record_authorization(
            state, job_id="job_w", statement="verify one attempt", repo="bundlebees/smoke",
            base_rev=base, scope=list(NEW_FILES), bees=1, wall_clock_s=600,
            require_tty=False, interactive=False, by="reviewer",
        )
        job = jobs.create_job(
            state, job_id="job_w",
            tasks=[{"task_id": "t1", "objective": "add a helper", "lease": list(NEW_FILES),
                    "acceptance": {"criteria": ["double"], "tests": [TEST_FILE]}}],
            workflow_ref=WORKFLOW_A,
        )
        verify_workers.run_verify_action(
            state, repo, "att_w1", owner=OWNER, key=WORKER_KEY, runner=sandboxed
        )
        evidence_job = dict(job)
        task = evidence_job["tasks"][0]
        for step in ("dispatching", "dispatched", "running", "verifying", "verified", "integrating", "accepted"):
            jobs.transition(evidence_job, "t1", step, actor="reconciler", reason="fixture walk")
        task["attempts"] = ["att_w1"]
        task["binding"] = {"source_rev": base}
        record = verify_local.read_record(state, "att_w1")
        evidence_job["integration"]["accepted_waves"] = [{"commit": record["sha"], "wave": 1}]
        evidence_job["integration"]["last_commit"] = record["sha"]
        jobs.save_job(state, evidence_job)

        assert feature_queue.job_accepted_reasons(state, "job_w") == []
        assert feature_queue.job_accepted(state, "job_w") is True


class EvidenceAuthenticity:
    __test__ = True

    def _signed(self, **overrides) -> dict:
        body = {
            "schema": verify_workers.EVIDENCE_SCHEMA,
            "action_id": "verify-att_x",
            "action_key": "verify:att_x",
            "fence_seq": 1,
            "owner": OWNER,
            "attempt_id": "att_x",
            "source_rev": "a" * 40,
            "workflow_ref": WORKFLOW_A,
            "job_id": "job_x",
            "repo": "bundlebees/smoke",
            "record": synthetic_record(),
        }
        body.update(overrides)
        return verify_workers.sign_evidence(body, WORKER_KEY)

    def test_evidence_binds_to_the_action_and_refuses_tampering(self, tmp_path):
        state = tmp_path / "state"
        repo = tmp_path / "repo"
        repo.mkdir()
        evidence = self._signed()
        assert verify_workers.evidence_signature_valid(evidence, WORKER_KEY) is True

        tampered = dict(evidence, record=dict(evidence["record"], ok=False))
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(state, repo, "att_x", tampered, key=WORKER_KEY)
        assert caught.value.code == "evidence_signature_invalid"

        unsigned = {name: value for name, value in evidence.items() if name != "signature"}
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(state, repo, "att_x", unsigned, key=WORKER_KEY)
        assert caught.value.code == "evidence_unsigned"

        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(state, repo, "att_x", evidence, key=OTHER_KEY)
        assert caught.value.code == "evidence_signature_invalid"

    def test_a_key_that_is_too_short_or_missing_is_refused(self):
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.worker_key(b"short")
        assert caught.value.code == "worker_key_weak"
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.worker_key(None)
        assert caught.value.code == "worker_key_missing"


class Fencing:
    __test__ = True

    def test_a_superseded_worker_is_refused_and_the_adopter_proceeds(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        patch = make_patch(repo, base, NEW_FILES)
        leases.record_lease(state, "att_w1", brief(base))
        verify_workers.enqueue_verify_action(state, request(base, patch, scanner))

        produced = verify_workers.produce_verify_evidence(
            state, repo, "att_w1", owner=OWNER, key=WORKER_KEY, runner=sandboxed,
            ttl_s=60.0, now=1000.0,
        )
        assert produced["fence_seq"] == 1

        # The claim lapsed and a second bee adopted it: the first worker's
        # evidence carries a stale fence and must be refused, unchanged.
        coordinator_actions.adopt(state, "verify-att_w1", OTHER_OWNER, ttl_s=600.0, now=1061.0)
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.publish_verify_result(state, "att_w1", produced, now=1062.0)
        assert caught.value.code == "stale_fence"
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(
                state, repo, "att_w1", produced["evidence"], key=WORKER_KEY, now=1062.0
            )
        assert caught.value.code == "stale_fence"

        result = verify_workers.run_verify_action(
            state, repo, "att_w1", owner=OTHER_OWNER, key=WORKER_KEY, runner=sandboxed, now=1063.0
        )
        assert result["ok"] is True
        accepted = verify_workers.accept_verify_evidence(
            state, repo, "att_w1", result["evidence"], key=WORKER_KEY, now=1064.0
        )
        assert accepted["ok"] is True and accepted["replayed"] is True

    def test_acceptance_is_idempotent_and_refuses_conflicting_evidence(self, tmp_path):
        repo, state, base, patch, scanner, req = flow(tmp_path)
        result = verify_workers.run_verify_action(
            state, repo, "att_w1", owner=OWNER, key=WORKER_KEY, runner=sandboxed
        )
        assert result["ok"] is True
        first = verify_workers.accept_verify_evidence(state, repo, "att_w1", result["evidence"], key=WORKER_KEY)
        assert first["replayed"] is True
        second = verify_workers.accept_verify_evidence(state, repo, "att_w1", result["evidence"], key=WORKER_KEY)
        assert second["replayed"] is True

        # A different evidence body claiming the same conclusion conflicts with
        # the one already recorded: one attempt has one verification result.
        forged = verify_workers.sign_evidence(
            dict(result["evidence"], record=dict(result["evidence"]["record"],
                                                 at="1999-01-01T00:00:00Z")),
            WORKER_KEY,
        )
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(state, repo, "att_w1", forged, key=WORKER_KEY)
        assert caught.value.code == "evidence_conflict"


class WorkerRefusals:
    __test__ = True

    def test_a_payload_narrower_than_the_lease_is_refused(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        patch = make_patch(repo, base, NEW_FILES)
        leases.record_lease(state, "att_w1", brief(base))
        # The abbreviated declared path is the documented sibling-lane trap:
        # dispatch must refuse it, never verify a partial lease.
        abbreviated = request(base, patch, scanner, files=["pkg/newmod.py"])
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.enqueue_verify_action(state, abbreviated)
        assert caught.value.code == "lease_scope_mismatch"

    def test_evidence_cannot_rebind_to_a_different_patch_or_rev(self, tmp_path):
        repo, state, base, patch, scanner, req = flow(tmp_path)
        result = verify_workers.run_verify_action(
            state, repo, "att_w1", owner=OWNER, key=WORKER_KEY, runner=sandboxed
        )
        evidence = result["evidence"]

        rebased = verify_workers.sign_evidence(
            dict(evidence, source_rev="b" * 40, record=dict(evidence["record"], base_rev="b" * 40)),
            WORKER_KEY,
        )
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(state, repo, "att_w1", rebased, key=WORKER_KEY)
        assert caught.value.code == "evidence_source_rev_mismatch"

        repinned = verify_workers.sign_evidence(
            dict(evidence, record=dict(evidence["record"], patch_sha256="e" * 64)), WORKER_KEY
        )
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.accept_verify_evidence(state, repo, "att_w1", repinned, key=WORKER_KEY)
        assert caught.value.code == "evidence_patch_mismatch"


class ShadowMode:
    __test__ = True

    def test_the_comparator_alarms_on_unequal_evidence(self, tmp_path):
        state = tmp_path / "state"
        left = synthetic_record()
        right = synthetic_record(sha="d" * 40, suite={"exit_code": 1, "timed_out": False})

        comparison = verify_workers.compare_evidence(left, right)
        assert comparison["equal"] is False
        fields = {entry["field"] for entry in comparison["divergences"]}
        assert {"sha", "suite"} <= fields

        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.run_shadow_comparison(
                state, "att_c1", in_beat_record=left, worker_record=right
            )
        assert caught.value.code == "shadow_divergence"
        assert (state / "verify-workers" / "shadow" / "alarms.jsonl").exists()
        stored = verify_workers.shadow_comparison(state, "att_c1")
        assert stored["equal"] is False

        equal = verify_workers.run_shadow_comparison(
            state, "att_c2", in_beat_record=left, worker_record=synthetic_record()
        )
        assert equal["equal"] is True
        assert verify_workers.shadow_alarms_present(state) is True

    def test_shadow_dispatch_runs_both_paths_and_keeps_in_beat_authoritative(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        patch = make_patch(repo, base, NEW_FILES)
        leases.record_lease(state, "att_w1", brief(base))

        def in_beat():
            return verify_local.verify_and_commit(
                state, repo, "att_w1",
                patch=patch,
                declared_files=list(NEW_FILES),
                base_rev=base,
                tests=[TEST_FILE],
                suite_command=SUITE,
                gitleaks=scanner,
                runner=sandboxed,
            )

        out = verify_workers.dispatch_verification(
            state, repo, request(base, patch, scanner, attempt="att_w1"),
            key=WORKER_KEY,
            in_beat=in_beat,
            worker={"owner": OWNER, "runner": sandboxed},
            mode="shadow",
            shadow_limit=1,
        )
        assert out["path"] == "in-beat"
        assert out["verdict"]["ok"] is True
        assert out["shadow"]["equal"] is True
        assert out["shadow"]["alarm"] is False
        assert verify_workers.shadow_alarms_present(state) is False
        assert out["shadow"]["used"] == 1

    def test_the_sample_set_is_bounded_and_skips_beyond_it(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        plans = ((1, NEW_FILES, [TEST_FILE]), (2, MORE_FILES, MORE_TESTS))
        for index, files, tests in plans:
            name = f"att_b{index}"
            patch = make_patch(repo, base, files)
            leases.record_lease(state, name, brief(base, files))
            verify_workers.dispatch_verification(
                state, repo, request(base, patch, scanner, attempt=name, files=files, tests=tests),
                key=WORKER_KEY,
                in_beat=lambda name=name, patch=patch, files=files, tests=tests: (
                    verify_local.verify_and_commit(
                        state, repo, name,
                        patch=patch,
                        declared_files=list(files),
                        base_rev=base,
                        tests=tests,
                        suite_command=SUITE,
                        gitleaks=scanner,
                        runner=sandboxed,
                    )
                ),
                worker={"owner": OWNER, "runner": sandboxed},
                mode="shadow",
                shadow_limit=1,
            )
        # The second sample was skipped: no verify action exists for it and the
        # in-beat verdict alone was returned.
        assert coordinator_actions.load(state, "verify-att_b2") is None
        assert verify_workers.shadow_budget_used(state) == 1

    def test_on_mode_consumes_worker_evidence_and_the_default_stays_in_beat(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        scanner = make_scanner(tmp_path)
        patch = make_patch(repo, base, NEW_FILES)
        leases.record_lease(state, "att_w1", brief(base))

        def in_beat():
            raise AssertionError("the in-beat verifier must not run in on mode")

        out = verify_workers.dispatch_verification(
            state, repo, request(base, patch, scanner, attempt="att_w1"),
            key=WORKER_KEY,
            in_beat=in_beat,
            worker={"owner": OWNER, "runner": sandboxed},
            mode="on",
        )
        assert out["path"] == "on"
        assert out["verdict"]["ok"] is True
        assert "verify-bee-1" in out["verdict"]["evidence"]
        assert out["shadow_alarms_present"] is False

        assert verify_workers.DEFAULT_VERIFY_WORKERS_MODE == "in-beat"
        assert verify_workers.resolve_mode() == "in-beat"
        assert verify_workers.resolve_mode(config={"verify_workers": "shadow"}) == "shadow"
        with pytest.raises(verify_workers.VerifyWorkersError) as caught:
            verify_workers.resolve_mode(config={"verify_workers": "yolo"})
        assert caught.value.code == "mode_invalid"


class Reproof:
    __test__ = True

    def test_reprove_commit_reproves_the_git_facts_without_tests(self, tmp_path):
        repo, base = make_repo(tmp_path)
        sha = commit_files(repo, base, NEW_FILES, "verify: att_r1")
        record = synthetic_record(sha=sha, base_rev=base, files=sorted(NEW_FILES), attempt_id="att_r1")

        assert verify_local.reprove_commit(repo, record)["ok"] is True

        forged = synthetic_record(sha="d" * 40, base_rev=base, files=sorted(NEW_FILES), attempt_id="att_r1")
        reproof = verify_local.reprove_commit(repo, forged)
        assert reproof["ok"] is False and reproof["problems"] == ["commit_missing"]

        moved = synthetic_record(sha=sha, base_rev="b" * 40, files=sorted(NEW_FILES), attempt_id="att_r1")
        reproof = verify_local.reprove_commit(repo, moved)
        assert reproof["ok"] is False and reproof["problems"] == ["commit_mismatch"]
