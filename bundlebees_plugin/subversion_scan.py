from __future__ import annotations

import ast
import re
from typing import TypedDict


class Finding(TypedDict):
    kind: str
    path: str
    line: int


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_PYTEST_PLUGINS_RE = re.compile(r"\bpytest_plugins\s*(?::[^=\n]+)?=")
_ADDOPTS_RE = re.compile(r"\baddopts\s*=")
_PLUGIN_OPTION_RE = re.compile(r"(?:^|[\s\"'])(?:-p(?:\s|=|[A-Za-z_])|--plugin(?:\s|=))")
_CONTENT_PATTERNS = (
    ("forced_exit", re.compile(r"\bos\s*\.\s*_exit\s*\(")),
    ("atexit", re.compile(r"\batexit\s*\.\s*register\s*\(")),
    ("pytest_hook", re.compile(r"^\s*(?:async\s+)?def\s+pytest_[A-Za-z0-9_]*\s*\(")),
    ("patched_assertion", re.compile(r"\b(?:builtins\s*\.\s*)?AssertionError\s*=")),
    ("patched_assertion", re.compile(r"\bunittest\s*\.\s*TestCase\s*\.\s*assert[A-Za-z0-9_]*\s*=")),
)
_PYTEST_CONFIG_NAMES = frozenset({"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"})


def _new_path(header: str) -> str | None:
    value = header[4:].strip()
    if value == "/dev/null":
        return None
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            decoded = value[1:-1]
        if isinstance(decoded, str):
            value = decoded
    value = value.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    return value[2:] if value.startswith("b/") else value


def _path_kinds(path: str) -> tuple[str, ...]:
    name = path.rsplit("/", 1)[-1]
    kinds = []
    if name == "conftest.py":
        kinds.append("conftest")
    if name in {"sitecustomize.py", "usercustomize.py"}:
        kinds.append("startup_hook")
    if path.endswith(".pth"):
        kinds.append("pth_file")
    return tuple(kinds)


def _loads_pytest_plugin(path: str, line: str) -> bool:
    if _PYTEST_PLUGINS_RE.search(line):
        return True
    name = path.rsplit("/", 1)[-1]
    return (
        name in _PYTEST_CONFIG_NAMES
        and _ADDOPTS_RE.search(line) is not None
        and _PLUGIN_OPTION_RE.search(line) is not None
    )


def _hunk_start(diff_line: str) -> int | None:
    match = _HUNK_RE.match(diff_line)
    return int(match.group(1)) if match else None


def _header_state(
    diff_line: str, path: str | None, in_hunk: bool, new_line: int
) -> tuple[bool, str | None, bool, int]:
    """Apply one diff header line; return ``(handled, path, in_hunk, new_line)``."""
    if diff_line.startswith("diff --git "):
        return True, None, False, new_line
    if not in_hunk and diff_line.startswith("+++ "):
        return True, _new_path(diff_line), in_hunk, new_line
    start = _hunk_start(diff_line)
    if start is not None:
        return True, path, True, start
    return False, path, in_hunk, new_line


def _body_step(
    diff_line: str, in_hunk: bool, path: str | None, new_line: int
) -> tuple[bool, int, tuple[str, int] | None]:
    """Advance one hunk body line; return ``(in_hunk, new_line, added)``.

    ``added`` is ``(content, line)`` when the line was added, else ``None``.
    """
    if not in_hunk or path is None or not diff_line:
        return in_hunk, new_line, None
    marker = diff_line[0]
    if marker == "+":
        return in_hunk, new_line + 1, (diff_line[1:], new_line)
    if marker == " ":
        return in_hunk, new_line + 1, None
    if marker == "-":
        return in_hunk, new_line, None
    return False, new_line, None


def _added_line_findings(path: str, content: str, line: int) -> list[Finding]:
    """Return every finding for one added line of *path* at new-file line *line*."""
    findings = [Finding(kind=kind, path=path, line=line) for kind in _path_kinds(path)]
    if _loads_pytest_plugin(path, content):
        findings.append(Finding(kind="pytest_plugin", path=path, line=line))
    for kind, pattern in _CONTENT_PATTERNS:
        if pattern.search(content):
            findings.append(Finding(kind=kind, path=path, line=line))
    return findings


def scan_patch(patch: str) -> list[Finding]:
    findings: list[Finding] = []
    path: str | None = None
    new_line = 0
    in_hunk = False
    for diff_line in patch.splitlines():
        handled, path, in_hunk, new_line = _header_state(diff_line, path, in_hunk, new_line)
        if handled:
            continue
        in_hunk, new_line, added = _body_step(diff_line, in_hunk, path, new_line)
        if added is not None and path is not None:
            findings.extend(_added_line_findings(path, *added))
    return findings
