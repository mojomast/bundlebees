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
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
