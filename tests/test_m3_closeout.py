"""M3 closeout: evidence validation, frozen-contract propagation, crash-safe waves."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job, job as jobs, job_cli, leases, verify_local

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]
SHA = "817eac479f441396562962e880c1af596638f399"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), capture_output=True, text=True)
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


FILES = {"pkg/a.py": "def a():\n    return 'a'\n", "tests/test_a.py": "from pkg.a import a\n\ndef test_a():\n    assert a() == 'a'\n"}


def setup_verified(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    brief = {
        "objective": "add a()",
        "source_rev": base,
        "assigned_files": list(FILES),
        "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"]},
    }
    leases.record_lease(state, "att_1", brief)
    patch = patch_for(repo, base, FILES)
    record = verify_local.verify_and_commit(
        state, repo, "att_1", patch=patch, declared_files=list(FILES), base_rev=base,
        tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False,
    )
    return repo, base, state, patch, record


class RecordReuse:
    __test__ = True

    @pytest.mark.parametrize(
        "field, value",
        [
            ("red", {"exit_code": 0}),            # claims tests passed before the patch
            ("suite", {"exit_code": 1}),          # claims a failing suite
            ("patch_sha256", "0" * 64),           # claims a different artifact
            ("tests", ["tests/other.py"]),        # claims different targets
            ("base_rev", "1" * 40),               # claims a different base
        ],
    )
    def test_present_but_invalid_evidence_is_re_earned(self, tmp_path, field, value):
        repo, base, state, patch, record = setup_verified(tmp_path)
        tampered = dict(record)
        tampered[field] = value
        verify_local.record_path(state, "att_1").write_text(json.dumps(tampered))

        again = verify_local.verify_and_commit(
            state, repo, "att_1", patch=patch, declared_files=list(FILES), base_rev=base,
            tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False,
        )

        # The invalid record was not reused: fresh evidence replaced it.
        assert again["red"]["exit_code"] != 0
        assert again["suite"]["exit_code"] == 0
        assert again["patch_sha256"] == record["patch_sha256"]
        assert again["base_rev"] == base
        assert again["tests"] == ["tests/test_a.py"]

    def test_a_record_for_a_different_commit_is_not_reused(self, tmp_path):
        repo, base, state, patch, record = setup_verified(tmp_path)
        tampered = dict(record, sha="2" * 40)
        verify_local.record_path(state, "att_1").write_text(json.dumps(tampered))

        again = verify_local.verify_and_commit(
            state, repo, "att_1", patch=patch, declared_files=list(FILES), base_rev=base,
            tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False,
        )

        assert again["sha"] != "2" * 40
        assert git(repo, "cat-file", "-t", again["sha"]).stdout.strip() == "commit"

    def test_a_valid_record_is_still_reused(self, tmp_path):
        repo, base, state, patch, record = setup_verified(tmp_path)
        again = verify_local.verify_and_commit(
            state, repo, "att_1", patch=patch, declared_files=list(FILES), base_rev=base,
            tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False,
        )
        assert again["sha"] == record["sha"]


CONTRACT = {
    "contract_version": "acdd/1",
    "id": "C-a",
    "title": "add a()",
    "outcome": {"summary": "a() exists and is tested", "observable_behavior": ["a() returns a"]},
    "source_rev": "PLACEHOLDER",
    "scope": {"assigned_files": ["pkg/a.py"], "test_files": ["tests/test_a.py"]},
    "acceptance_criteria": [
        {"id": "AC-001", "statement": "a() returns 'a'", "tests": ["tests/test_a.py"], "verification": "test"}
    ],
    "tdd": {"applicable": True, "test_first": True},
    "verification": {"independent": True},
    "evidence_required": ["red", "green"],
    "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
}


class Contracts:
    __test__ = True

    def _job(self, tmp_path, base, contract: dict, *, sha=None):
        state = tmp_path / "state"
        jobs.record_authorization(state, job_id="job_c", statement="do the contracted work", repo="mojomast/bundlebees", base_rev=base, scope=["pkg/**", "tests/**"], bees=1, wall_clock_s=3600, interactive=True)
        (tmp_path / "contract.json").write_text(json.dumps(contract))
        digest = __import__("hashlib").sha256((tmp_path / "contract.json").read_bytes()).hexdigest()
        jobs.create_job(
            state,
            job_id="job_c",
            tasks=[{
                "task_id": "T1",
                "objective": "add a()",
                "lease": ["pkg/a.py"],
                "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"]},
                "contract_path": "contract.json",
                "contract_sha256": sha if sha is not None else digest,
            }],
        )
        return state, jobs.load_job(state, "job_c")

    def test_a_declared_contract_is_hydrated_and_dispatched_in_the_brief(self, tmp_path):
        repo, base = make_repo(tmp_path)
        contract = dict(CONTRACT, source_rev=base)
        state, job = self._job(tmp_path, base, contract)

        hydrated = job_cli._hydrate_contracts(state, job, tmp_path)
        assert hydrated["tasks"][0]["contract"]["id"] == "C-a"

        from bundlebees_plugin.reconcile import _brief_for

        brief = _brief_for(hydrated, hydrated["tasks"][0])
        assert brief["contract"]["id"] == "C-a"

        leases.record_lease(state, "att_x", brief)
        record = leases.load_lease(state, "att_x")
        assert record["brief"]["contract"]["contract_version"] == "acdd/1"

    def test_a_missing_contract_file_stops_the_tick(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state, job = self._job(tmp_path, base, dict(CONTRACT, source_rev=base))
        (tmp_path / "contract.json").unlink()

        with pytest.raises(job_cli.JobCliError) as exc:
            job_cli._hydrate_contracts(state, job, tmp_path)
        assert exc.value.code == "contract_unavailable"

    def test_a_changed_contract_stops_the_tick(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state, job = self._job(tmp_path, base, dict(CONTRACT, source_rev=base), sha="0" * 64)

        with pytest.raises(job_cli.JobCliError) as exc:
            job_cli._hydrate_contracts(state, job, tmp_path)
        assert exc.value.code == "contract_mismatch"

    def test_a_contracted_task_never_verifies_without_a_preflight(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        leases.record_lease(state, "att_1", {"objective": "a", "source_rev": base, "assigned_files": list(FILES), "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"]}, "contract": dict(CONTRACT, source_rev=base)})
        patch = patch_for(repo, base, FILES)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(FILES), base_rev=base, tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False, contract_declared=True)
        assert exc.value.code == "contract_missing"

    def test_editing_an_approved_test_is_refused_by_the_contract(self, tmp_path):
        from bundlebees_plugin import acdd_cli
        from bundlebees_plugin import contract as contract_mod

        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        contract = contract_mod.freeze_contract(dict(CONTRACT, source_rev=base), approved_by="tester")
        brief = {
            "objective": "a",
            "source_rev": base,
            "assigned_files": ["pkg/a.py"],
            "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"]},
            "contract": contract,
        }
        leases.record_lease(state, "att_1", brief)
        record = leases.load_lease(state, "att_1")

        with pytest.raises(Exception) as exc:
            acdd_cli.verify_preflight(record, state, ["pkg/a.py", "tests/test_a.py"])
        assert getattr(exc.value, "code", "") == "tests_tampered"


class WaveRecovery:
    __test__ = True

    def _wave1(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        leases.record_lease(state, "att_1", {"objective": "a", "source_rev": base, "assigned_files": list(FILES), "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"]}})
        patch = patch_for(repo, base, FILES)
        verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(FILES), base_rev=base, tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False)
        job = {"job_id": "job_w", "goal": {"base_rev": base}, "tasks": [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}], "integration": {"wave": "w1"}}
        first = integrate_job.integrate(state, job, repo, suite_command=SUITE)
        return repo, base, state, job, first

    def test_a_crash_before_the_ref_update_is_repaired(self, tmp_path):
        repo, base, state, job, first = self._wave1(tmp_path)
        ref = integrate_job.integration_ref("job_w")
        git(repo, "update-ref", "-d", ref)

        again = integrate_job.integrate(state, job, repo, suite_command=SUITE)

        assert again["ok"] is True
        assert again["recovered"] is True
        assert git(repo, "rev-parse", ref).stdout.strip() == first["record"]["head_sha"]

    def test_a_crash_after_the_ref_does_not_reintegrate(self, tmp_path):
        repo, base, state, job, first = self._wave1(tmp_path)
        before = git(repo, "rev-parse", "refs/bb/integrated/job_w").stdout.strip()

        again = integrate_job.integrate(state, job, repo, suite_command=SUITE)

        assert again["recovered"] is True
        assert again["record"]["head_sha"] == first["record"]["head_sha"]
        assert git(repo, "rev-parse", "refs/bb/integrated/job_w").stdout.strip() == before

        waves = sorted((state / "waves").glob("*.json"))
        assert len(waves) == 1  # no second wave record was written
