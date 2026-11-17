"""Offline suite-evidence preflight for an explicitly supplied waves snapshot."""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from . import revision
from .security import scrub


MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_FINDINGS = 50


def _category(record: object) -> str:
    if not isinstance(record, dict) or type(record.get("accepted")) is not bool:
        return "malformed_record"
    if not record["accepted"]:
        return "not_accepted"
    suite = record.get("suite")
    if revision.suite_passed(suite):  # Same verdict as return_report._waves.
        return "suite_compatible"

    # These labels explain a refusal; none can grant a pass.
    if suite is None:
        return "missing_suite"
    if not isinstance(suite, dict):
        return "malformed_suite"
    if "exit_code" not in suite:
        return "missing_exit_code"
    if type(suite["exit_code"]) is not int:
        return "malformed_exit_code"
    if suite.get("timed_out"):
        return "suite_timed_out"
    if suite.get("blocking"):
        return "suite_blocked"
    if suite["exit_code"] != 0:
        return "nonzero_exit"
    return "suite_refused"


def _inspect_record(directory: int, entry: os.DirEntry) -> str:
    # Open relative to the directory being scanned. Refuse links and special
    # files, even if the entry was swapped since the scan (don't block on a FIFO).
    def opener(name: str, flags: int) -> int:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)

    try:
        if not entry.is_file(follow_symlinks=False):
            return "unreadable_record"
        with open(entry.name, "rb", opener=opener) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return "unreadable_record"
            raw = stream.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return "unreadable_record"
    if len(raw) > MAX_RECORD_BYTES:
        return "oversize_record"
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return "malformed_record"
    return _category(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--waves-dir", metavar="DIR", help="directory of copied wave records")
    group.add_argument("--state-dir", metavar="DIR", help="copied state directory containing waves/")
    args = parser.parse_args(argv)
    root = args.state_dir if args.state_dir is not None else args.waves_dir
    if not root:
        parser.error("expected a non-empty snapshot directory")

    counts: Counter[str] = Counter()
    findings = []
    try:
        if (os.scandir not in os.supports_fd or os.open not in os.supports_dir_fd
                or not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))):
            raise NotImplementedError
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as stack:
            # Drop a trailing slash or '/.' so NOFOLLOW checks the selected directory itself.
            directory = os.open(Path(root), flags)
            stack.callback(os.close, directory)
            if args.state_dir is not None:
                directory = os.open("waves", flags, dir_fd=directory)
                stack.callback(os.close, directory)
            entries = stack.enter_context(os.scandir(directory))
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                wave_id = entry.name[:-5]
                safe_name = revision._safe_wave_id(wave_id)
                category = _inspect_record(directory, entry) if safe_name else "invalid_record_name"
                counts[category] += 1
                if category not in ("suite_compatible", "not_accepted") and len(findings) < MAX_FINDINGS:
                    findings.append({"id": wave_id if safe_name and scrub(wave_id) == wave_id else None,
                                     "category": category})
    except NotImplementedError:
        status = "platform_unsupported"
    except FileNotFoundError:
        status = "directory_missing"
    except OSError:
        status = "directory_unreadable"
    else:
        if counts.total() > counts["not_accepted"] + counts["suite_compatible"]:
            status = "needs_review"
        elif counts["suite_compatible"]:
            status = "suite_compatible"
        else:
            status = "no_accepted_waves"

    print(json.dumps({"status": status, "records": counts.total(), "counts": counts,
                      "findings": findings}, sort_keys=True))
    return 0 if status in ("suite_compatible", "no_accepted_waves") else 1


if __name__ == "__main__":
    raise SystemExit(main())
