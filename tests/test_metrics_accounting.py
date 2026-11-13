"""Approved acceptance tests for evidence and metrics accounting (contract BM1-accounting, RED first).

- One integration failure is never counted as several regressions: failing test ids are de-duplicated
  across waves and retries.
- Environmental failures (network, name resolution, sandbox) and failures that also happen on the base
  revision are counted separately from real regressions.
- Missing token or cost data is reported as unavailable (None plus a count), never as a genuine 0.
  Per-attempt usage is cumulative, so re-verifying an attempt never double-counts it. (Parsing
  launcher camelCase/string usage is owned by bundlebees#6, not this contract.)
- First-pass rates name their denominator and the attempts excluded from it, with the reason.
- Historical (pre-BM1) integrate events are preserved and reported as legacy and unclassified.
- An integration record must fingerprint every approved contract test file.
- RED refuses contract test references that pytest did not collect.
"""

from __future__ import annotations

import json

from bundlebees_plugin import cli, metrics
from tests.test_cli_acdd import (ATTEMPT_ID, BRANCH, CID, GOOD_PATCH, FakeClient, _ctx, _frozen, _git, _invoke,
                                 _lease, _records, _repo, _result, _write)
from tests.test_self_integration import _integrate, _verified


def ev(seq, event, **f):
    return {"seq": seq, "event": event, "run_id": "r", "at": f"2026-11-12T20:{seq:02d}:00Z", **f}


def test_one_failure_seen_in_two_waves_is_one_regression():
    events = [
        ev(1, "integrate", wave_id="w1", ok=False, attempts=["a1"], regressions=2,
           failed_tests=["tests/t.py::test_a", "tests/t.py::test_b"], environmental=0, pre_existing=0),
        ev(2, "integrate", wave_id="w2", ok=False, attempts=["a2"], regressions=2,
           failed_tests=["tests/t.py::test_a", "tests/t.py::test_b"], environmental=0, pre_existing=0),
    ]
    s = metrics.summarize(events)
    assert s["regressions_found"] == 2
    assert s["regression_tests"] == ["tests/t.py::test_a", "tests/t.py::test_b"]


def test_environmental_and_pre_existing_failures_are_not_regressions():
    events = [ev(1, "integrate", wave_id="w1", ok=False, attempts=["a1"], regressions=1,
                 failed_tests=["tests/t.py::test_real"],
                 environmental_tests=["tests/t.py::test_net"], pre_existing_tests=["tests/t.py::test_old"])]
    s = metrics.summarize(events)
    assert s["regressions_found"] == 1
    assert s["environmental_failures"] == 1
    assert s["pre_existing_failures"] == 1


def test_legacy_integrate_events_are_preserved_as_unclassified():
    events = [ev(1, "integrate", wave_id="l3", ok=False, attempts=["a1"], regressions=129),
              ev(2, "integrate", wave_id="l4", ok=False, attempts=["a2"], regressions=129)]
    s = metrics.summarize(events)
    assert s["regressions_found"] == 0
    assert s["legacy_unclassified_failures"] == {"events": 2, "reported_total": 258,
                                                 "note": "pre-BM1 counts; may double-count; not regressions"}


def test_unavailable_usage_is_not_zero():
    events = [ev(1, "dispatch", attempt_id="a1"), ev(2, "verify", attempt_id="a1", ok=True, code="verified")]
    s = metrics.summarize(events)
    assert s["tokens_total"] is None and s["cost_usd_total"] is None
    assert s["usage"] == {"attempts_with_tokens": 0, "attempts_without_tokens": 1, "source": "unavailable"}


def test_genuine_zero_usage_is_zero_and_usage_is_cumulative_per_attempt():
    events = [
        ev(1, "verify", attempt_id="a1", ok=False, code="tests_failed", tokens=100),
        ev(2, "verify", attempt_id="a1", ok=True, code="verified", tokens=150),  # cumulative for a1
        ev(3, "verify", attempt_id="a2", ok=True, code="verified", tokens=0),
    ]
    s = metrics.summarize(events)
    assert s["tokens_total"] == 150
    assert s["usage"]["attempts_with_tokens"] == 2 and s["usage"]["attempts_without_tokens"] == 0


def test_first_pass_denominator_is_explicit_and_excludes_non_verdicts():
    events = [
        ev(1, "verify", attempt_id="a1", ok=False, code="no_result_yet"),
        ev(2, "verify", attempt_id="a1", ok=True, code="verified"),
        ev(3, "verify", attempt_id="a2", ok=False, code="tests_failed"),
        ev(4, "verify", attempt_id="a3", ok=False, code="launcher_unavailable"),
    ]
    s = metrics.summarize(events)
    fp = s["first_pass"]
    assert fp["passed"] == 1 and fp["denominator"] == 2 and s["first_pass_rate"] == 0.5
    assert fp["excluded"] == {"a3": "launcher_unavailable"}
    assert fp["rule"].startswith("first verify with a verdict")


def test_integrate_separates_environmental_failures_and_lists_failed_tests(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    (repo / "tests" / "test_env.py").write_text(
        "import socket\n\ndef test_needs_network():\n    socket.getaddrinfo('example.invalid', 443)\n\n"
        "def test_real_break():\n    assert 1 == 2\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "combined")
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    event = [e for e in metrics.read_events(state, "default") if e["event"] == "integrate"][-1]
    assert event["failed_tests"] == ["tests/test_env.py::test_real_break"]
    assert event["environmental_tests"] == ["tests/test_env.py::test_needs_network"]
    assert event["regressions"] == 1
    assert "environmental: 1" in out and "regressions: 1" in out


def test_integrate_marks_failures_that_also_fail_on_the_base_as_pre_existing(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    _git(repo, "checkout", "-q", "main")
    (repo / "tests" / "test_old.py").write_text("def test_old_break():\n    assert False\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "already broken on base")
    _git(repo, "push", "-q", "origin", "main")  # the integration target (origin's default branch) is red too
    _git(repo, "checkout", "-q", BRANCH); _git(repo, "merge", "-q", "--no-edit", "main")
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0  # still fail closed: the combined branch is red
    event = [e for e in metrics.read_events(state, "default") if e["event"] == "integrate"][-1]
    assert event["pre_existing_tests"] == ["tests/test_old.py::test_old_break"]
    assert event["regressions"] == 0 and event["failed_tests"] == []
    assert "pre_existing: 1" in out


def test_integration_record_fails_when_an_approved_test_file_is_missing(tmp_path, capsys):
    repo, state, _ = _verified(tmp_path, capsys)
    _git(repo, "rm", "-q", "checks/test_contract_extra.py"); _git(repo, "commit", "-qm", "drop approved test")
    rc = _integrate(repo, state)
    out = capsys.readouterr().out
    assert rc != 0
    record = _records(state)[-1]
    assert record["phase"] == "integration" and record["classification"] != "passed"
    assert "checks/test_contract_extra.py" in json.dumps(record.get("missing_test_files"))


def test_red_refuses_test_references_pytest_did_not_collect(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base, acceptance_criteria=[
        {"id": "AC-001", "given": "g", "when": "w", "then": "t", "verification": "test",
         "tests": ["tests/test_value.py::test_does_not_exist"]}])
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    rc = _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)])
    out = capsys.readouterr().out
    assert rc != 0 and "test_ref_unknown" in out and "test_does_not_exist" in out
