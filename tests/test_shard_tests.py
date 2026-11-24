"""CI shards (.github/scripts/shard_tests.py) must partition every collectable test file."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shard_tests", ROOT / ".github" / "scripts" / "shard_tests.py")
shard_tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shard_tests)


class ShardPartition(unittest.TestCase):
    def test_every_test_file_runs_in_exactly_one_shard(self):
        files = shard_tests.test_files()
        self.assertIn("tests/test_shard_tests.py", files)
        for total in range(1, 7):
            with self.subTest(total=total):
                parts = shard_tests.shards(total)
                flat = [f for part in parts for f in part]
                self.assertEqual(sorted(flat), files)
                self.assertEqual(len(flat), len(set(flat)))

    def test_split_is_deterministic(self):
        self.assertEqual(shard_tests.shards(4), shard_tests.shards(4))

    def test_ci_runs_four_shards_and_checks_the_partition(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("shard: [1, 2, 3, 4]", ci)
        self.assertIn("shard_tests.py --check 4", ci)
        self.assertIn("fail-fast: false", ci)
        self.assertIn("--check 4 --against-pytest", ci)

    def test_pytest_collects_nothing_the_shards_miss(self):
        # Independent of test_files(): ask pytest which files it collects for the whole suite.
        self.assertLessEqual(shard_tests.pytest_files(), set(shard_tests.test_files()))


if __name__ == "__main__":
    unittest.main()
