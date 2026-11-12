"""Command line interface for the standalone ACDD contract and evidence linter.

``python3 -m acdd_lint [PATH ...]`` walks each path, finds ``acdd/1``
contracts and ``tdd-evidence/1`` records, validates them against the shared
schemas, runs the protocol checks and prints a text or JSON report.  Only the
standard library is used so the tool runs when copied into another repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from acdd_lint import __version__, contracts, evidence, schema

Problem = dict[str, str]
Contract = tuple[str, Mapping[str, Any]]
Record = tuple[str, Mapping[str, Any]]

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
MAX_FILE_BYTES = 1024 * 1024
CONTRACT_SUFFIX = ".contract.json"
EVIDENCE_DIR = "evidence"
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _problem(code: str, file: str, where: str, message: str) -> Problem:
    """Build one report problem."""
    return {"code": code, "file": file, "where": where, "message": message}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="acdd-lint",
        description="Check acdd/1 contracts and tdd-evidence/1 records.",
    )
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories to scan")
    parser.add_argument("--schemas", metavar="DIR", help="directory holding the shared schemas")
    parser.add_argument("--key-file", metavar="FILE", help="32 raw bytes or 64 hex characters")
    parser.add_argument("--json", action="store_true", dest="json_output", help="print a JSON report")
    return parser.parse_args(argv)


def _load_key(path: Path) -> bytes:
    """Load and validate an HMAC key file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise schema.LintError(f"cannot read key file {path}: {exc.strerror or exc}") from exc
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        text = ""
    if len(text) == 64 and all(char in HEX_DIGITS for char in text):
        return bytes.fromhex(text)
    if len(raw) == 32:
        return raw
    raise schema.LintError("key file must contain 32 raw bytes or 64 hex characters")


def _tool_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_schemas(directory: str | None, paths: Sequence[Path]) -> tuple[dict, dict]:
    """Load the shared schemas from an explicit directory or by discovery."""
    if directory is not None:
        return schema.load_schemas(Path(directory))
    found = schema.find_schema_dir(paths[0]) if paths else None
    if found is None:
        found = schema.find_schema_dir(_tool_dir())
    if found is None:
        raise schema.LintError("cannot find the shared schemas; pass --schemas DIR")
    return schema.load_schemas(found)


def _eligible(path: Path) -> bool:
    """True for regular, non-symlinked ``*.json`` files of at most 1 MiB."""
    if not path.name.endswith(".json") or os.path.islink(path):
        return False
    try:
        return path.is_file() and path.stat().st_size <= MAX_FILE_BYTES
    except OSError:
        return False


def _walk(root: Path) -> Iterator[Path]:
    """Yield eligible JSON files under *root* in sorted order."""
    if root.is_file():
        if _eligible(root):
            yield root
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIRS and not os.path.islink(os.path.join(directory, name))
        )
        for name in sorted(filenames):
            candidate = Path(directory) / name
            if _eligible(candidate):
                yield candidate


def _relative(path: Path, root: Path) -> str:
    try:
        text = os.path.relpath(path, root)
    except ValueError:
        text = path.name
    return text.replace(os.sep, "/")


def _under_evidence(relative: str, path: Path) -> bool:
    parts = Path(relative).parts[:-1]
    if EVIDENCE_DIR in parts:
        return True
    return any(parent.name == EVIDENCE_DIR for parent in path.parents)


def _read_object(path: Path) -> tuple[Mapping[str, Any] | None, str | None]:
    """Parse *path* and return (object or None, error or None)."""
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        return None, str(exc) or exc.__class__.__name__
    if isinstance(data, Mapping):
        return data, None
    return None, None


def _validate(
    obj: Mapping[str, Any],
    contract_schema: Mapping[str, Any],
    evidence_schema: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any], list[tuple[str, str]]]:
    if "contract_version" in obj:
        return "contract", obj, schema.validate(obj, contract_schema)
    if "evidence_version" in obj:
        return "evidence", obj, schema.validate(obj, evidence_schema)
    return "other", obj, []


def _scan(
    roots: Sequence[Path],
    contract_schema: Mapping[str, Any],
    evidence_schema: Mapping[str, Any],
) -> tuple[list[Contract], list[Record], list[Problem]]:
    """Discover contracts, evidence records and file-level problems."""
    found_contracts: list[Contract] = []
    found_records: list[Record] = []
    problems: list[Problem] = []

    for root in roots:
        for path in _walk(root):
            relative = _relative(path, root)
            obj, error = _read_object(path)
            if error is not None:
                if relative.endswith(CONTRACT_SUFFIX) or _under_evidence(relative, path):
                    problems.append(
                        _problem(
                            "json_invalid",
                            relative,
                            "$",
                            f"cannot parse JSON: {error}; fix the syntax or remove the file",
                        )
                    )
                continue
            if obj is None:
                continue
            kind, body, errors = _validate(obj, contract_schema, evidence_schema)
            if kind == "contract":
                found_contracts.append((relative, body))
                code = "contract_invalid"
            elif kind == "evidence":
                found_records.append((relative, body))
                code = "evidence_invalid"
            else:
                continue
            for where, message in errors:
                problems.append(_problem(code, relative, where, message))

    return found_contracts, found_records, problems


def _contract_map(found: Sequence[Contract]) -> tuple[dict[str, Mapping[str, Any]], list[Problem]]:
    """Map contract ids to their first definition and flag duplicates."""
    mapping: dict[str, Mapping[str, Any]] = {}
    problems: list[Problem] = []
    for relative, contract in found:
        contract_id = contract.get("id")
        if not isinstance(contract_id, str) or not contract_id:
            continue
        if contract_id in mapping:
            problems.append(
                _problem(
                    "contract_duplicate",
                    relative,
                    "id",
                    f"contract id {contract_id!r} is already defined in another file; "
                    "give every contract a unique id",
                )
            )
            continue
        mapping[contract_id] = contract
    return mapping, problems


def _split_where(where: str, items: Sequence[Record]) -> tuple[str, str]:
    """Split a history problem's location into (file, local where)."""
    for relative, _ in sorted(items, key=lambda item: len(item[0]), reverse=True):
        if where == relative:
            return relative, "record"
        if where.startswith(relative + ":"):
            rest = where[len(relative):].lstrip()
            rest = rest[1:].strip() if rest.startswith(":") else rest
            return relative, rest or "record"
    if items:
        return items[0][0], where
    return "", where


def _check_protocol(
    found_contracts: Sequence[Contract],
    found_records: Sequence[Record],
    key: bytes | None,
) -> list[Problem]:
    """Run the contract and evidence protocol checks."""
    problems: list[Problem] = []
    mapping, duplicates = _contract_map(found_contracts)
    problems.extend(duplicates)

    for relative, contract in found_contracts:
        for item in contracts.check_contract(contract):
            problems.append(_problem(item["code"], relative, item["where"], item["message"]))

    groups: dict[Any, list[Record]] = {}
    order: list[Any] = []
    for relative, record in found_records:
        contract_id = record.get("contract_id")
        bucket = contract_id if isinstance(contract_id, str) else None
        if bucket not in groups:
            groups[bucket] = []
            order.append(bucket)
        groups[bucket].append((relative, record))

    for bucket in order:
        items = groups[bucket]
        contract = mapping.get(bucket) if bucket is not None else None
        for item in evidence.check_history(contract, items, key):
            relative, where = _split_where(item["where"], items)
            problems.append(_problem(item["code"], relative, where, item["message"]))

    return problems


def _summary(contracts_count: int, evidence_count: int, problems_count: int) -> str:
    contract_word = "contract" if contracts_count == 1 else "contracts"
    record_word = "evidence record" if evidence_count == 1 else "evidence records"
    problem_word = "problem" if problems_count == 1 else "problems"
    return (
        f"{contracts_count} {contract_word}, {evidence_count} {record_word}, "
        f"{problems_count} {problem_word}"
    )


def _print_text(
    contracts_count: int,
    evidence_count: int,
    problems: Sequence[Problem],
    checked: bool,
) -> None:
    lines = [f"acdd-lint {__version__}"]
    for item in problems:
        lines.append(f"{item['file']}: {item['code']}: {item['where']}: {item['message']}")
    lines.append(_summary(contracts_count, evidence_count, len(problems)))
    lines.append("signatures: checked" if checked else "signatures: not checked (pass --key-file)")
    print("\n".join(lines))


def _print_json(
    contracts_count: int,
    evidence_count: int,
    problems: Sequence[Problem],
    checked: bool,
) -> None:
    report = {
        "ok": not problems,
        "contracts": contracts_count,
        "evidence": evidence_count,
        "problems": list(problems),
        "unrun": [] if checked else ["signatures"],
    }
    print(json.dumps(report))


def main(argv: list[str] | None = None) -> int:
    """Run the linter and return 0 (clean), 1 (problems) or 2 (usage)."""
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        paths = [Path(item) for item in args.paths]
        missing = [path for path in paths if not path.exists()]
        if missing:
            raise schema.LintError(f"path does not exist: {missing[0]}")
        key = _load_key(Path(args.key_file)) if args.key_file else None
        contract_schema, evidence_schema = _load_schemas(args.schemas, paths)
    except schema.LintError as exc:
        print(f"acdd-lint: error: {exc}", file=sys.stderr)
        return 2

    found_contracts, found_records, problems = _scan(paths, contract_schema, evidence_schema)
    problems.extend(_check_protocol(found_contracts, found_records, key))

    if args.json_output:
        _print_json(len(found_contracts), len(found_records), problems, key is not None)
    else:
        _print_text(len(found_contracts), len(found_records), problems, key is not None)
    return 0 if not problems else 1
