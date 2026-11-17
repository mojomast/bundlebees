#!/usr/bin/env python3
"""Offline, opt-in index for local Rails attempt reads; see docs/rails-attempt-index.md."""
import argparse
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import UUID


NAME = 'rails_local_attempt_scan'
SQL = (f'CREATE INDEX {NAME} ON event_rows ('
       "json_extract(envelope_json,'$.scope.attempt_id'), ingest_seq, event_id, rails_hash, "
       'length(CAST(envelope_json AS BLOB)))')
MAX_BYTES = 512 * 1024**2
MAX_ROWS = 120_000


def install(path, ledger_uuid):
    """Index an existing operator-selected WAL ledger or fail without changing it."""
    if not path.is_absolute() or not path.is_file() or str(UUID(ledger_uuid)) != ledger_uuid:
        raise ValueError('expected existing ledger and UUID')
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=0)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        if (db.execute('PRAGMA trusted_schema').fetchone() != (0,) or
                db.execute('PRAGMA journal_mode').fetchone() != ('wal',)):
            raise ValueError('unsupported SQLite configuration')
        # With trusted_schema=OFF SQLite itself rejects expression indexes
        # involving functions not marked innocuous by this build.
        db.execute('BEGIN IMMEDIATE')  # no waiting for a busy writer; close rolls back on failure
        deadline = time.monotonic()+10
        db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        if db.execute("SELECT value FROM ledger_meta WHERE key='ledger_uuid'").fetchone() != (ledger_uuid,):
            raise ValueError('ledger identity mismatch')
        found = db.execute('SELECT sql FROM sqlite_schema WHERE name=?', (NAME,)).fetchone()
        if found is not None:
            if found != (SQL,):
                raise ValueError('index definition mismatch')
            return 'already installed'
        size = db.execute('PRAGMA page_size').fetchone()[0]*db.execute('PRAGMA page_count').fetchone()[0]
        if size > MAX_BYTES or shutil.disk_usage(path.parent).free < 3*MAX_BYTES:
            raise ValueError('offline size or free-space limit')
        if db.execute('SELECT 1 FROM event_rows LIMIT 1 OFFSET ?', (MAX_ROWS,)).fetchone():
            raise ValueError('offline row limit')
        db.execute(SQL)
        db.commit()
        return 'installed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('db', type=Path, help='absolute path to the existing local SQLite ledger')
    parser.add_argument('--ledger-uuid', required=True, help='expected local ledger identity')
    args = parser.parse_args()
    try:
        print(install(args.db, args.ledger_uuid))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'index not installed: {exc}\n')
