"""Advisory per-change rework metrics; never raise on bad input."""

from __future__ import annotations

import datetime as _dt
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_DEFAULT_WINDOW_DAYS = 14
_GIT_TIMEOUT_SECONDS = 30
_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"


def _entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        return [value]
    try:
        return list(value)
    except Exception:  # noqa: BLE001 - advisory metrics must not raise
        return [value]


def _parse_date(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _iso_z(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _window(window_days: Any) -> _dt.timedelta:
    try:
        return _dt.timedelta(days=float(window_days))
    except (TypeError, ValueError, OverflowError):
        return _dt.timedelta(days=_DEFAULT_WINDOW_DAYS)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _line_pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    added, removed = value
    if not _is_count(added) or not _is_count(removed):
        return None
    return added, removed


def _churn(commits: Any, window_days: Any = _DEFAULT_WINDOW_DAYS) -> tuple[dict[str, int], int]:
    skipped = 0
    normalized: list[tuple[_dt.datetime, str, dict[str, tuple[int, int]]]] = []
    for entry in _entries(commits):
        if not isinstance(entry, Mapping):
            skipped += 1
            continue
        sha = entry.get("sha")
        if not isinstance(sha, str) or not sha:
            skipped += 1
            continue
        date = _parse_date(entry.get("date"))
        if date is None:
            skipped += 1
            continue
        files = entry.get("files")
        if not isinstance(files, Mapping):
            skipped += 1
            continue
        parsed_files: dict[str, tuple[int, int]] = {}
        for path, value in files.items():
            if not isinstance(path, str) or not path:
                skipped += 1
                continue
            pair = _line_pair(value)
            if pair is None:
                skipped += 1
                continue
            parsed_files[path] = pair
        normalized.append((date, sha, parsed_files))

    normalized.sort(key=lambda item: (item[0], item[1]))
    window = _window(window_days)
    lines_added = 0
    lines_removed = 0
    reedited_lines = 0
    last_touch: dict[str, _dt.datetime] = {}
    for date, _sha, files in normalized:
        for path, (added, removed) in files.items():
            lines_added += added
            lines_removed += removed
            previous = last_touch.get(path)
            if previous is not None and date - previous <= window:
                reedited_lines += added + removed
            last_touch[path] = date

    return {
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "reedited_lines": reedited_lines,
    }, skipped


def churn(commits: Any, window_days: Any = _DEFAULT_WINDOW_DAYS) -> dict[str, int]:
    return _churn(commits, window_days)[0]


def _retry_chains(events: Any) -> tuple[dict[str, int], int]:
    skipped = 0
    attempts: set[str] = set()
    retries = 0
    parents: dict[str, str | None] = {}
    for entry in _entries(events):
        if not isinstance(entry, Mapping):
            skipped += 1
            continue
        attempt_id = entry.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            skipped += 1
            continue
        retry_of = entry.get("retry_of")
        if retry_of is not None and (not isinstance(retry_of, str) or not retry_of):
            skipped += 1
            continue
        attempts.add(attempt_id)
        if retry_of:
            retries += 1
            parents[attempt_id] = retry_of
        else:
            parents.setdefault(attempt_id, None)

    max_chain = 0
    for start in attempts:
        seen: set[str] = set()
        current: str | None = start
        length = 0
        while current is not None and current in attempts and current not in seen:
            seen.add(current)
            length += 1
            current = parents.get(current)
        if length > max_chain:
            max_chain = length

    return {"attempts": len(attempts), "retries": retries, "max_chain": max_chain}, skipped


def retry_chains(events: Any) -> dict[str, int]:
    return _retry_chains(events)[0]


def _review_findings(records: Any) -> tuple[dict[str, int], int]:
    skipped = 0
    counts: dict[str, int] = {}
    for entry in _entries(records):
        if not isinstance(entry, Mapping):
            skipped += 1
            continue
        contract_id = entry.get("contract_id")
        phase = entry.get("phase")
        classification = entry.get("classification")
        if (
            not isinstance(contract_id, str)
            or not contract_id
            or not isinstance(phase, str)
            or not isinstance(classification, str)
        ):
            skipped += 1
            continue
        if phase == "review" and classification == "failed":
            counts[contract_id] = counts.get(contract_id, 0) + 1
    return {key: counts[key] for key in sorted(counts)}, skipped


def review_findings(records: Any) -> dict[str, int]:
    return _review_findings(records)[0]


def _parse_git_log(output: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in output.split("\n"):
        if not line:
            continue
        if line.startswith(_RECORD_SEPARATOR):
            sha, separator, date_text = line[1:].partition(_FIELD_SEPARATOR)
            current = None
            if not separator or not sha:
                continue
            date = _parse_date(date_text)
            if date is None:
                continue
            current = {"sha": sha, "date": _iso_z(date), "files": {}}
            commits.append(current)
            continue
        if current is None:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_text, removed_text, path = parts[0], parts[1], "\t".join(parts[2:])
        try:
            added = 0 if added_text == "-" else int(added_text)
            removed = 0 if removed_text == "-" else int(removed_text)
        except ValueError:
            continue
        if not _is_count(added) or not _is_count(removed) or not path:
            continue
        current["files"][path] = (added, removed)
    return commits


def collect_git(repo_path: Any, rev_range: Any = None) -> list[dict[str, Any]]:
    try:
        path = Path(repo_path)
    except (TypeError, ValueError):
        return []
    args = [
        "git",
        "-C",
        str(path),
        "log",
        "--numstat",
        f"--format={_RECORD_SEPARATOR}%H{_FIELD_SEPARATOR}%aI",
    ]
    if rev_range is not None:
        if not isinstance(rev_range, str) or not rev_range:
            return []
        args.append(rev_range)
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return _parse_git_log(completed.stdout)


def summarize(commits: Any, events: Any, evidence: Any) -> dict[str, int]:
    churn_result, commits_skipped = _churn(commits)
    retry_result, events_skipped = _retry_chains(events)
    findings, evidence_skipped = _review_findings(evidence)
    return {
        "lines_added": churn_result["lines_added"],
        "lines_removed": churn_result["lines_removed"],
        "reedited_lines": churn_result["reedited_lines"],
        "attempts": retry_result["attempts"],
        "retries": retry_result["retries"],
        "max_chain": retry_result["max_chain"],
        "review_findings": sum(findings.values()),
        "skipped": commits_skipped + events_skipped + evidence_skipped,
    }
