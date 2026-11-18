"""The job/reconciler dispatch path can target a registered local-path project.

Defect (2026-11-17, M2 canary): `job tick` dispatched a scoped task to a project registered
with `add-project` and the launcher refused with
``workspace_invalid (project): scoped code work requires a registered local-path project``.
Root cause: ``reconcile._brief_for`` only copied ``brief.project`` from a provisioning
``binding`` (which ``job create`` always sets to None), and ``_normalize_task`` dropped the
task's own ``project`` — so a local-path project had no way to reach the launcher.

Pinned here:
  * a task carrying ``project`` produces a brief that carries it;
  * a provisioning binding still wins over the task's project;
  * a task with neither produces no ``project`` key (legacy behaviour unchanged).

Run: python -m pytest tests/test_job_local_project.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import job as job_mod  # noqa: E402
from bundlebees_plugin.reconcile import _brief_for  # noqa: E402

SOURCE = "eebb203aba31b8357835e7d296036cff37f5949b"


def _job(**task_extra) -> dict:
    task = {
        "task_id": "T1",
        "objective": "do the thing",
        "lease": ["canary/meter.py"],
        "depends_on": [],
        "contract_path": None,
        "contract_sha256": None,
        "acceptance": {"criteria": ["it works"]},
        "contract": None,
        "provision": None,
        "binding": None,
        "provisioning": None,
        "state": "pending",
        "attempts": [],
        "verify": None,
        "intent": None,
        "project": "",
    }
    task.update(task_extra)
    return {"job_id": "job-1", "repo": "mojomast/rizzstack-m2-canary",
            "goal": {"base_rev": SOURCE, "repo": "mojomast/rizzstack-m2-canary"},
            "tasks": [task]}


class BriefProject(unittest.TestCase):
    def test_task_project_reaches_the_brief(self):
        brief = _brief_for(_job(project="rizzstack-m2-canary"), _job(project="rizzstack-m2-canary")["tasks"][0])
        self.assertEqual(brief.get("project"), "rizzstack-m2-canary")

    def test_binding_project_wins_over_the_task_project(self):
        job = _job(project="task-project", binding={"project": "bind-project"})
        brief = _brief_for(job, job["tasks"][0])
        self.assertEqual(brief.get("project"), "bind-project")

    def test_no_project_anywhere_stays_absent(self):
        job = _job()
        brief = _brief_for(job, job["tasks"][0])
        self.assertNotIn("project", brief)

    def test_create_keeps_the_task_project(self):
        normalized = job_mod._normalize_task({"task_id": "T1", "objective": "x",
                                              "lease": ["a.py"], "project": "rizzstack-m2-canary"},
                                             for_create=True)
        self.assertEqual(normalized.get("project"), "rizzstack-m2-canary")


if __name__ == "__main__":
    unittest.main()
