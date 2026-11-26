"""Delivery reconciler: durable intents + idempotent PR convergence (rizzstack.delivery/1).

RED-first record: written against the requirement that finished work must not rest
in a coordinator's head. The incident shape (2026-11-17): three verified branches
were pushed and no PR existed; nothing in the system recorded that a PR was owed,
so nothing could notice the gap.

Patterns applied (researched):
  * transactional outbox (microservices.io): the intent is durable before the side
    effect (the push), so a crash cannot lose the obligation;
  * idempotent effects (temporal.io): PR creation keyed by (repo, branch); an
    existing PR is the success condition;
  * level-triggered reconciliation (Kubernetes controllers): the sweep compares
    desired (pushed => PR exists) with actual (gh pr list) and converges.

Run: python -m pytest tests/test_delivery_reconciler.py -q
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import cli, delivery  # noqa: E402
from tests.fakes import FakeCtx  # noqa: E402


class FakeGh:
    """Scripted gh: records calls, models 'pr list --head' and 'pr create'."""

    def __init__(self, prs: dict[str, str] | None = None, fail_create: str | None = None):
        self.prs = prs or {}          # branch -> url
        self.fail_create = fail_create
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            branch = args[args.index("--head") + 1]
            url = self.prs.get(branch)
            return 0, (url + "\n") if url else "\n"
        if args[:2] == ["pr", "create"]:
            if self.fail_create:
                return 1, self.fail_create
            head = args[args.index("--head") + 1]
            url = f"https://github.com/{args[args.index('--repo') + 1]}/pull/{900 + len(self.prs)}"
            self.prs[head] = url
            return 0, url + "\n"
        return 1, f"unexpected gh call: {args}"


def _intent(tmp, attempt="att_del000000001", repo="mojomast/bundlebees", branch="rails/r99-x"):
    return delivery.record_intent(tmp, attempt_id=attempt, repo=repo, branch=branch,
                                  base_sha="0" * 40, title="t", body="b")


def _write_record(state_dir, filename, **fields):
    deliveries = Path(state_dir) / "deliveries"
    deliveries.mkdir(parents=True, exist_ok=True)
    record = {"schema": delivery.SCHEMA, "created_at": 1.0}
    record.update(fields)
    (deliveries / filename).write_text(json.dumps(record), encoding="utf-8")


class DeliveryLifecycle(unittest.TestCase):
    def test_intent_without_push_is_blocked_not_pr_opened(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["blocked"][0]["branch"], "rails/r99-x")
            self.assertEqual(result["opened"], [])
            self.assertFalse([c for c in gh.calls if c[:2] == ["pr", "create"]])

    def test_pushed_delivery_opens_exactly_one_pr_and_records_it(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(len(result["opened"]), 1)
            url = result["opened"][0]["pr_url"]
            self.assertEqual(gh.prs["rails/r99-x"], url)
            # record is now terminal and no longer pending
            self.assertEqual(delivery.pending(d), [])
            # a second sweep is a no-op: idempotent
            result2 = delivery.sweep(d, gh)
            self.assertEqual(result2["opened"], [])
            self.assertEqual(len([c for c in gh.calls if c[:2] == ["pr", "create"]]), 1)

    def test_existing_pr_is_recognized_without_a_create_call(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            gh = FakeGh(prs={"rails/r99-x": "https://github.com/mojomast/bundlebees/pull/77"})
            result = delivery.sweep(d, gh)
            self.assertEqual(result["existing"][0]["pr_url"], gh.prs["rails/r99-x"])
            self.assertFalse([c for c in gh.calls if c[:2] == ["pr", "create"]])

    def test_already_exists_error_converges_via_the_list(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)

            class RacyGh(FakeGh):
                def __call__(self, args):
                    if args[:2] == ["pr", "create"]:
                        # someone else created it between our list and create
                        self.prs["rails/r99-x"] = "https://github.com/mojomast/bundlebees/pull/78"
                        return 1, "a pull request already exists for this head"
                    return super().__call__(args)
            result = delivery.sweep(d, RacyGh())
            self.assertEqual(result["existing"][0]["pr_url"], "https://github.com/mojomast/bundlebees/pull/78")

    def test_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            gh = FakeGh()
            result = delivery.sweep(d, gh, dry_run=True)
            self.assertFalse([c for c in gh.calls if c[:2] == ["pr", "create"]])
            self.assertTrue(delivery.pending(d), "dry run must not mark the record done")

    def test_create_failure_is_a_typed_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            result = delivery.sweep(d, FakeGh(fail_create="boom: network down"))
            self.assertEqual(result["errors"][0]["error"]["error"], "gh_create_failed")
            self.assertTrue(delivery.pending(d), "failed record stays pending for the next sweep")


class DeliveryDamagedRecords(unittest.TestCase):
    def test_truncated_record_is_damaged_and_valid_delivery_still_opens(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            path = Path(d) / "deliveries" / "att_damaged.json"
            path.write_text('{"schema": "rizzstack.delivery/1", "attempt_id": "att_damaged"',
                            encoding="utf-8")
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["damaged"], [{"attempt_id": "att_damaged",
                                                  "file": "att_damaged.json",
                                                  "reason": "unreadable_record"}])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(gh.prs["rails/r99-x"], result["opened"][0]["pr_url"])
            self.assertEqual(delivery.pending(d), [])
            record = json.loads((Path(d) / "deliveries" / "att_del000000001.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], delivery.STATE_PR_OPEN)

    def test_unknown_state_and_identity_mismatch_are_damaged_and_valid_delivery_converges(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            deliveries = Path(d) / "deliveries"
            (deliveries / "att_weird.json").write_text(json.dumps({
                "schema": delivery.SCHEMA,
                "attempt_id": "att_weird",
                "repo": "mojomast/bundlebees",
                "branch": "rails/weird",
                "state": "pushedish",
                "pushed_sha": "b" * 40,
                "created_at": 1.0,
            }), encoding="utf-8")
            (deliveries / "att_alpha.json").write_text(json.dumps({
                "schema": delivery.SCHEMA,
                "attempt_id": "att_beta",
                "repo": "mojomast/bundlebees",
                "branch": "rails/beta",
                "state": delivery.STATE_PUSHED,
                "pushed_sha": "c" * 40,
                "created_at": 2.0,
            }), encoding="utf-8")
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            reasons = {row["attempt_id"]: row["reason"] for row in result["damaged"]}
            self.assertEqual(reasons.get("att_weird"), "unknown_state")
            self.assertEqual(reasons.get("att_alpha"), "identity_mismatch")
            self.assertFalse((deliveries / "att_beta.json").exists())
            self.assertEqual(delivery.pending(d), [])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(
                json.loads((deliveries / "att_del000000001.json").read_text(encoding="utf-8"))["state"],
                delivery.STATE_PR_OPEN,
            )
            self.assertEqual(set(gh.prs), {"rails/r99-x"})
            self.assertEqual(len([c for c in gh.calls
                                  if c[:2] == ["pr", "create"] and c[c.index("--head") + 1] == "rails/r99-x"]), 1)

    def test_incomplete_pushed_record_does_not_abort_the_sweep(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            deliveries = Path(d) / "deliveries"
            (deliveries / "att_nobranch.json").write_text(json.dumps({
                "schema": delivery.SCHEMA,
                "attempt_id": "att_nobranch",
                "branch": "rails/nobranch",
                "state": delivery.STATE_PUSHED,
                "pushed_sha": "d" * 40,
                "created_at": 0.5,
            }), encoding="utf-8")
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["damaged"], [{"attempt_id": "att_nobranch",
                                                  "file": "att_nobranch.json",
                                                  "reason": "incomplete_record"}])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(
                json.loads((deliveries / "att_del000000001.json").read_text(encoding="utf-8"))["state"],
                delivery.STATE_PR_OPEN,
            )

    def test_non_utf8_record_is_damaged_and_valid_delivery_still_opens(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            deliveries = Path(d) / "deliveries"
            (deliveries / "att_bin.json").write_bytes(b'{"schema": "\xff\xfe"}')
            self.assertEqual([row["attempt_id"] for row in delivery.pending(d)], ["att_del000000001"])
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["damaged"], [{"attempt_id": "att_bin",
                                                  "file": "att_bin.json",
                                                  "reason": "unreadable_record"}])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(gh.prs["rails/r99-x"], result["opened"][0]["pr_url"])
            self.assertEqual(delivery.pending(d), [])
            record = json.loads((deliveries / "att_del000000001.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], delivery.STATE_PR_OPEN)

    def test_not_an_object_is_damaged_and_valid_delivery_converges(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            deliveries = Path(d) / "deliveries"
            (deliveries / "att_array.json").write_text("[]", encoding="utf-8")
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["damaged"], [{"attempt_id": "att_array",
                                                  "file": "att_array.json",
                                                  "reason": "not_an_object"}])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(gh.prs["rails/r99-x"], result["opened"][0]["pr_url"])
            self.assertEqual(delivery.pending(d), [])
            record = json.loads((deliveries / "att_del000000001.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], delivery.STATE_PR_OPEN)

    def test_schema_mismatch_is_damaged_and_valid_delivery_converges(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            deliveries = Path(d) / "deliveries"
            (deliveries / "att_schema.json").write_text(json.dumps({
                "schema": "rizzstack.delivery/2",
                "attempt_id": "att_schema",
                "repo": "mojomast/bundlebees",
                "branch": "rails/schema",
                "state": delivery.STATE_PUSHED,
                "pushed_sha": "e" * 40,
                "created_at": 0.5,
            }), encoding="utf-8")
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["damaged"], [{"attempt_id": "att_schema",
                                                  "file": "att_schema.json",
                                                  "reason": "schema_mismatch"}])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(gh.prs["rails/r99-x"], result["opened"][0]["pr_url"])
            self.assertEqual(set(gh.prs), {"rails/r99-x"})
            self.assertEqual(delivery.pending(d), [])
            record = json.loads((deliveries / "att_del000000001.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], delivery.STATE_PR_OPEN)

    def test_missing_attempt_id_is_damaged_and_valid_delivery_converges(self):
        with tempfile.TemporaryDirectory() as d:
            _intent(d)
            delivery.record_pushed(d, "att_del000000001", "a" * 40)
            deliveries = Path(d) / "deliveries"
            (deliveries / "att_absent.json").write_text(json.dumps({
                "schema": delivery.SCHEMA,
                "repo": "mojomast/bundlebees",
                "branch": "rails/absent",
                "state": delivery.STATE_PUSHED,
                "pushed_sha": "f" * 40,
                "created_at": 0.5,
            }), encoding="utf-8")
            (deliveries / "att_nonstring.json").write_text(json.dumps({
                "schema": delivery.SCHEMA,
                "attempt_id": 123,
                "repo": "mojomast/bundlebees",
                "branch": "rails/nonstring",
                "state": delivery.STATE_PUSHED,
                "pushed_sha": "1" * 40,
                "created_at": 0.6,
            }), encoding="utf-8")
            gh = FakeGh()
            result = delivery.sweep(d, gh)
            self.assertEqual(result["damaged"], [
                {"attempt_id": "att_absent",
                 "file": "att_absent.json",
                 "reason": "missing_attempt_id"},
                {"attempt_id": "att_nonstring",
                 "file": "att_nonstring.json",
                 "reason": "missing_attempt_id"},
            ])
            self.assertEqual(len(result["opened"]), 1)
            self.assertEqual(result["opened"][0]["attempt_id"], "att_del000000001")
            self.assertEqual(gh.prs["rails/r99-x"], result["opened"][0]["pr_url"])
            self.assertEqual(set(gh.prs), {"rails/r99-x"})
            self.assertEqual(delivery.pending(d), [])
            record = json.loads((deliveries / "att_del000000001.json").read_text(encoding="utf-8"))
            self.assertEqual(record["state"], delivery.STATE_PR_OPEN)


def test_cli_deliver_defaults_to_plugin_state_dir_on_a_dry_run(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    delivery.record_intent(state, attempt_id="att_del000000001", repo="mojomast/bundlebees",
                           branch="rails/r99-x", base_sha="0" * 40, title="t", body="b")
    delivery.record_pushed(state, "att_del000000001", "a" * 40)
    calls = {}

    def fake_sweep(path, dry_run=False):
        calls["path"] = Path(path)
        calls["dry_run"] = dry_run
        return {"opened": [], "existing": [], "blocked": [], "errors": [], "damaged": []}

    monkeypatch.setattr(cli, "plugin_state_dir", lambda: state)
    monkeypatch.setattr(delivery, "sweep", fake_sweep)

    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=None, dry_run=True))

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == {"path": state, "dry_run": True}
    assert "deliver: opened=0" in out


def test_cli_only_damaged_record_is_not_an_all_clear(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    deliveries = state / "deliveries"
    deliveries.mkdir(parents=True)
    (deliveries / "att_damaged.json").write_text("{not valid json", encoding="utf-8")

    def forbidden_sweep(*args, **kwargs):
        raise AssertionError("sweep must not be called when only damaged records exist")

    monkeypatch.setattr(delivery, "sweep", forbidden_sweep)
    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(state), dry_run=False))
    out = capsys.readouterr().out

    assert rc == 2
    assert "damaged: att_damaged.json - unreadable_record" in out
    assert "nothing pending" not in out


def test_cli_non_utf8_record_is_not_an_all_clear(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    deliveries = state / "deliveries"
    deliveries.mkdir(parents=True)
    (deliveries / "att_bin.json").write_bytes(b'{"schema": "\xff\xfe"}')

    def forbidden_sweep(*args, **kwargs):
        raise AssertionError("sweep must not be called when only damaged records exist")

    monkeypatch.setattr(delivery, "sweep", forbidden_sweep)
    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(state), dry_run=False))
    out = capsys.readouterr().out

    assert rc == 2
    assert "damaged: att_bin.json - unreadable_record" in out
    assert "nothing pending" not in out


def test_cli_reports_damaged_alongside_opened(tmp_path, capsys, monkeypatch):
    state = tmp_path / "state"
    delivery.record_intent(state, attempt_id="att_del000000001", repo="mojomast/bundlebees",
                           branch="rails/r99-x", base_sha="0" * 40, title="t", body="b")
    delivery.record_pushed(state, "att_del000000001", "a" * 40)
    deliveries = state / "deliveries"
    (deliveries / "att_damaged.json").write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(delivery, "sweep", lambda *args, **kwargs: {
        "opened": [{"branch": "fix/x", "pr_url": "u", "attempt_id": "att_x"}],
        "existing": [],
        "blocked": [],
        "errors": [],
        "damaged": [{"attempt_id": "att_damaged", "file": "att_damaged.json",
                     "reason": "unreadable_record"}],
    })
    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(state), dry_run=False))
    out = capsys.readouterr().out

    assert rc == 2
    assert "opened: fix/x -> u" in out
    assert "damaged: att_damaged.json - unreadable_record" in out
    assert "damaged=1" in out


def test_cli_pr_open_only_keeps_the_all_clear(tmp_path, capsys):
    state = tmp_path / "state"
    delivery.record_intent(state, attempt_id="att_done", repo="mojomast/bundlebees",
                           branch="rails/done", base_sha="0" * 40, title="t", body="b")
    delivery.record_pushed(state, "att_done", "a" * 40)
    delivery.record_pr(state, "att_done", "https://github.com/mojomast/bundlebees/pull/1")

    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(state), dry_run=False))
    out = capsys.readouterr().out

    assert rc == 0
    assert "deliver: nothing pending (every pushed delivery already has a PR)" in out


def test_non_numeric_created_at_is_total_and_sweeps_all(tmp_path):
    state = tmp_path / "state"
    _intent(state, attempt="att_strtime", branch="rails/strtime")
    delivery.record_pushed(state, "att_strtime", "a" * 40)
    _intent(state, attempt="att_numtime", branch="rails/numtime")
    delivery.record_pushed(state, "att_numtime", "b" * 40)

    string_path = state / "deliveries" / "att_strtime.json"
    record = json.loads(string_path.read_text(encoding="utf-8"))
    record["created_at"] = "2026-11-20T00:00:00Z"
    string_path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")

    rows = delivery.pending(state)
    assert [row["attempt_id"] for row in rows] == ["att_strtime", "att_numtime"]
    assert rows[0]["created_at"] == "2026-11-20T00:00:00Z"

    gh = FakeGh()
    result = delivery.sweep(state, gh)
    assert {row["attempt_id"] for row in result["opened"]} == {"att_strtime", "att_numtime"}
    assert len([call for call in gh.calls if call[:2] == ["pr", "create"]]) == 2
    assert delivery.pending(state) == []


def test_pushed_with_pr_url_converges_as_existing(tmp_path, capsys):
    url = "https://github.com/mojomast/bundlebees/pull/321"

    def make_store(name):
        state = tmp_path / name
        _intent(state, attempt="att_haspr", branch="rails/haspr")
        delivery.record_pushed(state, "att_haspr", "c" * 40)
        path = state / "deliveries" / "att_haspr.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["pr_url"] = url
        path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
        return state, path

    state, path = make_store("sweep")
    gh = FakeGh()
    result = delivery.sweep(state, gh)
    assert result["existing"] == [{"attempt_id": "att_haspr", "branch": "rails/haspr", "pr_url": url}]
    assert result["opened"] == []
    assert gh.calls == []
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == delivery.STATE_PR_OPEN

    second = delivery.sweep(state, gh)
    assert second == {"opened": [], "existing": [], "blocked": [], "errors": [], "damaged": []}
    assert gh.calls == []

    cli_state, _ = make_store("cli")
    capsys.readouterr()
    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(cli_state), dry_run=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "existing: rails/haspr -> " in out
    assert "existing=1" in out

    dry_state, dry_path = make_store("dry")
    before = dry_path.read_text(encoding="utf-8")
    gh_dry = FakeGh()
    dry = delivery.sweep(dry_state, gh_dry, dry_run=True)
    assert dry["existing"] == [{"attempt_id": "att_haspr", "branch": "rails/haspr",
                                "pr_url": url, "dry_run": True}]
    assert dry_path.read_text(encoding="utf-8") == before
    assert json.loads(dry_path.read_text(encoding="utf-8"))["state"] == delivery.STATE_PUSHED
    assert gh_dry.calls == []


def test_non_round_trip_filenames_are_damaged_without_abort(tmp_path):
    state = tmp_path / "state"
    _intent(state, attempt="att_del000000001", branch="rails/r99-x")
    delivery.record_pushed(state, "att_del000000001", "a" * 40)
    deliveries = state / "deliveries"

    (deliveries / ".json").write_text(json.dumps({
        "schema": delivery.SCHEMA,
        "attempt_id": ".json",
        "repo": "mojomast/bundlebees",
        "branch": "rails/dotjson",
        "state": delivery.STATE_PUSHED,
        "pushed_sha": "d" * 40,
        "created_at": 0.5,
    }), encoding="utf-8")
    (deliveries / "att space.json").write_text(json.dumps({
        "schema": delivery.SCHEMA,
        "attempt_id": "att space",
        "repo": "mojomast/bundlebees",
        "branch": "rails/space",
        "state": delivery.STATE_PUSHED,
        "pushed_sha": "e" * 40,
        "created_at": 0.6,
    }), encoding="utf-8")

    gh = FakeGh()
    result = delivery.sweep(state, gh)

    scanned = delivery.scan(state)
    reasons = {row["file"]: row["reason"] for row in scanned["damaged"]}
    assert reasons == {".json": "identity_mismatch", "att space.json": "identity_mismatch"}
    assert len(result["opened"]) == 1
    assert result["opened"][0]["attempt_id"] == "att_del000000001"
    assert not (deliveries / ".json.json").exists()
    assert not (deliveries / "att_space.json").exists()
    assert delivery.pending(state) == []


def test_pr_open_completeness_is_state_aware(tmp_path, capsys):
    def write_pr_open(directory, attempt_id, pr_url):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{attempt_id}.json").write_text(json.dumps({
            "schema": delivery.SCHEMA,
            "attempt_id": attempt_id,
            "state": delivery.STATE_PR_OPEN,
            "pr_url": pr_url,
            "created_at": 1.0,
        }), encoding="utf-8")

    good_state = tmp_path / "good"
    write_pr_open(good_state / "deliveries", "att_prgood",
                  "https://github.com/mojomast/bundlebees/pull/9")

    scanned = delivery.scan(good_state)
    assert scanned["pending"] == []
    assert scanned["damaged"] == []
    rc = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(good_state), dry_run=False))
    good_out = capsys.readouterr().out
    assert rc == 0
    assert "nothing pending" in good_out

    bad_state = tmp_path / "bad"
    write_pr_open(bad_state / "deliveries", "att_prgood",
                  "https://github.com/mojomast/bundlebees/pull/9")
    write_pr_open(bad_state / "deliveries", "att_prnull", None)

    scanned_bad = delivery.scan(bad_state)
    assert scanned_bad["pending"] == []
    assert scanned_bad["damaged"] == [{"attempt_id": "att_prnull",
                                       "file": "att_prnull.json",
                                       "reason": "incomplete_record"}]
    rc_bad = cli._cmd_deliver(FakeCtx(), argparse.Namespace(state_dir=str(bad_state), dry_run=False))
    bad_out = capsys.readouterr().out
    assert rc_bad == 2
    assert "damaged: att_prnull.json - incomplete_record" in bad_out
    assert "att_prgood" not in bad_out


def test_pushed_record_without_pushed_sha_is_incomplete_and_not_swept(tmp_path):
    state = tmp_path / "state"
    _intent(state, attempt="att_valid", branch="rails/valid")
    delivery.record_pushed(state, "att_valid", "a" * 40)
    _write_record(state, "att_nosha.json", attempt_id="att_nosha",
                  repo="mojomast/bundlebees", branch="rails/nosha",
                  state=delivery.STATE_PUSHED, pushed_sha=None, created_at=0.5)

    scanned = delivery.scan(state)
    assert scanned["damaged"] == [{"attempt_id": "att_nosha", "file": "att_nosha.json",
                                   "reason": "incomplete_record"}]
    assert [row["attempt_id"] for row in scanned["pending"]] == ["att_valid"]

    gh = FakeGh()
    result = delivery.sweep(state, gh)
    assert [row["attempt_id"] for row in result["opened"]] == ["att_valid"]
    assert set(gh.prs) == {"rails/valid"}
    assert not [call for call in gh.calls if "rails/nosha" in call]
    valid_record = json.loads((state / "deliveries" / "att_valid.json").read_text(encoding="utf-8"))
    assert valid_record["state"] == delivery.STATE_PR_OPEN


@pytest.mark.parametrize("fields", [
    pytest.param({"repo": "mojomast/bundlebees", "branch": "rails/nosha",
                  "state": delivery.STATE_PUSHED, "pushed_sha": None},
                 id="pushed-sha-none"),
    pytest.param({"branch": "rails/norepo",
                  "state": delivery.STATE_PUSHED, "pushed_sha": "b" * 40},
                 id="pushed-repo-absent"),
    pytest.param({"repo": "mojomast/bundlebees",
                  "state": delivery.STATE_PUSHED, "pushed_sha": "b" * 40},
                 id="pushed-branch-absent"),
    pytest.param({"repo": "", "branch": "rails/emptyrepo",
                  "state": delivery.STATE_PUSHED, "pushed_sha": "b" * 40},
                 id="pushed-repo-empty"),
    pytest.param({"repo": "mojomast/bundlebees", "branch": "",
                  "state": delivery.STATE_PUSHED, "pushed_sha": "b" * 40},
                 id="pushed-branch-empty"),
    pytest.param({"repo": "mojomast/bundlebees", "branch": "rails/emptysha",
                  "state": delivery.STATE_PUSHED, "pushed_sha": ""},
                 id="pushed-sha-empty"),
])
def test_pushed_record_missing_repo_branch_or_sha_is_incomplete_and_not_swept(tmp_path, fields):
    state = tmp_path / "state"
    _intent(state, attempt="att_valid", branch="rails/valid")
    delivery.record_pushed(state, "att_valid", "a" * 40)
    _write_record(state, "att_bad.json", attempt_id="att_bad", created_at=0.5, **fields)

    scanned = delivery.scan(state)
    assert scanned["damaged"] == [{"attempt_id": "att_bad", "file": "att_bad.json",
                                   "reason": "incomplete_record"}]
    assert [row["attempt_id"] for row in scanned["pending"]] == ["att_valid"]

    gh = FakeGh()
    result = delivery.sweep(state, gh)
    assert [row["attempt_id"] for row in result["opened"]] == ["att_valid"]
    assert set(gh.prs) == {"rails/valid"}
    valid_record = json.loads((state / "deliveries" / "att_valid.json").read_text(encoding="utf-8"))
    assert valid_record["state"] == delivery.STATE_PR_OPEN


@pytest.mark.parametrize("fields", [
    {"branch": "rails/norepo"},
    {"repo": "", "branch": "rails/emptyrepo"},
    pytest.param({"repo": "mojomast/bundlebees"}, id="intent-branch-absent"),
    pytest.param({"repo": "mojomast/bundlebees", "branch": ""}, id="intent-branch-empty"),
])
def test_intent_missing_repo_or_branch_is_incomplete_and_not_swept(tmp_path, fields):
    state = tmp_path / "state"
    _intent(state, attempt="att_valid", branch="rails/valid")
    delivery.record_pushed(state, "att_valid", "a" * 40)
    _write_record(state, "att_bad.json", attempt_id="att_bad",
                  state=delivery.STATE_INTENT, created_at=0.5, **fields)

    scanned = delivery.scan(state)
    assert scanned["damaged"] == [{"attempt_id": "att_bad", "file": "att_bad.json",
                                   "reason": "incomplete_record"}]
    assert [row["attempt_id"] for row in scanned["pending"]] == ["att_valid"]

    gh = FakeGh()
    result = delivery.sweep(state, gh)
    assert result["blocked"] == []
    assert [row["attempt_id"] for row in result["opened"]] == ["att_valid"]
    assert set(gh.prs) == {"rails/valid"}
    valid_record = json.loads((state / "deliveries" / "att_valid.json").read_text(encoding="utf-8"))
    assert valid_record["state"] == delivery.STATE_PR_OPEN


def test_empty_attempt_id_is_damaged_and_never_reaches_record_pr(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _intent(state, attempt="att_valid", branch="rails/valid")
    delivery.record_pushed(state, "att_valid", "a" * 40)
    _write_record(state, ".json", attempt_id="", repo="mojomast/bundlebees",
                  branch="rails/empty", state=delivery.STATE_PUSHED,
                  pushed_sha="b" * 40, created_at=0.5)

    scanned = delivery.scan(state)
    assert scanned["damaged"] == [{"attempt_id": ".json", "file": ".json",
                                   "reason": "missing_attempt_id"}]
    assert [row["attempt_id"] for row in scanned["pending"]] == ["att_valid"]
    assert [row["attempt_id"] for row in delivery.pending(state)] == ["att_valid"]

    recorded = []
    original = delivery.record_pr

    def spy(state_dir, attempt_id, pr_url):
        recorded.append(attempt_id)
        return original(state_dir, attempt_id, pr_url)

    monkeypatch.setattr(delivery, "record_pr", spy)
    gh = FakeGh()
    result = delivery.sweep(state, gh)

    assert [row["attempt_id"] for row in result["opened"]] == ["att_valid"]
    assert recorded == ["att_valid"]
    assert "" not in recorded
    assert set(gh.prs) == {"rails/valid"}
    assert not [call for call in gh.calls if "rails/empty" in call]


def test_row_oserror_is_contained_and_remaining_rows_converge(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _intent(state, attempt="att_first", branch="rails/first")
    delivery.record_pushed(state, "att_first", "a" * 40)
    _intent(state, attempt="att_second", branch="rails/second")
    delivery.record_pushed(state, "att_second", "b" * 40)

    original = delivery.record_pr

    def flaky(state_dir, attempt_id, pr_url):
        if attempt_id == "att_first":
            raise OSError("simulated disk failure")
        return original(state_dir, attempt_id, pr_url)

    monkeypatch.setattr(delivery, "record_pr", flaky)

    gh = FakeGh()
    result = delivery.sweep(state, gh)

    assert result["errors"] == [{
        "attempt_id": "att_first",
        "branch": "rails/first",
        "error": {"error": "record_io_failed", "message": "simulated disk failure",
                  "fix": "Inspect the deliveries directory; the sweep can be rerun safely."},
    }]
    assert [row["attempt_id"] for row in result["opened"]] == ["att_second"]
    second_record = json.loads((state / "deliveries" / "att_second.json").read_text(encoding="utf-8"))
    assert second_record["state"] == delivery.STATE_PR_OPEN
    assert [row["attempt_id"] for row in delivery.pending(state)] == ["att_first"]

    monkeypatch.setattr(delivery, "record_pr", original)
    gh_second = FakeGh()
    second = delivery.sweep(state, gh_second)

    assert [row["attempt_id"] for row in second["opened"]] == ["att_first"]
    first_record = json.loads((state / "deliveries" / "att_first.json").read_text(encoding="utf-8"))
    assert first_record["state"] == delivery.STATE_PR_OPEN


def test_runner_timeout_expired_is_contained_and_remaining_rows_converge(tmp_path):
    state = tmp_path / "state"
    _intent(state, attempt="att_first", branch="rails/first")
    delivery.record_pushed(state, "att_first", "a" * 40)
    _intent(state, attempt="att_second", branch="rails/second")
    delivery.record_pushed(state, "att_second", "b" * 40)

    class TimeoutOnFirstGh(FakeGh):
        def __call__(self, args):
            if args[:2] == ["pr", "list"] and args[args.index("--head") + 1] == "rails/first":
                self.calls.append(list(args))
                raise subprocess.TimeoutExpired(["gh", *args], 120)
            return super().__call__(args)

    result = delivery.sweep(state, TimeoutOnFirstGh())

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_first"
    assert error["branch"] == "rails/first"
    assert error["error"]["error"] == "unexpected_error"
    assert error["error"]["message"].startswith("TimeoutExpired:")
    assert error["error"]["fix"] == "Inspect the row; the sweep can be rerun safely."
    assert [row["attempt_id"] for row in result["opened"]] == ["att_second"]
    second_record = json.loads((state / "deliveries" / "att_second.json").read_text(encoding="utf-8"))
    assert second_record["state"] == delivery.STATE_PR_OPEN
    assert [row["attempt_id"] for row in delivery.pending(state)] == ["att_first"]


def test_record_pr_json_decode_error_is_contained_and_remaining_rows_converge(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _intent(state, attempt="att_first", branch="rails/first")
    delivery.record_pushed(state, "att_first", "a" * 40)
    _intent(state, attempt="att_second", branch="rails/second")
    delivery.record_pushed(state, "att_second", "b" * 40)

    original = delivery.record_pr

    def corrupted(state_dir, attempt_id, pr_url):
        if attempt_id == "att_first":
            raise json.JSONDecodeError("simulated corrupt record", "", 0)
        return original(state_dir, attempt_id, pr_url)

    monkeypatch.setattr(delivery, "record_pr", corrupted)
    result = delivery.sweep(state, FakeGh())

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_first"
    assert error["branch"] == "rails/first"
    assert error["error"]["error"] == "unexpected_error"
    assert error["error"]["message"].startswith("JSONDecodeError:")
    assert [row["attempt_id"] for row in result["opened"]] == ["att_second"]
    second_record = json.loads((state / "deliveries" / "att_second.json").read_text(encoding="utf-8"))
    assert second_record["state"] == delivery.STATE_PR_OPEN


def test_runner_unicode_decode_error_is_contained_and_remaining_rows_converge(tmp_path):
    state = tmp_path / "state"
    _intent(state, attempt="att_first", branch="rails/first")
    delivery.record_pushed(state, "att_first", "a" * 40)
    _intent(state, attempt="att_second", branch="rails/second")
    delivery.record_pushed(state, "att_second", "b" * 40)

    class UndecodableGh(FakeGh):
        def __call__(self, args):
            if args[:2] == ["pr", "list"] and args[args.index("--head") + 1] == "rails/first":
                self.calls.append(list(args))
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return super().__call__(args)

    result = delivery.sweep(state, UndecodableGh())

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_first"
    assert error["branch"] == "rails/first"
    assert error["error"]["error"] == "unexpected_error"
    assert error["error"]["message"].startswith("UnicodeDecodeError:")
    assert [row["attempt_id"] for row in result["opened"]] == ["att_second"]
    second_record = json.loads((state / "deliveries" / "att_second.json").read_text(encoding="utf-8"))
    assert second_record["state"] == delivery.STATE_PR_OPEN


def test_base_exception_from_runner_is_not_swallowed(tmp_path):
    state = tmp_path / "state"
    _intent(state, attempt="att_first", branch="rails/first")
    delivery.record_pushed(state, "att_first", "a" * 40)

    class SweepAbort(BaseException):
        pass

    class AbortingGh(FakeGh):
        def __call__(self, args):
            raise SweepAbort("control: BaseException must escape the per-row handler")

    with pytest.raises(SweepAbort):
        delivery.sweep(state, AbortingGh())


def test_existing_oserror_and_deliveryerror_classifications_are_preserved(tmp_path, monkeypatch):
    os_state = tmp_path / "os_state"
    _intent(os_state, attempt="att_os", branch="rails/os")
    delivery.record_pushed(os_state, "att_os", "a" * 40)

    def disk_failure(state_dir, attempt_id, pr_url):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(delivery, "record_pr", disk_failure)
    os_result = delivery.sweep(os_state, FakeGh())

    assert os_result["errors"] == [{
        "attempt_id": "att_os",
        "branch": "rails/os",
        "error": {"error": "record_io_failed", "message": "simulated disk failure",
                  "fix": "Inspect the deliveries directory; the sweep can be rerun safely."},
    }]

    delivery_state = tmp_path / "delivery_state"
    _intent(delivery_state, attempt="att_delivery", branch="rails/delivery")
    delivery.record_pushed(delivery_state, "att_delivery", "b" * 40)

    class DeliveryErrorGh(FakeGh):
        def __call__(self, args):
            raise delivery.DeliveryError("gh_list_failed", "simulated gh failure", "Check gh.")

    delivery_result = delivery.sweep(delivery_state, DeliveryErrorGh())

    assert delivery_result["errors"] == [{
        "attempt_id": "att_delivery",
        "branch": "rails/delivery",
        "error": {"error": "gh_list_failed", "message": "simulated gh failure", "fix": "Check gh."},
    }]
    assert all(row["error"]["error"] != "unexpected_error"
               for row in os_result["errors"] + delivery_result["errors"])


def test_record_pr_branch_oserror_stays_record_io_failed(tmp_path, monkeypatch):
    state = tmp_path / "state"
    _intent(state, attempt="att_record_pr_os", repo="mojomast/bundlebees",
           branch="rails/record-pr-os")
    delivery.record_pushed(state, "att_record_pr_os", "a" * 40)

    record_path = state / "deliveries" / "att_record_pr_os.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    original_pr_url = "https://github.com/mojomast/bundlebees/pull/216"
    record["pr_url"] = original_pr_url
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert delivery.next_step(record) == "record_pr"

    def disk_failure(state_dir, attempt_id, pr_url):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(delivery, "record_pr", disk_failure)

    gh = FakeGh()
    result = delivery.sweep(state, gh)

    assert result["existing"] == []
    assert result["errors"] == [{
        "attempt_id": "att_record_pr_os",
        "branch": "rails/record-pr-os",
        "error": {"error": "record_io_failed", "message": "simulated disk failure",
                  "fix": "Inspect the deliveries directory; the sweep can be rerun safely."},
    }]
    assert result["errors"][0]["error"]["error"] != "gh_run_failed"
    unchanged = json.loads(record_path.read_text(encoding="utf-8"))
    assert unchanged["state"] == delivery.STATE_PUSHED
    assert unchanged["pr_url"] == original_pr_url


def test_run_gh_timeout_becomes_gh_timeout_delivery_error(monkeypatch):
    calls = []

    def timing_out_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(["gh", "pr", "list"], 120)

    monkeypatch.setattr(delivery.subprocess, "run", timing_out_run)

    with pytest.raises(delivery.DeliveryError) as caught:
        delivery._run_gh(["pr", "list", "--repo", "mojomast/bundlebees", "--head", "rails/secret"])

    payload = caught.value.to_dict()
    assert payload["error"] == "gh_timeout"
    assert "pr list" in payload["message"]
    assert "idempotent" in payload["message"]
    assert "mojomast/bundlebees" not in payload["message"]
    assert "rails/secret" not in payload["message"]
    assert "network" in payload["fix"]
    assert "rerun" in payload["fix"]
    assert calls and calls[0][1]["timeout"] == 120


def test_run_gh_missing_binary_is_gh_run_failed_not_record_io_and_not_timeout(tmp_path, monkeypatch):
    _intent(tmp_path, attempt="att_gh_missing", repo="Hostile-Org/REPO-CANARY-4f1d",
           branch="rails/BRANCH-CANARY-9a2c")
    delivery.record_pushed(tmp_path, "att_gh_missing", "a" * 40)

    def missing_run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(delivery.subprocess, "run", missing_run)

    result = delivery.sweep(tmp_path)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_gh_missing"
    payload = error["error"]
    assert payload == {
        "error": "gh_run_failed",
        "message": "gh could not be run (FileNotFoundError): [Errno 2] No such file or directory: 'gh'",
        "fix": "Install gh or fix PATH so the sweep can reach GitHub, then rerun; it is idempotent.",
    }
    assert "Hostile-Org/REPO-CANARY-4f1d" not in payload["message"]
    assert "Hostile-Org/REPO-CANARY-4f1d" not in payload["fix"]
    assert "rails/BRANCH-CANARY-9a2c" not in payload["message"]
    assert "rails/BRANCH-CANARY-9a2c" not in payload["fix"]
    assert payload["error"] not in ("record_io_failed", "gh_timeout")


def test_gh_run_failed_message_is_bounded_at_160_chars(tmp_path):
    _intent(tmp_path, attempt="att_gh_long", branch="rails/long")
    delivery.record_pushed(tmp_path, "att_gh_long", "a" * 40)

    def overflowing_runner(args):
        raise OSError("x" * 400)

    result = delivery.sweep(tmp_path, overflowing_runner)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_gh_long"
    payload = error["error"]
    assert payload["error"] == "gh_run_failed"
    assert payload["message"] == "gh could not be run (OSError): " + "x" * 160
    assert len(payload["message"]) == len("gh could not be run (OSError): ") + 160
    assert "x" * 161 not in payload["message"]
    assert "x" * 400 not in payload["message"]


def test_run_gh_permission_error_is_gh_run_failed(tmp_path, monkeypatch):
    _intent(tmp_path, attempt="att_gh_permission", branch="rails/permission")
    delivery.record_pushed(tmp_path, "att_gh_permission", "a" * 40)

    def denied_run(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/usr/bin/gh")

    monkeypatch.setattr(delivery.subprocess, "run", denied_run)

    result = delivery.sweep(tmp_path)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_gh_permission"
    assert error["error"]["error"] == "gh_run_failed"
    assert "PermissionError" in error["error"]["message"]
    assert "Permission denied" in error["error"]["message"]


def test_custom_runner_oserror_is_gh_run_failed_not_record_io(tmp_path):
    _intent(tmp_path, attempt="att_gh_pipe", branch="rails/pipe")
    delivery.record_pushed(tmp_path, "att_gh_pipe", "a" * 40)

    def broken_runner(args):
        raise OSError("simulated pipe failure")

    result = delivery.sweep(tmp_path, broken_runner)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_gh_pipe"
    assert error["error"]["error"] == "gh_run_failed"
    assert "OSError" in error["error"]["message"]
    assert "simulated pipe failure" in error["error"]["message"]


def test_dry_run_missing_binary_is_gh_run_failed(tmp_path, monkeypatch):
    _intent(tmp_path, attempt="att_gh_dry_missing", branch="rails/dry-missing")
    delivery.record_pushed(tmp_path, "att_gh_dry_missing", "a" * 40)

    def missing_run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(delivery.subprocess, "run", missing_run)

    result = delivery.sweep(tmp_path, dry_run=True)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["attempt_id"] == "att_gh_dry_missing"
    assert error["error"]["error"] == "gh_run_failed"
    assert [row["attempt_id"] for row in delivery.pending(tmp_path)] == ["att_gh_dry_missing"]
    record = json.loads((tmp_path / "deliveries" / "att_gh_dry_missing.json").read_text(encoding="utf-8"))
    assert record["state"] == delivery.STATE_PUSHED
    assert record.get("pr_url") is None


def test_unexpected_error_message_is_capped_at_200_chars(tmp_path):
    _intent(tmp_path, attempt="att_unexpected", branch="rails/unexpected")
    delivery.record_pushed(tmp_path, "att_unexpected", "a" * 40)

    def exploding_runner(args):
        raise RuntimeError("x" * 500)

    result = delivery.sweep(tmp_path, exploding_runner)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["error"]["error"] == "unexpected_error"
    message = error["error"]["message"]
    assert message.startswith("RuntimeError: ")
    assert len(message) == 200


def test_run_gh_timeout_names_the_actual_subcommand(tmp_path, monkeypatch):
    _intent(tmp_path, attempt="att_gh_create_timeout", branch="rails/create")
    delivery.record_pushed(tmp_path, "att_gh_create_timeout", "a" * 40)

    def scripted_run(argv, *args, **kwargs):
        subcommand = list(argv)[1:]
        if subcommand[:2] == ["pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if subcommand[:2] == ["pr", "create"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=120)
        raise AssertionError(f"unexpected gh call: {argv}")

    monkeypatch.setattr(delivery.subprocess, "run", scripted_run)

    result = delivery.sweep(tmp_path)

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["error"]["error"] == "gh_timeout"
    assert "gh pr create" in error["error"]["message"]


def _assert_no_deliveries_written(state_dir):
    deliveries = Path(state_dir) / "deliveries"
    assert not deliveries.exists()


def test_t1_record_intent_rejects_slash_attempt_id_without_writing(tmp_path):
    with pytest.raises(delivery.DeliveryError) as caught:
        _intent(tmp_path, attempt="att/a")

    payload = caught.value.to_dict()
    assert payload["error"] == "unsafe_attempt_id"
    assert "empty or contains characters outside [A-Za-z0-9_.-]" in payload["message"]
    assert "att_<hex>" in payload["fix"]
    _assert_no_deliveries_written(tmp_path)


def test_t2_record_intent_rejects_question_mark_attempt_id_without_writing(tmp_path):
    with pytest.raises(delivery.DeliveryError) as caught:
        _intent(tmp_path, attempt="att?a")

    assert caught.value.to_dict()["error"] == "unsafe_attempt_id"
    _assert_no_deliveries_written(tmp_path)


def test_t3_record_intent_rejects_empty_attempt_id_without_writing(tmp_path):
    with pytest.raises(delivery.DeliveryError) as caught:
        _intent(tmp_path, attempt="")

    assert caught.value.to_dict()["error"] == "unsafe_attempt_id"
    _assert_no_deliveries_written(tmp_path)


@pytest.mark.parametrize("attempt_id", ["att ", "att\n"], ids=["space", "newline"])
def test_t4_record_intent_rejects_whitespace_attempt_id_without_writing(tmp_path, attempt_id):
    with pytest.raises(delivery.DeliveryError) as caught:
        _intent(tmp_path, attempt=attempt_id)

    assert caught.value.to_dict()["error"] == "unsafe_attempt_id"
    _assert_no_deliveries_written(tmp_path)


def test_t5_safe_attempt_id_round_trips(tmp_path):
    _intent(tmp_path, attempt="att_ok-1.x", branch="rails/ok")
    record_path = tmp_path / "deliveries" / "att_ok-1.x.json"
    assert record_path.exists()
    assert [row["attempt_id"] for row in delivery.pending(tmp_path)] == ["att_ok-1.x"]

    pushed = delivery.record_pushed(tmp_path, "att_ok-1.x", "a" * 40)
    assert pushed["state"] == delivery.STATE_PUSHED

    gh = FakeGh()
    result = delivery.sweep(tmp_path, gh)
    assert [row["attempt_id"] for row in result["opened"]] == ["att_ok-1.x"]
    assert set(gh.prs) == {"rails/ok"}
    assert delivery.pending(tmp_path) == []
    stored = json.loads(record_path.read_text(encoding="utf-8"))
    assert stored["state"] == delivery.STATE_PR_OPEN
    assert stored["pr_url"] == gh.prs["rails/ok"]


def test_t6_distinct_safe_ids_do_not_alias(tmp_path):
    _intent(tmp_path, attempt="att_one", branch="rails/one")
    _intent(tmp_path, attempt="att_two", branch="rails/two")

    files = sorted(path.name for path in (tmp_path / "deliveries").glob("*.json"))
    assert files == ["att_one.json", "att_two.json"]
    assert sorted(row["attempt_id"] for row in delivery.pending(tmp_path)) == [
        "att_one", "att_two"]


def test_t7_record_pushed_rejects_unsafe_id_instead_of_reading_alias(tmp_path):
    _intent(tmp_path, attempt="att_a", branch="rails/a")
    alias = tmp_path / "deliveries" / "att_a.json"
    before = alias.read_text(encoding="utf-8")

    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.record_pushed(tmp_path, "att/a", "b" * 40)

    assert caught.value.to_dict()["error"] == "unsafe_attempt_id"
    assert alias.read_text(encoding="utf-8") == before

    with pytest.raises(delivery.DeliveryError) as caught_pr:
        delivery.record_pr(tmp_path, "att/a", "https://github.com/mojomast/bundlebees/pull/1")

    assert caught_pr.value.to_dict()["error"] == "unsafe_attempt_id"
    assert alias.read_text(encoding="utf-8") == before
    assert [row["state"] for row in delivery.pending(tmp_path)] == [delivery.STATE_INTENT]


def test_t8_record_pushed_rejects_unsafe_id_without_creating_deliveries_dir(tmp_path):
    assert not (tmp_path / "deliveries").exists()

    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.record_pushed(tmp_path, "att/a", "b" * 40)

    assert caught.value.to_dict()["error"] == "unsafe_attempt_id"
    assert not (tmp_path / "deliveries").exists()


def test_t9_record_pr_rejects_unsafe_id_without_creating_deliveries_dir(tmp_path):
    assert not (tmp_path / "deliveries").exists()

    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.record_pr(tmp_path, "att/a", "https://github.com/mojomast/bundlebees/pull/1")

    assert caught.value.to_dict()["error"] == "unsafe_attempt_id"
    assert not (tmp_path / "deliveries").exists()


def test_t10_each_delivery_stores_its_own_distinct_exact_pr_url(tmp_path):
    _intent(tmp_path, attempt="att_url_a", branch="rails/url-a")
    _intent(tmp_path, attempt="att_url_b", branch="rails/url-b")
    delivery.record_pushed(tmp_path, "att_url_a", "a" * 40)
    delivery.record_pushed(tmp_path, "att_url_b", "b" * 40)

    gh = FakeGh()
    result = delivery.sweep(tmp_path, gh)

    assert sorted(row["attempt_id"] for row in result["opened"]) == ["att_url_a", "att_url_b"]
    stored_a = json.loads((tmp_path / "deliveries" / "att_url_a.json").read_text(encoding="utf-8"))
    stored_b = json.loads((tmp_path / "deliveries" / "att_url_b.json").read_text(encoding="utf-8"))
    assert stored_a["state"] == delivery.STATE_PR_OPEN
    assert stored_b["state"] == delivery.STATE_PR_OPEN
    assert stored_a["pr_url"] == gh.prs["rails/url-a"]
    assert stored_b["pr_url"] == gh.prs["rails/url-b"]
    assert stored_a["pr_url"] != stored_b["pr_url"]


@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "view", "--repo", "mojomast/bundlebees", "--json", "url"],
        ["pr", "merge", "--repo", "mojomast/bundlebees", "123", "--squash"],
        ["run", "list", "--repo", "mojomast/bundlebees", "--limit", "5"],
        ["api", "graphql", "-f", "query=query{viewer{login}}"],
        ["repo", "view", "mojomast/bundlebees", "--json", "name"],
        ["status"],
        ["auth"],
    ],
    ids=["pr-view", "pr-merge", "run-list", "api-graphql", "repo-view", "single-token", "single-token-auth"],
)
def test_t11_run_gh_timeout_names_unmodelled_subcommand(monkeypatch, argv):
    def timing_out_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh", *argv], 120)

    monkeypatch.setattr(delivery.subprocess, "run", timing_out_run)

    with pytest.raises(delivery.DeliveryError) as caught:
        delivery._run_gh(argv)

    payload = caught.value.to_dict()
    expected_shape = " ".join(argv[:2])
    assert payload["error"] == "gh_timeout"
    assert payload["message"].startswith(f"gh {expected_shape} ")
    assert not any(token in payload["message"].split() for token in argv[2:])
    for token in argv[2:]:
        if len(token) >= 3:
            assert token not in payload["message"]
    assert "gh pr create" not in payload["message"]
    assert "gh pr list" not in payload["message"]


if __name__ == "__main__":
    unittest.main()
