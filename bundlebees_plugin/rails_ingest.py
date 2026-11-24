"""Scheduled Rails ingest: fill one local ledger from coordinator records.

This is the unattended form of ``hermes bundlebees rails-import`` for a host
where the importer runs from a timer rather than inside a Hermes session::

    python -m bundlebees_plugin.rails_ingest --ledger /abs/ledger.sqlite3 \\
        --state-dir /abs/plugin-data/bundlebees --source bundlebees --source outbox

Each named source is imported a bounded page at a time until it reports no
more pages (or ``--max-pages`` is reached). The sources and the authorities
each may write are exactly the ones ``rails-import`` grants. The bundlebees
state is only read; only the ledger is written. One run at a time holds an
exclusive lock beside the ledger. Output is one JSON line with per-source
counts or a generic error code -- never a path, record or exception text.
Exit codes: 0 every source imported, 2 configuration refused, 3 a source failed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from contextlib import closing
from pathlib import Path

SOURCES = ("bundlebees", "outbox", "queue")
_CODE_RE = re.compile(r"^[a-z_]{1,48}$")


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _source(name: str, state: str):
    authorities = {"imported_observation"}
    if name == "bundlebees":
        from .rails_bundlebees_source import BundlebeesSource

        authorities.update(("worker_report", "coordinator_record", "verifier_record", "reviewer_record"))
        return BundlebeesSource.from_existing_state(state), authorities
    if name == "outbox":
        from .rails_outbox_source import RailsOutboxSource

        authorities.add("worker_report")
        return RailsOutboxSource(state), authorities
    if name == "queue":
        from .rails_queue_source import QueueSource

        authorities.add("coordinator_record")
        return QueueSource(state), authorities
    raise _Refused("invalid_source")


def _code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else "source_unavailable"


def ingest(ledger: str, state: str, sources, *, page_limit: int = 200, max_pages: int = 50) -> dict:
    from rizzstack_rails import RailsLedger
    from rizzstack_rails.importer import Importer

    report: dict = {}
    for name in sources:
        try:
            source, authorities = _source(name, state)
            imported, has_more = 0, False
            with closing(RailsLedger(ledger, allowed_authorities=authorities)) as led:
                importer = Importer(led, authorities=authorities)
                for _ in range(max_pages):
                    result = importer.import_once(source, limit=page_limit)
                    imported += int(result["imported"])
                    has_more = bool(result["has_more"])
                    if not has_more:
                        break
            report[name] = {"ok": True, "imported": imported, "has_more": has_more}
        except Exception as exc:  # noqa: BLE001 - reported as a generic code only
            report[name] = {"ok": False, "error": {"code": _code(exc)}}
    return report


def _print(doc: dict) -> None:
    print(json.dumps(doc, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import coordinator records into a local Rails ledger.")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--page-limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=50)
    args = parser.parse_args(argv)

    ledger, state = Path(args.ledger), Path(args.state_dir)
    if not ledger.is_absolute() or not state.is_absolute() or not state.is_dir() or not ledger.parent.is_dir():
        _print({"ok": False, "error": {"code": "not_configured"}})
        return 2
    names = list(dict.fromkeys(args.source))
    if not names or any(name not in SOURCES for name in names):
        _print({"ok": False, "error": {"code": "invalid_source"}})
        return 2
    if not 1 <= args.page_limit <= 500 or not 1 <= args.max_pages <= 1000:
        _print({"ok": False, "error": {"code": "invalid_limit"}})
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib"))
    lock_path = ledger.parent / (ledger.name + ".ingest.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _print({"ok": False, "error": {"code": "ingest_running"}})
            return 3
        report = ingest(str(ledger), str(state), names, page_limit=args.page_limit, max_pages=args.max_pages)
    finally:
        os.close(fd)
    ok = all(entry["ok"] for entry in report.values())
    _print({"ok": ok, "sources": report})
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
