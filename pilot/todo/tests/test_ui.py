import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.labels = set()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.add(values["id"])
        if tag == "label" and "for" in values:
            self.labels.add(values["for"])
        if tag in {"script", "link"}:
            self.scripts.append(values)


class UiTests(unittest.TestCase):
    def test_required_controls_and_inline_assets(self):
        text = HTML.read_text(encoding="utf-8")
        parser = Ids()
        parser.feed(text)
        for expected in {"todo-form", "todo-input", "todo-description", "todo-list",
                         "search", "status-filter", "error-message"}:
            self.assertIn(expected, parser.ids)
        for expected in {"todo-input", "todo-description", "search", "status-filter"}:
            self.assertIn(expected, parser.labels)
        self.assertNotRegex(text, r"https?://")
        self.assertNotRegex(text, r"<(script|link)[^>]+src=")

    def test_ui_calls_the_documented_api(self):
        text = HTML.read_text(encoding="utf-8")
        for route in ("/api/todos", "/complete", "PATCH"):
            self.assertIn(route, text)
        self.assertIn("textContent", text)
        self.assertNotIn("innerHTML", text)


if __name__ == "__main__":
    unittest.main()
