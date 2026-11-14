import copy
import json
import tempfile
import unittest
from pathlib import Path

from bundlebees_plugin import project


def spec():
    return {
        "schema": project.SCHEMA,
        "project_id": "pilot-todo",
        "repo": "mojomast/bundlebees",
        "base_rev": "a" * 40,
        "summary": "pilot",
        "acceptance": [
            {"id": "AC-1", "statement": "storage works", "test": "pilot/todo/tests/test_store.py"}
        ],
        "features": [
            {
                "feature_id": "store",
                "objective": "store",
                "lease": ["pilot/todo/store.py", "pilot/todo/tests/test_store.py"],
                "depends_on": [],
                "acceptance": {"tests": ["pilot/todo/tests/test_store.py"]},
            },
            {
                "feature_id": "http",
                "objective": "http",
                "lease": ["pilot/todo/server.py", "pilot/todo/tests/test_server.py"],
                "depends_on": ["store"],
                "acceptance": {"tests": ["pilot/todo/tests/test_server.py"]},
            },
        ],
    }


class PlanTests(unittest.TestCase):
    def test_valid_plan_is_deterministic_and_ordered(self):
        first = project.plan(spec())
        second = project.plan(copy.deepcopy(spec()))
        self.assertEqual(first, second)
        self.assertEqual([task["task_id"] for task in first["tasks"]], ["store", "http"])
        self.assertEqual(first["tasks"][1]["depends_on"], ["store"])
        self.assertEqual(first["tasks"][0]["acceptance"]["tests"], ["pilot/todo/tests/test_store.py"])

    def test_missing_schema_and_bad_sha_are_refused(self):
        with self.assertRaises(project.ProjectError) as caught:
            project.plan({**spec(), "schema": "wrong"})
        self.assertEqual(caught.exception.code, "project_invalid")
        with self.assertRaises(project.ProjectError) as caught:
            project.plan({**spec(), "base_rev": "abc"})
        self.assertEqual(caught.exception.code, "project_invalid")

    def test_unknown_dependency_and_cycle_are_refused(self):
        broken = spec()
        broken["features"][1]["depends_on"] = ["missing"]
        with self.assertRaises(project.ProjectError) as caught:
            project.plan(broken)
        self.assertEqual(caught.exception.code, "project_dependency_unknown")
        cycle = spec()
        cycle["features"][0]["depends_on"] = ["http"]
        with self.assertRaises(project.ProjectError) as caught:
            project.plan(cycle)
        self.assertEqual(caught.exception.code, "project_dependency_cycle")

    def test_overlapping_leases_are_refused(self):
        broken = spec()
        broken["features"][1]["lease"] = ["pilot/todo", "pilot/todo/tests/test_server.py"]
        with self.assertRaises(project.ProjectError) as caught:
            project.plan(broken)
        self.assertEqual(caught.exception.code, "project_lease_overlap")
        duplicate = spec()
        duplicate["features"][1]["lease"] = ["pilot/todo/store.py"]
        with self.assertRaises(project.ProjectError) as caught:
            project.plan(duplicate)
        self.assertEqual(caught.exception.code, "project_lease_overlap")

    def test_load_plan_reads_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            path.write_text(json.dumps(spec()))
            self.assertEqual(project.load_plan(path)["project_id"], "pilot-todo")


class CompletionTests(unittest.TestCase):
    def job(self, states):
        return {"tasks": [{"task_id": "store", "state": states[0], "depends_on": []},
                          {"task_id": "http", "state": states[1], "depends_on": ["store"]}]}

    def test_complete_only_when_every_task_is_accepted(self):
        result = project.project_completion(self.job(["accepted", "accepted"]))
        self.assertTrue(result["complete"])
        self.assertEqual(result["status"], "complete")
        result = project.project_completion(self.job(["accepted", "pending"]))
        self.assertFalse(result["complete"])
        self.assertEqual(result["next"], ["http"])

    def test_dependency_failure_is_blocked_not_ready(self):
        result = project.project_completion(self.job(["failed", "pending"]))
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["blocked"], ["http"])

    def test_pending_dependency_blocks_downstream(self):
        result = project.project_completion(self.job(["pending", "pending"]))
        self.assertEqual(result["next"], ["store"])
        self.assertEqual(result["blocked"], [])


if __name__ == "__main__":
    unittest.main()
