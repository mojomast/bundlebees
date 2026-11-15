"""User-run campaign checkpoint commands for bundlebees.

These commands create and inspect the durable review objective that sits above
one authorized job. They do not dispatch, merge or grant authority; the job's
sealed authorization remains authoritative.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import campaign


def add_parsers(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "campaign",
        help="Create or inspect the durable continuity checkpoint for an authorized job.",
    )
    sub = parser.add_subparsers(dest="campaign_handler", required=True)

    init = sub.add_parser("init", help="Create a campaign checkpoint for an existing job.")
    init.add_argument("--campaign-id", required=True)
    init.add_argument("--job-id", required=True)
    init.add_argument("--objective", required=True)
    init.add_argument("--branch", default="")
    init.add_argument("--pull-request", type=int, default=None)
    init.add_argument("--state-dir", default=None)
    init.set_defaults(command="campaign", campaign_handler="init")

    status = sub.add_parser("status", help="Reconcile and print the current campaign checkpoint.")
    status.add_argument("--campaign-id", required=True)
    status.add_argument("--state-dir", default=None)
    status.set_defaults(command="campaign", campaign_handler="status")

    reconcile = sub.add_parser("reconcile", help="Persist one reconciled campaign checkpoint.")
    reconcile.add_argument("--campaign-id", required=True)
    reconcile.add_argument("--state-dir", default=None)
    reconcile.set_defaults(command="campaign", campaign_handler="reconcile")

    resume = sub.add_parser("resume-plan", help="Print what a restarted coordinator should do next.")
    resume.add_argument("--campaign-id", required=True)
    resume.add_argument("--state-dir", default=None)
    resume.set_defaults(command="campaign", campaign_handler="resume-plan")


def cmd_campaign(ctx: Any, args: Any, *, state_dir: Path) -> int:
    handler = getattr(args, "campaign_handler", None)
    try:
        if handler == "init":
            record = campaign.create_campaign(
                state_dir,
                campaign_id=args.campaign_id,
                job_id=args.job_id,
                objective=args.objective,
                branch=args.branch,
                pull_request=args.pull_request,
            )
            print(json.dumps({"campaign_id": record["campaign_id"], "job_id": record["job_id"],
                              "phase": record["phase"]}, indent=2, sort_keys=True))
            return 0
        if handler == "status":
            record = campaign.reconcile(state_dir, args.campaign_id)
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0
        if handler == "reconcile":
            record = campaign.reconcile(state_dir, args.campaign_id)
            print(json.dumps({"campaign_id": record["campaign_id"], "phase": record["phase"],
                              "revision": record["revision"], "next_action": record["next_action"]},
                             indent=2, sort_keys=True))
            return 0
        if handler == "resume-plan":
            print(json.dumps(campaign.resume_plan(state_dir, args.campaign_id), indent=2, sort_keys=True))
            return 0
        print(json.dumps({"error": "campaign_handler_required"}, indent=2))
        return 2
    except campaign.CampaignError as exc:
        print(json.dumps(exc.as_dict(), indent=2, sort_keys=True))
        return 1
