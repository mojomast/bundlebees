"""Approved tests for acdd_lint.schema (slice L1, written first, RED).

A small, zero-dependency JSON Schema validator for exactly the keywords the
shared acdd/1 and tdd-evidence/1 schemas use, plus schema discovery.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.support import find_schemas, frozen, green, red, TOOL


class ValidateTests(unittest.TestCase):
    def setUp(self):
        from acdd_lint import schema
        self.schema = schema

    def v(self, instance, sch):
        return self.schema.validate(instance, sch)

    def test_valid_shared_documents_have_no_errors(self):
        cs, es = find_schemas()
        c = frozen()
        self.assertEqual(self.v(c, cs), [])
        self.assertEqual(self.v(red(c), es), [])
        self.assertEqual(self.v(green(c), es), [])

    def test_errors_are_path_message_pairs(self):
        cs, _ = find_schemas()
        c = frozen()
        del c["scope"]
        errs = self.v(c, cs)
        self.assertTrue(errs)
        for path, msg in errs:
            self.assertTrue(path.startswith("$"))
            self.assertIsInstance(msg, str)
        self.assertTrue(any("scope" in msg or "scope" in path for path, msg in errs))

    def test_every_used_keyword_is_enforced(self):
        cases = [
            ({"type": "string"}, 1), ({"type": "integer"}, True), ({"type": "integer"}, 1.5), ({"type": ["string", "null"]}, 3),
            ({"type": "object", "required": ["a"]}, {}),
            ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": 1}),
            ({"type": "object", "additionalProperties": False, "properties": {"a": {}}}, {"a": 1, "b": 2}),
            ({"type": "object", "additionalProperties": {"type": "string"}}, {"x": 1}),
            ({"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}}, {"A1": 1}),
            ({"enum": ["x", "y"]}, "z"), ({"const": "acdd/1"}, "acdd/2"),
            ({"type": "string", "pattern": "^AC-[0-9]{3,}$"}, "AC-1"),
            ({"type": "string", "minLength": 2}, "a"), ({"type": "string", "maxLength": 2}, "abc"),
            ({"type": "array", "minItems": 1}, []), ({"type": "array", "items": {"type": "integer"}}, [1, "2"]),
            ({"type": "array", "uniqueItems": True}, [1, 1]), ({"type": "integer", "minimum": 0}, -1),
            ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, []),
        ]
        for sch, bad in cases:
            with self.subTest(schema=sch):
                self.assertTrue(self.v(bad, sch), f"{bad!r} should fail {sch}")
        good = [({"type": "integer"}, 3), ({"type": "number"}, 1.5), ({"type": ["string", "null"]}, None),
                ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 2), ({"type": "array", "uniqueItems": True}, [1, 2])]
        for sch, ok in good:
            with self.subTest(schema=sch):
                self.assertEqual(self.v(ok, sch), [])

    def test_annotations_are_ignored(self):
        self.assertEqual(self.v("x", {"$schema": "s", "$id": "i", "title": "t", "description": "d", "type": "string"}), [])

    def test_nested_paths_point_at_the_field(self):
        sch = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "object", "required": ["id"]}}}}
        errs = self.v({"a": [{"id": 1}, {}]}, sch)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0][0], "$.a[1]")


class LoadTests(unittest.TestCase):
    def test_find_schema_dir_from_inside_the_repo(self):
        from acdd_lint import schema
        found = schema.find_schema_dir(TOOL)
        self.assertIsNotNone(found)
        cs, es = schema.load_schemas(found)
        self.assertEqual(cs, find_schemas()[0])
        self.assertEqual(es, find_schemas()[1])

    def test_load_accepts_both_naming_conventions_and_refuses_missing(self):
        from acdd_lint import schema
        cs, es = find_schemas()
        for cname, ename in (("acdd-contract.schema.json", "tdd-evidence.schema.json"), ("contract.schema.json", "evidence.schema.json")):
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / cname).write_text(json.dumps(cs))
                (Path(d) / ename).write_text(json.dumps(es))
                self.assertEqual(schema.load_schemas(Path(d)), (cs, es))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(schema.LintError):
                schema.load_schemas(Path(d))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(schema.find_schema_dir(Path(d)))


if __name__ == "__main__":
    unittest.main()
