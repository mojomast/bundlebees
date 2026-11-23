"""Command line interface for slop-lint."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from slop_lint import baseline, engine

SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", ".venv"})
SOURCE_SUFFIX = ".py"


class UsageError(Exception):
    """Raised for an unusable command line or input path."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slop-lint", description="Scan Python source for slop and ratchet it.")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="fail on findings above the baseline")
    check.add_argument("paths", nargs="+", metavar="PATH")
    check.add_argument("--baseline", required=True, metavar="FILE")
    check.add_argument("--report-only", action="store_true", help="print findings but never fail on them")
    check.add_argument(
        "--pass-on-unpruned",
        action="store_true",
        help="do not fail when the baseline allows more findings than now occur",
    )

    write = commands.add_parser("baseline", help="write a baseline from the current tree")
    write.add_argument("paths", nargs="+", metavar="PATH")
    write.add_argument("--write", required=True, metavar="FILE")

    prune = commands.add_parser("prune", help="drop stale baseline counts without raising any")
    prune.add_argument("paths", nargs="+", metavar="PATH")
    prune.add_argument("--baseline", required=True, metavar="FILE")
    return parser


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    return _parser().parse_args(argv)


def _relative(path: Path) -> str:
    try:
        text = os.path.relpath(os.fspath(path), os.fspath(Path.cwd()))
    except ValueError:
        text = os.fspath(path)
    return text.replace(os.sep, "/")


def _walk(root: Path):
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and name not in SKIP_DIRS
            and not os.path.islink(os.path.join(directory, name))
        )
        for name in sorted(filenames):
            if name.endswith(SOURCE_SUFFIX):
                yield Path(directory) / name


def _collect(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        root = Path(item)
        if not root.exists():
            raise UsageError(f"path does not exist: {item}")
        if root.is_dir():
            candidates = _walk(root)
        elif root.is_file():
            candidates = [root]
        else:
            raise UsageError(f"path is not a file or directory: {item}")
        for candidate in candidates:
            if not candidate.name.endswith(SOURCE_SUFFIX) or not candidate.is_file():
                continue
            key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
    found.sort(key=_relative)
    return found


def _read(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from exc
    text = raw.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def _scan(paths: list[str]) -> list:
    findings = []
    for path in _collect(paths):
        findings.extend(engine.scan_source(_read(path), _relative(path)))
    findings.sort(key=lambda finding: (finding.path, finding.line, finding.rule, finding.message))
    return findings


def _check(args: argparse.Namespace) -> int:
    try:
        allowed = baseline.load(args.baseline)
    except baseline.BaselineError as exc:
        print(f"slop-lint: error: {exc}", file=sys.stderr)
        return 2

    findings = _scan(args.paths)
    current = baseline.counts(findings)
    result = baseline.compare(current, allowed)
    _print_new(findings, result)

    if result.fixed:
        for path, rule in result.fixed:
            print(
                f"slop-lint: stale baseline entry: {path}: {rule}: "
                f"allowed {allowed[path][rule]}, found {current.get(path, {}).get(rule, 0)}",
                file=sys.stderr,
            )

    failed = False
    if result.new and not args.report_only:
        failed = True
    if result.fixed and not args.pass_on_unpruned:
        failed = True
    return 1 if failed else 0


def _print_new(findings: list, result) -> None:
    allowances = {(path, rule): count for path, rule, _, count in result.new}
    grouped: dict[tuple[str, str], list] = {}
    for finding in findings:
        key = (finding.path, finding.rule)
        if key in allowances:
            grouped.setdefault(key, []).append(finding)
    selected = []
    for key, count in allowances.items():
        selected.extend(grouped.get(key, [])[count:])
    selected.sort(key=lambda finding: (finding.path, finding.line, finding.rule, finding.message))
    for finding in selected:
        print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.message}")


def _write_baseline(args: argparse.Namespace) -> int:
    findings = _scan(args.paths)
    try:
        baseline.dump(baseline.counts(findings), args.write)
    except baseline.BaselineError as exc:
        print(f"slop-lint: error: {exc}", file=sys.stderr)
        return 2
    return 0


def _prune(args: argparse.Namespace) -> int:
    try:
        allowed = baseline.load(args.baseline)
    except baseline.BaselineError as exc:
        print(f"slop-lint: error: {exc}", file=sys.stderr)
        return 2

    current = baseline.counts(_scan(args.paths))
    pruned: dict[str, dict[str, int]] = {}
    for path in sorted(allowed):
        for rule in sorted(allowed[path]):
            found = current.get(path, {}).get(rule, 0)
            if found > 0:
                pruned.setdefault(path, {})[rule] = min(allowed[path][rule], found)
    try:
        baseline.dump(pruned, args.baseline)
    except baseline.BaselineError as exc:
        print(f"slop-lint: error: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run slop-lint and return 0 (clean), 1 (findings or stale) or 2 (usage)."""
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command == "check":
            return _check(args)
        if args.command == "baseline":
            return _write_baseline(args)
        return _prune(args)
    except UsageError as exc:
        print(f"slop-lint: error: {exc}", file=sys.stderr)
        return 2
