"""Graceful recovery for attempts that die on environment faults.

Every case runs in a fresh temporary state directory.  No model, bee,
launcher or network is involved: the module only reads and writes its own
durable records (``graceful/<attempt_id>/...`` and the coordinator claim
file) and, for the scheduler-unpoison helper, the durable work queue
through ``work_scheduler``'s public API.

The fixture is today's real workbench case: attempt ``att_0cdf5d9d``, a
2.6-hour debugging bee that died on a worker transport fault.  Its durable
event stream produced the unified survival-classification root cause, the
``menu [hidden]`` suspect, and the node22/tmux/env lessons — the diagnosis
that, before this mechanism, was recovered only by manually mining the
stream.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import graceful_recovery as gr
from bundlebees_plugin import work_scheduler as ws

BASE = 1_800_000_000.0

DEAD_ATTEMPT = "att_0cdf5d9d"

BASE_BRIEF = {
    "branch": "workbench/survival-classification",
    "lease_files": [
        "workbench/survival/panel.py",
        "workbench/survival/menu.py",
    ],
    "source_rev": "93b02f138b09db25a27fb79995d02ced460e75c1",
    "acceptance": {
        "criteria": ["survival panel rows survive the menu [hidden] toggle"],
    },
}

#: The durable event stream of the real ``att_0cdf5d9d`` workbench case:
#: worker narration (``worker:message`` texts) followed by the terminal
#: transport fault that killed the bee.
WORKBENCH_STREAM: tuple[dict, ...] = (
    {
        "ts": "2026-11-16T06:12:03Z",
        "type": "worker:message",
        "text": "Scope check: the survival panel loses its row highlight after "
        "the menu toggle; starting a repro before touching anything.",
    },
    {
        "ts": "2026-11-16T06:40:51Z",
        "kind": "worker:message",
        "text": "Root cause: the unified survival-classification pass re-derives "
        "each row's class on every render, so a toggle that re-renders "
        "mid-pass drops rows whose flag was not yet re-confirmed.",
    },
    {
        "ts": "2026-11-16T07:05:12Z",
        "type": "worker:message",
        "text": "Suspect: menu [hidden] — hiding the menu unmounts the panel "
        "before the classification pass finishes, which is why the row "
        "vanishes only while the menu is hidden.",
    },
    {
        "ts": "2026-11-16T07:31:44Z",
        "type": "worker:message",
        "text": "Repro: $ pytest workbench/tests/test_survival_panel.py::test_row_survives_menu_hidden -x",
    },
    {
        "ts": "2026-11-16T08:02:19Z",
        "type": "worker:message",
        "text": "Env lessons: run the workbench suite on node22 only inside tmux, "
        "the pty transport dies otherwise; and set WORKBENCH_CLASSIFY=1 or "
        "the unified survival-classification pass is skipped and the bug "
        "does not reproduce.",
    },
    {
        "ts": "2026-11-16T08:40:07Z",
        "type": "worker:message",
        "text": "Next step planned: pin the row class before the menu [hidden] "
        "unmount, then re-run the repro.",
    },
    {
        "ts": "2026-11-16T08:47:52Z",
        "type": "worker:event",
        "event": "attempt_failed",
        "code": "shell_transport_failed",
        "detail": "shell transport failed: worker pty hit EOF after 2.6h; "
        "stream closed before the final report could be returned",
    },
)


def clock(step: float = 0.0):
    value = [BASE]

    def tick() -> float:
        value[0] += step
        return value[0]

    return tick


def write_workbench_stream(state: Path, attempt_id: str = DEAD_ATTEMPT) -> None:
    """Persist the real workbench case's event stream into the state dir."""

    for record in WORKBENCH_STREAM:
        gr.record_stream_event(state, attempt_id, record, now="2026-11-16T09:00:00Z")


def _dead_pid() -> int:
    """A pid that provably exited (reaped child process)."""

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ---------------------------------------------------------------------------
# 1. Env-fault detection
# ---------------------------------------------------------------------------


def test_shell_transport_failure_is_environment_class() -> None:
    verdict = gr.classify_env_fault(
        {"code": "worker_failed", "detail": "shell transport failed: pty read EOF"}
    )
    assert verdict["env_fault"] is True
    assert verdict["class"] == "environment"
    assert verdict["code"] == "shell_transport_failed"
    assert verdict["blind_retry"] == "refused"
    assert gr.is_env_fault(verdict)


def test_workspace_invalid_is_environment_class() -> None:
    verdict = gr.classify_env_fault("workspace_invalid: the worktree vanished mid-run")
    assert verdict["env_fault"] is True
    assert verdict["class"] == "environment"
    assert verdict["code"] == "workspace_invalid"
    assert verdict["blind_retry"] == "refused"


def test_stream_closed_is_environment_class() -> None:
    verdict = gr.classify_env_fault(
        {"error": "stream_closed", "message": "output stream closed unexpectedly"}
    )
    assert verdict["env_fault"] is True
    assert verdict["class"] == "environment"
    assert verdict["code"] == "stream_closed"
    assert verdict["blind_retry"] == "refused"


def test_container_engine_eof_is_environment_class() -> None:
    verdict = gr.classify_env_fault(
        "container engine EOF: dockerd went away while the sandbox was running"
    )
    assert verdict["env_fault"] is True
    assert verdict["class"] == "environment"
    assert verdict["code"] == "container_engine_eof"
    assert verdict["blind_retry"] == "refused"


def test_generic_transport_codes_classify_as_env_fault() -> None:
    for record in (
        {"code": "transport_error"},
        {"state": "failed", "evidence": "worker_lost: the bee stopped reporting"},
        "connection lost while reading the worker stream",
        {"detail": "broken pipe on the worker transport"},
    ):
        verdict = gr.classify_env_fault(record)
        assert verdict["env_fault"] is True, record
        assert verdict["class"] == "environment"


def test_plain_patch_failure_is_not_env_fault() -> None:
    verdict = gr.classify_env_fault(
        {"code": "test_failed", "evidence": "tests/test_x.py::test_y failed"}
    )
    assert verdict["env_fault"] is False
    assert verdict["class"] is None
    assert verdict["blind_retry"] != "refused"


def test_unclassifiable_garbage_is_not_env_fault() -> None:
    for record in (None, "", {}, {"detail": "mysterious failure"}, 42):
        verdict = gr.classify_env_fault(record)
        assert verdict["env_fault"] is False


# ---------------------------------------------------------------------------
# 2. Event-stream recovery digest (the att_0cdf5d9d workbench fixture)
# ---------------------------------------------------------------------------


def test_stream_events_are_durable_and_ordered(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    records = gr.read_stream(tmp_path, DEAD_ATTEMPT)
    assert len(records) == len(WORKBENCH_STREAM)
    assert records[0]["text"].startswith("Scope check")
    assert records[-1]["code"] == "shell_transport_failed"
    assert gr.stream_path(tmp_path, DEAD_ATTEMPT).name == "stream.jsonl"


def test_stream_refuses_unsafe_attempt_id(tmp_path: Path) -> None:
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.record_stream_event(tmp_path, "../escape", {"type": "worker:message", "text": "x"})
    assert excinfo.value.code == "attempt_invalid"


def test_stream_refuses_non_object_records(tmp_path: Path) -> None:
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.record_stream_event(tmp_path, DEAD_ATTEMPT, "not-a-mapping")
    assert excinfo.value.code == "stream_record_invalid"


def test_digest_extracts_the_workbench_case(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    digest = gr.recovery_digest(tmp_path, DEAD_ATTEMPT)

    assert digest["schema"] == gr.DIGEST_SCHEMA
    assert digest["attempt_id"] == DEAD_ATTEMPT
    # the worker narration, in stream order
    assert digest["narration_count"] == 6
    assert digest["narration"][0]["text"].startswith("Scope check")

    # what was diagnosed
    assert any("unified survival-classification" in text for text in digest["root_cause"])
    # the suspect
    assert any("menu [hidden]" in text for text in digest["suspects"])
    # the repro command
    assert any(
        "pytest workbench/tests/test_survival_panel.py::test_row_survives_menu_hidden" in text
        for text in digest["repro"]
    )
    # the env lessons
    assert any("node22" in text and "tmux" in text for text in digest["lessons"])

    # the stream's terminal fault classifies as an environment fault
    assert digest["env_fault"]["env_fault"] is True
    assert digest["env_fault"]["code"] == "shell_transport_failed"

    # provenance: where the digest came from
    assert digest["source"]["stream"] == f"graceful/{DEAD_ATTEMPT}/stream.jsonl"
    assert digest["source"]["records"] == len(WORKBENCH_STREAM)
    assert digest["digest_sha256"]


def test_digest_is_empty_without_worker_narration(tmp_path: Path) -> None:
    gr.record_stream_event(tmp_path, "att_other", {"type": "worker:event", "event": "started"})
    digest = gr.recovery_digest(tmp_path, "att_other")
    assert digest["narration_count"] == 0
    assert digest["root_cause"] == []
    assert digest["usable"] is False


def test_digest_is_durable_create_only_and_verified(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    digest = gr.recovery_digest(tmp_path, DEAD_ATTEMPT, now=BASE)

    first = gr.save_digest(tmp_path, DEAD_ATTEMPT, digest)
    assert first["created"]
    replay = gr.save_digest(tmp_path, DEAD_ATTEMPT, digest)
    assert replay["created"] is False

    loaded = gr.load_digest(tmp_path, DEAD_ATTEMPT)
    assert loaded["digest_sha256"] == digest["digest_sha256"]
    assert loaded["root_cause"]

    with pytest.raises(gr.GracefulRecoveryError) as missing:
        gr.load_digest(tmp_path, "att_never_ran")
    assert missing.value.code == "digest_missing"


def test_tampered_digest_is_refused(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    gr.save_digest(tmp_path, DEAD_ATTEMPT, gr.recovery_digest(tmp_path, DEAD_ATTEMPT))
    path = gr.digest_path(tmp_path, DEAD_ATTEMPT)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["root_cause"] = ["totally different diagnosis"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.load_digest(tmp_path, DEAD_ATTEMPT)
    assert excinfo.value.code == "digest_mismatch"


def test_explicit_findings_beat_inference(tmp_path: Path) -> None:
    gr.record_stream_event(
        tmp_path,
        "att_findings",
        {"type": "finding", "kind": "root_cause", "text": "flag order inversion in classify()"},
    )
    gr.record_stream_event(
        tmp_path,
        "att_findings",
        {"type": "worker:message", "text": "Root cause guess: something else entirely"},
    )
    digest = gr.recovery_digest(tmp_path, "att_findings")
    assert digest["root_cause"][0] == "flag order inversion in classify()"
    assert any("something else entirely" in text for text in digest["root_cause"])


# ---------------------------------------------------------------------------
# 3. Resume-with-evidence re-dispatch
# ---------------------------------------------------------------------------


def test_resume_brief_embeds_the_digest_with_provenance(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    gr.save_digest(tmp_path, DEAD_ATTEMPT, gr.recovery_digest(tmp_path, DEAD_ATTEMPT))

    brief = gr.compose_resume_brief(
        tmp_path,
        dead_attempt_id=DEAD_ATTEMPT,
        job_id="job_workbench",
        task_id="T_survival",
        base_brief=BASE_BRIEF,
        now=BASE,
    )

    assert brief["schema"] == gr.RESUME_SCHEMA
    assert brief["kind"] == "resume_with_evidence"
    assert brief["dead_attempt_id"] == DEAD_ATTEMPT

    # the digest is embedded, explicitly labeled recovered-findings
    findings = brief["recovered_findings"]
    assert findings["label"] == "recovered-findings"
    assert any("unified survival-classification" in text for text in findings["root_cause"])
    assert any("menu [hidden]" in text for text in findings["suspects"])
    assert any("node22" in text for text in findings["lessons"])

    # the same lease/branch is re-dispatched
    assert brief["dispatch"]["branch"] == BASE_BRIEF["branch"]
    assert brief["dispatch"]["lease_files"] == BASE_BRIEF["lease_files"]
    assert brief["dispatch"]["source_rev"] == BASE_BRIEF["source_rev"]

    # provenance records the dead attempt and the digest source
    provenance = brief["provenance"]
    assert provenance["dead_attempt_id"] == DEAD_ATTEMPT
    assert provenance["digest_source"] == f"graceful/{DEAD_ATTEMPT}/digest.json"
    assert provenance["digest_sha256"]
    assert provenance["stream_source"] == f"graceful/{DEAD_ATTEMPT}/stream.jsonl"
    assert provenance["env_fault_code"] == "shell_transport_failed"

    gr.validate_resume_brief(brief)
    gr.verify_resume_brief(tmp_path, brief)


def test_blind_retry_without_digest_is_refused(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)  # stream exists, but no digest was saved
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.compose_resume_brief(
            tmp_path,
            dead_attempt_id=DEAD_ATTEMPT,
            base_brief=BASE_BRIEF,
            now=BASE,
        )
    assert excinfo.value.code == "resume_without_evidence"


def test_hand_built_brief_without_findings_is_refused_by_the_mechanism(tmp_path: Path) -> None:
    """A blind retry cannot be laundered through a hand-composed brief."""

    blind = {
        "schema": gr.RESUME_SCHEMA,
        "kind": "resume_with_evidence",
        "dead_attempt_id": DEAD_ATTEMPT,
        "dispatch": {"lease_files": BASE_BRIEF["lease_files"], "branch": BASE_BRIEF["branch"]},
        "provenance": {
            "dead_attempt_id": DEAD_ATTEMPT,
            "digest_source": f"graceful/{DEAD_ATTEMPT}/digest.json",
            "digest_sha256": "0" * 64,
        },
    }
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.validate_resume_brief(blind)
    assert excinfo.value.code == "resume_without_evidence"


def test_resume_with_empty_digest_is_refused(tmp_path: Path) -> None:
    gr.record_stream_event(tmp_path, "att_silent", {"type": "worker:event", "event": "started"})
    gr.record_stream_event(tmp_path, "att_silent", {"code": "shell_transport_failed"})
    gr.save_digest(tmp_path, "att_silent", gr.recovery_digest(tmp_path, "att_silent"))

    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.compose_resume_brief(
            tmp_path,
            dead_attempt_id="att_silent",
            base_brief={"lease_files": ["src/x.py"], "source_rev": "a" * 40},
            now=BASE,
        )
    assert excinfo.value.code == "resume_without_evidence"


def test_resume_refused_without_lease_scope(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    gr.save_digest(tmp_path, DEAD_ATTEMPT, gr.recovery_digest(tmp_path, DEAD_ATTEMPT))

    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.compose_resume_brief(
            tmp_path,
            dead_attempt_id=DEAD_ATTEMPT,
            base_brief={"acceptance": BASE_BRIEF["acceptance"]},
            now=BASE,
        )
    assert excinfo.value.code == "resume_scope_missing"


def test_resume_refused_when_the_fault_is_not_environmental(tmp_path: Path) -> None:
    gr.record_stream_event(
        tmp_path,
        "att_patch_fail",
        {"type": "worker:message", "text": "tests failed: test_row_survives_menu_hidden"},
    )
    gr.save_digest(tmp_path, "att_patch_fail", gr.recovery_digest(tmp_path, "att_patch_fail"))

    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.compose_resume_brief(
            tmp_path,
            dead_attempt_id="att_patch_fail",
            base_brief={"lease_files": ["src/x.py"], "source_rev": "b" * 40},
            now=BASE,
        )
    assert excinfo.value.code == "resume_not_env_fault"


def test_resume_evidence_mismatch_is_refused(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    gr.save_digest(tmp_path, DEAD_ATTEMPT, gr.recovery_digest(tmp_path, DEAD_ATTEMPT))
    brief = gr.compose_resume_brief(
        tmp_path, dead_attempt_id=DEAD_ATTEMPT, base_brief=BASE_BRIEF, now=BASE
    )

    # the durable digest changes underneath the brief
    forged = dict(brief)
    forged["recovered_findings"] = dict(brief["recovered_findings"], root_cause=["a different diagnosis"])

    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.verify_resume_brief(tmp_path, forged)
    assert excinfo.value.code == "resume_evidence_mismatch"


def test_resume_record_is_create_only_and_idempotent(tmp_path: Path) -> None:
    write_workbench_stream(tmp_path)
    gr.save_digest(tmp_path, DEAD_ATTEMPT, gr.recovery_digest(tmp_path, DEAD_ATTEMPT))
    brief = gr.compose_resume_brief(
        tmp_path, dead_attempt_id=DEAD_ATTEMPT, base_brief=BASE_BRIEF, now=BASE
    )

    first = gr.record_resume(tmp_path, DEAD_ATTEMPT, brief, now=BASE)
    assert first["created"] is True
    replay = gr.record_resume(tmp_path, DEAD_ATTEMPT, brief, now=BASE + 5)
    assert replay["created"] is False
    assert replay["record"]["provenance"]["dead_attempt_id"] == DEAD_ATTEMPT
    assert gr.resume_path(tmp_path, DEAD_ATTEMPT).name == "resume.json"


def test_resume_refuses_unsafe_attempt_id(tmp_path: Path) -> None:
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.compose_resume_brief(tmp_path, dead_attempt_id="../escape", now=BASE)
    assert excinfo.value.code == "attempt_invalid"


# ---------------------------------------------------------------------------
# 4. Claim release on death
# ---------------------------------------------------------------------------


def test_take_and_release_claim_file(tmp_path: Path) -> None:
    claim = gr.take_claim(tmp_path, owner="beat_cron", now=BASE)
    assert claim["owner"] == "beat_cron"
    assert claim["generation"] == 1
    assert gr.claim_path(tmp_path).name == "beat.claim.json"

    state = gr.claim_state(tmp_path, now=BASE + 10)
    assert state["exists"] is True
    assert state["live"] is True
    assert state["owner"] == "beat_cron"

    released = gr.release_claim(tmp_path, owner="beat_cron", now=BASE + 20)
    assert released["released"] is True
    assert gr.claim_state(tmp_path, now=BASE + 21)["exists"] is False


def test_second_owner_is_refused_while_the_claim_is_live(tmp_path: Path) -> None:
    gr.take_claim(tmp_path, owner="beat_cron", now=BASE, pid=os.getpid())
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.take_claim(tmp_path, owner="beat_manual", now=BASE + 10)
    assert excinfo.value.code == "claim_held"


def test_same_owner_renews_the_claim(tmp_path: Path) -> None:
    first = gr.take_claim(tmp_path, owner="beat_cron", now=BASE)
    renewed = gr.take_claim(tmp_path, owner="beat_cron", now=BASE + 300)
    assert renewed["generation"] == first["generation"]
    assert renewed["expires_epoch"] > first["expires_epoch"]


def test_release_on_death_with_env_fault_evidence(tmp_path: Path) -> None:
    """Today's case: a manual fire took the claim, then died on a transport fault."""

    gr.take_claim(tmp_path, owner="beat_manual", now=BASE, ttl_s=3600)
    evidence = gr.classify_env_fault(
        {"code": "shell_transport_failed", "detail": "shell transport failed: pty EOF"}
    )

    released = gr.release_claim_on_death(
        tmp_path,
        owner="beat_manual",
        death_evidence=evidence,
        now=BASE + 60,
    )
    assert released["released"] is True
    assert released["proof"] == "env_fault_evidence"
    assert gr.claim_state(tmp_path, now=BASE + 61)["exists"] is False

    # the scheduler is unpoisoned at once: the next beat can take the claim
    nxt = gr.take_claim(tmp_path, owner="beat_cron", now=BASE + 62)
    assert nxt["owner"] == "beat_cron"

    # the release is auditable
    releases = gr.read_release_log(tmp_path)
    assert any(
        entry["owner"] == "beat_manual" and entry["proof"] == "env_fault_evidence"
        for entry in releases
    )


def test_release_on_death_refused_without_proof(tmp_path: Path) -> None:
    gr.take_claim(tmp_path, owner="beat_cron", now=BASE, ttl_s=3600)
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.release_claim_on_death(tmp_path, owner="beat_cron", now=BASE + 10)
    assert excinfo.value.code == "claim_release_refused"
    assert gr.claim_state(tmp_path, now=BASE + 11)["exists"] is True


def test_release_on_death_refuses_a_live_foreign_owner(tmp_path: Path) -> None:
    gr.take_claim(tmp_path, owner="beat_cron", now=BASE, ttl_s=3600)
    evidence = gr.classify_env_fault("shell transport failed")
    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.release_claim_on_death(
            tmp_path, owner="beat_manual", death_evidence=evidence, now=BASE + 10
        )
    assert excinfo.value.code == "claim_not_held"


def test_release_on_death_with_dead_pid(tmp_path: Path) -> None:
    dead = _dead_pid()
    gr.take_claim(tmp_path, owner="beat_manual", now=BASE, ttl_s=3600, pid=dead)
    released = gr.release_claim_on_death(tmp_path, owner="beat_manual", now=BASE + 10)
    assert released["released"] is True
    assert released["proof"] == "holder_pid_dead"


def test_release_on_death_without_claim_is_a_noop(tmp_path: Path) -> None:
    released = gr.release_claim_on_death(tmp_path, owner="beat_cron", now=BASE)
    assert released["released"] is False
    assert released["reason"] == "no_claim"


def test_stale_takeover_only_after_the_documented_threshold(tmp_path: Path) -> None:
    gr.take_claim(tmp_path, owner="beat_manual", now=BASE, ttl_s=3600)  # pid alive, unexpired

    with pytest.raises(gr.GracefulRecoveryError) as early:
        gr.takeover_claim(tmp_path, owner="beat_cron", now=BASE + 301)
    assert early.value.code == "claim_not_stale"

    taken = gr.takeover_claim(tmp_path, owner="beat_cron", now=BASE + 601)
    assert taken["owner"] == "beat_cron"
    assert taken["took_over_from"]["owner"] == "beat_manual"
    assert taken["took_over_from"]["reason"] == "stale_threshold"
    assert taken["took_over_from"]["threshold_s"] == gr.CLAIM_STALE_THRESHOLD_S


def test_expired_claim_is_replaceable_by_a_normal_take(tmp_path: Path) -> None:
    gr.take_claim(tmp_path, owner="beat_manual", now=BASE, ttl_s=600)
    taken = gr.take_claim(tmp_path, owner="beat_cron", now=BASE + 601)
    assert taken["owner"] == "beat_cron"
    assert taken["took_over_from"]["reason"] == "expired"


def test_dead_holder_claim_is_replaceable_by_a_normal_take(tmp_path: Path) -> None:
    dead = _dead_pid()
    gr.take_claim(tmp_path, owner="beat_manual", now=BASE, ttl_s=3600, pid=dead)
    taken = gr.take_claim(tmp_path, owner="beat_cron", now=BASE + 5)
    assert taken["owner"] == "beat_cron"
    assert taken["took_over_from"]["reason"] == "holder_dead"


# ---------------------------------------------------------------------------
# 5. The scheduler is unpoisoned, and the sweep is not disturbed
# ---------------------------------------------------------------------------


def test_dead_beats_queue_claim_is_recovered_with_evidence(tmp_path: Path) -> None:
    """A manual fire claimed a queue item, then died: the item must not stay claimed."""

    ws.add_item(tmp_path, work_id="ws_sweep", title="Sweep", kind="triage", now=BASE)
    ws.claim(tmp_path, "ws_sweep", owner="beat_manual", now=BASE)

    evidence = gr.classify_env_fault("shell transport failed: stream closed")
    result = gr.recover_scheduler_claim(
        tmp_path,
        "ws_sweep",
        dead_owner="beat_manual",
        death_evidence=evidence,
        new_owner="beat_cron",
        now=BASE + 60,
    )
    assert result["released"] is True
    assert result["claimed_by"] == "beat_cron"
    item = {i["work_id"]: i for i in ws.list_items(tmp_path)}["ws_sweep"]
    assert item["owner"] == "beat_cron"
    assert item["status"] == "claimed"


def test_scheduler_recovery_refused_without_death_proof(tmp_path: Path) -> None:
    ws.add_item(tmp_path, work_id="ws_sweep", title="Sweep", kind="triage", now=BASE)
    ws.claim(tmp_path, "ws_sweep", owner="beat_manual", now=BASE)

    with pytest.raises(gr.GracefulRecoveryError) as excinfo:
        gr.recover_scheduler_claim(
            tmp_path, "ws_sweep", dead_owner="beat_manual", now=BASE + 60
        )
    assert excinfo.value.code == "claim_release_refused"

    item = {i["work_id"]: i for i in ws.list_items(tmp_path)}["ws_sweep"]
    assert item["status"] == "claimed"
    assert item["owner"] == "beat_manual"


def test_scheduler_recovery_falls_back_to_stale_takeover(tmp_path: Path) -> None:
    ws.add_item(tmp_path, work_id="ws_stale", title="Stale", kind="triage", now=BASE)
    ws.claim(tmp_path, "ws_stale", owner="beat_manual", now=BASE)

    result = gr.recover_scheduler_claim(
        tmp_path,
        "ws_stale",
        dead_owner="beat_manual",
        new_owner="beat_cron",
        threshold_s=gr.CLAIM_STALE_THRESHOLD_S,
        now=BASE + gr.CLAIM_STALE_THRESHOLD_S + 1,
    )
    assert result["released"] is True
    assert result["proof"] == "stale_threshold"
    item = {i["work_id"]: i for i in ws.list_items(tmp_path)}["ws_stale"]
    assert item["owner"] == "beat_cron"


def test_beat_claim_file_never_disturbs_the_reconciliation_sweep(tmp_path: Path) -> None:
    """A live beat claim must not block the sweep or gate-watcher queue duties."""

    gr.take_claim(tmp_path, owner="beat_cron", now=BASE, ttl_s=3600)

    ws.add_item(tmp_path, work_id="ws_gate", title="Gate check", kind="triage", now=BASE + 1)
    claimed = ws.claim(tmp_path, "ws_gate", owner="sweep_beat", now=BASE + 2)
    assert claimed["status"] == "claimed"

    ready = ws.ready_items(tmp_path)
    assert ready == []

    done = ws.complete(tmp_path, "ws_gate", owner="sweep_beat", now=BASE + 3)
    assert done["status"] == "done"

    # the claim file is untouched by the sweep
    state = gr.claim_state(tmp_path, now=BASE + 4)
    assert state["owner"] == "beat_cron"
    assert state["live"] is True


def test_full_graceful_pass_end_to_end(tmp_path: Path) -> None:
    """Stream -> digest -> resume brief, and the claim released on death."""

    write_workbench_stream(tmp_path)
    ws.add_item(tmp_path, work_id="ws_bee", title="Bee", kind="feature", now=BASE)
    ws.claim(tmp_path, "ws_bee", owner="beat_manual", now=BASE)
    gr.take_claim(tmp_path, owner="beat_manual", now=BASE)

    # the recovery pass: classify, digest, save, resume
    stream = gr.read_stream(tmp_path, DEAD_ATTEMPT)
    verdict = gr.classify_env_fault(stream[-1])
    assert verdict["env_fault"] is True

    digest = gr.recovery_digest(tmp_path, DEAD_ATTEMPT)
    gr.save_digest(tmp_path, DEAD_ATTEMPT, digest, now=BASE)

    brief = gr.compose_resume_brief(
        tmp_path, dead_attempt_id=DEAD_ATTEMPT, base_brief=BASE_BRIEF, now=BASE
    )
    recorded = gr.record_resume(tmp_path, DEAD_ATTEMPT, brief, now=BASE)
    assert recorded["created"] is True

    # the claim release: the manual fire's claim does not poison the scheduler
    released = gr.release_claim_on_death(
        tmp_path, owner="beat_manual", death_evidence=verdict, now=BASE + 1
    )
    assert released["released"] is True
    queue_result = gr.recover_scheduler_claim(
        tmp_path,
        "ws_bee",
        dead_owner="beat_manual",
        death_evidence=verdict,
        new_owner="beat_cron",
        now=BASE + 2,
    )
    assert queue_result["claimed_by"] == "beat_cron"

    # and the resume is recorded durably with its provenance
    stored = json.loads(gr.resume_path(tmp_path, DEAD_ATTEMPT).read_text(encoding="utf-8"))
    assert stored["recovered_findings"]["label"] == "recovered-findings"
    assert stored["provenance"]["dead_attempt_id"] == DEAD_ATTEMPT
