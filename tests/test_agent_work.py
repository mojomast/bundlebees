"""M4.3 lane A: durable recovery work queue and out-of-band decision consumer.

The cases are split in two:

* queue mechanics (create-only publication, single claim, stale adoption,
  terminal states, duplicate delivery) run against synthetic durable requests;
* the real paths (reconcile publication, deterministic consumer, crash after a
  persisted proposal) use a real sealed job, a real frozen contract and the
  pinned recovery protocol from ``WORKFLOW_SRC``.

Run the protocol-dependent cases with::

    WORKFLOW_SRC=/tmp/verify/wf-packet /tmp/verify/bb15-venv/bin/python -m pytest \\
        tests/test_agent_work.py -q
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bundlebees_plugin import agent_consumer, agent_work
from bundlebees_plugin import contract as contracts
from bundlebees_plugin import job as jobs
from bundlebees_plugin import job_cli
from bundlebees_plugin import reconcile as rec
from bundlebees_plugin import recovery

TEST_FILE = "tests/test_t.py"
GOOD_TEST = f"{TEST_FILE}::test_real"
BASE = "2026-11-13T00:00:00Z"
NOW = 1_800_000_000.0
DECIDED = "2027-01-15T08:00:00Z"
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
HAS_WORKFLOW = bool(WORKFLOW_SRC) and (Path(WORKFLOW_SRC) / "tools" / "recovery.py").is_file()
requires_workflow = pytest.mark.skipif(
    not HAS_WORKFLOW,
    reason="set WORKFLOW_SRC to the pinned workflow checkout for the real recovery protocol",
)


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Synthetic durable requests: queue mechanics need no protocol
# ---------------------------------------------------------------------------


def synthetic_request(
    *,
    job_id: str = "job_a",
    task_id: str = "T1",
    number: int = 1,
    salt: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": recovery.SCHEMA,
        "request_id": f"{job_id}.{task_id}.{number}",
        "job_id": job_id,
        "task_id": task_id,
        "attempt_id": "att_0",
        "failure": {
            "class": "implementation",
            "code": "test_failed",
            "evidence": [],
            "rationale": "the approved test failed",
        },
        "contract": {"id": "SPEC-1", "sha256": "a" * 64},
        "supersession_permitted": False,
        "envelope": {
            "max_attempts_per_task": 2,
            "contract_corrections_allowed": 0,
            "corrected_tests_allowed": [],
        },
        "outstanding_question": salt or "retry, supersede or escalate?",
    }
    body["request_digest"] = recovery.request_digest(body)
    return body


def work_files(state: Path) -> list[Path]:
    return sorted((state / "recovery").glob("*/*.work.json"))


def decision_files(state: Path) -> list[Path]:
    return sorted((state / "recovery").glob("*/*.decision.json"))


def proposal_files(state: Path) -> list[Path]:
    return sorted((state / "recovery").glob("*/*.proposal.json"))


# ---------------------------------------------------------------------------
# Real job / contract / request fixtures
# ---------------------------------------------------------------------------


def git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / TEST_FILE).parent.mkdir(parents=True, exist_ok=True)
    (repo / "tools" / "t.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (repo / TEST_FILE).write_text("def test_real():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit", "-q", "-m", "fixture"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def frozen_contract(rev: str) -> dict[str, Any]:
    body = {
        "contract_version": "acdd/1",
        "id": "SPEC-1",
        "title": "the tool answers",
        "outcome": {"summary": "the tool answers", "observable_behavior": ["the tool answers 42"]},
        "source_rev": rev,
        "scope": {"assigned_files": ["tools/t.py"], "test_files": [TEST_FILE]},
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "statement": "the tool answers 42",
                "tests": [f"{TEST_FILE}::test_missing"],
                "verification": "test",
            }
        ],
        "tdd": {"applicable": True, "test_first": False, "reason": "the approved test exists at baseline"},
        "verification": {"independent": True},
        "evidence_required": ["green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    return contracts.freeze_contract(body, approved_by="reviewer", approved_at=BASE)


def new_state(tmp_path: Path, rev: str, *, task_state: str = "rejected") -> Path:
    state = tmp_path / "state"
    envelope = {
        "max_attempts_per_task": 2,
        "contract_corrections_allowed": True,
        "corrected_tests_allowed": [GOOD_TEST],
        "file_scope": ["tools/**", TEST_FILE],
        "test_scope": [TEST_FILE],
    }
    jobs.record_authorization(
        state,
        job_id="job_a",
        statement="Add contract source-rev validation to acdd-lint, nothing else.",
        repo="mojomast/workflow",
        base_rev=rev,
        scope=["tools/**", "tests/**"],
        bees=3,
        wall_clock_s=3600,
        interactive=True,
        recovery=envelope,
    )
    contract = frozen_contract(rev)
    jobs.create_job(
        state,
        job_id="job_a",
        tasks=[
            {
                "task_id": "T1",
                "objective": "make the tool answer 42",
                "lease": ["tools/t.py"],
                "contract": contract,
                "contract_sha256": contracts.check_frozen(contract),
                "acceptance": {"criteria": ["the tool answers 42"], "tests": [TEST_FILE]},
            }
        ],
    )
    job = jobs.load_job(state, "job_a", recover=False)
    walk = {
        "rejected": ("dispatching", "dispatched", "running", "verifying", "rejected"),
        "dispatched": ("dispatching", "dispatched"),
    }[task_state]
    for to_state in walk:
        jobs.transition(job, "T1", to_state, actor="reconciler", reason="agent work fixture")
    job["tasks"][0]["attempts"] = ["att_0"]
    jobs.save_job(state, job)
    return state


def build_request(state: Path) -> dict[str, Any]:
    job = jobs.load_job(state, "job_a", recover=False)
    task = job["tasks"][0]
    return recovery.build_request(
        state,
        job,
        task,
        failure={"ok": False, "evidence": "tests_failed: named tests failed after the patch",
                 "attempt_id": task["attempts"][-1]},
        authorization=jobs.load_authorization(state, "job_a"),
        now=NOW,
    )


def publish_real(state: Path) -> dict[str, Any]:
    return agent_work.publish(state, build_request(state), now=NOW)


class FakeLauncher:
    """The reconciler's four-member contract, recording every POST."""

    def __init__(self) -> None:
        self.idempotent = True
        self.entries: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []

    def list_attempts(self) -> list[dict[str, Any]]:
        return list(self.entries)

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self.entries if entry["attempt_id"] == attempt_id), None)

    def post_attempt(self, brief: dict[str, Any]) -> dict[str, Any]:
        self.posts.append(dict(brief))
        attempt_id = f"att_{len(self.entries)}"
        self.entries.append(
            {
                "attempt_id": attempt_id,
                "state": "running",
                "created_at": _iso(NOW),
                "brief": dict(brief),
            }
        )
        return {"attempt_id": attempt_id}


# ---------------------------------------------------------------------------
# Queue mechanics
# ---------------------------------------------------------------------------


def test_work_id_is_the_fixed_derivation():
    request = synthetic_request()
    expected = "aw_" + hashlib.sha256(
        f"{request['request_id']}:{request['request_digest']}".encode("utf-8")
    ).hexdigest()[:16]
    assert agent_work.work_id_for(request) == expected
    assert agent_work.SCHEMA == "rizzstack.agent-work/1"
    assert agent_work.STATES == ("pending", "claimed", "completed", "refused")


def test_publish_is_create_only_and_idempotent(tmp_path):
    request = synthetic_request()
    first = agent_work.publish(tmp_path, request, now=NOW)
    second = agent_work.publish(tmp_path, request, now=NOW + 60)

    assert first["work_id"] == second["work_id"]
    assert first["created_at"] == second["created_at"]
    assert first["history"] == second["history"]
    assert len(work_files(tmp_path)) == 1
    path = tmp_path / "recovery" / "job_a" / "T1.1.work.json"
    assert path.is_file()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["schema"] == "rizzstack.agent-work/1"
    assert stored["state"] == "pending"
    assert stored["attempts"] == 0
    assert stored["claim"] is None
    assert stored["result"] == {"proposal": None, "decision": None, "decision_digest": ""}
    assert agent_work.load(tmp_path, first["work_id"])["work_id"] == first["work_id"]
    assert [item["work_id"] for item in agent_work.list(tmp_path)] == [first["work_id"]]


def test_a_work_item_carries_an_adapter_hint_not_agent_instructions(tmp_path):
    item = agent_work.publish(tmp_path, synthetic_request(), adapter="hermes", now=NOW)

    assert item["adapter"] == "hermes"
    forbidden = {"prompt", "argv", "stdout", "stderr", "transcript", "command", "instructions"}
    assert forbidden.isdisjoint(item)
    assert forbidden.isdisjoint(item["result"])


def test_publish_refuses_a_different_digest_at_the_same_path(tmp_path):
    request = synthetic_request()
    agent_work.publish(tmp_path, request, now=NOW)
    changed = dict(request)
    changed["outstanding_question"] = "a different request at the same ordinal"
    changed["request_digest"] = recovery.request_digest(changed)

    with pytest.raises(agent_work.WorkError) as caught:
        agent_work.publish(tmp_path, changed, now=NOW)

    assert caught.value.code == "work_conflict"
    stored = agent_work.load(tmp_path, agent_work.work_id_for(request))
    assert stored["request_digest"] == request["request_digest"]


def test_claim_has_exactly_one_active_owner(tmp_path):
    request = synthetic_request()
    item = agent_work.publish(tmp_path, request, now=NOW)
    pid = os.getpid()

    first = agent_work.claim(tmp_path, item["work_id"], consumer="consumer-a", pid=pid, now=NOW)
    assert first is not None and first["state"] == "claimed"
    assert first["attempts"] == 1
    assert first["claim"]["consumer"] == "consumer-a"
    assert first["claim"]["pid"] == pid

    assert agent_work.claim(tmp_path, item["work_id"], consumer="consumer-b", pid=pid, now=NOW) is None
    # Re-claiming as the live owner is idempotent, not a second attempt.
    again = agent_work.claim(tmp_path, item["work_id"], consumer="consumer-a", pid=pid, now=NOW)
    assert again["attempts"] == 1 and again["claim"] == first["claim"]


def test_concurrent_claims_produce_one_owner(tmp_path):
    item = agent_work.publish(tmp_path, synthetic_request(), now=NOW)
    pid = os.getpid()

    def attempt(n: int):
        return agent_work.claim(
            tmp_path, item["work_id"], consumer=f"consumer-{n}", pid=pid, now=NOW
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    stored = agent_work.load(tmp_path, item["work_id"])
    assert stored["claim"]["consumer"] == winners[0]["claim"]["consumer"]
    assert stored["attempts"] == 1


def test_a_dead_pid_claim_is_adopted(tmp_path):
    request = synthetic_request()
    item = agent_work.publish(tmp_path, request, now=NOW)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()

    agent_work.claim(tmp_path, item["work_id"], consumer="crashed", pid=dead.pid, now=NOW)
    adopted = agent_work.claim(tmp_path, item["work_id"], consumer="restart", pid=os.getpid(), now=NOW + 1)

    assert adopted is not None
    assert adopted["attempts"] == 2
    assert adopted["claim"]["consumer"] == "restart"
    assert [event["event"] for event in adopted["history"]][-1] == "adopted_stale_claim"


def test_an_expired_ttl_claim_is_adopted_even_when_the_pid_is_alive(tmp_path):
    request = synthetic_request()
    item = agent_work.publish(tmp_path, request, now=NOW)
    agent_work.claim(tmp_path, item["work_id"], consumer="stuck", pid=os.getpid(), ttl_s=10, now=NOW)

    assert agent_work.claim(
        tmp_path, item["work_id"], consumer="next", pid=os.getpid(), ttl_s=10, now=NOW + 5
    ) is None
    adopted = agent_work.claim(
        tmp_path, item["work_id"], consumer="next", pid=os.getpid(), ttl_s=10, now=NOW + 11
    )
    assert adopted is not None and adopted["claim"]["consumer"] == "next"
    assert adopted["attempts"] == 2


def test_complete_and_refuse_are_terminal_and_idempotent(tmp_path):
    decision = {
        "schema": recovery.SCHEMA,
        "request_id": "job_a.T1.1",
        "request_digest": synthetic_request()["request_digest"],
        "adapter": "deterministic",
        "decision": "retry",
        "corrected_tests": [],
        "supersedes": None,
        "rationale": "the recorded failure is repairable inside the budget",
        "decided_at": DECIDED,
    }
    first = agent_work.publish(tmp_path, synthetic_request(), now=NOW)
    pid = os.getpid()
    agent_work.claim(tmp_path, first["work_id"], consumer="c", pid=pid, now=NOW)
    completed = agent_work.complete(
        tmp_path, first["work_id"], consumer="c", pid=pid, decision=decision, now=NOW
    )
    assert completed["state"] == "completed"
    assert completed["claim"] is None
    assert completed["result"]["decision"] == decision
    assert completed["result"]["decision_digest"] == recovery.decision_digest(decision)
    replay = agent_work.complete(
        tmp_path, first["work_id"], consumer="c", pid=pid, decision=decision, now=NOW + 1
    )
    assert replay["state"] == "completed" and replay["updated_at"] == completed["updated_at"]
    assert agent_work.claim(tmp_path, first["work_id"], consumer="d", pid=pid, now=NOW + 2) is None
    other = dict(decision, decision="escalate")
    with pytest.raises(agent_work.WorkError) as caught:
        agent_work.complete(tmp_path, first["work_id"], consumer="c", pid=pid, decision=other, now=NOW + 3)
    assert caught.value.code == "work_conflict"

    second = agent_work.publish(tmp_path, synthetic_request(number=2), now=NOW)
    agent_work.claim(tmp_path, second["work_id"], consumer="c", pid=pid, now=NOW)
    refused = agent_work.refuse(tmp_path, second["work_id"], consumer="c", pid=pid, error="boom", now=NOW)
    assert refused["state"] == "refused" and refused["last_error"] == "boom"
    replay = agent_work.refuse(tmp_path, second["work_id"], consumer="c", pid=pid, error="other", now=NOW + 1)
    assert replay["state"] == "refused" and replay["last_error"] == "boom"
    assert agent_work.claim(tmp_path, second["work_id"], consumer="d", pid=pid, now=NOW + 2) is None
    with pytest.raises(agent_work.WorkError) as caught:
        agent_work.complete(tmp_path, second["work_id"], consumer="c", pid=pid, now=NOW + 3)
    assert caught.value.code == "work_terminal"


def test_release_returns_to_pending_and_only_the_claimant_may_release(tmp_path):
    item = agent_work.publish(tmp_path, synthetic_request(), now=NOW)
    pid = os.getpid()
    agent_work.claim(tmp_path, item["work_id"], consumer="owner", pid=pid, now=NOW)

    with pytest.raises(agent_work.WorkError) as caught:
        agent_work.release(tmp_path, item["work_id"], consumer="other", pid=pid, error="x", now=NOW)
    assert caught.value.code == "claim_not_held"

    released = agent_work.release(
        tmp_path, item["work_id"], consumer="owner", pid=pid, error="transient", now=NOW
    )
    assert released["state"] == "pending"
    assert released["claim"] is None
    assert released["last_error"] == "transient"
    reclaim = agent_work.claim(tmp_path, item["work_id"], consumer="next", pid=pid + 1, now=NOW + 1)
    assert reclaim is not None and reclaim["attempts"] == 2


def test_list_filters_by_job_and_state(tmp_path):
    one = agent_work.publish(tmp_path, synthetic_request(number=1), now=NOW)
    two = agent_work.publish(tmp_path, synthetic_request(number=2), now=NOW)
    agent_work.claim(tmp_path, one["work_id"], consumer="c", pid=os.getpid(), now=NOW)

    assert len(agent_work.list(tmp_path, job_id="job_a")) == 2
    assert [item["work_id"] for item in agent_work.list(tmp_path, state="claimed")] == [one["work_id"]]
    assert [item["work_id"] for item in agent_work.list(tmp_path, state="pending")] == [two["work_id"]]
    assert agent_work.list(tmp_path, job_id="job_b") == []


# ---------------------------------------------------------------------------
# Reconcile: publish, expose, and never dispatch before a decision
# ---------------------------------------------------------------------------


@requires_workflow
def test_tick_publishes_work_for_a_request_created_by_a_reaped_failure(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev, task_state="dispatched")
    launcher = FakeLauncher()
    launcher.entries.append(
        {"attempt_id": "att_0", "state": "failed", "created_at": _iso(NOW), "brief": {}}
    )

    result = rec.tick(state, "job_a", launcher=launcher, now=NOW)

    assert launcher.posts == []
    assert "T1->rejected" in result.transitions
    assert result.work_state == "pending"
    assert result.work_id.startswith("aw_")
    assert len(work_files(state)) == 1
    assert decision_files(state) == []
    request = json.loads(next((state / "recovery" / "job_a").glob("*.request.json")).read_text())
    item = agent_work.load(state, result.work_id)
    assert item["request_id"] == request["request_id"]
    assert item["request_digest"] == request["request_digest"]


@requires_workflow
def test_tick_publishes_exactly_one_outstanding_item_and_exposes_it(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    request = build_request(state)
    launcher = FakeLauncher()

    first = rec.tick(state, "job_a", launcher=launcher, now=NOW)
    second = rec.tick(state, "job_a", launcher=launcher, now=NOW + 1)

    assert first.status == "awaiting_decision"
    assert first.action == "recovery"
    assert first.work_state == "pending"
    assert first.work_id == agent_work.work_id_for(request)
    assert second.work_state == "pending"
    assert len(work_files(state)) == 1
    assert launcher.posts == []


@requires_workflow
def test_no_dispatch_before_a_decision_and_exactly_one_correction_after(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    request = build_request(state)
    launcher = FakeLauncher()

    before = rec.tick(state, "job_a", launcher=launcher, now=NOW)
    assert before.status == "awaiting_decision"
    assert launcher.posts == []
    assert agent_work.load(state, before.work_id)["state"] == "pending"

    job = jobs.load_job(state, "job_a", recover=False)
    decision = recovery.deterministic_adapter(request, task=job["tasks"][0])
    recovery.record_decision(state, request["request_id"], decision, adapter="deterministic", now=NOW)

    after = rec.tick(state, "job_a", launcher=launcher, now=NOW + 1)
    assert after.status == "ok"
    assert after.action == "dispatch"
    assert "T1->dispatching" in after.transitions
    assert len(launcher.posts) == 1

    again = rec.tick(state, "job_a", launcher=launcher, now=NOW + 2)
    assert again.status != "awaiting_decision"
    assert len(launcher.posts) == 1


@requires_workflow
def test_consumer_completes_after_a_crash_between_decision_and_completion(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    request = build_request(state)
    item = agent_work.publish(state, request, now=NOW)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    agent_work.claim(state, item["work_id"], consumer="crashed", pid=dead.pid, now=NOW)
    job = jobs.load_job(state, "job_a", recover=False)
    decision = recovery.deterministic_adapter(request, task=job["tasks"][0])
    recovery.record_decision(state, request["request_id"], decision, adapter="deterministic", now=NOW)
    recorded = recovery.read_decision(state, request)

    result = agent_consumer.consume_once(state, consumer="restart", adapter="deterministic", now=NOW + 1)

    assert result["action"] == "already_decided"
    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "completed"
    assert stored["attempts"] == 2
    assert stored["result"]["decision_digest"] == recovery.decision_digest(recorded)


@requires_workflow
def test_consumer_invokes_the_adapter_once_after_a_crash_before_a_proposal(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    item = publish_real(state)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    agent_work.claim(state, item["work_id"], consumer="crashed", pid=dead.pid, now=NOW)

    result = agent_consumer.consume_once(state, consumer="restart", adapter="deterministic", now=NOW + 1)

    assert result["action"] == "decided"
    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "completed"
    assert stored["attempts"] == 2
    assert len(decision_files(state)) == 1


@requires_workflow
def test_tick_exposes_a_completed_work_item_after_the_consumer(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    request = build_request(state)
    item = agent_work.publish(state, request, now=NOW)
    job = jobs.load_job(state, "job_a", recover=False)
    decision = recovery.deterministic_adapter(request, task=job["tasks"][0])
    recovery.record_decision(state, request["request_id"], decision, adapter="deterministic", now=NOW)
    agent_consumer.consume_once(state, consumer="c", adapter="deterministic", now=NOW + 1)

    result = rec.tick(state, "job_a", launcher=FakeLauncher(), now=NOW + 2)

    assert result.work_state == "completed"
    assert result.work_id == item["work_id"]


@requires_workflow
def test_a_publish_failure_does_not_block_the_mechanical_tick(tmp_path, monkeypatch):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    build_request(state)

    def broken(*args, **kwargs):
        raise agent_work.WorkError("disk_full", "the queue directory is not writable")

    monkeypatch.setattr(agent_work, "publish_outstanding", broken)
    result = rec.tick(state, "job_a", launcher=FakeLauncher(), now=NOW)

    assert result.status == "awaiting_decision"
    assert result.work_state == "publish_failed"
    assert result.work_id == ""


@requires_workflow
def test_job_recover_publishes_before_it_records_a_decision(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    build_request(state)

    result = job_cli.recover(state, "job_a", adapter="deterministic", now=NOW)

    assert len(result["work"]) == 1
    assert result["work"][0]["state"] == "pending"
    assert len(decision_files(state)) == 1
    assert len(work_files(state)) == 1


# ---------------------------------------------------------------------------
# Consumer: deterministic path and bounded retry
# ---------------------------------------------------------------------------


def _consumer_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "WORKFLOW_SRC": WORKFLOW_SRC,
        }
    )
    env.update(extra)
    return env


def _run_consumer(
    state: Path,
    tmp_path: Path,
    *,
    consumer: str = "test-consumer",
    adapter: str = "deterministic",
    module: str = "bundlebees_plugin.agent_consumer",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", module, "--state", str(state), "--consumer", consumer,
         "--adapter", adapter, "--once"],
        cwd=str(ROOT),
        env=env or _consumer_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
    )


@requires_workflow
def test_deterministic_consumer_completes_once_in_its_own_process(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    item = publish_real(state)

    first = _run_consumer(state, tmp_path)
    assert first.returncode == 0, first.stderr
    payload = json.loads(first.stdout.strip().splitlines()[-1])
    assert payload["state"] == "completed"
    assert payload["action"] == "decided"

    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "completed"
    assert stored["attempts"] == 1
    assert stored["claim"] is None
    assert stored["result"]["decision"]["adapter"] == "deterministic"
    assert stored["result"]["decision_digest"] == recovery.decision_digest(
        stored["result"]["decision"]
    )
    assert len(decision_files(state)) == 1
    digest = hashlib.sha256(decision_files(state)[0].read_bytes()).hexdigest()

    again = _run_consumer(state, tmp_path)
    assert again.returncode == 0, again.stderr
    assert len(decision_files(state)) == 1
    assert hashlib.sha256(decision_files(state)[0].read_bytes()).hexdigest() == digest
    assert agent_work.load(state, item["work_id"])["updated_at"] == stored["updated_at"]


@requires_workflow
def test_consumer_releases_once_then_refuses_on_the_second_failure(tmp_path, monkeypatch):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    item = publish_real(state)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_consumer.recovery, "deterministic_adapter", broken)

    first = agent_consumer.consume_once(state, consumer="c", adapter="deterministic", now=NOW)
    assert first["action"] == "released"
    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "pending" and stored["attempts"] == 1
    assert stored["last_error"] == "RuntimeError"

    second = agent_consumer.consume_once(state, consumer="c", adapter="deterministic", now=NOW + 1)
    assert second["action"] == "refused"
    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "refused" and stored["attempts"] == 2
    assert stored["last_error"] == "RuntimeError"

    assert agent_consumer.consume_once(state, consumer="c", adapter="deterministic", now=NOW + 2) is None
    assert decision_files(state) == []


@requires_workflow
def test_consumer_completes_idempotently_when_a_decision_already_exists(tmp_path, monkeypatch):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    request = build_request(state)
    item = agent_work.publish(state, request, now=NOW)
    job = jobs.load_job(state, "job_a", recover=False)
    decision = recovery.deterministic_adapter(request, task=job["tasks"][0])
    recovery.record_decision(state, request["request_id"], decision, adapter="deterministic", now=NOW)
    recorded = recovery.read_decision(state, request)

    def forbidden(*args, **kwargs):
        raise AssertionError("the adapter must not run when a decision exists")

    monkeypatch.setattr(agent_consumer.recovery, "deterministic_adapter", forbidden)

    result = agent_consumer.consume_once(state, consumer="c", adapter="deterministic", now=NOW + 1)
    assert result["action"] == "already_decided"
    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "completed"
    assert stored["result"]["decision_digest"] == recovery.decision_digest(recorded)
    assert agent_consumer.consume_once(state, consumer="c", adapter="deterministic", now=NOW + 2) is None


# ---------------------------------------------------------------------------
# Crash after a persisted proposal: restart adopts, never asks the model again
# ---------------------------------------------------------------------------


def fake_hermes(tmp_path: Path, log: Path) -> Path:
    script = tmp_path / "fake-hermes.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys

            if "config" in sys.argv[1:]:
                sys.exit(1)
            with open({str(log)!r}, "a", encoding="utf-8") as handle:
                handle.write("call\\n")
            proposal = {{
                "schema": "rizzstack.recovery-proposal/1",
                "action": "retry",
                "corrected_tests": [],
                "rationale": "the recorded failure is inside the attempt budget",
                "evidence_refs": [],
            }}
            sys.stdout.write(json.dumps(proposal))
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@requires_workflow
def test_crash_after_proposal_is_adopted_on_restart_without_the_model(tmp_path):
    _, rev = git_repo(tmp_path)
    state = new_state(tmp_path, rev)
    item = publish_real(state)
    hermes_log = tmp_path / "hermes.log"
    binary = fake_hermes(tmp_path, hermes_log)
    adopt_log = tmp_path / "adopt.log"
    decide_log = tmp_path / "decide.log"

    crashed = _run_consumer(
        state,
        tmp_path,
        adapter="hermes",
        env=_consumer_env(
            tmp_path,
            HERMES_BIN=str(binary),
            BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL="1",
        ),
    )
    assert crashed.returncode == agent_consumer.CRASH_EXIT_CODE, crashed.stderr
    markers = sorted((state / "recovery" / "job_a").glob("*.proposal.crash.json"))
    assert len(markers) == 1
    marker = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker["schema"] == agent_consumer.CRASH_SCHEMA
    assert marker["decision_recorded"] is False
    assert marker["request_id"] == item["request_id"]
    assert len(proposal_files(state)) == 1
    assert decision_files(state) == []
    assert hermes_log.read_text(encoding="utf-8").splitlines() == ["call"]
    after_crash = agent_work.load(state, item["work_id"])
    assert after_crash["state"] == "claimed"
    assert after_crash["attempts"] == 1

    restarted = _run_consumer(
        state,
        tmp_path,
        consumer="restart",
        adapter="hermes",
        module="tests.agent_consumer_harness",
        env=_consumer_env(
            tmp_path,
            HERMES_BIN=str(binary),
            ADOPT_LOG=str(adopt_log),
            DECIDE_LOG=str(decide_log),
        ),
    )
    assert restarted.returncode == 0, restarted.stderr
    payload = json.loads(restarted.stdout.strip().splitlines()[-1])
    assert payload["action"] == "adopted"

    assert adopt_log.read_text(encoding="utf-8").splitlines() == [item["request_id"]]
    assert not decide_log.exists(), "a persisted proposal must never re-invoke the model"
    assert hermes_log.read_text(encoding="utf-8").splitlines() == ["call"]
    assert len(proposal_files(state)) == 1
    assert len(decision_files(state)) == 1
    stored = agent_work.load(state, item["work_id"])
    assert stored["state"] == "completed"
    assert stored["attempts"] == 2
    assert stored["result"]["proposal"] == "recovery/job_a/T1.1.proposal.json"
    assert stored["result"]["decision_digest"] == recovery.decision_digest(
        stored["result"]["decision"]
    )
