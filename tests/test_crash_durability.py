"""Durable reconciliation: corrupt evidence, atomic acceptance, crash recovery.

Every case here is about a state a crash can leave behind:

* a queue claim record that cannot be read;
* a wave record that was accepted and published *before* the job was saved;
* a wave record that exists but is damaged;
* a dispatch that was persisted and a tick that never returned.

The rule under test is always the same: the durable record is the truth.
Damage is a named refusal — never "no evidence", which would be permission to
do the same work again. A clean restart adds no commits, attempts, wave records
or authorizations.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from bundlebees_plugin import integrate_job, job as jobs, job_cli, leases, queue, reconcile, revision, verify_local
from bundlebees_plugin import faults

PY = sys.executable
SUITE = [PY, "-m", "pytest", "-q"]
REPO = "mojomast/smoke-real"

GITLEAKS = (
    os.environ.get("GITLEAKS")
    or shutil.which("gitleaks")
    or "/home/clanker01/.local/bin/gitleaks"
)
# The scanner is one exit code in this lane's evidence; use the real binary when
# it is installed and a trivially-clean stand-in when it is not.
SCANNER = GITLEAKS if Path(GITLEAKS).exists() else "/bin/true"

FILES = {
    "pkg/a.py": "def a():\n    return 'a'\n",
    "tests/test_a.py": "from pkg.a import a\n\n\ndef test_a():\n    assert a() == 'a'\n",
}
ACCEPTANCE = {"criteria": ["a() returns 'a'"], "tests": ["tests/test_a.py"], "commands": ["pytest tests/test_a.py"]}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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


def authorize(state: Path, *, job_id: str, base: str) -> dict:
    return jobs.record_authorization(
        state,
        job_id=job_id,
        statement="durable reconciliation lane work, nothing else",
        repo=REPO,
        base_rev=base,
        scope=sorted(FILES),
        bees=1,
        wall_clock_s=900,
        require_tty=False,
        interactive=False,
        by="reviewer",
    )


def make_job(state: Path, *, job_id: str, base: str) -> dict:
    authorize(state, job_id=job_id, base=base)
    return jobs.create_job(
        state,
        job_id=job_id,
        tasks=[{"task_id": "T1", "objective": "add a()", "lease": sorted(FILES), "acceptance": ACCEPTANCE}],
    )


def verified_attempt(state: Path, repo: Path, base: str, attempt_id: str = "att_1") -> dict:
    """A real verified, locally committed attempt (real git, real suite)."""
    brief = {
        "objective": "add a()",
        "source_rev": base,
        "assigned_files": sorted(FILES),
        "acceptance": ACCEPTANCE,
    }
    leases.record_lease(state, attempt_id, brief)
    patch = patch_for(repo, base, FILES)
    return verify_local.verify_and_commit(
        state,
        repo,
        attempt_id,
        patch=patch,
        declared_files=sorted(FILES),
        base_rev=base,
        tests=["tests/test_a.py"],
        suite_command=SUITE,
        gitleaks=SCANNER,
        require_gitleaks=True,
    )


def verified_job(tmp_path: Path, *, job_id: str = "job_d") -> tuple[Path, str, Path, dict]:
    """A real repo and a job whose single task is verified but not integrated."""
    repo, base = make_repo(tmp_path)
    state = tmp_path / "state"
    job = make_job(state, job_id=job_id, base=base)
    verified_attempt(state, repo, base)
    for step in ("dispatching", "dispatched", "running", "verifying", "verified"):
        jobs.transition(job, "T1", step, actor="test", reason=step)
    # What a dispatch tick records: the attempt belongs to the task from the
    # moment the launcher accepted it, before verification.
    job["tasks"][0]["attempts"] = ["att_1"]
    jobs.save_job(state, job)
    return repo, base, state, job


def crash_state(tmp_path: Path, *, job_id: str = "job_d") -> tuple[Path, str, Path, dict, dict]:
    """The exact state a crash at the integration boundary leaves behind.

    The wave record is accepted and published, the durable ref is updated, and
    the job document on disk still says the task is ``verified``: it never got
    the save that follows integration.
    """
    repo, base, state, job = verified_job(tmp_path, job_id=job_id)
    integrated = integrate_job.integrate(state, job, repo, suite_command=SUITE)
    assert integrated["ok"] is True, integrated
    return repo, base, state, job, integrated


def accepted_job(tmp_path: Path, *, job_id: str = "job_d") -> tuple[Path, Path, str]:
    """crash_state, recovered: the accepted wave is adopted and persisted."""
    repo, _base, state, _job, _integrated = crash_state(tmp_path, job_id=job_id)
    adopted = jobs.load_job(state, job_id)
    jobs.save_job(state, adopted)
    job_cli.recover(state, job_id, repo)
    return repo, state, job_id


@pytest.fixture
def faults_hooks():
    """The real per-process fault registry, disarmed around every test."""
    faults.clear()
    yield faults
    faults.clear()


def record(*names: str) -> list[tuple[str, dict[str, Any]]]:
    """Register one recording hook per checkpoint name; return its log.

    The real registry calls ``hook(**payload)`` without the name, so a shared
    hook could not tell the names apart; one closure per name keeps it exact.
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    def recorder(name: str):
        return lambda **payload: calls.append((name, dict(payload)))

    for name in names:
        faults.register(name, recorder(name))
    return calls


def recovering_tick(state: Path, repo: Path):
    """A tick that performs the crash recovery a production tick performs."""

    def tick(job_id: str) -> dict[str, Any]:
        job_cli.recover(state, job_id, repo)
        return {"status": "ok"}

    return tick


def integrating_tick(state: Path, repo: Path):
    """A tick that integrates for real, then recovers as production does.

    ``integrate_job.integrate`` publishes the accepted wave before the job is
    saved, which is exactly the crash window this lane closes; the following
    ``recover`` is the restart's adoption of that record.
    """

    def tick(job_id: str) -> dict[str, Any]:
        job = jobs.load_job(state, job_id, recover=False)
        integrate_job.integrate(state, job, repo, suite_command=SUITE)
        jobs.save_job(state, job)
        job_cli.recover(state, job_id, repo)
        return {"status": "ok"}

    return tick


def boom(**_payload: Any) -> None:
    raise RuntimeError("simulated crash at a fault checkpoint")


class Client:
    """A launcher client that records POSTs and never recycles a journal entry."""

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def create(self, brief):
        self.posts.append(dict(brief))
        return {"attempt_id": f"att_{len(self.posts)}"}

    def attempts_raw(self) -> list[dict]:
        return []

    def get_raw(self, attempt_id):
        return None

    def result_envelope(self, attempt_id):
        return {"patch": {"diff": "", "files": []}}


class Launcher:
    """The reconciler's four-member contract, with no journal entries."""

    idempotent = True

    def __init__(self) -> None:
        self.posts: list[dict] = []

    def list_attempts(self) -> list[dict]:
        return []

    def get_attempt(self, attempt_id):
        return None

    def post_attempt(self, brief):
        self.posts.append(dict(brief))
        return {"attempt_id": f"att_{len(self.posts)}"}


def snapshot(state: Path, repo: Path, job_id: str) -> dict[str, Any]:
    def names(path: Path) -> list[str]:
        return sorted(item.name for item in path.glob("*.json")) if path.exists() else []

    return {
        "commits": git(repo, "rev-list", "--count", "HEAD").stdout.strip(),
        "head": git(repo, "rev-parse", "HEAD").stdout.strip(),
        "attempts": names(state / "leases"),
        "waves": names(state / "waves"),
        "wave_bytes": {item.name: item.read_bytes() for item in sorted((state / "waves").glob("*.json"))},
        "authorizations": names(state / "authorizations"),
        "job": jobs.job_path(state, job_id).read_bytes(),
    }


# ---------------------------------------------------------------------------
# (a) A claim that cannot be read is a refusal, not a dropped item
# ---------------------------------------------------------------------------


class QueueClaims:
    __test__ = True

    def test_a_corrupt_claim_record_is_refused_not_dropped(self, tmp_path):
        store = queue.QueueStore(tmp_path)
        store.path("f1").write_text('{"feature_id": "f1", "state": "runni', encoding="utf-8")

        with pytest.raises(queue.QueueError) as caught:
            store.active()
        assert caught.value.code == "queue_state_corrupt"
        assert "f1" in caught.value.message

    def test_an_unreadable_claim_is_a_distinct_refusal(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root ignores file permissions; unreadability cannot be simulated")
        store = queue.QueueStore(tmp_path)
        path = store.path("f1")
        path.write_text(json.dumps({"state": "running", "job_id": "job_d"}), encoding="utf-8")
        path.chmod(0o000)

        with pytest.raises(queue.QueueError) as caught:
            store.active()
        assert caught.value.code == "queue_state_unreadable"

    def test_a_non_object_record_is_corrupt(self, tmp_path):
        store = queue.QueueStore(tmp_path)
        store.path("f1").write_text('["claimed"]', encoding="utf-8")

        with pytest.raises(queue.QueueError) as caught:
            store.active()
        assert caught.value.code == "queue_state_corrupt"

    def test_a_genuinely_absent_record_is_normal(self, tmp_path):
        store = queue.QueueStore(tmp_path)
        assert store.active() == []

        store.claim("f1", job_id="job_d")
        assert [record["feature_id"] for record in store.active()] == ["f1"]

        store.path("f1").unlink()
        assert store.active() == [], "a deleted claim is absent, not an error"

    def test_run_once_refuses_to_schedule_over_a_corrupt_claim(self, tmp_path):
        (tmp_path / "queue.json").write_text(
            json.dumps({"f1": {"repo": REPO, "job_id": "job_d", "status": "queued"}}), encoding="utf-8"
        )
        queue.QueueStore(tmp_path).path("f1").write_text("{not json", encoding="utf-8")
        backend = queue.JsonQueue(tmp_path / "queue.json")
        ticked: list[str] = []

        with pytest.raises(queue.QueueError) as caught:
            queue.run_once(tmp_path, backend, tick=lambda job_id: ticked.append(job_id) or {"status": "ok"})

        assert caught.value.code == "queue_state_corrupt"
        assert ticked == [], "nothing may be dispatched while an active claim is unreadable"


# ---------------------------------------------------------------------------
# (b) Accepted-wave persistence: atomic, durable, create-only, strict reads
# ---------------------------------------------------------------------------


class WaveEvidence:
    __test__ = True

    def test_missing_unreadable_and_malformed_evidence_are_distinct(self, tmp_path):
        with pytest.raises(revision.RevisionError) as missing:
            revision.read_wave(tmp_path, "wave-none")
        assert missing.value.code == revision.WAVE_MISSING == "wave_missing"

        waves = tmp_path / "waves"
        waves.mkdir()
        (waves / "wave-broken.json").write_text('{"accepted": tru', encoding="utf-8")
        with pytest.raises(revision.RevisionError) as malformed:
            revision.read_wave(tmp_path, "wave-broken")
        assert malformed.value.code == revision.WAVE_MALFORMED == "wave_malformed"

        (waves / "wave-list.json").write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(revision.RevisionError) as not_object:
            revision.read_wave(tmp_path, "wave-list")
        assert not_object.value.code == revision.WAVE_MALFORMED

        if os.geteuid() != 0:
            locked = waves / "wave-locked.json"
            locked.write_text(json.dumps({"accepted": True}), encoding="utf-8")
            locked.chmod(0o000)
            with pytest.raises(revision.RevisionError) as unreadable:
                revision.read_wave(tmp_path, "wave-locked")
            assert unreadable.value.code == revision.WAVE_UNREADABLE == "wave_unreadable"

        # The lenient reporting reader still maps absence (and only failure) to None.
        assert revision.load_wave(tmp_path, "wave-none") is None

    def test_an_accepted_wave_is_fsynced_and_never_overwritten(self, tmp_path, monkeypatch):
        synced: list[int] = []
        real = os.fsync

        def counting(fd: int) -> None:
            synced.append(fd)
            real(fd)

        monkeypatch.setattr(revision.os, "fsync", counting)
        record = {"wave_id": "wave-a", "accepted": True, "head_sha": "a" * 40, "attempts": ["att_1"]}
        revision._write_wave(tmp_path, "wave-a", record)

        assert len(synced) >= 2, "the file and its directory must both be fsynced"
        assert not list((tmp_path / "waves").glob(".*.tmp")), "no temporary file may survive"
        before = (tmp_path / "waves" / "wave-a.json").read_bytes()

        with pytest.raises(revision.RevisionError) as caught:
            revision._write_wave(tmp_path, "wave-a", dict(record, head_sha="b" * 40))
        assert caught.value.code == "wave_already_accepted"
        assert (tmp_path / "waves" / "wave-a.json").read_bytes() == before
        assert stat.S_IMODE((tmp_path / "waves" / "wave-a.json").stat().st_mode) == 0o600

    def test_a_filesystem_without_hard_links_still_publishes_create_only(self, tmp_path, monkeypatch):
        # The O_EXCL fallback is the only publish path on filesystems that refuse
        # os.link; it must stay create-only there too, or an accepted record could
        # be replaced by a later writer on exactly those filesystems.
        def no_links(*_args, **_kwargs):
            raise OSError("hard links are not supported here")

        monkeypatch.setattr(revision.os, "link", no_links)
        record = {"wave_id": "wave-x", "accepted": True, "head_sha": "c" * 40, "attempts": ["att_1"]}
        revision._write_wave(tmp_path, "wave-x", record)
        assert revision.read_wave(tmp_path, "wave-x")["head_sha"] == "c" * 40

        with pytest.raises(revision.RevisionError) as caught:
            revision._write_wave(tmp_path, "wave-x", dict(record, head_sha="d" * 40))
        assert caught.value.code == "wave_already_accepted"
        assert revision.read_wave(tmp_path, "wave-x")["head_sha"] == "c" * 40

    def test_two_concurrent_writers_cannot_both_publish_an_accepted_wave(self, tmp_path):
        barrier = threading.Barrier(2)
        results: list[tuple[str, str]] = []

        def writer(head: str) -> None:
            barrier.wait(timeout=10)
            try:
                revision._write_wave(
                    tmp_path,
                    "wave-race",
                    {"wave_id": "wave-race", "accepted": True, "head_sha": head, "attempts": [head[:4]]},
                )
                results.append(("published", head))
            except revision.RevisionError as exc:
                results.append((exc.code, head))

        threads = [threading.Thread(target=writer, args=(value * 40,)) for value in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        codes = sorted(code for code, _head in results)
        assert codes == ["published", "wave_already_accepted"], results
        record = revision.read_wave(tmp_path, "wave-race")
        published = [head for code, head in results if code == "published"]
        assert record["accepted"] is True
        assert record["head_sha"] == published[0], "the file holds exactly the winner's record"
        assert record["attempts"] == [published[0][:4]]

    def test_wave_ids_agree_with_the_integration_layer(self):
        # Recovery finds a crashed tick's record by deriving its name from the
        # job document. If this derivation ever disagrees with the layer that
        # writes the record, recovery silently misses evidence and integrates
        # the same work twice — so the agreement is pinned here.
        cases = [("job_d", "w1"), ("job_d", 3), ("job.with_underscores", 2), ("j" * 96, "w1")]
        for job_id, wave in cases:
            derived = revision.wave_id_for(job_id, wave)
            assert derived == integrate_job.job_wave_id({"job_id": job_id, "integration": {"wave": wave}})
            assert len(derived) <= 64

    def test_a_corrupt_wave_refuses_integration_before_any_work(self, tmp_path):
        repo, base = make_repo(tmp_path)
        state = tmp_path / "state"
        revision._write_wave(state, "wave-c", {
            "wave_id": "wave-c", "accepted": True, "head_sha": base,
            "attempts": ["att_1"], "problems": {}, "at": "2026-02-03T04:05:06Z",
        })
        commits = git(repo, "rev-list", "--count", "HEAD").stdout.strip()

        path = state / "waves" / "wave-c.json"
        path.chmod(0o600)
        path.write_text('{"accepted": true, "head_sha": "', encoding="utf-8")
        suites: list[Path] = []

        with pytest.raises(revision.RevisionError) as caught:
            revision.integrate_wave(state, "wave-c", repo, ["att_1"], lambda p: suites.append(p) or {"exit_code": 0})

        assert caught.value.code == "wave_malformed"
        assert suites == [], "the suite must not run against damaged evidence"
        assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == commits
        with pytest.raises(revision.RevisionError) as refused:
            revision.attempt_accepted(state, "att_1")
        assert refused.value.code == "wave_malformed", "a damaged record is not 'not accepted'"


# ---------------------------------------------------------------------------
# (c) Recovery adopts the accepted record instead of integrating again
# ---------------------------------------------------------------------------


class JobRecovery:
    __test__ = True

    def test_a_job_load_adopts_the_accepted_wave_a_crash_left_behind(self, tmp_path):
        _repo, _base, state, _job, integrated = crash_state(tmp_path)
        head = integrated["record"]["head_sha"]

        on_disk = jobs.load_job(state, "job_d", recover=False)
        assert on_disk["tasks"][0]["state"] == "verified"
        assert not (on_disk.get("integration") or {}).get("accepted_waves")

        adopted = jobs.load_job(state, "job_d")
        assert adopted["tasks"][0]["state"] == "accepted"
        entry = adopted["integration"]["accepted_waves"][0]
        assert entry["commit"] == head and entry["recovered"] is True
        assert adopted["integration"]["last_commit"] == head
        assert adopted["integration"]["wave"] == 2
        assert adopted["integration"]["wave_id"] == "job_d.w2"
        assert adopted["integration"]["recovered"]["wave_id"] == "job_d.w1"
        assert queue._job_accepted(state, "job_d") is True, queue._job_accepted_reasons(state, "job_d")

        jobs.save_job(state, adopted)
        again = jobs.load_job(state, "job_d")
        assert again["integration"] == adopted["integration"], "adoption is idempotent"
        assert len([event for event in again["events"] if event.get("actor") == "recovery"]) == 2

    def test_a_corrupt_accepted_wave_is_not_permission_to_integrate_again(self, tmp_path):
        repo, _base, state, _job, _integrated = crash_state(tmp_path)
        path = state / "waves" / "job_d.w1.json"
        path.chmod(0o600)
        path.write_text('{"accepted": true', encoding="utf-8")
        commits = git(repo, "rev-list", "--count", "HEAD").stdout.strip()
        launcher = Launcher()
        integrations: list[dict] = []

        with pytest.raises(jobs.JobError) as caught:
            reconcile.tick(state, "job_d", launcher=launcher,
                           integrator=lambda job: integrations.append(dict(job)) or {"ok": True, "evidence": "x"})

        assert caught.value.code == "wave_malformed"
        assert integrations == [] and launcher.posts == []
        assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == commits
        assert jobs.load_job(state, "job_d", recover=False)["tasks"][0]["state"] == "verified"

        with pytest.raises(jobs.JobError) as refused:
            job_cli.recover(state, "job_d", repo)
        assert refused.value.code == "wave_malformed"

    def test_recovery_restores_the_durable_ref_without_a_new_commit(self, tmp_path):
        repo, _base, state, _job, integrated = crash_state(tmp_path)
        ref = integrate_job.integration_ref("job_d")
        head = integrated["record"]["head_sha"]
        git(repo, "update-ref", "-d", ref)
        commits = git(repo, "rev-list", "--count", "HEAD").stdout.strip()

        outcome = job_cli.recover(state, "job_d", repo)

        assert outcome["adopted"] is True
        assert outcome["ref_restored"] == head
        assert git(repo, "rev-parse", ref).stdout.strip() == head
        assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == commits
        assert jobs.load_job(state, "job_d")["integration"]["wave"] == 2

        again = job_cli.recover(state, "job_d", repo)
        assert again["adopted"] is False, "a second recovery must adopt nothing"

    @pytest.mark.parametrize(
        "tamper, code",
        [
            ({"head_sha": "short"}, "wave_evidence_invalid"),
            ({"attempts": ["att_other"]}, "wave_evidence_mismatch"),
            ({"wave_id": "another-job.w1"}, "wave_evidence_mismatch"),
            ({"attempts": []}, "wave_evidence_invalid"),
        ],
    )
    def test_accepted_evidence_that_does_not_match_the_job_is_refused(self, tmp_path, tamper, code):
        repo, _base, state, _job, _integrated = crash_state(tmp_path)
        path = state / "waves" / "job_d.w1.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(tamper)
        path.chmod(0o600)
        path.write_text(json.dumps(record), encoding="utf-8")
        commits = git(repo, "rev-list", "--count", "HEAD").stdout.strip()
        integrations: list[dict] = []

        with pytest.raises(jobs.JobError) as caught:
            jobs.load_job(state, "job_d")
        assert caught.value.code == code

        with pytest.raises(jobs.JobError) as refused:
            reconcile.tick(state, "job_d", launcher=Launcher(),
                           integrator=lambda job: integrations.append(dict(job)) or {"ok": True, "evidence": "x"})
        assert refused.value.code == code
        assert integrations == []
        assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == commits

    def test_a_save_cannot_drop_or_rewind_accepted_evidence(self, tmp_path):
        _repo, state, job_id = accepted_job(tmp_path)
        before = jobs.job_path(state, job_id).read_bytes()

        dropped = jobs.load_job(state, job_id)
        dropped["integration"]["accepted_waves"] = []
        with pytest.raises(jobs.JobError) as caught:
            jobs.save_job(state, dropped)
        assert caught.value.code == "accepted_wave_dropped"

        rewound = jobs.load_job(state, job_id)
        rewound["integration"]["wave"] = 1
        with pytest.raises(jobs.JobError) as caught:
            jobs.save_job(state, rewound)
        assert caught.value.code == "integration_wave_regressed"

        assert jobs.job_path(state, job_id).read_bytes() == before


# ---------------------------------------------------------------------------
# (d) Fault checkpoints at the dispatch and acknowledgement boundaries
# ---------------------------------------------------------------------------


class DispatchBoundary:
    __test__ = True

    def _job(self, tmp_path: Path) -> tuple[Path, dict]:
        state = tmp_path / "state"
        base = "a" * 40
        job = make_job(state, job_id="job_d", base=base)
        return state, job

    def test_the_checkpoint_fires_after_the_dispatch_is_persisted(self, tmp_path, faults_hooks):
        state, _job = self._job(tmp_path)
        client = Client()
        seen: list[dict[str, Any]] = []
        observed: dict[str, Any] = {}

        def observe(**payload):
            seen.append(dict(payload))
            # Read the job *inside* the hook: what the checkpoint can see is
            # exactly what a crash at this point would leave on disk.
            on_disk = jobs.load_job(state, "job_d", recover=False)
            observed.update(state=on_disk["tasks"][0]["state"],
                            attempts=list(on_disk["tasks"][0]["attempts"]))

        faults.register("tick.after_dispatch", observe)
        result = job_cli.tick(state, "job_d", client=client, repo=tmp_path, require_gitleaks=False)

        assert result["action"] == "dispatch"
        assert seen == [{"job_id": "job_d", "attempts": ["att_1"]}]
        assert observed == {"state": "dispatched", "attempts": ["att_1"]}

    def test_a_crash_at_the_dispatch_boundary_never_dispatches_twice(self, tmp_path, faults_hooks):
        state, _job = self._job(tmp_path)
        client = Client()
        faults.register("tick.after_dispatch", boom)

        with pytest.raises(RuntimeError):
            job_cli.tick(state, "job_d", client=client, repo=tmp_path, require_gitleaks=False)

        on_disk = jobs.load_job(state, "job_d", recover=False)
        assert on_disk["tasks"][0]["state"] == "dispatched"
        assert on_disk["tasks"][0]["attempts"] == ["att_1"]
        assert len(client.posts) == 1

        faults.clear()  # the restart is a fresh process with no hooks armed
        second = job_cli.tick(state, "job_d", client=client, repo=tmp_path, require_gitleaks=False)

        assert second["status"] == "idle", second
        assert len(client.posts) == 1, "a restart must adopt the persisted dispatch, not repeat it"
        assert jobs.load_job(state, "job_d")["tasks"][0]["attempts"] == ["att_1"]

    def test_no_checkpoint_fires_when_nothing_was_dispatched(self, tmp_path, faults_hooks):
        repo, state, job_id = accepted_job(tmp_path)
        client = Client()
        calls = record("tick.after_dispatch", "queue.before_ack")

        result = job_cli.tick(state, job_id, client=client, repo=repo, require_gitleaks=False)

        assert result["status"] == "idle", result
        assert client.posts == []
        assert calls == []


class AckBoundary:
    __test__ = True

    def _queue(self, state: Path, *, claimed: bool = False) -> queue.JsonQueue:
        path = state / "queue.json"
        path.write_text(json.dumps({"f1": {"repo": REPO, "job_id": "job_d", "status": "queued"}}), encoding="utf-8")
        backend = queue.JsonQueue(path)
        if claimed:
            queue.QueueStore(state).claim("f1", job_id="job_d")
            backend.set_status("f1", "running")
        return backend

    def test_the_ack_checkpoint_fires_before_the_card_is_marked(self, tmp_path, faults_hooks):
        # The admit branch: the tick integrates for real, so the feature becomes
        # complete during this very pass and the acknowledgement is the boundary.
        repo, _base, state, _job = verified_job(tmp_path)
        backend = self._queue(state)
        seen: list[dict[str, Any]] = []
        observed: dict[str, Any] = {}

        def observe(**payload):
            seen.append(dict(payload))
            observed["card"] = backend.get("f1").status
            observed["record"] = queue.QueueStore(state).read("f1")["state"]

        faults.register("queue.before_ack", observe)
        result = queue.run_once(state, backend, tick=integrating_tick(state, repo))

        assert result["action"] == "ready_for_review", result
        assert seen == [{"feature_id": "f1", "job_id": "job_d", "state": "ready_for_review"}]
        assert observed == {"card": "claimed", "record": "claimed"}
        assert backend.get("f1").status == "ready_for_review"
        assert queue._job_accepted(state, "job_d") is True

    def test_a_crash_before_the_ack_leaves_the_claim_active(self, tmp_path, faults_hooks):
        repo, _base, state, _job, _integrated = crash_state(tmp_path)
        backend = self._queue(state, claimed=True)
        store = queue.QueueStore(state)
        faults.register("queue.before_ack", boom)

        with pytest.raises(RuntimeError):
            queue.run_once(state, backend, tick=recovering_tick(state, repo))

        assert backend.get("f1").status == "running"
        assert store.read("f1")["state"] == "claimed"
        assert [record["feature_id"] for record in store.active()] == ["f1"]

        faults.clear()  # the restart is a fresh process with no hooks armed
        again = queue.run_once(state, backend, tick=recovering_tick(state, repo))

        assert again["action"] == "ready_for_review"
        assert backend.get("f1").status == "ready_for_review"
        assert store.read("f1")["state"] == "ready_for_review"

    def test_the_checkpoints_are_a_no_op_without_the_faults_module(self, tmp_path, monkeypatch):
        repo, _base, state, _job, _integrated = crash_state(tmp_path)
        backend = self._queue(state, claimed=True)
        monkeypatch.setitem(sys.modules, "bundlebees_plugin.faults", None)

        result = queue.run_once(state, backend, tick=recovering_tick(state, repo))

        assert result["action"] == "ready_for_review", result
        assert backend.get("f1").status == "ready_for_review"
        dispatch_state = tmp_path / "dispatch"
        make_job(dispatch_state, job_id="job_x", base="b" * 40)
        client = Client()
        assert job_cli.tick(dispatch_state, "job_x", client=client, repo=tmp_path,
                            require_gitleaks=False)["action"] == "dispatch"


# ---------------------------------------------------------------------------
# (e) A clean restart changes nothing
# ---------------------------------------------------------------------------


class CleanRestart:
    __test__ = True

    def test_a_restart_adds_no_commits_attempts_waves_or_authorizations(self, tmp_path, faults_hooks):
        repo, state, job_id = accepted_job(tmp_path)
        backend = queue.JsonQueue(state / "queue.json")
        (state / "queue.json").write_text(
            json.dumps({"f1": {"repo": REPO, "job_id": job_id, "status": "queued"}}), encoding="utf-8"
        )
        before = snapshot(state, repo, job_id)
        calls = record("tick.after_dispatch", "queue.before_ack")

        def tick(one_job: str):
            return job_cli.tick(state, one_job, client=Client(), repo=repo, require_gitleaks=False)

        for _ in range(2):
            job_cli.tick(state, job_id, client=Client(), repo=repo, require_gitleaks=False)
        result = queue.run_once(state, backend, tick=tick)
        assert result["action"] == "idle"

        after = snapshot(state, repo, job_id)
        assert after == before
        assert calls == [], "a clean restart dispatches and acks nothing"
