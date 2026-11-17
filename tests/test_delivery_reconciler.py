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

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import delivery  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
