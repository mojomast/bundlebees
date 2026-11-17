"""Exercise the registered job report command with independent local fixture ledgers."""

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from bundlebees_plugin import job as jobs, leases, return_report
from tests.fakes import FakeCtx, load_plugin
from tests.test_return_report import _frozen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contrib"))
from rizzstack_rails import RailsLedger  # noqa: E402

REV = "a" * 40
JOB, TASK, ATTEMPT = "job_return", "T1", "att_1234567890abcdef"


@pytest.fixture
def packet(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    contract, digest = _frozen(
        REV,
        contract_id="TOOLS",
        statement="Command recorded",
        assigned="tools/a.py",
        test_file="tests/test_a.py",
        test_first=True,
    )
    jobs.record_authorization(
        state,
        job_id=JOB,
        statement="Exercise local handoff",
        repo="fixture/tools",
        base_rev=REV,
        scope=["tools/**"],
        bees=1,
        wall_clock_s=1800,
        require_tty=False,
        interactive=False,
        by="owner",
    )
    jobs.create_job(
        state,
        job_id=JOB,
        tasks=[
            {
                "task_id": TASK,
                "objective": "record tools",
                "lease": ["tools/a.py"],
                "contract": contract,
                "contract_sha256": digest,
            }
        ],
    )
    job = jobs.load_job(state, JOB)
    job["tasks"][0]["attempts"] = [ATTEMPT]
    jobs.save_job(state, job)
    leases.record_lease(
        state,
        ATTEMPT,
        {
            "job_id": JOB,
            "task_id": TASK,
            "source_rev": REV,
            "contract_sha256": digest,
            "objective": "record tools",
            "assigned_files": ["tools/a.py"],
            "acceptance": {"tests": ["tests/test_a.py"]},
        },
    )
    ledger = tmp_path / "coordinator.sqlite"
    with closing(RailsLedger(tmp_path / "worker.sqlite")) as worker:
        worker.append(
            "tool.finish",
            scope={"attempt_id": ATTEMPT},
            source=dict(adapter="launcher-log", instance="worker-1", record_id="call-1", revision="v1"),
            authority="worker_report",
            dedupe_key="call-1",
            payload=dict(
                call_id="call-1",
                tool_name="read",
                command_family=None,
                outcome="ok",
                summary="Tool completion observed",
                evidence_ref="rec:1",
            ),
        )
        worker.append(
            "source.gap",
            scope={"attempt_id": ATTEMPT},
            source=dict(adapter="launcher-log", instance="worker-1", record_id="gap-1", revision="v1"),
            authority="worker_report",
            dedupe_key="gap-1",
            payload=dict(
                source_id="launcher-log",
                cursor="5",
                code="source_gap",
                range_start=3,
                range_end=4,
                count=2,
                digest=None,
            ),
        )
        wh = worker.head()  # independently supplied, not taken from the imported copy
        pages = [
            r["envelope_json"] for r in worker.query("select envelope_json from event_rows order by seq")
        ]
    observation = wh["chain_id"] + ":1"
    with closing(RailsLedger(ledger)) as db:
        db.import_origin_page(wh["chain_id"], pages)
        verify = dict(
            kind="verification.result",
            source=dict(adapter="verifier", instance="coordinator", record_id="red", revision="1"),
            scope=dict(job_id=JOB, task_id=TASK, contract_sha256=digest, code_rev=REV),
            authority="verifier_record",
            payload=dict(
                ac_id="AC-001",
                phase="red",
                verdict="EXPECTED_RED",
                reason="planned",
                evidence_refs=[observation],
                expected_intent_ref="intent:1",
                run_id="run:1",
                code_rev=REV,
                contract_sha256=digest,
            ),
            dedupe_key="red",
        )
        red = db.append(**verify)["event_id"]
        ch = db.head()
    anchors = tmp_path / "independent-heads.json"
    cfg = dict(
        schema="bundlebees.rails-return-input/1",
        job_id=JOB,
        task_id=TASK,
        code_rev=REV,
        contract_sha256=digest,
        coordinator=ch,
        workers=[wh],
        evidence={"AC-001": [observation, red]},
    )
    anchors.write_text(json.dumps(cfg))
    anchors.chmod(0o600)
    ctx = FakeCtx({"rails_db": str(ledger)})
    load_plugin().register(ctx)
    cmd = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    cmd["setup_fn"](parser)

    def invoke(*args, with_anchors=True):
        argv = ["job", "report", "--job-id", JOB, "--state-dir", str(state), *args]
        if with_anchors:
            argv.extend(["--rails-anchors", str(anchors)])
        rc = cmd["handler_fn"](parser.parse_args(argv))
        return rc, json.loads(capsys.readouterr().out)

    return invoke, cfg, anchors, state, ledger, verify


def test_registered_report_attachment_and_verification(packet):
    invoke, cfg, _, state, _, _ = packet
    path = return_report.return_path(state, JOB)
    md, side = path.with_name("RETURN.md"), path.with_name("rails-evidence.json")
    assert invoke(with_anchors=False)[0] == 0
    original = json.loads(path.read_bytes())
    assert not side.exists()
    rc, result = invoke()
    assert rc == 0, result
    data = side.read_bytes()
    doc = json.loads(data)
    schema = (
        Path(os.environ.get("WORKFLOW_SRC") or ROOT.parent / "workflow")
        / "schemas/rails/evidence.schema.json"
    )
    if schema.exists():
        import jsonschema

        jsonschema.Draft202012Validator(json.loads(schema.read_text())).validate(doc)
    assert doc["receiver_decision"] == "pending"
    assert doc["origin_heads"] == [cfg["coordinator"], *cfg["workers"]]
    assert doc["imported_checkpoints"] == [dict(cfg["workers"][0], source="rails_import")]
    assert {g["code"] for s in doc["source_coverage"] for g in s["gaps"]} == {"source_gap"}
    assert doc["verification_links"] == []
    assert set(doc['selected_event_ids']) == set(cfg['evidence']['AC-001'])
    ref = f"jobs/{JOB}/rails-evidence.json#sha256={hashlib.sha256(data).hexdigest()}"
    assert ref in md.read_text()
    new = json.loads(path.read_bytes())
    assert new["acceptance"][0]["evidence"].pop() == ref
    assert new["acceptance"][0]["status"] == "NOT RUN"
    assert new["red_green"] == {"test_first": False, "red": None, "green": None}
    assert any('signed RED' in note for note in new['unrun'])
    assert new["pending_approvals"] and result["measurements"]["tokens"] == "UNKNOWN"
    del new["evidence_digest"], original["evidence_digest"]
    assert new == original
    mtimes = {p: p.stat().st_mtime_ns for p in (md, path, side)}
    assert invoke("--verify")[0] == 0
    assert mtimes == {p: p.stat().st_mtime_ns for p in mtimes}
    assert invoke("--verify", with_anchors=False)[0] == 1
    side.write_bytes(data + b" ")
    assert invoke("--verify")[1]["error"] == "report_tampered"
    side.write_bytes(data)
    tampered = json.loads(path.read_bytes())
    tampered["acceptance"][0]["evidence"] = []
    tampered["evidence_digest"] = return_report.evidence_digest(tampered)
    path.write_bytes(return_report.document_bytes(tampered))
    assert invoke("--verify")[1]["error"] == "report_tampered"


def test_native_worker_export_becomes_a_resolvable_observation(packet, tmp_path, monkeypatch):
    invoke, cfg, anchors, state, coordinator, _ = packet
    hyvv = Path(os.environ.get("RAILS_R18_ROOT", ROOT.parent)) / "hyvv"
    if not hyvv.is_dir():
        if os.environ.get("RAILS_R18_REQUIRED") == "1":
            pytest.fail("missing required Hyvv checkout")
        pytest.skip("requires sibling Hyvv checkout")
    monkeypatch.syspath_prepend(str(hyvv))
    from rails_emitter import RailsEmitter
    from rails_export import export_page
    from rizzstack_rails import contracts as wire

    root = tmp_path / "native-worker"
    (root / "attempts").mkdir(parents=True)
    rows = [
        dict(lseq=1, kind="worker:tool_call", tool_id="call_1234567890abcdef", line="[tool read] read"),
        dict(lseq=2, kind="worker:tool_update", tool_id="call_1234567890abcdef", tool_status="completed"),
    ]
    (root / "attempts" / f"{ATTEMPT}.events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    worker = RailsEmitter(root)
    worker.ingest(ATTEMPT)
    page = export_page(worker.path)
    (selected,) = [e["event_id"] for e in page["events"] if e["kind"] == "tool.finish"]
    with closing(RailsLedger(worker.path, read_only=True)) as worker_reader:
        cfg["workers"].append(worker_reader.head())
    with closing(RailsLedger(coordinator)) as db:
        db.import_origin_page(page["chain_id"], [wire.canonicalize(e).decode() for e in page["events"]])
        cfg["coordinator"] = db.head()
    cfg["evidence"]["AC-001"].append(selected)
    anchors.write_text(json.dumps(cfg))
    assert invoke()[0] == invoke("--verify")[0] == 0
    doc = json.loads((return_report.return_dir(state, JOB) / "rails-evidence.json").read_bytes())
    assert selected in doc["selected_event_ids"]
    assert len(doc["imported_checkpoints"]) == 2


def test_native_wave_blocker_precedes_attachment(packet):
    invoke, _, _, state, _, _ = packet
    job = jobs.load_job(state, JOB)
    wave = dict(
        wave_id=f"{JOB}.w1",
        accepted=True,
        head_sha=REV,
        attempts=[ATTEMPT],
        suite={"exit_code": 0, "blocking": ["contract"]},
    )
    (state / "waves").mkdir(exist_ok=True)
    (state / "waves" / f"{JOB}.w1.json").write_text(json.dumps(wave))
    job["integration"]["accepted_waves"] = [dict(wave=1, commit=REV, attempts=[ATTEMPT])]
    jobs.save_job(state, job)
    rc, result = invoke()
    assert rc == 1 and result["error"] == "wave_mismatch"
    assert not (return_report.return_dir(state, JOB) / "rails-evidence.json").exists()


def test_native_signed_records_resolve_and_later_verifier_failure_refuses_stale_pass(packet):
    from bundlebees_plugin import rails_bundlebees_source as source, tdd

    invoke, cfg, anchors, state, coordinator, _ = packet
    task = jobs.load_job(state, JOB)["tasks"][0]
    key = leases.evidence_key(state)
    for phase, outcome, code in [("red", "failed", 1), ("green", "passed", 0)]:
        record = tdd.make_record(
            task["contract"],
            cfg["contract_sha256"],
            phase=phase,
            revision=REV,
            test_fingerprints={"tests/test_a.py": "b" * 64},
            command=["pytest", "tests/test_a.py"],
            result={
                "exit_code": code,
                "tests": 1,
                "errors": 0,
                "failures": code,
                "skipped": 0,
                "timed_out": False,
                "cases": [{"id": "tests/test_a.py::test_tool", "outcome": outcome}],
            },
            produced_by={"role": "verifier", "name": "fixture"},
            test_first=True,
        )
        tdd.write_record(state, record, key)
    drafts = source.collect_events(state, key=key)
    assert [d["payload"]["verdict"] for d in drafts] == ["EXPECTED_RED", "PASS"]
    with closing(RailsLedger(coordinator)) as db:
        eids = [db.append(**d)["event_id"] for d in drafts]
        cfg["coordinator"] = db.head()
    cfg["evidence"]["AC-001"][1:] = eids
    anchors.write_text(json.dumps(cfg))
    rc, result = invoke()
    assert rc == 0, result
    assert invoke("--verify")[0] == 0
    path = return_report.return_path(state, JOB)
    native = json.loads(path.read_bytes())
    assert native["red_green"]["red"] and native["red_green"]["green"]
    assert native["acceptance"][0]["status"] == "PASS" and native["receiver_decision"]["status"] == "pending"
    side = json.loads(path.with_name("rails-evidence.json").read_bytes())
    assert [link["verdict"] for link in side["verification_links"]] == ["EXPECTED_RED", "PASS"]
    # Do not let the sender select yesterday's independent PASS after a failure
    # at this exact task, revision and contract. Native records still exist.
    failure = copy.deepcopy(drafts[-1])
    failure.update(dedupe_key="later-failure")
    failure["payload"].update(verdict="FAIL", reason="tests_failed")
    with closing(RailsLedger(coordinator)) as db:
        db.append(**failure)
        cfg["coordinator"] = db.head()
    anchors.write_text(json.dumps(cfg))
    assert invoke()[1]["error"] == "rails_evidence_refused"


@pytest.mark.parametrize(
    "change",
    [
        "worker_head",
        "local_head",
        "criterion",
        "missing",
        "foreign_job",
        "revision",
        "unresolved_ref",
        "wrong_ac",
        "false_pass",
        "worker_verdict",
        "receipt",
        "tamper",
        "bound",
    ],
)
def test_refuses_wrong_anchors_bindings_and_records_before_writing(packet, change):
    invoke, cfg, anchors, state, db_path, verification = packet
    refs = cfg["evidence"]["AC-001"]
    if change == "worker_head":
        cfg["workers"][0]["hash"] = "0" * 64
    elif change == "local_head":
        cfg["coordinator"]["hash"] = "0" * 64
    elif change == "criterion":
        cfg["evidence"] = {"AC-002": refs}
    elif change == "missing":
        refs[1] = "absent:1"
    elif change == "foreign_job":
        cfg["job_id"] = "job_elsewhere"
    elif change == "revision":
        cfg["code_rev"] = "b" * 40
    elif change == "bound":
        cfg["workers"][0]["seq"] = 100000000
    elif change in ("receipt", "tamper"):
        with closing(sqlite3.connect(db_path)) as db:
            if change == "receipt":
                db.execute("DROP TRIGGER import_receipts_no_update")
                db.execute("UPDATE import_receipts SET origin_count=0")
            else:
                db.execute("DROP TRIGGER imported_origin_no_update")
                db.execute(
                    "UPDATE imported_origin_rows SET origin_bytes=replace(origin_bytes,'case','other') || ' '"
                )
            db.commit()
    else:
        v = copy.deepcopy(verification)
        v["dedupe_key"] = "changed"
        if change == "unresolved_ref":
            v["payload"]["evidence_refs"] = ["other:404"]
        elif change == "wrong_ac":
            v["payload"]["ac_id"] = "AC-002"
        elif change == "false_pass":
            v["payload"]["verdict"] = "PASS"
        else:
            v["authority"] = "worker_report"
        with closing(RailsLedger(db_path)) as db:
            refs[1] = db.append(**v)["event_id"]
            cfg["coordinator"] = db.head()
    anchors.write_text(json.dumps(cfg))
    rc, result = invoke()
    assert rc == 1, result
    assert result["error"] == "rails_evidence_refused"
    path = return_report.return_path(state, JOB)
    assert not path.exists() and not path.with_name("rails-evidence.json").exists()
