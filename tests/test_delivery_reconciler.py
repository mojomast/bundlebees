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


if __name__ == "__main__":
    unittest.main()
