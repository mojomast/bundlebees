import argparse
import json
import tempfile
import unittest
from pathlib import Path

from bundlebees_plugin import job_cli


SPEC = {
    "schema": "rizzstack.project-plan/1",
    "project_id": "pilot-todo",
    "repo": "mojomast/bundlebees",
    "base_rev": "a" * 40,
    "acceptance": [{"id": "AC-1", "statement": "works", "test": "pilot/todo/tests/test_store.py"}],
    "features": [{
        "feature_id": "store",
        "objective": "store",
        "lease": ["pilot/todo/store.py"],
        "depends_on": [],
        "acceptance": {"tests": ["pilot/todo/tests/test_store.py"]},
    }],
}


class ProjectCliTests(unittest.TestCase):
    def test_plan_writes_the_task_list(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = Path(directory) / "project.json"
            out = Path(directory) / "tasks.json"
            spec.write_text(json.dumps(SPEC))
            args = argparse.Namespace(project_handler="plan", spec=str(spec), out=str(out))
            self.assertEqual(job_cli.cmd_project(None, args, state_dir=Path(directory)), 0)
            self.assertEqual(json.loads(out.read_text())[0]["task_id"], "store")

    def test_parser_exposes_project_commands(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        job_cli.add_parsers(sub)
        args = parser.parse_args(["job", "project", "status", "--project-id", "pilot-todo"])
        self.assertEqual(args.job_handler, "project")
        self.assertEqual(args.project_handler, "status")

    def test_create_records_a_project_after_authorization(self):
        from bundlebees_plugin import job as jobs
        from bundlebees_plugin import project as project_mod

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            spec = state / "project.json"
            spec.write_text(json.dumps(SPEC))
            jobs.record_authorization(
                state, job_id="pilot-todo", statement="Build the pilot todo application.",
                repo=SPEC["repo"], base_rev=SPEC["base_rev"], scope=["pilot/**"],
                bees=4, wall_clock_s=600, interactive=True,
            )
            args = argparse.Namespace(project_handler="create", spec=str(spec), job_id="pilot-todo")
            self.assertEqual(job_cli.cmd_project(None, args, state_dir=state), 0)
            record = project_mod.load_project(state, "pilot-todo")
            self.assertEqual(record["job_id"], "pilot-todo")
            job = jobs.load_job(state, "pilot-todo")
            self.assertEqual(job["tasks"][0]["task_id"], "store")


if __name__ == "__main__":
    unittest.main()
