"""P4: RETURN packets are independently verifiable from durable evidence.

Every positive manifest here is built from a real temporary Git repository, a
real ``verify --commit-local`` record, and a real durable wave record written by
``integrate_job``/``revision``; the descendant manifest additionally comes from
``handoff.materialize`` (an enrolled policy, a source-resolution record and a
transport receipt published to a local bare mirror).

The negatives corrupt exactly one durable record and demand the matching
refusal name, so a report can never be produced by describing a job as
accepted: acceptance needs evidence, and evidence needs to still be there.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import contract as contracts
from bundlebees_plugin import (
    handoff,
    integrate_job,
    job as jobs,
    job_cli,
    leases,
    packets,
    return_report,
    tdd,
    transport,
    verify_local,
)
from tests.fakes import FakeCtx, load_plugin

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()

REPO = "mojomast/smoke-real"
GITLEAKS = os.environ.get("GITLEAKS") or shutil.which("gitleaks")
SUITE = [sys.executable, "-m", "pytest", "-q"]

pytestmark = pytest.mark.skipif(
    not (HAS_WORKFLOW or REQUIRE_WORKFLOW),
    reason="set WORKFLOW_SRC to the pinned workflow checkout",
)


def _need_workflow() -> None:
    if not HAS_WORKFLOW:
        pytest.fail("WORKFLOW_SRC must point at the pinned workflow checkout when REQUIRE_WORKFLOW=1")
    if not GITLEAKS:
        pytest.fail("GITLEAKS must point at the gitleaks scanner when REQUIRE_WORKFLOW=1")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _git(args, cwd: Path) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": os.environ.get("HOME", "/tmp"), "GIT_TERMINAL_PROMPT": "0"},
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def _patch_new(path: str, body: str) -> str:
    lines = body.splitlines()
    out = [
        f"diff --git a/{path} b/{path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    out += ["+" + line for line in lines]
    return "\n".join(out) + "\n"


def _frozen(source_rev: str, *, contract_id: str, statement: str, assigned: str,
            test_file: str, test_first: bool = False) -> tuple[dict, str]:
    body = {
        "contract_version": "acdd/1",
        "id": contract_id,
        "title": statement,
        "outcome": {"summary": statement, "observable_behavior": [statement]},
        "source_rev": source_rev,
        "scope": {"assigned_files": [assigned], "test_files": [test_file]},
        "acceptance_criteria": [
            {"id": "AC-001", "statement": statement, "tests": [test_file], "verification": "test"}
        ],
        "tdd": {"applicable": True, "test_first": test_first,
                "reason": "the approved test exists and fails at the baseline"},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"] if test_first else ["green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    frozen = contracts.freeze_contract(body, approved_by="reviewer", approved_at="2026-11-13T00:00:00Z")
    return frozen, contracts.check_frozen(frozen)


def _exact_packet(*, job_id: str, feature_id: str, source_rev: str, frozen: dict,
                  contract_sha: str, lease: str, test_file: str) -> dict:
    packet = json.loads((Path(WORKFLOW_SRC) / "schemas" / "examples" / "feature-exact.json").read_text())
    packet["workflow_ref"] = _git(["rev-parse", "HEAD"], Path(WORKFLOW_SRC))
    packet["job_id"] = job_id
    packet["feature_id"] = feature_id
    packet["repository"]["identity"] = REPO
    packet["source_policy"]["revision"] = source_rev
    packet["contract"] = frozen
    packet["contract_sha256"] = contract_sha
    packet["scope"] = {"lease": [lease], "read_only": [test_file]}
    packet["tests"] = [test_file]
    packet["acceptance_ids"] = ["AC-001"]
    packet["budget"] = {"bees": 1, "wall_clock_s": 1800}
    return packet


def _make_repo(base: Path) -> tuple[Path, str]:
    repo = base / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    (repo / "README.md").write_text("base\n")
    (repo / "smoke").mkdir()
    (repo / "smoke" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_smoke.py").write_text(
        "from smoke.answer import answer\n\n\ndef test_answer_is_42():\n    assert answer() == 42\n"
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "root"], repo)
    return repo, _git(["rev-parse", "HEAD"], repo)


def _build_exact(base: Path) -> dict:
    """A packet-native exact job whose verification and wave records are real."""

    _need_workflow()
    state = base / "state"
    state.mkdir()
    repo, root = _make_repo(base)
    frozen, contract_sha = _frozen(root, contract_id="SMOKE-A", statement="answer() returns 42",
                                   assigned="smoke/answer.py", test_file="tests/test_smoke.py")
    packet = _exact_packet(job_id="job_a", feature_id="feature-a", source_rev=root, frozen=frozen,
                           contract_sha=contract_sha, lease="smoke/answer.py",
                           test_file="tests/test_smoke.py")
    jobs.record_authorization(state, job_id="job_a", statement="Feature A: answer() returns 42", repo=REPO,
                              base_rev=root, scope=["smoke/answer.py"], bees=1, wall_clock_s=1800,
                              require_tty=False, interactive=False, by="reviewer")
    jobs.create_job(
        state, job_id="job_a", packet=packet,
        tasks=[{"task_id": "T1", "objective": "answer", "lease": ["smoke/answer.py"],
                "contract": frozen, "contract_sha256": contract_sha,
                "acceptance": {"criteria": ["answer() returns 42"], "tests": ["tests/test_smoke.py"]}}],
    )
    leases.record_lease(state, "att_1", {
        "objective": "answer", "source_rev": root, "assigned_files": ["smoke/answer.py"],
        "job_id": "job_a", "task_id": "T1",
        "acceptance": {"criteria": ["answer() returns 42"], "tests": ["tests/test_smoke.py"]}})
    record = verify_local.verify_and_commit(
        state, repo, "att_1", patch=_patch_new("smoke/answer.py", "def answer():\n    return 42\n"),
        declared_files=["smoke/answer.py"], base_rev=root, tests=["tests/test_smoke.py"],
        suite_command=SUITE, gitleaks=GITLEAKS,
    )
    job = jobs.load_job(state, "job_a")
    job["tasks"][0].update(state="verified", attempts=["att_1"])
    jobs.save_job(state, job)
    result = integrate_job.integrate(state, jobs.load_job(state, "job_a"), repo, suite_command=SUITE)
    assert result["ok"], result["evidence"]
    accepted = result["record"]["head_sha"]
    job = jobs.load_job(state, "job_a")
    job["tasks"][0].update(state="accepted")
    job["integration"] = {
        "wave": 2, "state": "pending", "last_commit": accepted,
        "accepted_waves": [{"wave": 1, "at": result["record"]["at"],
                            "evidence": "integrated", "commit": accepted}],
    }
    jobs.save_job(state, job)
    return {
        "state": state, "repo": repo, "job_id": "job_a", "attempt": "att_1", "root": root,
        "verified": record["sha"], "accepted": accepted, "contract": frozen,
        "contract_sha256": contract_sha, "packet_digest": packets.digest(packet),
        "workflow_ref": packet["workflow_ref"], "wave_path": state / "waves" / "job_a.w1.json",
    }


def _build_descendant(base: Path) -> dict:
    """Two features: an accepted dependency and a materialized dependent job."""

    _need_workflow()
    state = base / "state"
    state.mkdir()
    repo, root = _make_repo(base)
    mirror = base / "mirror.git"
    _git(["init", "-q", "--bare", str(mirror)], base)

    jobs.record_authorization(state, job_id="job_a", statement="Feature A: answer() returns 42", repo=REPO,
                              base_rev=root, scope=["smoke/answer.py", "tests/test_double.py"], bees=1,
                              wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
    jobs.create_job(state, job_id="job_a", summary="Feature A", workflow_ref="d" * 40,
                    tasks=[{"task_id": "TA", "objective": "answer", "lease": ["smoke/answer.py"]}])
    leases.record_lease(state, "att_1", {
        "objective": "answer", "source_rev": root, "assigned_files": ["smoke/answer.py"],
        "job_id": "job_a", "task_id": "TA", "acceptance": {"tests": ["tests/test_smoke.py"]}})
    record_a = verify_local.verify_and_commit(
        state, repo, "att_1", patch=_patch_new("smoke/answer.py", "def answer():\n    return 42\n"),
        declared_files=["smoke/answer.py"], base_rev=root, tests=["tests/test_smoke.py"],
        suite_command=SUITE, gitleaks=GITLEAKS,
    )
    _git(["checkout", "-q", "-B", "main", record_a["sha"]], repo)
    (repo / "tests" / "test_double.py").write_text(
        "from smoke.double import double_answer\n\n\ndef test_double_is_84():\n    assert double_answer() == 84\n"
    )
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "approved test for feature B"], repo)
    accepted = _git(["rev-parse", "HEAD"], repo)
    job_a = jobs.load_job(state, "job_a")
    job_a["tasks"][0].update(state="accepted", attempts=["att_1"])
    job_a["integration"] = {"wave": 2, "state": "pending", "last_commit": accepted,
                            "accepted_waves": [{"wave": 1, "at": "2026-11-13T00:00:00Z",
                                                "evidence": "integrated", "commit": accepted}]}
    jobs.save_job(state, job_a)

    head = _git(["rev-parse", "HEAD"], Path(WORKFLOW_SRC))
    packet = json.loads((Path(WORKFLOW_SRC) / "schemas" / "examples" / "feature-descendant.json").read_text())
    packet["workflow_ref"] = head
    packet["job_id"] = "job_b"
    packet["feature_id"] = "feature-b"
    packet["repository"]["identity"] = REPO
    packet["source_policy"].update(root_revision=root, policy_id="policy-b", dependencies=["feature-a"])
    packet["contract_template"]["source_rev"] = root
    packet["budget"] = {"bees": 1, "wall_clock_s": 1800}

    handoff.create_approval_key(state)
    handoff.enroll_policy(
        state, policy_id="policy-b", statement="Feature B may build on Feature A's accepted commit",
        repo=REPO, root_rev=root, dependency_job="job_a", dependency_feature="feature-a",
        dependent_feature="feature-b", dependent_job="job_b", scope=["smoke/double.py"],
        bees=1, wall_clock_s=1800, contract_template=copy.deepcopy(packet["contract_template"]),
        gates=("merge",), workflow_ref=head,
    )
    made = handoff.materialize(state, feature_id="feature-b", job_id="job_b",
                               packet=copy.deepcopy(packet), worktree=repo, mirror=mirror,
                               repo_identity=REPO)
    job_b = jobs.load_job(state, "job_b")
    task = job_b["tasks"][0]
    leases.record_lease(state, "att_b", {
        "objective": "double", "source_rev": accepted,
        "assigned_files": list(task["contract"]["scope"]["assigned_files"]),
        "job_id": "job_b", "task_id": task["task_id"],
        "acceptance": {"tests": ["tests/test_double.py"]}})
    record_b = verify_local.verify_and_commit(
        state, repo, "att_b", patch=_patch_new("smoke/double.py", "def double_answer():\n    return 84\n"),
        declared_files=list(task["contract"]["scope"]["assigned_files"]), base_rev=accepted,
        tests=["tests/test_double.py"], suite_command=SUITE, gitleaks=GITLEAKS,
    )
    job_b = jobs.load_job(state, "job_b")
    job_b["tasks"][0].update(state="verified", attempts=["att_b"])
    jobs.save_job(state, job_b)
    result = integrate_job.integrate(state, jobs.load_job(state, "job_b"), repo, suite_command=SUITE)
    assert result["ok"], result["evidence"]
    head_b = result["record"]["head_sha"]
    job_b = jobs.load_job(state, "job_b")
    job_b["tasks"][0].update(state="accepted")
    job_b["integration"] = {"wave": 2, "state": "pending", "last_commit": head_b,
                            "accepted_waves": [{"wave": 1, "at": result["record"]["at"],
                                                "evidence": "integrated", "commit": head_b}]}
    jobs.save_job(state, job_b)
    return {
        "state": state, "repo": repo, "job_id": "job_b", "attempt": "att_b", "root": root,
        "accepted": accepted, "verified": record_b["sha"], "integration": head_b,
        "packet_digest": made["packet_digest"], "policy_digest": made["policy_digest"],
        "mirror_ref": made["mirror_ref"], "contract_sha256": made["contract_sha256"],
    }


@pytest.fixture(scope="module")
def exact_env(tmp_path_factory):
    return _build_exact(tmp_path_factory.mktemp("exact"))


@pytest.fixture(scope="module")
def descendant_env(tmp_path_factory):
    return _build_descendant(tmp_path_factory.mktemp("descendant"))


def _copy_state(env: dict, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    shutil.copytree(env["state"], state)
    return state


def _forced_write(path: Path, payload: str) -> None:
    """Rewrite a create-only record (0400) in a fixture the test owns."""

    os.chmod(path, 0o600)
    path.write_text(payload)


def _edit_job(state: Path, job_id: str, mutate) -> None:
    job = jobs.load_job(state, job_id)
    mutate(job)
    jobs.save_job(state, job)


def _rewrite_verify(state: Path, attempt: str, mutate) -> None:
    path = verify_local.record_path(state, attempt)
    record = json.loads(path.read_text())
    mutate(record)
    path.write_text(json.dumps(record, indent=2, sort_keys=True))


def _standalone_job(state: Path, *, job_id: str, attempt: str | None, state_name: str = "pending",
                    claim: str = "") -> None:
    """A packet-free job with one task, for classification cases."""

    jobs.record_authorization(state, job_id=job_id, statement="A small, honest job with one task",
                              repo=REPO, base_rev="a" * 40, scope=["tools/**"], bees=1,
                              wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
    jobs.create_job(state, job_id=job_id, tasks=[{
        "task_id": "T1", "objective": "x", "lease": ["tools/x.py"],
        "acceptance": {"criteria": ["the tool answers 42"], "tests": ["tests/test_tool.py"]}}])
    job = jobs.load_job(state, job_id)
    job["tasks"][0]["state"] = state_name
    job["tasks"][0]["attempts"] = [attempt] if attempt else []
    if claim:
        job["tasks"][0]["evidence"] = claim
    jobs.save_job(state, job)


def _oracle_digest(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k not in ("evidence_digest", "generated_at")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Positive manifests
# ---------------------------------------------------------------------------


class TestExactManifest:
    def test_the_manifest_binds_packet_contract_verification_and_wave(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)

        manifest = return_report.build(state, "job_a", repo=exact_env["repo"])

        assert manifest["schema"] == "rizzstack.return/1"
        assert manifest["feature_id"] == "feature-a"
        assert manifest["job_id"] == "job_a"
        # The packet digest is recomputed from the stored bytes, not copied.
        stored = packets.read_packet(state, "job_a")
        assert stored is not None
        assert manifest["packet_digest"] == hashlib.sha256(stored).hexdigest()
        assert manifest["packet_digest"] == exact_env["packet_digest"]
        assert manifest["workflow_ref"] == exact_env["workflow_ref"]
        assert manifest["repository"] == {"identity": REPO, "source_rev": exact_env["root"]}
        assert manifest["authorization"]["reference"] == "authorizations/job_a.json"
        assert manifest["authorization"]["approved_by"] == "reviewer"
        assert manifest["authorization"]["authority"] == "human"
        assert manifest["descendant"] is None
        assert manifest["contract"] == {"id": "SMOKE-A", "sha256": exact_env["contract_sha256"]}

        assert [item["id"] for item in manifest["acceptance"]] == ["AC-001"]
        criterion = manifest["acceptance"][0]
        assert criterion["status"] == "PASS"
        assert criterion["tests"] == ["tests/test_smoke.py"]
        assert criterion["evidence"] == ["verify-results/att_1.json"]

        assert manifest["red_green"] == {"test_first": False, "red": None, "green": None}
        assert manifest["verification"]["commit"] == exact_env["verified"]
        assert manifest["verification"]["suite"] == {"exit_code": 0}
        assert manifest["verification"]["gitleaks"] == {"exit_code": 0}
        assert manifest["verification"]["record_path"] == "verify-results/att_1.json"
        assert manifest["integration"]["commit"] == exact_env["accepted"]
        assert manifest["integration"]["wave"] == 1
        wave_record = json.loads(exact_env["wave_path"].read_text())
        assert manifest["integration"]["accepted_wave_digest"] == hashlib.sha256(
            return_report.canonical_bytes(wave_record)).hexdigest()
        assert manifest["transport"] is None
        assert manifest["attempts"] == [{"attempt_id": "att_1", "state": "accepted", "project": "",
                                        "source_rev": exact_env["root"]}]
        assert manifest["pending_approvals"] == ["merge"]
        assert manifest["receiver_decision"] == {"status": "pending", "decided_by": None,
                                                 "decided_at": None, "note": None}

        written = json.loads(return_report.return_path(state, "job_a").read_text())
        assert written == manifest
        assert manifest["evidence_digest"] == _oracle_digest(manifest)
        assert len(manifest["evidence_digest"]) == 64
        assert return_report.render_path(state, "job_a").read_text().startswith("# RETURN — feature-a")

    def test_a_descendant_manifest_carries_its_resolution_and_transport(self, descendant_env, tmp_path):
        state = _copy_state(descendant_env, tmp_path)

        manifest = return_report.build(state, "job_b", repo=descendant_env["repo"])

        resolution = json.loads((state / "resolutions" / "job_b.json").read_text())
        stored_packet = packets.read_packet(state, "job_b")
        assert stored_packet is not None
        packets.validate_resolution(resolution, packet=json.loads(stored_packet),
                                    accepted_commit=resolution["accepted_commit"])
        descendant = manifest["descendant"]
        assert descendant is not None
        assert descendant["policy_id"] == resolution["policy_id"] == "policy-b"
        assert descendant["policy_digest"] == resolution["policy_digest"] == descendant_env["policy_digest"]
        assert descendant["source_resolution_digest"] == hashlib.sha256(
            return_report.canonical_bytes(resolution)).hexdigest()
        assert descendant["dependency_feature"] == "feature-a"
        assert descendant["dependency_job"] == "job_a"
        assert descendant["accepted_commit"] == descendant_env["accepted"]
        assert manifest["repository"]["source_rev"] == descendant_env["accepted"]
        assert manifest["contract"]["id"] == "SMOKE-B"
        assert manifest["acceptance"][0]["status"] == "PASS"
        receipt = json.loads((state / "transport" / f"job_a.{descendant_env['accepted'][:12]}.json").read_text())
        assert manifest["transport"] == {
            "mirror_ref": receipt["mirror_ref"],
            "source_sha": receipt["source_sha"],
            "receipt_path": f"transport/job_a.{descendant_env['accepted'][:12]}.json",
        }
        assert manifest["transport"]["mirror_ref"] == descendant_env["mirror_ref"]
        assert manifest["transport"]["source_sha"] == descendant_env["accepted"]

    def test_building_twice_writes_byte_identical_files(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)

        first = return_report.build(state, "job_a", repo=exact_env["repo"])
        first_json = return_report.return_path(state, "job_a").read_bytes()
        first_md = return_report.render_path(state, "job_a").read_bytes()
        second = return_report.build(state, "job_a", repo=exact_env["repo"])

        assert first == second
        assert return_report.return_path(state, "job_a").read_bytes() == first_json
        assert return_report.render_path(state, "job_a").read_bytes() == first_md
        assert first["evidence_digest"] == second["evidence_digest"]

    def test_verify_accepts_a_report_built_from_the_same_state(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        manifest = return_report.build(state, "job_a", repo=exact_env["repo"])

        result = return_report.verify(state, "job_a", repo=exact_env["repo"])

        assert result["ok"] is True
        assert result["evidence_digest"] == manifest["evidence_digest"]
        assert result["return"] == "jobs/job_a/return.json"

    def test_the_legacy_report_is_extended_not_replaced(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)

        payload = job_cli.report(state, "job_a", repo=exact_env["repo"])

        assert payload["job_id"] == "job_a"
        assert payload["tasks"][0]["verified"] is True
        assert payload["measurements"] == {"tokens": "UNKNOWN", "cost": "UNKNOWN"}
        manifest = json.loads(return_report.return_path(state, "job_a").read_text())
        assert payload["return"] == {
            "schema": "rizzstack.return/1",
            "evidence_digest": manifest["evidence_digest"],
            "path": "jobs/job_a/return.json",
            "markdown": "jobs/job_a/RETURN.md",
            "acceptance": {"AC-001": "PASS"},
        }
        on_disk = json.loads((state / "jobs" / "job_a.report.json").read_text())
        assert on_disk == payload

    def test_the_cli_reports_and_verifies_with_the_right_exit_codes(self, exact_env, tmp_path, capsys):
        state = _copy_state(exact_env, tmp_path)
        ctx = FakeCtx()
        load_plugin().register(ctx)

        def invoke(argv):
            parser = argparse.ArgumentParser()
            ctx.cli_commands["bundlebees"]["setup_fn"](parser)
            return ctx.cli_commands["bundlebees"]["handler_fn"](parser.parse_args(argv))

        base = ["job", "report", "--job-id", "job_a", "--state-dir", str(state),
                "--repo", str(exact_env["repo"])]
        assert invoke(base) == 0
        assert json.loads(capsys.readouterr().out)["return"]["acceptance"] == {"AC-001": "PASS"}

        assert invoke(base + ["--verify"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["ok"] is True and len(printed["evidence_digest"]) == 64

        return_path = return_report.return_path(state, "job_a")
        body = json.loads(return_path.read_text())
        body["acceptance"][0]["status"] = "FAIL"
        return_path.write_text(json.dumps(body, indent=2, sort_keys=True))
        assert invoke(base + ["--verify"]) == 1
        assert json.loads(capsys.readouterr().out)["error"] == "report_tampered"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    def _manifest(self, env, tmp_path, mutate_record=None, task_state="rejected"):
        state = _copy_state(env, tmp_path)
        if mutate_record is not None:
            _rewrite_verify(state, env["attempt"], mutate_record)
        if task_state is not None:
            _edit_job(state, env["job_id"], lambda job: job["tasks"][0].update(state=task_state))
        return return_report.build(state, env["job_id"], repo=env["repo"])

    def test_a_pending_task_without_records_is_not_run(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        _standalone_job(state, job_id="job_n", attempt=None)

        manifest = return_report.build(state, "job_n")

        assert manifest["acceptance"][0]["status"] == "NOT RUN"
        assert manifest["acceptance"][0]["evidence"] == []
        assert "verification of T1" in manifest["unrun"]
        assert manifest["transport"] is None and manifest["descendant"] is None

    def test_a_claim_without_a_record_is_unverified(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        _standalone_job(state, job_id="job_n", attempt=None, claim="worker says it works")

        manifest = return_report.build(state, "job_n")

        assert manifest["acceptance"][0]["status"] == "UNVERIFIED"

    def test_a_record_that_does_not_cover_the_criterion_tests_is_unverified(self, exact_env, tmp_path):
        manifest = self._manifest(exact_env, tmp_path,
                                  mutate_record=lambda record: record.update(tests=["tests/other.py"]))

        assert manifest["acceptance"][0]["status"] == "UNVERIFIED"
        assert "does not cover" in " ".join(manifest["limitations"])

    def test_a_verification_without_a_suite_result_is_unverified(self, exact_env, tmp_path):
        manifest = self._manifest(exact_env, tmp_path, mutate_record=lambda record: record.update(suite=None))

        assert manifest["acceptance"][0]["status"] == "UNVERIFIED"
        assert "regression suite result" in manifest["unrun"]

    def test_a_failed_suite_is_fail(self, exact_env, tmp_path):
        manifest = self._manifest(
            exact_env, tmp_path,
            mutate_record=lambda record: record.update(suite={"exit_code": 1}))

        assert manifest["acceptance"][0]["status"] == "FAIL"
        assert manifest["acceptance"][0]["evidence"] == ["verify-results/att_1.json"]

    def test_a_failed_gitleaks_scan_is_fail(self, exact_env, tmp_path):
        manifest = self._manifest(
            exact_env, tmp_path,
            mutate_record=lambda record: record.update(gitleaks={"exit_code": 1}))

        assert manifest["acceptance"][0]["status"] == "FAIL"

    def test_an_unreadable_evidence_file_blocks_the_criterion(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        _standalone_job(state, job_id="job_n", attempt="att_x")
        path = verify_local.record_path(state, "att_x")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")

        manifest = return_report.build(state, "job_n")

        assert manifest["acceptance"][0]["status"] == "BLOCKED"
        assert "cannot be read" in " ".join(manifest["limitations"])

    def test_a_frozen_contract_that_no_longer_verifies_blocks_its_criteria(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        job = jobs.load_job(state, "job_a")
        job["tasks"][0]["contract"]["outcome"]["summary"] = "mutated after approval"
        job["tasks"][0]["state"] = "rejected"
        jobs.save_job(state, job)

        manifest = return_report.build(state, "job_a", repo=exact_env["repo"])

        assert manifest["acceptance"][0]["status"] == "BLOCKED"
        assert manifest["contract"] == {"id": "", "sha256": ""}
        assert "cannot be used" in " ".join(manifest["limitations"])


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_a_changed_intent_packet_is_refused(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        path = packets.packet_path(state, "job_a")
        body = json.loads(path.read_text())
        body["request"]["summary"] = "something else entirely"
        _forced_write(path, json.dumps(body, sort_keys=True, separators=(",", ":")))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "packet_mismatch"

    def test_a_mismatched_resolution_digest_is_refused(self, descendant_env, tmp_path):
        state = _copy_state(descendant_env, tmp_path)
        path = handoff.resolution_path(state, "job_b")
        record = json.loads(path.read_text())
        record["packet_digest"] = "0" * 64
        _forced_write(path, json.dumps(record, indent=2, sort_keys=True))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_b", repo=descendant_env["repo"])

        assert caught.value.code == "resolution_mismatch"

    def test_a_resolution_whose_policy_bytes_changed_is_refused(self, descendant_env, tmp_path):
        state = _copy_state(descendant_env, tmp_path)
        path = handoff.resolution_path(state, "job_b")
        record = json.loads(path.read_text())
        record["policy_digest"] = "1" * 64
        _forced_write(path, json.dumps(record, indent=2, sort_keys=True))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_b", repo=descendant_env["repo"])

        assert caught.value.code == "resolution_mismatch"

    def test_an_invalid_wave_record_is_refused(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        record = json.loads((state / "waves" / "job_a.w1.json").read_text())
        record["accepted"] = False
        (state / "waves" / "job_a.w1.json").write_text(json.dumps(record, indent=2, sort_keys=True))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "wave_mismatch"

    def test_a_wave_head_that_moved_is_refused(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        record = json.loads((state / "waves" / "job_a.w1.json").read_text())
        record["head_sha"] = "0" * 40
        (state / "waves" / "job_a.w1.json").write_text(json.dumps(record, indent=2, sort_keys=True))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "wave_mismatch"

    def test_a_verification_commit_absent_from_git_is_refused(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        _rewrite_verify(state, "att_1", lambda record: record.update(sha="0" * 40))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "verification_commit_absent"

    def test_a_missing_verification_record_for_an_accepted_task_is_refused(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        verify_local.record_path(state, "att_1").unlink()

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "verification_missing"

    def test_an_accepted_job_with_an_uncovered_criterion_is_refused(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        _rewrite_verify(state, "att_1", lambda record: record.update(tests=["tests/other.py"]))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "acceptance_uncovered"
        assert "AC-001: UNVERIFIED" in str(caught.value)

    def test_an_unverified_mirror_claim_is_refused(self, descendant_env, tmp_path):
        state = _copy_state(descendant_env, tmp_path)
        receipt = state / "transport" / f"job_a.{descendant_env['accepted'][:12]}.json"
        receipt.unlink()

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_b", repo=descendant_env["repo"])

        assert caught.value.code == "transport_mismatch"
        assert "no transport receipt" in str(caught.value)

    def test_a_receipt_for_another_mirror_ref_is_refused(self, descendant_env, tmp_path):
        state = _copy_state(descendant_env, tmp_path)
        path = state / "transport" / f"job_a.{descendant_env['accepted'][:12]}.json"
        receipt = json.loads(path.read_text())
        receipt["mirror_ref"] = transport.ref_for("job_a", "0" * 40)
        path.write_text(json.dumps(receipt, indent=2, sort_keys=True))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.build(state, "job_b", repo=descendant_env["repo"])

        assert caught.value.code == "transport_mismatch"


# ---------------------------------------------------------------------------
# Signed evidence, legacy jobs, receiver decision, tampering
# ---------------------------------------------------------------------------


class TestHonesty:
    def test_a_fabricated_signed_record_never_produces_a_pass(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        # Without a suite result the verification record alone cannot PASS...
        _rewrite_verify(state, "att_1", lambda record: record.update(suite=None))
        _edit_job(state, "job_a", lambda job: job["tasks"][0].update(state="rejected"))
        contract = exact_env["contract"]
        record = tdd.make_record(
            contract, exact_env["contract_sha256"], phase="green", revision=exact_env["verified"],
            test_fingerprints={"tests/test_smoke.py": "b" * 64},
            command=["pytest", "tests/test_smoke.py"],
            result={"exit_code": 0, "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "timed_out": False,
                    "cases": [{"id": "tests/test_smoke.py::test_answer_is_42", "outcome": "passed"}]},
            produced_by={"role": "verifier", "name": "fixture"},
            test_first=False,
        )
        forged = tdd.sign_record(record, b"not-the-evidence-key" * 2)
        evidence = state / "evidence" / "SMOKE-A" / "0001-green.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(forged))

        manifest = return_report.build(state, "job_a", repo=exact_env["repo"])

        assert manifest["acceptance"][0]["status"] != "PASS"
        assert manifest["red_green"]["green"] is None
        assert any("not authentic" in note for note in manifest["limitations"])

    def test_a_validated_green_record_is_evidence(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        _rewrite_verify(state, "att_1", lambda record: record.update(suite=None))
        _edit_job(state, "job_a", lambda job: job["tasks"][0].update(state="rejected"))
        key = leases.evidence_key(state)
        record = tdd.make_record(
            exact_env["contract"], exact_env["contract_sha256"], phase="green",
            revision=exact_env["verified"],
            test_fingerprints={"tests/test_smoke.py": "b" * 64},
            command=["pytest", "tests/test_smoke.py"],
            result={"exit_code": 0, "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "timed_out": False,
                    "cases": [{"id": "tests/test_smoke.py::test_answer_is_42", "outcome": "passed"}]},
            produced_by={"role": "verifier", "name": "fixture"},
            test_first=False,
        )
        tdd.write_record(state, record, key)

        manifest = return_report.build(state, "job_a", repo=exact_env["repo"])

        assert manifest["acceptance"][0]["status"] == "PASS"
        assert manifest["red_green"]["green"].startswith("evidence/SMOKE-A/")

    def test_a_test_first_contract_reports_its_missing_red(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        frozen, sha = _frozen("a" * 40, contract_id="SMOKE-T", statement="the tool answers 42",
                              assigned="tools/x.py", test_file="tests/test_tool.py", test_first=True)
        jobs.record_authorization(state, job_id="job_t", statement="A test-first job with one task",
                                  repo=REPO, base_rev="a" * 40, scope=["tools/**"], bees=1,
                                  wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
        jobs.create_job(state, job_id="job_t", tasks=[{
            "task_id": "T1", "objective": "x", "lease": ["tools/x.py"],
            "contract": frozen, "contract_sha256": sha,
            "acceptance": {"criteria": ["the tool answers 42"], "tests": ["tests/test_tool.py"]}}])

        manifest = return_report.build(state, "job_t")

        assert manifest["red_green"] == {"test_first": True, "red": None, "green": None}
        assert any(note.startswith("signed RED evidence") for note in manifest["unrun"])
        assert manifest["acceptance"][0]["status"] == "NOT RUN"

    def test_a_packet_free_job_is_labelled_and_fabricates_no_packet_evidence(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        _standalone_job(state, job_id="job_legacy", attempt=None)

        manifest = return_report.build(state, "job_legacy")

        assert manifest["packet_digest"] == "" and manifest["feature_id"] == ""
        assert manifest["workflow_ref"] == ""
        assert manifest["descendant"] is None and manifest["transport"] is None
        assert manifest["contract"] == {"id": "", "sha256": ""}
        assert any(note.startswith("legacy:") for note in manifest["limitations"])
        assert set(manifest) == {
            "schema", "feature_id", "job_id", "packet_digest", "workflow_ref", "repository",
            "authorization", "descendant", "contract", "acceptance", "red_green", "verification",
            "integration", "transport", "attempts", "limitations", "unrun", "pending_approvals",
            "receiver_decision", "evidence_digest",
        }
        assert json.loads(return_report.return_path(state, "job_legacy").read_text()) == manifest

    def test_receiver_decision_stays_pending_even_if_the_job_doc_says_otherwise(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        _edit_job(state, "job_a", lambda job: job.update(
            receiver_decision={"status": "accepted", "decided_by": "mallory",
                               "decided_at": "2026-11-13T00:00:00Z", "note": "looks good"}))

        manifest = return_report.build(state, "job_a", repo=exact_env["repo"])

        assert manifest["receiver_decision"] == {"status": "pending", "decided_by": None,
                                                 "decided_at": None, "note": None}

    def test_a_hand_edited_manifest_is_detected(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        return_report.build(state, "job_a", repo=exact_env["repo"])
        path = return_report.return_path(state, "job_a")
        body = json.loads(path.read_text())
        body["acceptance"][0]["status"] = "PASS"
        body["acceptance"][0]["statement"] = "something the evidence never showed"
        path.write_text(json.dumps(body, indent=2, sort_keys=True))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.verify(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "report_tampered"

    def test_a_hand_edited_markdown_is_detected(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)
        return_report.build(state, "job_a", repo=exact_env["repo"])
        path = return_report.render_path(state, "job_a")
        path.write_text(path.read_text().replace("| PASS |", "| PASS (verified by hand) |"))

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.verify(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "report_tampered"

    def test_a_missing_report_cannot_be_verified(self, exact_env, tmp_path):
        state = _copy_state(exact_env, tmp_path)

        with pytest.raises(return_report.ReturnError) as caught:
            return_report.verify(state, "job_a", repo=exact_env["repo"])

        assert caught.value.code == "report_tampered"
