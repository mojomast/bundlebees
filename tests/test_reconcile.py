"""M2 acceptance tests: one crash-safe reconciler tick for durable jobs."""

from __future__ import annotations

from pathlib import Path

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

SHA = "817eac479f441396562962e880c1af596638f399"


def authorize_and_create(
    state: Path,
    *,
    job_id: str = "job_a",
    tasks: list[dict] | None = None,
) -> dict:
    jobs.record_authorization(
        state,
        job_id=job_id,
        statement="Add source-rev validation to acdd-lint, nothing else.",
        repo="mojomast/workflow",
        base_rev=SHA,
        scope=["tools/acdd-lint/**"],
        bees=4,
        wall_clock_s=3600,
        interactive=True,
    )
    if tasks is None:
        tasks = [
            {
                "task_id": "T1",
                "objective": "reject malformed source_rev",
                "lease": ["tools/acdd-lint/acdd_lint/checks_source_rev.py"],
            }
        ]
    return jobs.create_job(state, job_id=job_id, tasks=tasks)


def two_tasks() -> list[dict]:
    return [
        {
            "task_id": "T1",
            "objective": "reject malformed source_rev",
            "lease": ["tools/acdd-lint/acdd_lint/checks_source_rev.py"],
        },
        {
            "task_id": "T2",
            "objective": "reject unreachable source_rev",
            "lease": ["tools/acdd-lint/acdd_lint/checks_reachability.py"],
        },
    ]


class FakeLauncher:
    """A small launcher whose POST behaviour is scripted per call."""

    def __init__(self, *, idempotent: bool = True, script: list | None = None):
        self.idempotent = idempotent
        self.script = list(script or [])
        self.entries: list[dict] = []
        self.posts: list[dict] = []
        self.journal_calls = 0
        self.verify_calls: list[str | None] = []
        self.entered = None
        self.release = None

    def _attempt(
        self, brief: dict, idempotency_key: str | None, attempt_id: str
    ) -> dict:
        entry = {
            "attempt_id": attempt_id,
            "job_id": brief.get("job_id"),
            "task_id": brief.get("task_id"),
            "idempotency_key": idempotency_key,
        }
        self.entries.append(entry)
        return entry

    def post(self, brief: dict, idempotency_key: str | None = None):
        self.posts.append(
            {
                "brief": dict(brief),
                "idempotency_key": idempotency_key,
                "job_id": brief.get("job_id"),
                "task_id": brief.get("task_id"),
            }
        )
        if self.entered is not None and self.release is not None:
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("test never released the blocked POST")
        action = self.script.pop(0) if self.script else "ok"
        if isinstance(action, BaseException):
            raise action
        if action == "not_sent":
            raise rec.DispatchNotSent("request was never built")
        if action == "uncertain":
            raise rec.DispatchUncertain("connection dropped after send")
        if action == "uncertain_present":
            self._attempt(brief, idempotency_key, "att_late")
            raise rec.DispatchUncertain("response was lost")
        if action == "uncertain_ambiguous":
            self._attempt(brief, idempotency_key, "att_one")
            self._attempt(brief, idempotency_key, "att_two")
            raise rec.DispatchUncertain("two attempts may exist")
        attempt_id = f"att_{len(self.entries)}"
        self._attempt(brief, idempotency_key, attempt_id)
        return {"attempt_id": attempt_id}

    def add_attempt(
        self,
        *,
        job_id: str,
        task_id: str,
        idempotency_key: str | None,
        attempt_id: str = "att_late",
    ) -> dict:
        return self._attempt(
            {"job_id": job_id, "task_id": task_id},
            idempotency_key,
            attempt_id,
        )

    def journal(
        self,
        task_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> list[dict]:
        self.journal_calls += 1
        return list(self.entries)

    def verify(self, attempt_id: str | None = None, task: dict | None = None):
        self.verify_calls.append(attempt_id)
        return {"ok": True, "evidence": "verifier green"}


def transition_to_accepted(state: Path, job: dict, task_id: str) -> dict:
    for target in (
        "dispatching",
        "dispatched",
        "running",
        "verifying",
        "verified",
        "integrating",
        "accepted",
    ):
        jobs.transition(job, task_id, target, actor="reconciler")
    jobs.save_job(state, job)
    return job


class Idempotency:
    __test__ = True

    def test_unchanged_world_is_a_noop(self, tmp_path):
        job = authorize_and_create(tmp_path)
        transition_to_accepted(tmp_path, job, "T1")
        job_file = jobs.job_path(tmp_path, "job_a")
        before = job_file.read_bytes()
        before_events = len(jobs.load_job(tmp_path, "job_a")["events"])
        launcher = FakeLauncher()

        for _ in range(10):
            result = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
            assert result["status"] == "noop"
            assert result["posts"] == 0
            assert result["transitions"] == []

        assert launcher.posts == []
        assert launcher.journal_calls == 0
        assert job_file.read_bytes() == before
        assert len(jobs.load_job(tmp_path, "job_a")["events"]) == before_events


class Honesty:
    __test__ = True

    def test_exactly_once_is_never_claimed_without_server_idempotency(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(
            idempotent=False, script=["uncertain_present"]
        )

        result = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")

        assert result["status"] == "adopted"
        assert result["exactly_once"] is False
        assert result["evidence"] in {"adopted", "retried"}
        assert result["journal_evidence"] == "proven_present"
        assert len(launcher.posts) == 1
        assert len(launcher.entries) == 1

    def test_exactly_once_is_claimed_only_with_server_idempotency(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher(
            idempotent=True, script=["uncertain_present"]
        )

        result = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")

        assert result["status"] == "adopted"
        assert result["exactly_once"] is True
        assert result["evidence"] == "proven_present"


class Granularity:
    __test__ = True

    def test_dispatch_tick_does_not_verify(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher()

        result = rec.tick(tmp_path, "job_a", launcher, mode="dispatch")

        assert result["status"] == "dispatched"
        assert launcher.verify_calls == []
        assert result["verifier_calls"] == 0
        task = jobs.load_job(tmp_path, "job_a")["tasks"][0]
        assert task["state"] == "dispatched"
        assert task["verify"] is None
        assert not any(
            event["transition"].endswith(("integrating", "accepted"))
            for event in result["transitions"]
        )

    def test_verify_tick_is_one_heavy_operation(self, tmp_path):
        authorize_and_create(tmp_path, tasks=two_tasks())
        launcher = FakeLauncher()
        assert rec.tick(tmp_path, "job_a", launcher, mode="dispatch")["status"] == (
            "dispatched"
        )
        assert rec.tick(tmp_path, "job_a", launcher, mode="dispatch")["status"] == (
            "dispatched"
        )
        posts_after_dispatch = len(launcher.posts)
        calls: list[str | None] = []

        def verifier(attempt_id=None, task=None):
            calls.append(attempt_id)
            return {"ok": True, "evidence": "green"}

        result = rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="verify",
            verifier=verifier,
        )

        assert result["status"] == "verified"
        assert len(calls) == 1
        assert result["verifier_calls"] == 1
        assert len(launcher.posts) == posts_after_dispatch
        states = [task["state"] for task in jobs.load_job(tmp_path, "job_a")["tasks"]]
        assert states == ["verified", "dispatched"]


class Attribution:
    __test__ = True

    def test_transitions_are_recorded_and_persisted(self, tmp_path):
        authorize_and_create(tmp_path)
        launcher = FakeLauncher()

        rec.tick(tmp_path, "job_a", launcher, mode="dispatch")
        rec.tick(
            tmp_path,
            "job_a",
            launcher,
            mode="verify",
            verifier=lambda attempt_id=None: {
                "ok": True,
                "evidence": "green",
            },
        )

        reloaded = jobs.load_job(tmp_path, "job_a")
        task = reloaded["tasks"][0]
        assert task["state"] == "verified"
        assert task["attempts"][0]["attempt_id"] == "att_0"
        assert task["verify"]["ok"] is True

        transitions = [event["transition"] for event in reloaded["events"]]
        assert transitions == [
            "pending->dispatching",
            "dispatching->dispatched",
            "dispatched->running",
            "running->verifying",
            "verifying->verified",
        ]
        assert {event["actor"] for event in reloaded["events"]} == {"reconciler"}

        intent = rec.load_intent(tmp_path, "job_a", "T1")
        assert intent is not None
        assert intent["status"] == "sent"
        assert intent["attempt_id"] == "att_0"


class PublicSurface:
    __test__ = True

    def test_durable_paths_are_stable(self, tmp_path):
        authorize_and_create(tmp_path)
        intent = rec.intent_path(tmp_path, "job_a", "T1")
        lock = rec.lock_path(tmp_path, "job_a")
        assert intent == tmp_path / "intents" / "job_a" / "T1.json"
        assert lock == tmp_path / "locks" / "job_a.lock"

    def test_dispatch_accepts_a_job_mapping_and_reports_the_lock(self, tmp_path):
        job = authorize_and_create(tmp_path)
        launcher = FakeLauncher()
        lock = rec._try_lock(tmp_path, "job_a")
        assert lock is not None
        try:
            result = rec.tick(job, tmp_path, launcher, mode="dispatch")
        finally:
            lock.release()
        assert result["status"] == "locked"
        assert launcher.posts == []
        assert result["transitions"] == []
