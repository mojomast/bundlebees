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

``launcher`` is the read-only worker export. Because the runner may start
outside a Hermes session it takes the URL, origin chain and read credential
file explicitly (``--launcher-url``, ``--launcher-chain``,
``--rails-token-file`` and the optional ``--allow-private``). A missing or
malformed launcher configuration is refused before any ledger is created.
The launcher's committed cursor is per origin chain.

``launcher-rails`` reads that same export bind-mount strictly: it stages every
page of the pending range first and admits the range only after the last page
has been read and the ledger's own validator has accepted every record. A page
error, malformed record or schema mismatch therefore aborts the run with a
non-zero exit and leaves the ledger and its cursor exactly as they were. Use
it for bounded admission (worker ``usage.observed`` receipts); use ``launcher``
for a large backfill, whose committed pages survive a later page failure and
resume from the per-chain cursor.

Exit codes: 0 every source imported, 2 configuration refused, 3 a source failed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import sys
import urllib.parse
from contextlib import closing
from pathlib import Path

SOURCES = ("bundlebees", "outbox", "queue", "launcher", "launcher-rails")
_LAUNCHER_SOURCES = ("launcher", "launcher-rails")
_CODE_RE = re.compile(r"^[a-z_]{1,48}$")
_MAX_ACCESS_BYTES = 514


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _PageFailure(Exception):
    """A staged page could not be read; keep where it failed and why."""

    def __init__(self, cursor: int, cause: BaseException) -> None:
        super().__init__("page read failed")
        self.cursor = int(cursor)
        self.cause = cause


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


def _last_identity(led, adapter: str, instance: str):
    """The dedupe identity of the newest record this ledger consumed from a source."""
    rows = led.query(
        "select envelope_json from event_rows"
        " where json_extract(envelope_json,'$.source.adapter')=?"
        " and json_extract(envelope_json,'$.source.instance')=? order by seq desc limit 1",
        (adapter, instance))
    if not rows:
        return None
    try:
        key = json.loads(rows[0]["envelope_json"]).get("dedupe_key")
    except (TypeError, ValueError):
        return None
    return "r1." + key if isinstance(key, str) and key else None


def _rebase_legacy_cursor(led, importer, source, exc) -> bool:
    """Move a stale positional cursor onto the last record the ledger consumed.

    A directory snapshot is ordered by file mtime, so rewriting one record moves
    it and invalidates every positional hash after it for good -- the retry can
    never validate that cursor again. The ledger still knows which record it
    consumed last: while that identity survives in the snapshot, one empty
    checkpoint moves the cursor onto it (no rewind, no skipped row, no second
    dedupe layer). A genuinely lost record keeps raising ``source_gap``.
    """
    if _code(exc) != "source_gap" or not getattr(source, "_identity_cursors", False):
        return False
    position = importer.position(source.adapter_id, source.instance)
    cursor = position["cursor"]
    if not cursor or cursor.startswith("r1."):
        return False
    identity = _last_identity(led, source.adapter_id, source.instance)
    if identity is not None:
        try:
            source.read_batch(identity, 1, 1 << 20)
        except Exception:  # noqa: BLE001 - the identity is gone: a real gap
            return False
    importer.commit_batch(source.adapter_id, source.instance, [], expected_cursor=cursor,
                          next_cursor=identity, expected_page=position["page"])
    return True


def _local_pages(name: str, ledger: str, state: str, page_limit: int, max_pages: int) -> dict:
    from rizzstack_rails import RailsLedger
    from rizzstack_rails.importer import Importer

    source, authorities = _source(name, state)
    imported, has_more, rebased = 0, False, False
    with closing(RailsLedger(ledger, allowed_authorities=authorities)) as led:
        importer = Importer(led, authorities=authorities)
        for _ in range(max_pages):
            try:
                result = importer.import_once(source, limit=page_limit)
            except Exception as exc:  # noqa: BLE001 - only a stale legacy cursor is recoverable
                if not _rebase_legacy_cursor(led, importer, source, exc):
                    raise
                rebased = True
                result = importer.import_once(source, limit=page_limit)
            imported += int(result["imported"])
            has_more = bool(result["has_more"])
            if not has_more:
                break
    report = {"ok": True, "imported": imported, "has_more": has_more}
    if rebased:
        report["rebased"] = True
    return report


def _launcher_page(led, importer, source, chain: str, page_limit: int) -> tuple[int, bool]:
    pos = led.import_cursor(chain)
    if pos["state"] != "active":
        raise _Refused("source_fork")
    report = source.import_into(importer, chain, cursor=pos["next_seq"] - 1, limit=page_limit)
    return int(report["ledger"].get("imported", 0)), bool(report["has_more"])


def _launcher_pages(ledger: str, source, chain: str, page_limit: int, max_pages: int) -> dict:
    from rizzstack_rails import RailsLedger
    from rizzstack_rails.importer import Importer

    authorities = {"imported_observation", "worker_report"}
    imported, has_more = 0, False
    with closing(RailsLedger(ledger, allowed_authorities=authorities)) as led:
        importer = Importer(led, authorities=authorities)
        for _ in range(max_pages):
            page_imported, has_more = _launcher_page(led, importer, source, chain, page_limit)
            imported += page_imported
            if not has_more:
                break
    return {"ok": True, "imported": imported, "has_more": has_more}


def _transport_detail(exc: BaseException) -> str:
    """A scrubbed one-line transport label; never a path or raw record."""
    from rizzstack_rails import privacy

    code = getattr(exc, "code", None)
    label = code if isinstance(code, str) and code else type(exc).__name__
    status = getattr(exc, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        label += " " + str(status)
    message = privacy.scrub_text(str(exc))[:160]
    return label + (": " + message if message and message != label else "")


def _next_cursor(report, cursor: int) -> int:
    following = report.get("next_cursor")
    if isinstance(following, bool) or not isinstance(following, int) or following <= cursor:
        raise _PageFailure(cursor, _Refused("peer_protocol"))
    return following


def _stage_launcher_pages(source, chain: str, cursor: int, page_limit: int,
                          max_pages: int) -> list[list[str]]:
    """Read every pending page without touching the ledger."""
    staged: list[list[str]] = []
    for _ in range(max_pages):
        try:
            report = source.fetch(chain, after=cursor, limit=page_limit)
        except Exception as exc:  # noqa: BLE001 - re-raised with the failing cursor
            raise _PageFailure(cursor, exc) from None
        gaps = report.get("gaps") or []
        if gaps:
            from .rails_remote_source import ExportGapDetected

            raise _PageFailure(
                cursor, ExportGapDetected("peer export has a cursor gap", gaps, report=report)
            )
        records = list(report.get("events") or [])
        if records:
            staged.append(records)
        if not report.get("has_more"):
            return staged
        cursor = _next_cursor(report, cursor)
    raise _PageFailure(cursor, _Refused("max_pages_exceeded"))


def _refuse_uncommittable_stage(staged: list[list[str]]) -> None:
    """Refuse before the first write if a page cannot be committed as one page."""
    from rizzstack_rails import contracts

    for page in staged:
        if not 1 <= len(page) <= contracts.MAX_RECORDS:
            raise _Refused("page_size")
        total = 0
        for text in page:
            if not isinstance(text, str) or not text.isascii() or len(text) > contracts.MAX_RECORD:
                raise _Refused("origin_bytes")
            total += len(text)
        if total > contracts.MAX_PAGE:
            raise _Refused("page_size")


def _preflight_origin_page(led, chain: str, records: list[str]) -> None:
    """Prove the whole staged range admissible with the ledger's own validator.

    ``import_origin_page`` commits one page atomically, so a rejection on a
    later page cannot be undone. Running the same validator over the whole
    range first is what makes the strict source all-or-nothing; without it the
    run refuses rather than writing a partial range.
    """
    validator = getattr(led, "_validate_origin_pages", None)
    connection = getattr(led, "_conn", None)
    if not callable(validator) or connection is None:
        raise _Refused("ledger_validation_unavailable")
    outcome = validator(connection, chain, records)
    if not isinstance(outcome, tuple):
        raise _Refused("source_fork")


def _launcher_rails_pages(ledger: str, source, chain: str, page_limit: int,
                          max_pages: int) -> dict:
    """Stage the pending range, validate it, then admit it as one run."""
    from rizzstack_rails import RailsLedger

    authorities = {"imported_observation", "worker_report"}
    with closing(RailsLedger(ledger, allowed_authorities=authorities)) as led:
        position = led.import_cursor(chain)
        if position["state"] != "active":
            raise _Refused("source_fork")
        staged = _stage_launcher_pages(
            source, chain, position["next_seq"] - 1, page_limit, max_pages
        )
        records = [text for page in staged for text in page]
        if not records:
            return {"ok": True, "imported": 0, "has_more": False}
        _refuse_uncommittable_stage(staged)
        _preflight_origin_page(led, chain, records)
        imported = 0
        for page in staged:
            imported += int(led.import_origin_page(chain, page).get("imported", 0))
    return {"ok": True, "imported": imported, "has_more": False}


def ingest(ledger: str, state: str, sources, *, page_limit: int = 200, max_pages: int = 50,
           launcher_source=None, launcher_chain: str | None = None) -> dict:
    report: dict = {}
    for name in sources:
        try:
            if name in _LAUNCHER_SOURCES:
                if launcher_source is None or launcher_chain is None:
                    raise _Refused("not_configured")
                if name == "launcher-rails":
                    report[name] = _launcher_rails_pages(
                        ledger, launcher_source, launcher_chain, page_limit, max_pages
                    )
                else:
                    report[name] = _launcher_pages(
                        ledger, launcher_source, launcher_chain, page_limit, max_pages
                    )
            else:
                report[name] = _local_pages(name, ledger, state, page_limit, max_pages)
        except _PageFailure as failure:
            report[name] = {
                "ok": False,
                "error": {
                    "code": _code(failure.cause),
                    "cursor": failure.cursor,
                    "transport": _transport_detail(failure.cause),
                },
            }
        except Exception as exc:  # noqa: BLE001 - reported as a generic code only
            report[name] = {"ok": False, "error": {"code": _code(exc)}}
    return report


def _valid_launcher_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in ("http", "https")
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and not any(ch.isspace() for ch in value)
    )


def _read_access(path: str) -> str:
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.R_OK):
            raise _Refused("not_configured")
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_ACCESS_BYTES + 1)
    except _Refused:
        raise
    except OSError:
        raise _Refused("not_configured") from None
    if len(raw) > _MAX_ACCESS_BYTES:
        raise _Refused("not_configured")
    raw = raw.rstrip(b"\r\n")
    if not 32 <= len(raw) <= 512:
        raise _Refused("not_configured")
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        raise _Refused("not_configured") from None
    if any(not 0x21 <= ord(ch) <= 0x7E for ch in value):
        raise _Refused("not_configured")
    return value


def _launcher_source(url: str, token_provider, allow_private: bool):
    from .rails_remote_source import RemoteRailSource

    return RemoteRailSource(
        base_url=url,
        peer_id="worker-1",
        env={},
        config={"launcher_url": url, "allow_private": allow_private},
        token_provider=token_provider,
        max_response_bytes=1 << 20,
    )


def _launcher_settings(args):
    if not all(isinstance(value, str) and value for value in
               (args.launcher_url, args.launcher_chain, args.rails_token_file)):
        raise _Refused("not_configured")
    if not _valid_launcher_url(args.launcher_url):
        raise _Refused("not_configured")
    from rizzstack_rails.source import token

    try:
        chain = token(args.launcher_chain)
    except Exception:
        raise _Refused("not_configured") from None
    material = _read_access(args.rails_token_file)
    source = _launcher_source(args.launcher_url, lambda: material, bool(args.allow_private))
    return source, chain


def _print(doc: dict) -> None:
    print(json.dumps(doc, sort_keys=True))


def _target(args):
    ledger, state = Path(args.ledger), Path(args.state_dir)
    if not ledger.is_absolute() or not state.is_absolute() or not state.is_dir() or not ledger.parent.is_dir():
        raise _Refused("not_configured")
    names = list(dict.fromkeys(args.source))
    if not names or any(name not in SOURCES for name in names):
        raise _Refused("invalid_source")
    if not 1 <= args.page_limit <= 500 or not 1 <= args.max_pages <= 1000:
        raise _Refused("invalid_limit")
    return ledger, state, names


def _run_sources(ledger, state, names, args, launcher_source, launcher_chain) -> int:
    lock_path = ledger.parent / (ledger.name + ".ingest.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _print({"ok": False, "error": {"code": "ingest_running"}})
            return 3
        report = ingest(str(ledger), str(state), names, page_limit=args.page_limit,
                        max_pages=args.max_pages, launcher_source=launcher_source,
                        launcher_chain=launcher_chain)
    finally:
        os.close(fd)
    ok = all(entry["ok"] for entry in report.values())
    _print({"ok": ok, "sources": report})
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import coordinator records into a local Rails ledger.")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--page-limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--launcher-url")
    parser.add_argument("--launcher-chain")
    parser.add_argument("--rails-token-file")
    parser.add_argument("--allow-private", action="store_true")
    args = parser.parse_args(argv)

    try:
        ledger, state, names = _target(args)
    except _Refused as exc:
        _print({"ok": False, "error": {"code": exc.code}})
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib"))
    launcher_source = launcher_chain = None
    if any(name in _LAUNCHER_SOURCES for name in names):
        try:
            launcher_source, launcher_chain = _launcher_settings(args)
        except Exception:
            _print({"ok": False, "error": {"code": "not_configured"}})
            return 2
    return _run_sources(ledger, state, names, args, launcher_source, launcher_chain)


if __name__ == "__main__":
    sys.exit(main())
