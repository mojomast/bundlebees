"""Completed-attempt reconciliation: the beat's first-class sweep.

The gap found today: three completed bees (``umbrella-modularity``,
``rebrand-and-website-plan``, ``orbit-decomposition``) sat unprocessed for
30+ minutes because their branches were never pushed, which made them
invisible to the scout's origin-delta detection while every beat turn was
busy with the merge wave. These tests pin the fix RED-first:

* a succeeded attempt whose branch is NOT on origin and has NO open PR is
  detected by the reconciliation sweep even when the merge stream is busy;
* the sweep is a first-class beat duty — every beat (and, optionally, the
  no-LLM scout tick) diffs the launcher's attempt list against origin and
  processes detected attempts (verify, push, open PR, record);
* the sweep is idempotent and serialized: it respects the claim file, repeated
  runs never double-push or double-open, and a branch already on origin or an
  attempt with an open PR is skipped with its reason recorded.

The scout's origin-delta detection is unchanged: :func:`beat.scout_tick`
performs exactly the same read-only classification and writes nothing; the
sweep is the launcher-side duty the scout structurally cannot do.

No network, no launcher, no tokens: the launcher, origin and PR views are
fakes; the mutating steps of the default processor are injected.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bundlebees_plugin import beat, beat_reconciliation
from bundlebees_plugin import test_receipts as receipts

SHA = "817eac479f441396562962e880c1af596638f399"
T0 = 1_800_000_000.0

#: The three concrete gap cases recorded today: completed docs bees whose
#: branches were never pushed, invisible to origin-delta detection, unprocessed
#: for 30+ minutes while the beats were busy with the merge wave.
GAP_CASES = (
    {"attempt_id": "umbrella-modularity", "branch": "bee/umbrella-modularity"},
    {"attempt_id": "rebrand-and-website-plan", "branch": "bee/rebrand-and-website-plan"},
    # The F2 case declares a frozen contract: its verification must go through
    # the contract path, never legacy mode.
    {"attempt_id": "orbit-decomposition", "branch": "bee/orbit-decomposition"},
)

GAP_IDS = tuple(case["attempt_id"] for case in GAP_CASES)


def iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def gap_attempt(case: dict, *, finished_at: float) -> dict:
    """One launcher attempt record of the incident's gap class.

    ``finished_at`` is set 30+ minutes before the beat's clock, the exact
    signature the incident report described.
    """

    return {
        "attempt_id": case["attempt_id"],
        "task_id": case["attempt_id"],
        "state": "succeeded",
        "branch": case["branch"],
        "created_at": iso(finished_at - 3600),
        "finished_at": iso(finished_at),
    }


class FakeLauncher:
    """The launcher's attempt list, with a poll counter for beat-flow tests."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = list(entries)
        self.polls = 0

    def list_attempts(self) -> list[dict]:
        self.polls += 1
        return list(self.entries)


class Recorder:
    """A processor that records its calls and reports a verified landing."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, attempt_id: str, *, entry: dict, branch: str, stage: str) -> dict:
        self.calls.append({"attempt_id": attempt_id, "branch": branch, "stage": stage})
        if stage == beat_reconciliation.STAGE_PR:
            return {"ok": True, "pushed": False, "sha": "", "pull_request": 4100 + len(self.calls), "stage": stage}
        return {"ok": True, "pushed": True, "sha": SHA, "pull_request": 4100 + len(self.calls), "stage": stage}


def gap_launcher() -> FakeLauncher:
    """The incident: all three gap attempts finished 32.5 minutes ago."""

    return FakeLauncher([gap_attempt(case, finished_at=T0 - 1950) for case in GAP_CASES])


def busy_merge() -> dict:
    return {"status": "busy", "detail": "merge wave in flight"}


def sweep(tmp_path: Path, **overrides) -> dict:
    kwargs = {
        "state_dir": tmp_path,
        "launcher": gap_launcher(),
        "origin_branches": {},
        "open_prs": {},
        "now": T0,
    }
    kwargs.update(overrides)
    return beat_reconciliation.sweep(**kwargs)


# ---------------------------------------------------------------------------
# RED: the exact gap class found today
# ---------------------------------------------------------------------------


def test_succeeded_unpushed_no_pr_is_detected_even_when_the_merge_stream_is_busy(tmp_path):
    recorder = Recorder()

    report = beat.beat(
        tmp_path,
        launcher=gap_launcher(),
        origin_branches={},
        open_prs={},
        merge_stream=busy_merge,
        processor=recorder,
        now=T0,
    )

    sweep_report = report["duties"]["reconcile_completed_attempts"]
    assert [item["attempt_id"] for item in sweep_report["detected"]] == list(GAP_IDS)
    # Every detected attempt was processed: verify, push, open PR, record.
    assert {item["attempt_id"] for item in sweep_report["processed"]} == set(GAP_IDS)
    assert all(item["pushed"] for item in sweep_report["processed"])
    assert all(item["pull_request"] for item in sweep_report["processed"])
    assert [call["stage"] for call in recorder.calls] == ["full"] * len(GAP_IDS)
    # The merge wave's busyness did not gate the sweep: both duties reported.
    assert report["duties"]["merge_stream"]["status"] == "busy"
    assert report["status"] == "ok"
    # The unprocessed age is visible: the 30+ minutes incident signature.
    assert all(item["age_s"] >= 1800 for item in sweep_report["detected"])


def test_the_sweep_alone_detects_the_gap_class(tmp_path):
    report = sweep(tmp_path, processor=Recorder())

    assert report["status"] == "ok"
    assert [item["attempt_id"] for item in report["detected"]] == list(GAP_IDS)
    assert all(item["reason"] == "succeeded_unpushed_no_pr" for item in report["detected"])
    assert {item["attempt_id"] for item in report["processed"]} == set(GAP_IDS)


def test_a_merge_stream_failure_does_not_eat_the_sweep(tmp_path):
    def merge() -> dict:
        raise RuntimeError("integration exploded")

    recorder = Recorder()
    report = beat.beat(
        tmp_path,
        launcher=gap_launcher(),
        origin_branches={},
        open_prs={},
        merge_stream=merge,
        processor=recorder,
        now=T0,
    )

    assert report["duties"]["merge_stream"]["status"] == "error"
    sweep_report = report["duties"]["reconcile_completed_attempts"]
    assert {item["attempt_id"] for item in sweep_report["processed"]} == set(GAP_IDS)
    assert report["status"] == "partial"


# ---------------------------------------------------------------------------
# The sweep is a first-class beat duty
# ---------------------------------------------------------------------------


def test_every_beat_diffs_the_launcher_attempt_list_against_origin(tmp_path):
    launcher = gap_launcher()

    first = beat.beat(
        tmp_path, launcher=launcher, origin_branches={}, open_prs={},
        merge_stream=busy_merge, processor=Recorder(), now=T0,
    )
    second = beat.beat(
        tmp_path, launcher=launcher, origin_branches={}, open_prs={},
        merge_stream=busy_merge, processor=Recorder(), now=T0 + 1,
    )

    # The attempt list was diffed against origin on every beat.
    assert launcher.polls == 2
    second_sweep = second["duties"]["reconcile_completed_attempts"]
    assert second_sweep["processed"] == []
    assert second_sweep["counts"]["by_reason"]["already_recorded"] == len(GAP_IDS)


def test_the_sweep_runs_before_the_merge_stream_duty(tmp_path):
    order: list[str] = []
    recorder = Recorder()

    def merge() -> dict:
        order.append("merge")
        return busy_merge()

    class RecordingProcessor:
        def __call__(self, attempt_id, *, entry, branch, stage):
            order.append("sweep")
            return Recorder()(attempt_id, entry=entry, branch=branch, stage=stage)

    beat.beat(
        tmp_path, launcher=gap_launcher(), origin_branches={}, open_prs={},
        merge_stream=merge, processor=RecordingProcessor(), now=T0,
    )

    assert order[0] == "sweep"


def test_scout_tick_diffs_without_processing_and_writes_nothing(tmp_path):
    report = beat.scout_tick(
        tmp_path, launcher=gap_launcher(), origin_branches={}, open_prs={}, now=T0,
    )

    assert [item["attempt_id"] for item in report["detected"]] == list(GAP_IDS)
    assert report["processed"] == []
    assert report["failed"] == []
    # Read-only: the scout's delta detection stays report-only.
    assert not (tmp_path / "beat").exists()


def test_launcher_failure_is_reported_not_raised(tmp_path):
    class BrokenLauncher:
        def list_attempts(self):
            raise RuntimeError("launcher is down")

    report = beat.beat(
        tmp_path, launcher=BrokenLauncher(), origin_branches={}, open_prs={},
        merge_stream=busy_merge, processor=Recorder(), now=T0,
    )

    sweep_report = report["duties"]["reconcile_completed_attempts"]
    assert sweep_report["status"] == "error"
    assert sweep_report["error"]["code"] == "launcher_unavailable"
    assert report["status"] == "partial"


# ---------------------------------------------------------------------------
# Idempotent and serialized
# ---------------------------------------------------------------------------


def test_repeated_runs_do_not_double_push_or_double_open(tmp_path):
    first = Recorder()
    second = Recorder()

    sweep(tmp_path, processor=first)
    again = sweep(tmp_path, processor=second)

    assert len(first.calls) == len(GAP_IDS)
    assert second.calls == []
    assert again["processed"] == []
    record = beat_reconciliation.ledger_record(tmp_path, GAP_IDS[0])
    assert record["status"] == "processed" and record["pushed"] is True


def test_branch_already_on_origin_is_skipped_with_reason_recorded(tmp_path):
    origin = {case["branch"]: SHA for case in GAP_CASES}

    report = sweep(tmp_path, origin_branches=origin, processor=Recorder())

    assert report["processed"] == []
    skipped = {item["attempt_id"]: item["reason"] for item in report["skipped"]}
    assert skipped == {case["attempt_id"]: "branch_on_origin" for case in GAP_CASES}
    durable = json.loads(
        (tmp_path / "beat" / "attempts-reconciliation" / "skips.json").read_text()
    )
    assert durable["counts"]["branch_on_origin"] == len(GAP_CASES)


def test_an_open_pr_is_skipped_with_reason_recorded(tmp_path):
    prs = {case["branch"]: [77] for case in GAP_CASES}

    report = sweep(tmp_path, open_prs=prs, processor=Recorder())

    assert report["processed"] == []
    skipped = {item["attempt_id"]: item["reason"] for item in report["skipped"]}
    assert skipped == {case["attempt_id"]: "pr_open" for case in GAP_CASES}


def test_the_sweep_claim_file_serializes_concurrent_sweeps(tmp_path):
    held = receipts.claim_key(tmp_path, beat_reconciliation.SWEEP_CLAIM_KEY, wait_s=5)
    try:
        report = sweep(tmp_path, processor=Recorder())

        assert report["status"] == "busy" and report["busy"] is True
        assert report["processed"] == []
    finally:
        held.release()

    # Once the claim file is free the sweep proceeds.
    after = sweep(tmp_path, processor=Recorder())
    assert {item["attempt_id"] for item in after["processed"]} == set(GAP_IDS)


def test_a_held_attempt_claim_blocks_only_that_attempt(tmp_path):
    held = receipts.claim_key(
        tmp_path, beat_reconciliation.attempt_claim_key(GAP_IDS[0]), wait_s=5
    )
    try:
        recorder = Recorder()
        report = sweep(tmp_path, processor=recorder)
        processed = {item["attempt_id"] for item in report["processed"]}
        assert processed == set(GAP_IDS) - {GAP_IDS[0]}
        skipped = {item["attempt_id"]: item["reason"] for item in report["skipped"]}
        assert skipped[GAP_IDS[0]] == "claim_busy"
    finally:
        held.release()

    after = sweep(tmp_path, processor=Recorder())
    assert GAP_IDS[0] in {item["attempt_id"] for item in after["processed"]}


def test_pushed_without_a_pr_resumes_the_pr_without_repushing(tmp_path):
    # A previous run recorded the push but crashed before the PR was opened.
    ledger = beat_reconciliation.ledger_dir(tmp_path)
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / f"{GAP_IDS[0]}.json").write_text(
        json.dumps(
            {
                "schema": beat_reconciliation.RECORD_SCHEMA,
                "attempt_id": GAP_IDS[0],
                "branch": GAP_CASES[0]["branch"],
                "status": "processed",
                "pushed": True,
                "sha": SHA,
                "pull_request": None,
                "at": iso(T0 - 60),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    recorder = Recorder()

    report = sweep(tmp_path, processor=recorder)

    assert [call["stage"] for call in recorder.calls if call["attempt_id"] == GAP_IDS[0]] == ["pr"]
    assert not any(
        call["attempt_id"] == GAP_IDS[0] and call["stage"] == "full"
        for call in recorder.calls
    )
    record = beat_reconciliation.ledger_record(tmp_path, GAP_IDS[0])
    assert record["pull_request"] and record["pushed"] is True


def test_failed_processing_backs_off_and_then_reprocesses(tmp_path):
    class Failing:
        def __call__(self, attempt_id, *, entry, branch, stage):
            return {"ok": False, "code": "push_failed", "error": "denied", "pushed": False}

    first = sweep(tmp_path, processor=Failing())
    assert {item["attempt_id"] for item in first["failed"]} == set(GAP_IDS)
    record = beat_reconciliation.ledger_record(tmp_path, GAP_IDS[0])
    assert record["status"] == "failed" and record["next_retry_at"]

    # Inside the backoff window the attempts are skipped, with the reason recorded.
    inside = sweep(tmp_path, processor=Failing())
    skipped = {item["attempt_id"]: item["reason"] for item in inside["skipped"]}
    assert skipped == {case["attempt_id"]: "backoff" for case in GAP_CASES}

    # After the bounded backoff the sweep tries again.
    after = sweep(tmp_path, processor=Recorder(), now=T0 + 3600)
    assert {item["attempt_id"] for item in after["processed"]} == set(GAP_IDS)


# ---------------------------------------------------------------------------
# The three concrete gap cases from today are regression fixtures
# ---------------------------------------------------------------------------


def test_gap_case_umbrella_modularity(tmp_path):
    report = sweep(
        tmp_path,
        launcher=FakeLauncher([gap_attempt(GAP_CASES[0], finished_at=T0 - 1950)]),
        processor=Recorder(),
    )

    assert [item["attempt_id"] for item in report["detected"]] == ["umbrella-modularity"]
    assert report["counts"]["processed"] == 1


def test_gap_case_rebrand_and_website_plan(tmp_path):
    report = sweep(
        tmp_path,
        launcher=FakeLauncher([gap_attempt(GAP_CASES[1], finished_at=T0 - 1950)]),
        processor=Recorder(),
    )

    assert [item["attempt_id"] for item in report["detected"]] == ["rebrand-and-website-plan"]
    assert report["counts"]["processed"] == 1


def test_gap_case_orbit_decomposition_f2_require_contract(tmp_path, monkeypatch):
    """The F2 case: a contracted attempt is verified through the contract path."""

    import bundlebees_plugin.acdd_cli as acdd_cli
    import bundlebees_plugin.verify_local as verify_local
    from bundlebees_plugin import leases

    brief = {
        "objective": "decompose the orbit module",
        "source_rev": SHA,
        "assigned_files": ["src/orbit.py"],
        "acceptance": {"criteria": ["module decomposed"], "tests": ["tests/test_orbit.py"]},
        "job_id": "job_f2",
        "task_id": "orbit-decomposition",
        "contract": {"id": "contract-f2", "sha256": "0" * 64},
    }
    leases.record_lease(tmp_path, "orbit-decomposition", brief, now=iso(T0))

    class StubClient:
        def result_envelope(self, attempt_id):
            return {
                "patch": {"diff": "--- a/src/orbit.py\n+++ b/src/orbit.py\n@@\n", "files": [{"path": "src/orbit.py"}]},
                "source": {"repo_rev": SHA},
                "patch_sha256": None,
            }

    seen: dict = {}

    def fake_verify(state_dir, repo, attempt_id, **kwargs):
        seen.update(kwargs, attempt_id=attempt_id, state_dir=state_dir)
        return {"ok": True, "sha": SHA, "branch": f"bb-verify/{attempt_id}"}

    monkeypatch.setattr(verify_local, "verify_and_commit", fake_verify)
    monkeypatch.setattr(acdd_cli, "verify_preflight", lambda *a, **k: {"contract": {"id": "contract-f2"}})
    pushes: list[tuple[str, str]] = []

    processor = beat_reconciliation.build_processor(
        StubClient(),
        tmp_path / "repo",
        tmp_path,
        verify=fake_verify,
        push=lambda repo, sha, branch: pushes.append((sha, branch)) or True,
        open_pr=lambda repo, branch, title, body: 4242,
    )
    outcome = processor(
        "orbit-decomposition",
        entry=gap_attempt(GAP_CASES[2], finished_at=T0),
        branch=GAP_CASES[2]["branch"],
        stage=beat_reconciliation.STAGE_FULL,
    )

    assert outcome["ok"] is True and outcome["pushed"] is True and outcome["pull_request"] == 4242
    # F2 require-contract: the frozen contract was honoured, never legacy mode.
    assert seen["contract_declared"] is True
    assert seen["acdd_preflight"] is not None
    assert seen["expected_patch_sha256"] is None
    assert pushes == [(SHA, "bee/orbit-decomposition")]


def test_a_lease_without_a_contract_verifies_in_legacy_mode(tmp_path, monkeypatch):
    import bundlebees_plugin.verify_local as verify_local
    from bundlebees_plugin import leases

    brief = {
        "objective": "rebrand the website plan",
        "source_rev": SHA,
        "assigned_files": ["docs/plan.md"],
        "acceptance": {"criteria": ["plan rebranded"], "tests": ["tests/test_plan.py"]},
        "job_id": "job_docs",
        "task_id": "rebrand-and-website-plan",
    }
    leases.record_lease(tmp_path, "rebrand-and-website-plan", brief, now=iso(T0))

    class StubClient:
        def result_envelope(self, attempt_id):
            return {
                "patch": {"diff": "--- a/docs/plan.md\n+++ b/docs/plan.md\n@@\n", "files": [{"path": "docs/plan.md"}]},
                "source": {"repo_rev": SHA},
                "patch_sha256": None,
            }

    seen: dict = {}

    def fake_verify(state_dir, repo, attempt_id, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "sha": SHA, "branch": f"bb-verify/{attempt_id}"}

    monkeypatch.setattr(verify_local, "verify_and_commit", fake_verify)

    processor = beat_reconciliation.build_processor(
        StubClient(),
        tmp_path / "repo",
        tmp_path,
        verify=fake_verify,
        push=lambda repo, sha, branch: True,
        open_pr=lambda repo, branch, title, body: 4243,
    )
    outcome = processor(
        "rebrand-and-website-plan",
        entry=gap_attempt(GAP_CASES[1], finished_at=T0),
        branch=GAP_CASES[1]["branch"],
        stage=beat_reconciliation.STAGE_FULL,
    )

    assert outcome["ok"] is True
    assert seen["contract_declared"] is False
    assert seen["acdd_preflight"] is None


def test_the_default_processor_refuses_to_verify_without_a_lease(tmp_path):
    class StubClient:
        def result_envelope(self, attempt_id):
            return {"patch": {"diff": "x"}, "source": {"repo_rev": SHA}}

    pushed: list[str] = []
    processor = beat_reconciliation.build_processor(
        StubClient(),
        tmp_path / "repo",
        tmp_path,
        push=lambda repo, sha, branch: pushed.append(branch) or True,
        open_pr=lambda repo, branch, title, body: 1,
    )
    outcome = processor(
        "umbrella-modularity",
        entry=gap_attempt(GAP_CASES[0], finished_at=T0),
        branch=GAP_CASES[0]["branch"],
        stage=beat_reconciliation.STAGE_FULL,
    )

    assert outcome["ok"] is False and outcome["code"] == "lease_missing"
    assert pushed == []


def test_the_default_processor_pushes_the_feature_branch_only(tmp_path):
    """The push refspec is explicit and never main; the PR heads the branch."""

    import bundlebees_plugin.verify_local as verify_local
    from bundlebees_plugin import leases

    brief = {
        "objective": "umbrella modularity",
        "source_rev": SHA,
        "assigned_files": ["docs/u.md"],
        "acceptance": {"criteria": ["modular"], "tests": ["tests/test_u.py"]},
    }
    leases.record_lease(tmp_path, "umbrella-modularity", brief, now=iso(T0))

    class StubClient:
        def result_envelope(self, attempt_id):
            return {
                "patch": {"diff": "--- a/docs/u.md\n+++ b/docs/u.md\n@@\n", "files": [{"path": "docs/u.md"}]},
                "source": {"repo_rev": SHA},
                "patch_sha256": None,
            }

    monkeypatch_verify = lambda state_dir, repo, attempt_id, **kwargs: {  # noqa: E731
        "ok": True, "sha": SHA, "branch": f"bb-verify/{attempt_id}",
    }
    calls: dict = {}

    def fake_push(repo, sha, branch):
        calls["push"] = (sha, branch)
        return True

    def fake_pr(repo, branch, title, body):
        calls["pr"] = (branch, title, body)
        return 77

    processor = beat_reconciliation.build_processor(
        StubClient(), tmp_path / "repo", tmp_path,
        verify=monkeypatch_verify, push=fake_push, open_pr=fake_pr,
    )
    outcome = processor(
        "umbrella-modularity",
        entry=gap_attempt(GAP_CASES[0], finished_at=T0),
        branch=GAP_CASES[0]["branch"],
        stage=beat_reconciliation.STAGE_FULL,
    )

    assert outcome["ok"] is True
    assert calls["push"] == (SHA, "bee/umbrella-modularity")
    assert calls["pr"][0] == "bee/umbrella-modularity"


def test_a_pr_failure_after_the_push_resumes_without_repushing(tmp_path):
    """A push that succeeded but whose PR step failed is retried PR-only."""

    import bundlebees_plugin.verify_local as verify_local
    from bundlebees_plugin import leases

    brief = {
        "objective": "umbrella modularity",
        "source_rev": SHA,
        "assigned_files": ["docs/u.md"],
        "acceptance": {"criteria": ["modular"], "tests": ["tests/test_u.py"]},
    }
    leases.record_lease(tmp_path, "umbrella-modularity", brief, now=iso(T0))

    class StubClient:
        def result_envelope(self, attempt_id):
            return {
                "patch": {"diff": "--- a/docs/u.md\n+++ b/docs/u.md\n@@\n", "files": [{"path": "docs/u.md"}]},
                "source": {"repo_rev": SHA},
                "patch_sha256": None,
            }

    pushes: list[str] = []

    def failing_pr(repo, branch, title, body):
        raise RuntimeError("gh is down")

    processor = beat_reconciliation.build_processor(
        StubClient(), tmp_path / "repo", tmp_path,
        verify=lambda *a, **k: {"ok": True, "sha": SHA, "branch": "x"},
        push=lambda repo, sha, branch: pushes.append(branch) or True,
        open_pr=failing_pr,
    )
    outcome = processor(
        "umbrella-modularity",
        entry=gap_attempt(GAP_CASES[0], finished_at=T0),
        branch=GAP_CASES[0]["branch"],
        stage=beat_reconciliation.STAGE_FULL,
    )
    assert outcome["ok"] is False and outcome["code"] == "pr_open_failed"
    assert outcome["pushed"] is True
    assert pushes == ["bee/umbrella-modularity"]

    first = sweep(tmp_path, processor=processor)
    assert first["failed"][0]["code"] == "pr_open_failed"

    # The retry opens only the PR: the branch is never pushed twice.
    pushes.clear()

    def working_pr(repo, branch, title, body):
        return 88

    retry_processor = beat_reconciliation.build_processor(
        StubClient(), tmp_path / "repo", tmp_path,
        verify=lambda *a, **k: {"ok": True, "sha": SHA, "branch": "x"},
        push=lambda repo, sha, branch: pushes.append(branch) or True,
        open_pr=working_pr,
    )
    retry = sweep(tmp_path, processor=retry_processor)
    assert pushes == []
    assert retry["processed"][0]["pull_request"] == 88
    assert retry["processed"][0]["pushed"] is True


# ---------------------------------------------------------------------------
# Classification edges
# ---------------------------------------------------------------------------


def test_unfinished_attempts_are_skipped_with_reason_recorded(tmp_path):
    entry = gap_attempt(GAP_CASES[0], finished_at=T0)
    entry["state"] = "running"

    report = sweep(
        tmp_path,
        launcher=FakeLauncher([entry]),
        processor=Recorder(),
    )

    assert report["processed"] == []
    assert report["skipped"][0]["reason"] == "not_succeeded"


def test_a_missing_or_unsafe_branch_is_never_pushed(tmp_path):
    missing = gap_attempt(GAP_CASES[0], finished_at=T0)
    missing["branch"] = ""
    unsafe = gap_attempt(GAP_CASES[1], finished_at=T0)
    unsafe["branch"] = "../escape"
    main = gap_attempt(GAP_CASES[2], finished_at=T0)
    main["branch"] = "main"

    report = sweep(
        tmp_path,
        launcher=FakeLauncher([missing, unsafe, main]),
        processor=Recorder(),
    )

    reasons = {item["attempt_id"]: item["reason"] for item in report["skipped"]}
    assert reasons["umbrella-modularity"] == "branch_unknown"
    assert reasons["rebrand-and-website-plan"] == "branch_invalid"
    assert reasons["orbit-decomposition"] == "branch_invalid"
    assert report["processed"] == []


def test_a_malformed_attempt_entry_is_counted_never_crashes(tmp_path):
    report = sweep(
        tmp_path,
        launcher=FakeLauncher([{"state": "succeeded", "branch": "bee/x"}]),
        processor=Recorder(),
    )

    assert report["processed"] == []
    assert report["skipped"][0]["reason"] == "malformed_attempt"
    assert report["status"] == "ok"


def test_the_last_sweep_report_is_durable(tmp_path):
    sweep(tmp_path, processor=Recorder())

    last = json.loads(
        (tmp_path / "beat" / "attempts-reconciliation" / "last-sweep.json").read_text()
    )
    assert last["schema"] == beat_reconciliation.SCHEMA
    assert {item["attempt_id"] for item in last["processed"]} == set(GAP_IDS)


# ---------------------------------------------------------------------------
# The production views: read-only origin and PR observation
# ---------------------------------------------------------------------------


def test_default_origin_view_parses_ls_remote_output(tmp_path):
    def git(argv):
        assert argv == ["ls-remote", "--heads", "origin"]
        return 0, f"{SHA}\trefs/heads/bee/umbrella-modularity\n", ""

    view = beat_reconciliation.default_origin_branches(tmp_path, git=git)
    assert view == {"bee/umbrella-modularity": SHA}


def test_default_open_pr_view_parses_gh_list_output(tmp_path):
    def gh(argv):
        return 0, '[{"number": 12, "headRefName": "bee/orbit-decomposition"}]', ""

    view = beat_reconciliation.default_open_prs(tmp_path, gh=gh)
    assert view == {"bee/orbit-decomposition": [12]}


def test_an_unobservable_pr_view_fails_the_sweep_closed(tmp_path):
    def gh(argv):
        return 1, "", "authentication failed"

    report = sweep(
        tmp_path,
        open_prs=lambda: beat_reconciliation.default_open_prs(tmp_path, gh=gh),
        processor=Recorder(),
    )

    assert report["status"] == "error"
    assert report["error"]["code"] == "pr_view_unavailable"
    assert report["processed"] == []
