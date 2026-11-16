"""Causal regression attribution (workflow-F6): name the offending attempt.

When a wave's full suite fails, the recovery the failure earns must name the
worker patch that caused it, not just the failing suite. The attribution
correlates each failing test selector with the attempts whose leases cover the
failing file:

* one attempt's lease covers every failing file -> that attempt is named, with
  the failing selectors, in the integrate result and in the recovery request;
* the attribution is lease-based, so it survives supersession: the named
  attempt is the one whose lease covered the failing file *now*, and a
  superseded attempt is never named for a failure its replacement caused;
* two attempts' leases covering a file is ambiguity: the record states that
  explicitly and names no attempt, instead of guessing.

The protocol-gated request tests run against the pinned recovery validator:

    WORKFLOW_SRC=/tmp/verify/wf-packet REQUIRE_WORKFLOW=1 \\
        python -m pytest tests/test_regression_attribution.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import contract as contracts
from bundlebees_plugin import integrate_job, leases, recovery, verify_local
from bundlebees_plugin import job as jobs

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"

SHA = "817eac479f441396562962e880c1af596638f399"
APPROVED_AT = "2026-11-13T00:00:00Z"


@pytest.fixture
def pinned_protocol():
    if not WORKFLOW_SRC:
        if REQUIRE_WORKFLOW:
            pytest.fail("REQUIRE_WORKFLOW=1 but WORKFLOW_SRC does not name the pinned workflow checkout")
        pytest.skip("set WORKFLOW_SRC to the pinned workflow checkout; recovery refuses without its validator")


# ---------------------------------------------------------------------------
# Real repository helpers: verify commits are real, the wave suite is faked
# ---------------------------------------------------------------------------


def sandboxed(workdir, argv):
    """The explicit runner the verify stages use (disposable copy, exact env)."""

    from bundlebees_plugin import sandbox

    return sandbox.run_in_sandbox(workdir, list(argv), allow_unsafe_local=True, timeout=120)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD").stdout.strip()


def patch_for(repo: Path, base: str, files: dict[str, str]) -> str:
    git(repo, "checkout", "-q", "-b", "w", base)
    for name, body in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", base).stdout
    git(repo, "reset", "-q", "--hard", base)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", "w")
    return diff


def verified(state: Path, repo: Path, base: str, attempt: str, files: dict[str, str]) -> str:
    test_file = next(name for name in files if name.startswith("tests/"))
    leases.record_lease(
        state,
        attempt,
        {
            "objective": f"work for {attempt}",
            "source_rev": base,
            "assigned_files": list(files),
            "acceptance": {"criteria": ["x"], "tests": [test_file]},
        },
    )
    record = verify_local.verify_and_commit(
        state,
        repo,
        attempt,
        patch=patch_for(repo, base, files),
        declared_files=list(files),
        base_rev=base,
        tests=[test_file],
        suite_command=SUITE,
        require_gitleaks=False,
        runner=sandboxed,
    )
    return record["sha"]


def lease_only(state: Path, base: str, attempt: str, files: list[str], test_file: str) -> None:
    leases.record_lease(
        state,
        attempt,
        {
            "objective": f"work for {attempt}",
            "source_rev": base,
            "assigned_files": list(files),
            "acceptance": {"criteria": ["x"], "tests": [test_file]},
        },
    )


def failing_suite(failed: list[str]):
    """A wave-suite runner that reports the named failing ids and exits 1."""

    def run(workdir, argv):
        return {
            "exit_code": 1,
            "timed_out": False,
            "output_tail": "FAILED " + " FAILED ".join(failed),
            "junit": {"tests": 2, "failures": len(failed), "errors": 0},
            "cases": [{"id": test_id, "outcome": "failed"} for test_id in failed],
        }

    return run


def accepted_suite(workdir, argv):
    return {"exit_code": 0, "timed_out": False, "output_tail": "1 passed", "junit": {"tests": 1, "failures": 0, "errors": 0}}


def job_for(base: str, tasks: list[dict]) -> dict:
    return {
        "job_id": "job_attr",
        "goal": {"base_rev": base},
        "tasks": tasks,
        "integration": {"wave": 1},
    }


def task_of(job: dict, task_id: str) -> dict:
    return next(task for task in job["tasks"] if task["task_id"] == task_id)


# ---------------------------------------------------------------------------
# Attribution itself: lease correlation, ambiguity, supersession
# ---------------------------------------------------------------------------


class Attribution:
    __test__ = True

    def _leases(self, tmp_path: Path, spec: dict[str, list[str]]) -> tuple[Path, str]:
        state = tmp_path / "state"
        for attempt, files in spec.items():
            lease_only(
                state,
                SHA,
                attempt,
                files,
                next((name for name in files if name.startswith("tests/")), "tests/test_x.py"),
            )
        return state, SHA

    def test_one_attempt_covering_the_failing_file_is_named(self, tmp_path):
        state, _base = self._leases(tmp_path, {"att_1": ["pkg/a.py", "tests/test_a.py"]})
        job = job_for(SHA, [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}])

        found = recovery.attribute_regressions(
            state, job, failed_tests=["tests/test_a.py::test_a"], attempts=["att_1"]
        )

        assert found["attributed"] is True
        assert found["attempt_id"] == "att_1"
        assert found["tests"] == ["tests/test_a.py::test_a"]
        assert found["files"] == ["tests/test_a.py"]
        assert found["reason"] == ""

    def test_two_attempts_touching_the_file_leave_no_attribution(self, tmp_path):
        state, _base = self._leases(
            tmp_path,
            {"att_1": ["pkg/a.py", "tests/test_shared.py"], "att_2": ["pkg/b.py", "tests/test_shared.py"]},
        )
        job = job_for(
            SHA,
            [
                {"task_id": "T1", "state": "verified", "attempts": ["att_1"]},
                {"task_id": "T2", "state": "verified", "attempts": ["att_2"]},
            ],
        )

        found = recovery.attribute_regressions(
            state, job, failed_tests=["tests/test_shared.py::test_x"], attempts=["att_1", "att_2"]
        )

        assert found["attributed"] is False
        assert found["attempt_id"] == ""
        assert found["candidates"]["tests/test_shared.py"] == ["att_1", "att_2"]
        assert found["ambiguous"] == ["tests/test_shared.py"]
        assert "att_1" in found["reason"] and "att_2" in found["reason"]
        assert "ambiguous" in found["reason"]

    def test_attribution_survives_supersession_by_naming_the_current_lease_holder(self, tmp_path):
        state, _base = self._leases(
            tmp_path,
            {"att_1": ["pkg/a.py", "tests/test_t.py"], "att_2": ["pkg/b.py", "tests/test_t.py"]},
        )
        # The superseded attempt's lease still covers the file, but the wave
        # integrated att_2: the named attempt is the lease holder *now*.
        job = job_for(SHA, [{"task_id": "T1", "state": "verified", "attempts": ["att_1", "att_2"]}])

        found = recovery.attribute_regressions(
            state, job, failed_tests=["tests/test_t.py::test_t"], attempts=["att_2"]
        )
        assert found["attributed"] is True
        assert found["attempt_id"] == "att_2"

        derived = recovery.attribute_regressions(state, job, failed_tests=["tests/test_t.py::test_t"])
        assert derived["attributed"] is True
        assert derived["attempt_id"] == "att_2"

    def test_a_file_no_attempt_covered_is_stated_not_guessed(self, tmp_path):
        state, _base = self._leases(tmp_path, {"att_1": ["pkg/a.py", "tests/test_a.py"]})
        job = job_for(SHA, [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}])

        found = recovery.attribute_regressions(
            state, job, failed_tests=["tests/test_pre_existing.py::test_old"], attempts=["att_1"]
        )

        assert found["attributed"] is False
        assert found["uncovered"] == ["tests/test_pre_existing.py"]
        assert "no attempt in the wave has a lease covering" in found["reason"]

    def test_failures_on_two_different_attempts_are_ambiguous_too(self, tmp_path):
        state, _base = self._leases(
            tmp_path,
            {"att_1": ["pkg/a.py", "tests/test_a.py"], "att_2": ["pkg/b.py", "tests/test_b.py"]},
        )
        job = job_for(
            SHA,
            [
                {"task_id": "T1", "state": "verified", "attempts": ["att_1"]},
                {"task_id": "T2", "state": "verified", "attempts": ["att_2"]},
            ],
        )

        found = recovery.attribute_regressions(
            state, job, failed_tests=["tests/test_a.py::test_a", "tests/test_b.py::test_b"],
            attempts=["att_1", "att_2"],
        )

        assert found["attributed"] is False
        assert found["attempt_id"] == ""
        assert "att_1" in found["reason"] and "att_2" in found["reason"]

    def test_no_failing_test_names_nothing(self, tmp_path):
        state, _base = self._leases(tmp_path, {"att_1": ["pkg/a.py", "tests/test_a.py"]})
        job = job_for(SHA, [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}])

        found = recovery.attribute_regressions(state, job, failed_tests=[], attempts=["att_1"])

        assert found["attributed"] is False
        assert "names no failing test" in found["reason"]


# ---------------------------------------------------------------------------
# The failing integration reports the cause
# ---------------------------------------------------------------------------


A_FILES = {
    "pkg/a.py": "def a():\n    return 'a'\n",
    "tests/test_a.py": "from pkg.a import a\n\ndef test_a():\n    assert a() == 'a'\n",
}
B_FILES = {
    "pkg/b.py": "def b():\n    return 'b'\n",
    "tests/test_b.py": "from pkg.b import b\n\ndef test_b():\n    assert b() == 'b'\n",
}
SHARED_TEST = "def test_shared():\n    assert True\n"
SHARED_FILES_A = {"pkg/shared.py": "def shared():\n    return 'shared'\n", "tests/test_shared.py": SHARED_TEST}
SHARED_FILES_B = {"pkg/b.py": "def b():\n    return 'b'\n", "tests/test_shared.py": SHARED_TEST}


class FailingIntegration:
    __test__ = True

    def test_a_failing_integration_names_the_offending_attempt_and_selector(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        verified(state, repo, base, "att_1", A_FILES)
        verified(state, repo, base, "att_2", B_FILES)
        job = job_for(
            base,
            [
                {"task_id": "T1", "state": "verified", "attempts": ["att_1"]},
                {"task_id": "T2", "state": "verified", "attempts": ["att_2"]},
            ],
        )

        result = integrate_job.integrate(
            state, job, repo, suite_command=SUITE, runner=failing_suite(["tests/test_a.py::test_a"])
        )

        assert result["ok"] is False
        attribution = result["attribution"]
        assert attribution["attributed"] is True
        assert attribution["attempt_id"] == "att_1"
        assert attribution["tests"] == ["tests/test_a.py::test_a"]
        assert "offending attempt att_1" in result["evidence"]
        assert "tests/test_a.py::test_a" in result["evidence"]
        assert result["failure"]["code"] == "combined_suite_failed"
        assert result["failure"]["attribution"] == attribution
        assert result["failure"]["failed_tests"] == ["tests/test_a.py::test_a"]

    def test_an_ambiguous_failing_integration_says_so_instead_of_guessing(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        verified(state, repo, base, "att_1", SHARED_FILES_A)
        verified(state, repo, base, "att_2", SHARED_FILES_B)
        job = job_for(
            base,
            [
                {"task_id": "T1", "state": "verified", "attempts": ["att_1"]},
                {"task_id": "T2", "state": "verified", "attempts": ["att_2"]},
            ],
        )

        result = integrate_job.integrate(
            state, job, repo, suite_command=SUITE, runner=failing_suite(["tests/test_shared.py::test_shared"])
        )

        assert result["ok"] is False
        attribution = result["attribution"]
        assert attribution["attributed"] is False
        assert attribution["attempt_id"] == ""
        assert attribution["candidates"]["tests/test_shared.py"] == ["att_1", "att_2"]
        assert "no offending attempt named" in result["evidence"]
        assert "att_1" in result["evidence"] and "att_2" in result["evidence"]

    def test_the_named_attempt_is_the_lease_holder_after_a_supersession(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        # att_1 is the superseded attempt: its lease covered the file, but the
        # wave integrates only att_2, the task's current attempt.
        verified(state, repo, base, "att_1", SHARED_FILES_A)
        verified(state, repo, base, "att_2", SHARED_FILES_B)
        job = job_for(base, [{"task_id": "T1", "state": "verified", "attempts": ["att_1", "att_2"]}])

        result = integrate_job.integrate(
            state, job, repo, suite_command=SUITE, runner=failing_suite(["tests/test_shared.py::test_shared"])
        )

        assert result["ok"] is False
        attribution = result["attribution"]
        assert attribution["attributed"] is True
        assert attribution["attempt_id"] == "att_2"
        assert "offending attempt att_2" in result["evidence"]
        assert "att_1" not in result["evidence"]

    def test_an_accepted_wave_carries_no_attribution(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        verified(state, repo, base, "att_1", A_FILES)
        verified(state, repo, base, "att_2", B_FILES)
        job = job_for(
            base,
            [
                {"task_id": "T1", "state": "verified", "attempts": ["att_1"]},
                {"task_id": "T2", "state": "verified", "attempts": ["att_2"]},
            ],
        )

        result = integrate_job.integrate(state, job, repo, suite_command=SUITE, runner=accepted_suite)

        assert result["ok"] is True
        assert result["attribution"] is None
        assert result["failure"] is None


# ---------------------------------------------------------------------------
# The recovery request records the attribution
# ---------------------------------------------------------------------------


def frozen_contract(
    *,
    contract_id: str,
    assigned: str,
    test_file: str,
    test_id: str,
) -> dict:
    body = {
        "contract_version": "acdd/1",
        "id": contract_id,
        "title": "the feature works",
        "outcome": {"summary": "the feature works", "observable_behavior": ["the feature works"]},
        "source_rev": SHA,
        "scope": {"assigned_files": [assigned], "test_files": [test_file]},
        "acceptance_criteria": [
            {"id": "AC-001", "statement": "the feature works", "tests": [test_id], "verification": "test"}
        ],
        "tdd": {"applicable": True, "test_first": False, "reason": "the approved test exists at baseline"},
        "verification": {"independent": True},
        "evidence_required": ["green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    return contracts.freeze_contract(body, approved_by="reviewer", approved_at=APPROVED_AT)


def attributed_job(state: Path, *, test_files: tuple[str, str] = ("tests/test_a.py", "tests/test_b.py")) -> tuple[dict, dict]:
    """An authorized two-task job whose attempts hold leases over their files."""

    specs = [
        ("T1", "att_1", "SPEC-1", "pkg/a.py", test_files[0]),
        ("T2", "att_2", "SPEC-2", "pkg/b.py", test_files[1]),
    ]
    bodies = []
    attempt_of: dict[str, str] = {}
    authorized: list[str] = []
    for task_id, attempt, contract_id, assigned, test_file in specs:
        test_id = f"{test_file}::test_{task_id.lower()}"
        lease_only(state, SHA, attempt, [assigned, test_file], test_file)
        contract = frozen_contract(
            contract_id=contract_id, assigned=assigned, test_file=test_file, test_id=test_id
        )
        attempt_of[task_id] = attempt
        authorized.append(test_id)
        bodies.append(
            {
                "task_id": task_id,
                "objective": f"work for {task_id}",
                "lease": [assigned, test_file],
                "contract": contract,
                "contract_sha256": contracts.check_frozen(contract),
                "acceptance": {"criteria": ["the feature works"], "tests": [test_file]},
            }
        )
    envelope = {
        "max_attempts_per_task": 2,
        "contract_corrections_allowed": True,
        # The approver pre-authorizes the regression test a bounded repair may
        # add: the failing selector. The envelope can only narrow this.
        "corrected_tests_allowed": authorized,
        "file_scope": ["pkg/**", "tests/**"],
        "test_scope": ["tests/**"],
    }
    jobs.record_authorization(
        state,
        job_id="job_attr",
        statement="Two independent features; a bounded repair may add the named regression test.",
        repo="mojomast/smoke",
        base_rev=SHA,
        scope=["pkg/**", "tests/**"],
        bees=3,
        wall_clock_s=3600,
        interactive=True,
        recovery=envelope,
    )
    jobs.create_job(state, job_id="job_attr", tasks=bodies)
    job = jobs.load_job(state, "job_attr")
    for task in job["tasks"]:
        # The coordinator's ledger: each task's current (verified) attempt.
        task["state"] = "verified"
        task["attempts"] = [attempt_of[str(task["task_id"])]]
    jobs.save_job(state, job)
    return jobs.load_job(state, "job_attr"), jobs.load_authorization(state, "job_attr")


def suite_failure_record(failed: list[str], attempts: list[str], *, carried: dict | None = None) -> dict:
    record = {
        "code": "combined_suite_failed",
        "evidence": f"combined_suite_failed: wave job_attr.w1 rejected: failed {', '.join(failed)}",
        "wave_id": "job_attr.w1",
        "failed_tests": list(failed),
        "attempts": list(attempts),
    }
    if carried is not None:
        record["attribution"] = carried
    return record


class RecoveryRequest:
    __test__ = True

    def test_the_request_names_the_offending_attempt_and_its_failing_selector(self, tmp_path, pinned_protocol):
        state = tmp_path / "state"
        job, authorization = attributed_job(state)
        task_t1 = task_of(job, "T1")
        record = suite_failure_record(["tests/test_a.py::test_t1"], ["att_1", "att_2"])

        request = recovery.build_request(
            state, job, task_t1, failure=record, authorization=authorization, now=1750000000.0
        )

        # The offending attempt is named, and the request is recorded against
        # the task whose attempt caused the failure.
        assert request["attempt_id"] == "att_1"
        assert request["task_id"] == "T1"
        assert "the offending attempt is att_1" in request["failure"]["rationale"].lower()
        assert "tests/test_a.py::test_t1" in request["failure"]["rationale"]
        assert "the offending attempt is att_1" in request["outstanding_question"].lower()
        assert request["failure"]["evidence"][0]["paths"] == ["tests/test_a.py"]
        # The failing selector the repair may add rides in the envelope, because
        # the approver pre-authorized it there.
        assert "tests/test_a.py::test_t1" in request["envelope"]["corrected_tests_allowed"]

        artefact = json.loads((state / request["failure"]["evidence"][0]["reference"]).read_text())
        assert artefact["record"]["attribution"]["attempt_id"] == "att_1"
        assert artefact["record"]["attribution"]["tests"] == ["tests/test_a.py::test_t1"]

        verdict = recovery.deterministic_adapter(request, task=task_of(jobs.load_job(state, "job_attr"), "T1"))
        assert verdict["decision"] == "retry"
        recovery.record_decision(
            state, request["request_id"], {"decision": "retry", "rationale": "one bounded repair of the offending patch"}
        )

    def test_the_request_redirects_to_the_offending_task(self, tmp_path, pinned_protocol):
        state = tmp_path / "state"
        job, authorization = attributed_job(state)
        # The caller names T1; the failing file is covered only by att_2's lease.
        record = suite_failure_record(["tests/test_b.py::test_t2"], ["att_1", "att_2"])

        request = recovery.build_request(
            state,
            job,
            task_of(job, "T1"),
            failure=record,
            authorization=authorization,
            now=1750000000.0,
        )

        assert request["task_id"] == "T2"
        assert request["attempt_id"] == "att_2"
        assert "the offending attempt is att_2" in request["failure"]["rationale"].lower()
        assert "tests/test_b.py::test_t2" in request["envelope"]["corrected_tests_allowed"]

    def test_an_ambiguous_failure_records_no_offender_explicitly(self, tmp_path, pinned_protocol):
        state = tmp_path / "state"
        job, authorization = attributed_job(
            state, test_files=("tests/test_shared.py", "tests/test_shared.py")
        )
        # Both attempts' leases cover the shared file: the evidence is ambiguous.
        record = suite_failure_record(
            ["tests/test_shared.py::test_t1"], ["att_1", "att_2"]
        )

        request = recovery.build_request(
            state,
            job,
            task_of(job, "T1"),
            failure=record,
            authorization=authorization,
            now=1750000000.0,
        )

        assert request["attempt_id"] == "att_1"  # the ledger's attempt, not a guess
        rationale = request["failure"]["rationale"]
        assert "no offending attempt is named" in rationale.lower()
        assert "att_1" in rationale and "att_2" in rationale
        assert "ambiguous" in rationale
        assert "no offending attempt is named" in request["outstanding_question"].lower()
        artefact = json.loads((state / request["failure"]["evidence"][0]["reference"]).read_text())
        assert artefact["record"]["attribution"]["attributed"] is False

    def test_a_carried_attribution_is_recorded_verbatim(self, tmp_path, pinned_protocol):
        state = tmp_path / "state"
        job, authorization = attributed_job(state)
        carried = {
            "attributed": True,
            "attempt_id": "att_1",
            "tests": ["tests/test_a.py::test_t1"],
            "files": ["tests/test_a.py"],
        }
        record = suite_failure_record(["tests/test_a.py::test_t1"], ["att_1", "att_2"], carried=carried)

        request = recovery.build_request(
            state,
            job,
            task_of(job, "T1"),
            failure=record,
            authorization=authorization,
            now=1750000000.0,
        )

        assert request["attempt_id"] == "att_1"
        assert "the offending attempt is att_1" in request["failure"]["rationale"].lower()
        artefact = json.loads((state / request["failure"]["evidence"][0]["reference"]).read_text())
        assert artefact["record"]["attribution"]["tests"] == ["tests/test_a.py::test_t1"]
