"""M3 correction tests: evidence retention, fail-closed checks, cumulative waves,
agent-claim recovery, strict rebinding, and one end-to-end mechanical run.

The end-to-end case starts from M1's real `create_job()` and drives the real job
CLI tick against a launcher HTTP fixture, through verification, two waves, and a
final integrated tree that contains both tasks.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bundlebees_plugin import integrate_job, job as jobs, job_cli, leases, verify_local
from bundlebees_plugin.launcher_adapter import LeaseBinder

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]


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


A_FILES = {"pkg/a.py": "def a():\n    return 'a'\n", "tests/test_a.py": "from pkg.a import a\n\ndef test_a():\n    assert a() == 'a'\n"}
B_FILES = {"pkg/b.py": "def b():\n    return 'b'\n", "tests/test_b.py": "from pkg.b import b\n\ndef test_b():\n    assert b() == 'b'\n"}


def lease_brief(base: str, files: dict[str, str], objective: str, tests: list[str]) -> dict:
    return {
        "objective": objective,
        "source_rev": base,
        "assigned_files": list(files),
        "acceptance": {"criteria": [objective], "tests": tests},
    }


class FailClosed:
    __test__ = True

    def _repo(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        leases.record_lease(state, "att_1", lease_brief(base, A_FILES, "a", ["tests/test_a.py"]))
        return repo, base, state, patch_for(repo, base, A_FILES)

    def test_missing_gitleaks_is_an_environment_failure(self, tmp_path):
        repo, base, state, patch = self._repo(tmp_path)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(A_FILES), base_rev=base, tests=["tests/test_a.py"], suite_command=SUITE, gitleaks=None, require_gitleaks=True)

        assert exc.value.code == "security_tool_missing"
        assert verify_local.read_record(state, "att_1") is None

    def test_a_gitleaks_finding_is_refused(self, tmp_path):
        repo, base, state, patch = self._repo(tmp_path)
        fake = tmp_path / "gitleaks"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(A_FILES), base_rev=base, tests=["tests/test_a.py"], suite_command=SUITE, gitleaks=str(fake), require_gitleaks=True)

        assert exc.value.code == "secrets_found"

    def test_an_empty_test_list_is_refused(self, tmp_path):
        repo, base, state, patch = self._repo(tmp_path)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(A_FILES), base_rev=base, tests=[], suite_command=SUITE, require_gitleaks=False)

        assert exc.value.code == "tests_required"

    def test_tampered_patch_bytes_are_refused(self, tmp_path):
        repo, base, state, patch = self._repo(tmp_path)

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch + "\n+EVIL = 1\n", declared_files=list(A_FILES), base_rev=base, tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False, expected_patch_sha256="0" * 64)

        assert exc.value.code == "artifact_mismatch"

    def test_a_contract_refusal_stops_verification(self, tmp_path):
        repo, base, state, patch = self._repo(tmp_path)

        def refuse(files):
            raise RuntimeError("the patch edits approved tests")

        with pytest.raises(verify_local.VerifyError) as exc:
            verify_local.verify_and_commit(state, repo, "att_1", patch=patch, declared_files=list(A_FILES), base_rev=base, tests=["tests/test_a.py"], suite_command=SUITE, require_gitleaks=False, acdd_preflight=refuse)

        assert exc.value.code == "contract_violation"


class Waves:
    __test__ = True

    def _verify(self, state, repo, base, attempt, files, tests):
        patch = patch_for(repo, base, files)
        leases.record_lease(state, attempt, lease_brief(base, files, attempt, tests))
        return verify_local.verify_and_commit(state, repo, attempt, patch=patch, declared_files=list(files), base_rev=base, tests=tests, suite_command=SUITE, require_gitleaks=False)["sha"]

    def test_second_wave_builds_on_the_first(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        sha1 = self._verify(state, repo, base, "att_1", A_FILES, ["tests/test_a.py"])

        job1 = {"job_id": "job_a", "goal": {"base_rev": base}, "tasks": [{"task_id": "T1", "state": "verified", "attempts": ["att_1"]}], "integration": {"wave": "w1"}}
        first = integrate_job.integrate(state, job1, repo, suite_command=SUITE)
        assert first["ok"] is True
        ref = integrate_job.integration_ref("job_a")
        assert git(repo, "rev-parse", ref).stdout.strip() == first["record"]["head_sha"]

        sha2 = self._verify(state, repo, base, "att_2", B_FILES, ["tests/test_b.py"])
        job2 = {"job_id": "job_a", "goal": {"base_rev": base}, "tasks": [{"task_id": "T2", "state": "verified", "attempts": ["att_2"]}], "integration": {"wave": "w2"}}
        second = integrate_job.integrate(state, job2, repo, suite_command=SUITE)

        assert second["ok"] is True
        assert second["start_rev"] == first["record"]["head_sha"]  # not the original base
        final = git(repo, "rev-parse", ref).stdout.strip()
        tree = git(repo, "ls-tree", "-r", "--name-only", final).stdout.split()
        assert "pkg/a.py" in tree and "pkg/b.py" in tree
        for sha in (sha1, sha2):
            assert git(repo, "merge-base", "--is-ancestor", sha, final, check=False).returncode == 0
        assert git(repo, "rev-parse", f"{sha1}^").stdout.strip() == base  # identities untouched


class ClaimRecovery:
    __test__ = True

    def _escalated(self, tmp_path):
        jobs.record_authorization(tmp_path, job_id="job_a", statement="a real statement of intent", repo="mojomast/workflow", base_rev="817eac479f441396562962e880c1af596638f399", scope=["tools/**"], bees=2, wall_clock_s=3600, interactive=True)
        jobs.create_job(tmp_path, job_id="job_a", tasks=[{"task_id": "T1", "objective": "x", "lease": ["tools/x.py"], "acceptance": {"criteria": ["x"], "tests": ["tests/x.py"]}}])
        from bundlebees_plugin.reconcile import TickResult

        job_cli.record_handoff(tmp_path, "job_a", TickResult(status="escalated", evidence="dispatch_ambiguous", detail="two matches"))

    def test_a_dead_agent_turn_releases_its_claim(self, tmp_path):
        self._escalated(tmp_path)
        import time as _time

        t0 = _time.time()
        assert job_cli.pending_prompt(tmp_path, "job_a", now=t0) is not None
        assert job_cli.pending_prompt(tmp_path, "job_a", now=t0 + 10) is None
        again = job_cli.pending_prompt(tmp_path, "job_a", now=t0 + 10_000, claim_timeout_s=300)

        assert again is not None
        assert job_cli.read_handoff(tmp_path, "job_a")["reclaimed"] == 1

    def test_acknowledgement_is_idempotent(self, tmp_path):
        self._escalated(tmp_path)
        import time as _time

        job_cli.acknowledge(tmp_path, "job_a", "re-dispatch", now=_time.time())
        job_cli.acknowledge(tmp_path, "job_a", "re-dispatch", now=_time.time() + 5)

        events = [e for e in jobs.load_job(tmp_path, "job_a")["events"] if e["transition"] == "agent_ack"]
        assert len(events) == 1

    def test_a_crash_between_handoff_and_event_is_repaired(self, tmp_path):
        self._escalated(tmp_path)
        payload = job_cli.read_handoff(tmp_path, "job_a")
        # Simulate the crash: the handoff was marked done, the event never written.
        payload.update(status="done", acknowledged_at="2026-11-13T05:00:00Z", decision="merge after review")
        job_cli._write_handoff(tmp_path, "job_a", payload)
        assert not [e for e in jobs.load_job(tmp_path, "job_a")["events"] if e["transition"] == "agent_ack"]

        job_cli.acknowledge(tmp_path, "job_a", "merge after review")

        events = [e for e in jobs.load_job(tmp_path, "job_a")["events"] if e["transition"] == "agent_ack"]
        assert len(events) == 1


class Rebinding:
    __test__ = True

    def test_an_objective_change_is_a_lease_mismatch(self, tmp_path):
        brief = lease_brief("817eac479f441396562962e880c1af596638f399", A_FILES, "a", ["tests/test_a.py"])
        LeaseBinder(tmp_path)("att_1", brief)

        with pytest.raises(Exception) as exc:
            LeaseBinder(tmp_path)("att_1", dict(brief, objective="different work"))
        assert getattr(exc.value, "code", "") == "lease_mismatch"

    def test_an_acceptance_change_is_a_lease_mismatch(self, tmp_path):
        brief = lease_brief("817eac479f441396562962e880c1af596638f399", A_FILES, "a", ["tests/test_a.py"])
        LeaseBinder(tmp_path)("att_1", brief)

        with pytest.raises(Exception) as exc:
            LeaseBinder(tmp_path)("att_1", dict(brief, acceptance={"criteria": ["changed"], "tests": ["tests/other.py"]}))
        assert getattr(exc.value, "code", "") == "lease_mismatch"


# ---------------------------------------------------------------------------
# End-to-end: M1 create_job -> two dependent tasks -> two waves
# ---------------------------------------------------------------------------


class LauncherFixture:
    """HTTP launcher: accepts a brief, journals it, serves its result envelope."""

    def __init__(self, base_rev: str, patches: dict[str, tuple[str, list[str]]]):
        self.base_rev = base_rev
        self.patches = patches
        self.journal: list[dict] = []
        self.counter = 0
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path == "/v1/attempts":
                    return self._json(200, {"attempts": fixture.journal})
                if self.path.endswith("/result"):
                    attempt_id = self.path.split("/")[-2]
                    entry = next((e for e in fixture.journal if e["attempt_id"] == attempt_id), None)
                    if entry is None:
                        return self._json(404, {"error": "unknown_attempt"})
                    patch, files = fixture.patches[entry["brief"]["task_id"]]
                    return self._json(
                        200,
                        {
                            "schema": "orbit.local-worker.result/v1",
                            "attempt_id": attempt_id,
                            "source": {"repo_rev": fixture.base_rev},
                            "patch": {"diff": patch, "files": [{"path": name, "status": "A"} for name in files]},
                        },
                    )
                if self.path.startswith("/v1/attempts/"):
                    attempt_id = self.path.rsplit("/", 1)[-1]
                    entry = next((e for e in fixture.journal if e["attempt_id"] == attempt_id), None)
                    return self._json(200, entry) if entry else self._json(404, {"error": "unknown_attempt"})
                return self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else payload
                fixture.counter += 1
                attempt_id = f"att_{fixture.counter}"
                entry = {
                    "attempt_id": attempt_id,
                    "state": "succeeded",
                    "created_at": job_cli._now(),
                    "brief": brief,
                }
                fixture.journal.append(entry)
                return self._json(200, {"attempt_id": attempt_id, "state": "queued"})

        return Handler


class EndToEnd:
    __test__ = True

    def test_two_dependent_tasks_run_through_two_waves_without_a_human(self, tmp_path):
        repo, base = make_repo(tmp_path)
        patches = {
            "T1": (patch_for(repo, base, A_FILES), list(A_FILES)),
            "T2": (patch_for(repo, base, B_FILES), list(B_FILES)),
        }
        fixture = LauncherFixture(base, patches)
        try:
            state = tmp_path / "state"
            jobs.record_authorization(
                state,
                job_id="job_e2e",
                statement="Add a() and b() with tests, nothing else.",
                repo="mojomast/bundlebees",
                base_rev=base,
                scope=["pkg/**", "tests/**"],
                bees=2,
                wall_clock_s=3600,
                interactive=True,
            )
            jobs.create_job(
                state,
                job_id="job_e2e",
                tasks=[
                    {"task_id": "T1", "objective": "add a()", "lease": list(A_FILES), "acceptance": {"criteria": ["a"], "tests": ["tests/test_a.py"]}},
                    {"task_id": "T2", "objective": "add b()", "lease": list(B_FILES), "depends_on": ["T1"], "acceptance": {"criteria": ["b"], "tests": ["tests/test_b.py"]}},
                ],
            )

            from bundlebees_plugin.client import LauncherClient

            client = LauncherClient(fixture.url, token="test-token")
            heads_before = git(repo, "rev-parse", "HEAD").stdout.strip()

            statuses = []
            for step in range(10):
                result = job_cli.tick(state, "job_e2e", client=client, repo=repo, suite_command=SUITE, require_gitleaks=False)
                statuses.append(result["status"])
                job = jobs.load_job(state, "job_e2e")
                if all(t["state"] == "accepted" for t in job["tasks"]):
                    break

            job = jobs.load_job(state, "job_e2e")
            assert [t["state"] for t in job["tasks"]] == ["accepted", "accepted"], statuses
            assert len(job["integration"]["accepted_waves"]) == 2

            ref = integrate_job.integration_ref("job_e2e")
            final = git(repo, "rev-parse", ref).stdout.strip()
            tree = git(repo, "ls-tree", "-r", "--name-only", final).stdout.split()
            assert {"pkg/a.py", "pkg/b.py", "tests/test_a.py", "tests/test_b.py"} <= set(tree)

            record_a = verify_local.read_record(state, "att_1")
            record_b = verify_local.read_record(state, "att_2")
            for record in (record_a, record_b):
                assert record["ok"] is True
                assert git(repo, "merge-base", "--is-ancestor", record["sha"], final, check=False).returncode == 0
            # Worker commits and the user's checkout are untouched.
            assert git(repo, "rev-parse", "HEAD").stdout.strip() == heads_before
            assert git(repo, "status", "--porcelain").stdout == ""
            assert git(repo, "remote").stdout.strip() == ""
        finally:
            fixture.close()
