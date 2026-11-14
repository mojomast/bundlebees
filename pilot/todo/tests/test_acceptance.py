import json
import tempfile
import unittest
from pathlib import Path

from pilot.todo.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_real_http_server_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run(Path(directory) / "todos.json")
        self.assertTrue(report["ok"])
        self.assertEqual(
            report["checks"],
            ["create", "list", "search", "edit", "complete", "validation", "not-found", "persist"],
        )


if __name__ == "__main__":
    unittest.main()
