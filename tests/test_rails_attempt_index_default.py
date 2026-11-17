"""Default-on Rails attempt index: new ledgers indexed at creation, existing ones migrated.

Owner decision 2026-11-17 (after the release qualification measured authenticated 500-row
pages at 1,614 ms p95 unindexed vs 499.7 ms indexed): the attempt scan index becomes the
DEFAULT, with a migration path for existing ledgers. It stays an optimisation: a build whose
trusted_schema=OFF refuses expression indexes runs unindexed and never fails.

RED-first: before this change, a freshly created ledger had no `rails_local_attempt_scan`
and scripts/rails_attempt_index.py had no directory migration mode.

Run: python -m pytest tests/test_rails_attempt_index_default.py -q
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contrib"))
sys.path.insert(0, str(ROOT))

import rizzstack_rails  # noqa: E402
import rizzstack_rails.ledger as rails_ledger  # noqa: E402

INDEX = "rails_local_attempt_scan"


def _index_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}


class NewLedgerIsIndexed(unittest.TestCase):
    def test_fresh_ledger_has_the_attempt_index(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ledger.sqlite"
            with closing(rails_ledger.RailsLedger(path)):
                pass
            names = _index_names(path)
            self.assertIn(INDEX, names,
                          f"new ledger is not indexed by default: {sorted(names)}")

    def test_index_definition_matches_the_maintenance_script(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ledger.sqlite"
            with closing(rails_ledger.RailsLedger(path)):
                pass
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
                sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (INDEX,)).fetchone()[0]
            self.assertIn("json_extract(envelope_json", sql)
            self.assertIn("ingest_seq", sql)


class DirectoryMigration(unittest.TestCase):
    def _ledger(self, d: str) -> Path:
        path = Path(d) / "ledger.sqlite"
        with closing(rails_ledger.RailsLedger(path)):
            pass
        # simulate a pre-migration ledger: drop the index the new code created
        with closing(sqlite3.connect(f"file:{path}?mode=rw", uri=True)) as db:
            db.execute(f"DROP INDEX IF EXISTS {INDEX}")
            db.commit()
        return path

    def test_migrate_indexes_an_existing_ledger_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d)
            self.assertNotIn(INDEX, _index_names(path))
            script = ROOT / "scripts" / "rails_attempt_index.py"
            first = subprocess.run([sys.executable, str(script), "--migrate", d],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn(INDEX, _index_names(path))
            second = subprocess.run([sys.executable, str(script), "--migrate", d],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(second.returncode, 0)
            self.assertIn("already installed", second.stdout)

    def test_migrate_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d)
            script = ROOT / "scripts" / "rails_attempt_index.py"
            subprocess.run([sys.executable, str(script), "--migrate", d, "--dry-run"],
                           capture_output=True, text=True, timeout=120)
            self.assertNotIn(INDEX, _index_names(path))

    def test_migrate_skips_a_foreign_database(self):
        with tempfile.TemporaryDirectory() as d:
            foreign = Path(d) / "other.sqlite"
            with closing(sqlite3.connect(foreign)) as db:
                db.execute("CREATE TABLE unrelated (x)")
                db.commit()
            script = ROOT / "scripts" / "rails_attempt_index.py"
            r = subprocess.run([sys.executable, str(script), "--migrate", d],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("skipped: no ledger identity", r.stdout)


if __name__ == "__main__":
    unittest.main()
