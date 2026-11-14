import json
import tempfile
import unittest
from pathlib import Path

from pilot.todo.store import SCHEMA, TodoStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "todos.json"
        self.store = TodoStore(self.path)

    def test_create_persists_and_reloads(self):
        row = self.store.create("Write tests", "with detail")
        self.assertEqual(row["title"], "Write tests")
        self.assertFalse(row["completed"])
        self.assertTrue(self.path.exists())
        reloaded = TodoStore(self.path)
        self.assertEqual(reloaded.get(row["id"]), row)
        self.assertEqual(json.loads(self.path.read_text())["schema"], SCHEMA)

    def test_search_and_status_filters(self):
        first = self.store.create("Buy milk")
        second = self.store.create("Write docs", "Milk references")
        self.store.update(first["id"], completed=True)
        self.assertEqual({row["id"] for row in self.store.list(query="milk")}, {first["id"], second["id"]})
        self.assertEqual([row["id"] for row in self.store.list(status="open")], [second["id"]])
        self.assertEqual([row["id"] for row in self.store.list(status="done")], [first["id"]])

    def test_update_and_missing_id(self):
        row = self.store.create("old")
        edited = self.store.update(row["id"], title="new", description="body", completed=True)
        self.assertEqual((edited["title"], edited["description"], edited["completed"]), ("new", "body", True))
        with self.assertRaises(ValueError):
            self.store.update("missing", title="x")

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.store.create("")
        with self.assertRaises(ValueError):
            self.store.create("x" * 201)
        with self.assertRaises(ValueError):
            self.store.create("ok", "x" * 2001)
        with self.assertRaises(ValueError):
            self.store.list(status="invalid")

    def test_malformed_file_is_an_error(self):
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            TodoStore(self.path)


if __name__ == "__main__":
    unittest.main()
