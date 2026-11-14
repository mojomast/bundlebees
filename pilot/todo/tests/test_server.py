import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pilot.todo.server import make_server
from pilot.todo.store import TodoStore


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = make_server(TodoStore(Path(self.tmp.name) / "todos.json"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_crud_and_filters(self):
        status, body = self.request("POST", "/api/todos", {"title": "write", "description": "hello"})
        self.assertEqual(status, 201)
        todo = body["todo"]
        status, body = self.request("GET", "/api/todos?q=write&status=open")
        self.assertEqual(status, 200)
        self.assertEqual(body["todos"][0]["id"], todo["id"])
        status, body = self.request("PATCH", f"/api/todos/{todo['id']}", {"title": "edited", "completed": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["todo"]["completed"], True)
        status, body = self.request("POST", f"/api/todos/{todo['id']}/complete")
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/api/todos?status=done")
        self.assertEqual(body["todos"][0]["id"], todo["id"])

    def test_errors_are_json(self):
        status, body = self.request("POST", "/api/todos", {"title": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        status, body = self.request("GET", "/api/todos/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")
        status, body = self.request("DELETE", "/api/todos")
        self.assertEqual(status, 405)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
