from __future__ import annotations

import ast
import re
from typing import TypedDict


class Finding(TypedDict):
    kind: str
    path: str
    line: int


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_GIT_RE = re.compile(
    r'^diff --git ("(?:\\.|[^"\\])*"|\S+) ("(?:\\.|[^"\\])*"|\S+)$'
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PYTEST_PLUGINS_RE = re.compile(r"\bpytest_plugins\s*(?::[^=\n]+)?=")
_ADDOPTS_RE = re.compile(r"\baddopts\s*=")
_PLUGIN_OPTION_RE = re.compile(r"(?:^|[\s\"'])(?:-p(?:\s|=|[A-Za-z_])|--plugin(?:\s|=))")
_PYTEST11_SECTION_RE = re.compile(r"^\s*\[\s*project\.entry-points\.pytest11\s*\]\s*(?:#.*)?$")
_PYTEST11_ENTRY_RE = re.compile(r"^\s*pytest11\s*=")
_CONTENT_PATTERNS = (
    ("forced_exit", re.compile(r"(?:\bfrom\s+os\s+import\s+[^#\n]*\b_exit\b|\b_exit\s*\()")),
    ("atexit", re.compile(r"(?:\batexit\s*\.\s*register\s*\(|\bimport\s+atexit\s+as\s+[A-Za-z_]\w*\b)")),
    ("pytest_hook", re.compile(r"^\s*(?:async\s+)?def\s+pytest_[A-Za-z0-9_]*\s*\(")),
    ("patched_assertion", re.compile(r"\b(?:builtins\s*\.\s*)?AssertionError\s*=(?!=)")),
    ("patched_assertion", re.compile(r"\bunittest\s*\.\s*TestCase\s*\.\s*assert[A-Za-z0-9_]*\s*=(?!=)")),
)
_PYTEST_CONFIG_NAMES = frozenset({"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"})
_CONTENT_SUFFIXES = (".py", ".pyi", ".pth")


def _path_value(value: str) -> str | None:
    value = value.strip()
    if value == "/dev/null":
        return None
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            decoded = value[1:-1]
        if isinstance(decoded, str):
            value = decoded
    value = _CONTROL_CHARS_RE.sub("", value.split("\t", 1)[0])
    if value == "/dev/null":
        return None
    return value


def _new_path(header: str) -> str | None:
    value = _path_value(header[4:])
    if value is None:
        return None
    return value[2:] if value.startswith("b/") else value


def _diff_git_new_path(header: str) -> str | None:
    match = _DIFF_GIT_RE.match(header)
    if match is None:
        return None
    old = _path_value(match.group(1))
    new = _path_value(match.group(2))
    if old is None or new is None:
        return None
    old = old[2:] if old.startswith("a/") else old
    new = new[2:] if new.startswith("b/") else new
    return new if old == new else None


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


def _scans_content(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in _PYTEST_CONFIG_NAMES or name.endswith(_CONTENT_SUFFIXES)


def _loads_pytest_plugin(
    path: str, line: str, addopts_continuation: str | None = None
) -> bool:
    if not _scans_content(path):
        return False
    if _PYTEST_PLUGINS_RE.search(line):
        return True
    name = path.rsplit("/", 1)[-1]
    if name not in _PYTEST_CONFIG_NAMES:
        return False
    if name == "pyproject.toml" and _PYTEST11_SECTION_RE.search(line):
        return True
    if name == "setup.cfg" and _PYTEST11_ENTRY_RE.search(line):
        return True
    if _PLUGIN_OPTION_RE.search(line) is None:
        return False
    return _ADDOPTS_RE.search(line) is not None or addopts_continuation is not None


def _next_addopts_continuation(
    path: str, line: str, current: str | None
) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name not in _PYTEST_CONFIG_NAMES:
        return None
    if current in {'"""', "'''"}:
        return None if current in line else current
    if current == "indent":
        if line and line[0].isspace():
            return current
        current = None
    if _ADDOPTS_RE.search(line) is None:
        return None
    value = line.split("=", 1)[1]
    for quote in ('"""', "'''"):
        if value.count(quote) % 2 == 1:
            return quote
    return None if name == "pyproject.toml" else "indent"


def _hunk_start(diff_line: str) -> int | None:
    match = _HUNK_RE.match(diff_line)
    return int(match.group(1)) if match else None


def _header_state(
    diff_line: str, path: str | None, in_hunk: bool, new_line: int
) -> tuple[bool, str | None, bool, int]:
    """Apply one diff header line; return ``(handled, path, in_hunk, new_line)``."""
    if diff_line.startswith("diff --git "):
        return True, _diff_git_new_path(diff_line), False, new_line
    if not in_hunk and diff_line.startswith("+++ "):
        return True, _new_path(diff_line), in_hunk, new_line
    if not in_hunk and diff_line.startswith("rename to "):
        return True, _new_path("+++ " + diff_line[10:]), in_hunk, new_line
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


def _added_line_findings(
    path: str,
    content: str,
    line: int,
    addopts_continuation: str | None = None,
) -> list[Finding]:
    """Return every finding for one added line of *path* at new-file line *line*."""
    findings = [Finding(kind=kind, path=path, line=line) for kind in _path_kinds(path)]
    if not _scans_content(path):
        return findings
    if _loads_pytest_plugin(path, content, addopts_continuation):
        findings.append(Finding(kind="pytest_plugin", path=path, line=line))
    for kind, pattern in _CONTENT_PATTERNS:
        if pattern.search(content):
            findings.append(Finding(kind=kind, path=path, line=line))
    return findings


def _file_level_findings(path: str) -> list[Finding]:
    return [Finding(kind=kind, path=path, line=1) for kind in _path_kinds(path)]


def scan_patch(patch: str) -> list[Finding]:
    findings: list[Finding] = []
    path: str | None = None
    new_line = 0
    in_hunk = False
    path_reported = False
    addopts_continuation: str | None = None
    for diff_line in patch.splitlines():
        if diff_line.startswith("diff --git "):
            if path is not None and not path_reported:
                findings.extend(_file_level_findings(path))
            path_reported = False
            addopts_continuation = None
        if _hunk_start(diff_line) is not None:
            addopts_continuation = None
        handled, path, in_hunk, new_line = _header_state(diff_line, path, in_hunk, new_line)
        if handled:
            continue
        context = (
            diff_line[1:]
            if in_hunk and path is not None and diff_line.startswith(" ")
            else None
        )
        in_hunk, new_line, added = _body_step(diff_line, in_hunk, path, new_line)
        if added is not None and path is not None:
            findings.extend(
                _added_line_findings(path, *added, addopts_continuation)
            )
            if _path_kinds(path):
                path_reported = True
            addopts_continuation = _next_addopts_continuation(
                path, added[0], addopts_continuation
            )
        elif context is not None and path is not None:
            addopts_continuation = _next_addopts_continuation(
                path, context, addopts_continuation
            )
    if path is not None and not path_reported:
        findings.extend(_file_level_findings(path))
    return findings
