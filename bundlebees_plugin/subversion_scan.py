from __future__ import annotations

import ast
import configparser
import datetime as dt
import difflib
import logging
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, NamedTuple, Sequence, TypedDict

from . import risk


class Finding(TypedDict):
    kind: str
    path: str
    line: int


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_DIFF_GIT_RE = re.compile(
    r'^diff --git ("(?:\\.|[^"\\])*"|\S+) ("(?:\\.|[^"\\])*"|\S+)$'
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_PYTEST_PLUGINS_RE = re.compile(r"\bpytest_plugins\s*(?::[^=\n]+)?=")
_ADDOPTS_RE = re.compile(r"\baddopts\s*=")
_PLUGIN_OPTION_RE = re.compile(r"(?:^|[\s\"'])(?:-p(?:\s|=|[A-Za-z_]|$)|--plugin(?:\s|=))")
_PYTEST11_SECTION_RE = re.compile(
    r"^\s*\[\s*project\s*\.\s*entry-points\s*\.\s*"
    r"(?:pytest11|\"pytest11\"|'pytest11')\s*\]\s*(?:#.*)?$"
)
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


def _v1_path_kinds(path: str) -> tuple[str, ...]:
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
    findings = [Finding(kind=kind, path=path, line=line) for kind in _v1_path_kinds(path)]
    if not _scans_content(path):
        return findings
    if _loads_pytest_plugin(path, content, addopts_continuation):
        findings.append(Finding(kind="pytest_plugin", path=path, line=line))
    for kind, pattern in _CONTENT_PATTERNS:
        if pattern.search(content):
            findings.append(Finding(kind=kind, path=path, line=line))
    return findings


def _file_level_findings(path: str) -> list[Finding]:
    return [Finding(kind=kind, path=path, line=1) for kind in _v1_path_kinds(path)]


_ScanState = dict[str, Any]


def _new_scan_state() -> _ScanState:
    return {
        "findings": [],
        "path": None,
        "new_line": 0,
        "in_hunk": False,
        "path_reported": False,
        "addopts": None,
    }


def _flush_file(state: _ScanState) -> None:
    """Emit the file-level kinds for the file that just ended, if any."""
    path = state["path"]
    if path is not None and not state["path_reported"]:
        state["findings"].extend(_file_level_findings(path))
    state["path_reported"] = False
    state["addopts"] = None


def _context_line(diff_line: str, in_hunk: bool, path: str | None) -> str | None:
    """Return the content of a hunk context line, else ``None``."""
    if in_hunk and path is not None and diff_line.startswith(" "):
        return diff_line[1:]
    return None


def _scan_added_line(state: _ScanState, path: str, added: tuple[str, int]) -> None:
    content, line = added
    state["findings"].extend(_added_line_findings(path, content, line, state["addopts"]))
    if _v1_path_kinds(path):
        state["path_reported"] = True
    state["addopts"] = _next_addopts_continuation(path, content, state["addopts"])


def _scan_context_line(state: _ScanState, path: str, context: str) -> None:
    state["addopts"] = _next_addopts_continuation(path, context, state["addopts"])


def _scan_patch_v1(patch: str) -> list[Finding]:
    """Scan added lines for test subversion.

    Addopts continuation state resets at every hunk. An addopts opener outside
    the hunk therefore leaves a long triple-quoted addopts block inside the
    hunk undetectable; a patch-only scan cannot see the opener.
    """
    state = _new_scan_state()
    for diff_line in patch.splitlines():
        if diff_line.startswith("diff --git "):
            _flush_file(state)
        if _hunk_start(diff_line) is not None:
            state["addopts"] = None
        handled, state["path"], state["in_hunk"], state["new_line"] = _header_state(
            diff_line, state["path"], state["in_hunk"], state["new_line"]
        )
        if handled:
            continue
        context = _context_line(diff_line, state["in_hunk"], state["path"])
        state["in_hunk"], state["new_line"], added = _body_step(
            diff_line, state["in_hunk"], state["path"], state["new_line"]
        )
        if added is not None and state["path"] is not None:
            _scan_added_line(state, state["path"], added)
        elif context is not None and state["path"] is not None:
            _scan_context_line(state, state["path"], context)
    _flush_file(state)
    return state["findings"]


_LOGGER = logging.getLogger(__name__)
_DIFFERENTIAL_END = dt.date(2026, 12, 2)
_CONFIG_NAMES = frozenset({"pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"})
_BINARY_SUFFIXES = frozenset({".so", ".pyd", ".dll", ".dylib", ".pyc", ".pyo"})
_DISCOVERY_OPTIONS = frozenset({"python_files", "testpaths"})
_NAME_KINDS = {
    "conftest.py": "conftest",
    "sitecustomize.py": "startup_hook",
    "usercustomize.py": "startup_hook",
    "pytest.ini": "pytest_config",
    "tox.ini": "pytest_config",
    "setup.cfg": "pytest_config",
    "noxfile.py": "pytest_config",
}


class ScanFindings(list[Finding]):
    def __init__(
        self,
        findings: Iterable[Finding] = (),
        *,
        differential: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(findings)
        self.differential = differential


class _ScanFailure(Exception):
    def __init__(self, path: str = "<input>") -> None:
        super().__init__(path)
        self.path = path


class _Change(NamedTuple):
    status: str
    path: str
    old_path: str | None


def safe_path(path: str) -> str:
    cleaned = _CONTROL_CHARS_RE.sub("", str(path))
    return cleaned or "<path>"


def _finding(kind: str, path: str, line: int = 1) -> Finding:
    return Finding(kind=kind, path=safe_path(path), line=max(1, int(line)))


def _input_finding(path: str = "<input>") -> ScanFindings:
    return ScanFindings([_finding("unreadable_input", path)])


def _git_env(index: Path | None = None) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("BUNDLEBEES_", "HERMES_"))
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    if index is not None:
        env["GIT_INDEX_FILE"] = os.fspath(index)
    return env


def _git(
    repo: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=os.fspath(repo),
            input=input_bytes,
            capture_output=True,
            env=env or _git_env(),
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _ScanFailure() from exc
    if proc.returncode != 0:
        raise _ScanFailure()
    return proc.stdout


def _resolve_tree(repo: Path, source_rev: str) -> str:
    output = _git(repo, ["rev-parse", "--verify", f"{source_rev}^{{tree}}"])
    tree = output.decode("ascii", "strict").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", tree) is None:
        raise _ScanFailure()
    return tree


def _decode_path(raw: bytes) -> str:
    try:
        return safe_path(raw.decode("utf-8", "strict"))
    except UnicodeDecodeError as exc:
        raise _ScanFailure() from exc


def _changed_paths(output: bytes) -> list[_Change]:
    fields = output.split(b"\0")
    changes = []
    index = 0
    while index < len(fields) - 1:
        status = fields[index].decode("ascii", "strict")
        index += 1
        if status.startswith(("R", "C")):
            old_path = _decode_path(fields[index])
            path = _decode_path(fields[index + 1])
            index += 2
            changes.append(_Change(status, path, old_path))
            continue
        path = _decode_path(fields[index])
        index += 1
        changes.append(_Change(status, path, None))
    return changes


def _index_entries(output: bytes) -> dict[str, str]:
    entries = {}
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.split()
        except ValueError as exc:
            raise _ScanFailure() from exc
        if stage != b"0":
            raise _ScanFailure(_decode_path(raw_path))
        path = _decode_path(raw_path)
        if path in entries:
            raise _ScanFailure(path)
        entries[path] = oid.decode("ascii", "strict")
    return entries


def _batch_blobs(repo: Path, env: dict[str, str], oids: Iterable[str]) -> dict[str, bytes]:
    unique = list(dict.fromkeys(oids))
    if not unique:
        return {}
    request = "".join(f"{oid}\n" for oid in unique).encode("ascii")
    output = _git(repo, ["cat-file", "--batch"], env=env, input_bytes=request)
    blobs = {}
    for oid in unique:
        try:
            header, output = output.split(b"\n", 1)
            parts = header.split()
            size = int(parts[2])
        except (ValueError, IndexError) as exc:
            raise _ScanFailure() from exc
        if len(parts) != 3 or parts[0].decode("ascii", "replace") != oid or parts[1] != b"blob":
            raise _ScanFailure()
        if len(output) < size + 1 or output[size : size + 1] != b"\n":
            raise _ScanFailure()
        blobs[oid] = output[:size]
        output = output[size + 1 :]
    return blobs


def _preimage(repo: Path, tree: str, change: _Change) -> bytes:
    if change.status.startswith("A"):
        return b""
    old_path = change.old_path or change.path
    return _git(repo, ["show", f"{tree}:{old_path}"])


def _added_lines(change: _Change, before: bytes, after: bytes) -> list[int]:
    if change.status.startswith("A"):
        return list(range(1, len(after.splitlines()) + 1))
    if before == after:
        return []
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines(), autojunk=False
    )
    lines = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            lines.extend(range(j1 + 1, j2 + 1))
    return lines


def _workflow_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.startswith(".github/workflows/") or "/.github/workflows/" in normalized


_PYTEST_TABLE = ("tool", "pytest")
_COVERAGE_TABLE = ("tool", "coverage")
_PYTEST11_TABLES = (
    ("project", "entry-points", "pytest11"),
    ("tool", "poetry", "plugins", "pytest11"),
)
_DEPENDENCY_TABLES = (
    ("project", "dependencies"),
    ("project", "optional-dependencies"),
    ("dependency-groups",),
)
_QUOTED_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"|\'([^\']*)\'')
_DEPENDENCY_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _toml_python_path(document: Any) -> tuple[str, ...] | None:
    """The one dotted key path a tiny parsed TOML document names, else None."""
    path: list[str] = []
    node = document
    while True:
        if isinstance(node, dict):
            if not node:
                return tuple(path)
            if len(node) != 1:
                return None
            key, node = next(iter(node.items()))
            path.append(str(key))
            continue
        if isinstance(node, list):
            if len(node) != 1:
                return None
            node = node[0]
            continue
        return tuple(path)


def _toml_header_path(line: str) -> tuple[str, ...] | None:
    if not line.startswith("["):
        return None
    try:
        return _toml_python_path(tomllib.loads(line))
    except (tomllib.TOMLDecodeError, ValueError):
        return None


def _toml_key_path(text: str) -> tuple[str, ...] | None:
    try:
        return _toml_python_path(tomllib.loads(f"{text} = 0"))
    except (tomllib.TOMLDecodeError, ValueError):
        return None


def _skip_basic_string(value: str, index: int) -> int:
    while index < len(value):
        if value[index] == "\\":
            index += 2
        elif value[index] == '"':
            return index + 1
        else:
            index += 1
    return len(value)


def _toml_string_end(value: str, index: int) -> tuple[int, str | None]:
    """Return ``(next_index, open_multiline_delimiter)`` for a string at *index*."""
    if value.startswith('"""', index):
        close = value.find('"""', index + 3)
        return (len(value), '"""') if close < 0 else (close + 3, None)
    if value.startswith("'''", index):
        close = value.find("'''", index + 3)
        return (len(value), "'''") if close < 0 else (close + 3, None)
    if value[index] == '"':
        return _skip_basic_string(value, index + 1), None
    close = value.find("'", index + 1)
    return (len(value) if close < 0 else close + 1), None


def _toml_value_state(value: str, depth: int = 0) -> tuple[str | None, int]:
    """Return the open multiline string delimiter and array depth after *value*."""
    index = 0
    while index < len(value):
        char = value[index]
        if char in {'"', "'"}:
            index, multiline = _toml_string_end(value, index)
            if multiline is not None:
                return multiline, depth
            continue
        if char == "#":
            return None, depth
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        index += 1
    return None, depth


def _toml_multiline_close_end(line: str, delimiter: str) -> int | None:
    """Index just past this line's closing *delimiter*, or None if it stays open."""
    index = 0
    while index < len(line):
        if line.startswith(delimiter, index):
            return index + len(delimiter)
        if delimiter == '"""' and line[index] == "\\":
            index += 2
        else:
            index += 1
    return None


def _split_toml_assignment(line: str) -> tuple[str, str] | None:
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is not None:
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "=":
            return line[:index].strip(), line[index + 1 :]
        elif char == "#":
            return None
        index += 1
    return None


def _toml_structural_line(
    stripped: str, table: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], str | None, int]:
    """Classify one structural TOML line; return (table, address, multiline, depth)."""
    if not stripped or stripped.startswith("#"):
        return table, table, None, 0
    if stripped.startswith("["):
        header = _toml_header_path(stripped)
        if header is not None:
            return header, header, None, 0
    assignment = _split_toml_assignment(stripped)
    if assignment is None:
        return table, table, None, 0
    key = _toml_key_path(assignment[0])
    if key is None:
        return table, table, None, 0
    multiline, depth = _toml_value_state(assignment[1])
    return table, table + key, multiline, depth


def _toml_line_addresses(text: str) -> dict[int, tuple[str, ...]]:
    """Map each line to the dotted key path of the real table that owns it."""
    addresses: dict[int, tuple[str, ...]] = {}
    table: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    multiline: str | None = None
    depth = 0
    for number, raw in enumerate(text.splitlines(), 1):
        if multiline is not None:
            addresses[number] = pending
            close = _toml_multiline_close_end(raw, multiline)
            if close is None:
                continue
            # The remainder of the line still counts: ``"""]`` closes the
            # multiline string and its array on one line, and dropping that
            # bracket left ``depth`` above zero, so every later line was
            # addressed as the array's pending key and real pytest tables
            # became invisible.
            multiline, depth = _toml_value_state(raw[close:], depth)
            continue
        if depth > 0:
            addresses[number] = pending
            multiline, depth = _toml_value_state(raw, depth)
            continue
        table, pending, multiline, depth = _toml_structural_line(raw.strip(), table)
        addresses[number] = pending
    return addresses


def _entry_point_modules(document: Any) -> frozenset[str]:
    """Module roots named by this document's ``pytest11`` entry points."""
    modules = set()
    for keys in _PYTEST11_TABLES:
        table = _mapping_path(document, keys)
        for value in table.values():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str):
                    continue
                root = re.split(r"[.:]", item.strip(), 1)[0]
                if root:
                    modules.add(re.sub(r"[-_.]+", "-", root).lower())
    return frozenset(modules)


def _dependency_name(spec: str) -> str | None:
    match = _DEPENDENCY_NAME_RE.match(spec)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _dependency_is_pytest_plugin(line: str, modules: frozenset[str]) -> bool:
    for match in _QUOTED_STRING_RE.finditer(line):
        spec = match.group(1) if match.group(1) is not None else match.group(2)
        name = _dependency_name(spec)
        if name is not None and (name.startswith("pytest-") or name in modules):
            return True
    return False


def _starts_with(address: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return address[: len(prefix)] == prefix


def _plugin_option_addresses(document: Any) -> frozenset[tuple[str, ...]]:
    """Key addresses whose parsed value loads a pytest plugin."""
    options = _mapping_path(document, ("tool", "pytest", "ini_options"))
    candidates = (
        (("tool", "pytest", "ini_options", "addopts"), options, "addopts"),
        (("tool", "pytest", "ini_options", "pytest_plugins"), options, "pytest_plugins"),
        (("addopts",), document, "addopts"),
        (("pytest_plugins",), document, "pytest_plugins"),
    )
    addresses = set()
    for address, table, key in candidates:
        if not isinstance(table, Mapping) or key not in table:
            continue
        if key == "pytest_plugins":
            addresses.add(address)
            continue
        try:
            if _addopts_plugin(table[key]):
                addresses.add(address)
        except (ValueError, TypeError):
            addresses.add(address)
    return frozenset(addresses)


def _pyproject_line_kinds(
    address: tuple[str, ...],
    line: str,
    modules: frozenset[str],
    plugin_addresses: frozenset[tuple[str, ...]] = frozenset(),
) -> tuple[str, ...]:
    """Classify one pyproject line by the real TOML table that contains it."""
    kinds = []
    if _starts_with(address, _PYTEST_TABLE):
        kinds.append("pytest_config")
    if address in plugin_addresses:
        kinds.append("pytest_plugin")
    if _starts_with(address, _COVERAGE_TABLE):
        kinds.append("coverage_config")
    if any(_starts_with(address, prefix) for prefix in _PYTEST11_TABLES):
        kinds.append("pytest_plugin")
    if any(_starts_with(address, prefix) for prefix in _DEPENDENCY_TABLES):
        if _dependency_is_pytest_plugin(line, modules):
            kinds.append("pytest_plugin")
    return tuple(dict.fromkeys(kinds))


def _pyproject_findings(
    path: str, added: list[int], content: bytes | None
) -> list[Finding]:
    """Line findings for pyproject.toml, scoped to pytest/coverage tables only.

    Build and packaging tables (``[build-system]``, ``[tool.hatch.build*]``)
    and ordinary ``[project]`` metadata are not test-runner subversion and are
    deliberately not findings.  The table of every line comes from the parsed
    TOML document, not from a text match, so a header-looking string inside a
    build list or a multiline string cannot move a line into a pytest table.
    """
    if content is None:
        return []
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return []
    lines = text.splitlines()
    addresses = _toml_line_addresses(text)
    try:
        document: Any = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        document = None
    modules = _entry_point_modules(document) if document is not None else frozenset()
    plugin_addresses = (
        _plugin_option_addresses(document) if document is not None else frozenset()
    )
    findings = []
    for line in added:
        if not 1 <= line <= len(lines):
            continue
        address = addresses.get(line)
        if address is None:
            continue
        for kind in _pyproject_line_kinds(
            address, lines[line - 1], modules, plugin_addresses
        ):
            findings.append(_finding(kind, path, line))
    return findings


def _path_kinds(path: str) -> tuple[str, ...]:
    name = path.rsplit("/", 1)[-1].lower()
    kinds = []
    if name in _NAME_KINDS:
        kinds.append(_NAME_KINDS[name])
    if name.endswith(".pth"):
        kinds.append("pth_file")
    if _workflow_path(path):
        kinds.append("workflow")
    if any(name.endswith(suffix) for suffix in _BINARY_SUFFIXES):
        kinds.append("binary_importable")
    floor = risk.derive_floor([path])
    reasons = floor.get("reasons") or []
    if any("policy is missing, unreadable, or invalid" in str(reason) for reason in reasons):
        kinds.append("risk_policy")
    return tuple(kinds)


def _path_findings(
    change: _Change, added: list[int], content: bytes | None = None
) -> list[Finding]:
    kinds = _path_kinds(change.path)
    entered = change.status.startswith(("A", "R", "C"))
    lines = added or ([1] if entered and kinds else [])
    findings = [
        _finding(kind, change.path, line)
        for line in lines
        for kind in kinds
    ]
    if change.path.rsplit("/", 1)[-1].lower() == "pyproject.toml":
        targets = list(added)
        if not targets and entered and content is not None:
            targets = list(range(1, len(content.splitlines()) + 1))
        findings.extend(_pyproject_findings(change.path, targets, content))
    return findings


def _config_error(path: str) -> list[Finding]:
    return [_finding("config_parse", path)]


def _mapping_path(document: Mapping[str, Any], keys: Iterable[str]) -> Mapping[str, Any]:
    current: Any = document
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _option_tokens(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    tokens = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("non-string option")
        tokens.extend(shlex.split(item, comments=False, posix=True))
    return tokens


def _has_plugin_option(tokens: list[str]) -> bool:
    for token in tokens:
        if token in {"-p", "--plugin"}:
            return True
        if token.startswith("--plugin="):
            return True
        if token.startswith("-p") and not token.startswith("--") and len(token) > 2:
            return True
    return False


def _addopts_plugin(value: Any) -> bool:
    return _has_plugin_option(_option_tokens(value))


def _option_findings(path: str, options: Mapping[str, Any]) -> list[Finding]:
    findings = []
    if "addopts" in options:
        try:
            if _addopts_plugin(options["addopts"]):
                findings.append(_finding("pytest_plugin", path))
        except (ValueError, TypeError):
            findings.extend(_config_error(path))
    if "pytest_plugins" in options:
        findings.append(_finding("pytest_plugin", path))
    if any(name in options for name in _DISCOVERY_OPTIONS):
        findings.append(_finding("pytest_discovery", path))
    return findings


def _dedupe(findings: Iterable[Finding]) -> list[Finding]:
    return list({(item["kind"], item["path"], item["line"]): item for item in findings}.values())


def _toml_findings(path: str, text: str, *, recover: bool = False) -> list[Finding]:
    """Validate a pyproject document; line semantics live in ``_path_findings``."""
    try:
        tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return [] if recover else _config_error(path)
    return []


def _ini_parser() -> configparser.ConfigParser:
    return configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
    )


def _read_ini(path: str, text: str) -> configparser.ConfigParser:
    parser = _ini_parser()
    try:
        parser.read_string(text)
        return parser
    except configparser.MissingSectionHeaderError:
        default = "tool:pytest" if path.endswith("setup.cfg") else "pytest"
        section = "options.entry_points" if text.lstrip().startswith("pytest11") else default
        parser = _ini_parser()
        parser.read_string(f"[{section}]\n{text}")
        return parser


def _ini_findings(path: str, text: str) -> list[Finding]:
    try:
        parser = _read_ini(path, text)
    except (configparser.Error, ValueError):
        return _config_error(path)
    section = "tool:pytest" if path.endswith("setup.cfg") else "pytest"
    findings = _option_findings(path, parser[section] if parser.has_section(section) else {})
    entry_points = parser["options.entry_points"] if parser.has_section("options.entry_points") else {}
    if "pytest11" in entry_points:
        findings.append(_finding("pytest_plugin", path))
    return _dedupe(findings)


def _config_findings(
    path: str, content: bytes, *, recover: bool = False
) -> list[Finding]:
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return _config_error(path)
    if path.rsplit("/", 1)[-1].lower() == "pyproject.toml":
        return _toml_findings(path, text, recover=recover)
    return _ini_findings(path, text)


_MODULE_NAMES = ("os", "atexit", "sys", "builtins", "unittest")


def _qualified(node: ast.AST, names: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value, names)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Subscript):
        return _qualified(node.value, names)
    return None


def _record_import(node: ast.Import, names: dict[str, str]) -> None:
    for alias in node.names:
        if alias.name in _MODULE_NAMES:
            names[alias.asname or alias.name] = alias.name


def _record_from_import(node: ast.ImportFrom, names: dict[str, str]) -> None:
    if node.module not in _MODULE_NAMES:
        return
    for alias in node.names:
        if alias.name != "*":
            names[alias.asname or alias.name] = f"{node.module}.{alias.name}"


def _record_aliases(tree: ast.AST, names: dict[str, str]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        qualified = _qualified(node.value, names)
        if qualified is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names[target.id] = qualified


def _import_names(tree: ast.AST) -> dict[str, str]:
    names = {name: name for name in _MODULE_NAMES}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            _record_import(node, names)
        elif isinstance(node, ast.ImportFrom):
            _record_from_import(node, names)
    _record_aliases(tree, names)
    return names


def _target_kinds(qualified: str) -> tuple[str, ...]:
    kinds = []
    if qualified == "builtins.AssertionError" or qualified.startswith("unittest.TestCase.assert"):
        kinds.append("patched_assertion")
    if qualified == "builtins" or qualified.startswith("builtins."):
        kinds.append("patched_builtins")
    if qualified == "sys.modules" or qualified.startswith("sys.modules."):
        kinds.append("patched_sys_modules")
    return tuple(kinds)


class _AstScanner(ast.NodeVisitor):
    def __init__(self, path: str, tree: ast.AST) -> None:
        self.path = path
        self.names = _import_names(tree)
        self.findings: list[Finding] = []
        self.function_depth = 0

    def _record(self, kind: str, node: ast.AST) -> None:
        finding = _finding(kind, self.path, getattr(node, "lineno", 1))
        if finding not in self.findings:
            self.findings.append(finding)

    def _check_target(self, target: ast.AST, *, assignment: bool = True) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._check_target(element, assignment=assignment)
            return
        if assignment and isinstance(target, ast.Name):
            if target.id == "AssertionError":
                self._record("patched_assertion", target)
            if target.id == "pytest_plugins" and self.function_depth == 0:
                self._record("pytest_plugin", target)
        qualified = _qualified(target, self.names)
        if qualified:
            for kind in _target_kinds(qualified):
                self._record(kind, target)

    def _assignment(self, node: ast.AST, targets: Iterable[ast.AST]) -> None:
        for target in targets:
            self._check_target(target)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assignment(node, node.targets)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assignment(node, [node.target])

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._assignment(node, [node.target])

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._assignment(node, [node.target])

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._check_target(target, assignment=False)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os" and any(alias.name == "_exit" for alias in node.names):
            self._record("forced_exit", node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if node.name.startswith("pytest_"):
            self._record("pytest_hook", node)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _setattr_patch(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id != "setattr" or not node.args:
            return
        qualified = _qualified(node.args[0], self.names)
        if qualified:
            for kind in _target_kinds(qualified):
                self._record(kind, node)

    def visit_Call(self, node: ast.Call) -> None:
        qualified = _qualified(node.func, self.names)
        if qualified == "os._exit":
            self._record("forced_exit", node)
        if qualified == "atexit.register":
            self._record("atexit", node)
        if qualified == "sys.exit" and self.function_depth == 0:
            self._record("forced_exit", node)
        if qualified and qualified.startswith("sys.modules."):
            self._record("patched_sys_modules", node)
        self._setattr_patch(node)
        self.generic_visit(node)


#: Synthetic scopes for a fragment of a Python file, smallest first.
#:
#: A patch-only scan rebuilds only the lines its hunks carry, so the
#: "post-image" it parses is a fragment of the real file: it can open on a
#: block continuation (``else:``, ``except``), on a ``return``, or inside a
#: bracketed expression. Each entry is an opener plus whether the body is
#: indented under it. The module-level openers come first: ``sys.exit`` is a
#: ``forced_exit`` only at module level, so a hunk that really is at module
#: level must keep that depth.
_FRAGMENT_SCOPES: tuple[tuple[str, bool], ...] = (
    ("", False),
    ("if True:\n    pass\n", False),
    ("try:\n    pass\n", False),
    ("while True:\n", True),
    ("def _scan_fragment():\n", True),
)


def _pad_open_block(text: str) -> str:
    """Close a fragment whose last line opens a block (``if x:``)."""
    lines = text.rstrip("\n").split("\n")
    if not lines[-1].rstrip().endswith(":"):
        return text
    indent = len(lines[-1]) - len(lines[-1].lstrip())
    return "\n".join([*lines, " " * (indent + 4) + "pass"]) + "\n"


def _repair_python(text: str) -> ast.Module | None:
    """Parse *text* as a fragment of a Python file, under a synthetic scope.

    The fragment is dedented first, so a hunk that starts mid-function parses
    as a statement list while its own relative indentation is preserved, and
    then wrapped in the smallest scope that makes it parse. The synthetic
    prefix lines are subtracted from every node's line number, so findings keep
    the post-image's own numbering.
    """
    body = _pad_open_block(textwrap.dedent(text))
    for prefix, indented in _FRAGMENT_SCOPES:
        source = prefix + (textwrap.indent(body, "    ") if indented else body)
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        prefix_lines = prefix.count("\n")
        if prefix_lines:
            ast.increment_lineno(tree, -prefix_lines)
        return tree
    return None


def _scan_tree(path: str, tree: ast.AST) -> list[Finding]:
    scanner = _AstScanner(path, tree)
    scanner.visit(tree)
    return _dedupe(scanner.findings)


def _contiguous_runs(numbers: Sequence[int]) -> list[list[int]]:
    """Group ascending line numbers into runs of consecutive numbers."""
    runs: list[list[int]] = []
    for number in numbers:
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    return runs


def _fragment_statement_tree(body: str, *, indented: bool) -> ast.AST | None:
    """Parse one run of added lines as statements, preserving its depth.

    A column-0 run is module-level code, so an unguarded ``sys.exit`` there
    keeps module depth. A run that is indented in the post-image continues a
    block, and is parsed inside a synthetic function so it is not promoted to
    module level; the synthetic line is subtracted from every node again.
    """
    candidates = (
        (("def _scan_fragment():\n", True),)
        if indented
        else (("", False), ("while True:\n", True), ("def _scan_fragment():\n", True))
    )
    for prefix, wrap in candidates:
        source = prefix + (textwrap.indent(body, "    ") if wrap else body)
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            continue
        prefix_lines = prefix.count("\n")
        if prefix_lines:
            ast.increment_lineno(tree, -prefix_lines)
        return tree
    return None


def _fragment_run_findings(
    path: str, text: str, added: Sequence[int]
) -> list[Finding]:
    """Tree rules for the added lines of an unparseable hunk fragment.

    The per-line content rules have no pattern for the module-level shapes
    that only the tree scanner knows (a ``sys.modules`` write or call, an
    unguarded ``sys.exit``). When no synthetic scope repairs the whole
    fragment, each run of consecutive added lines is parsed on its own, so
    those shapes are still evaluated on the lines the hunk adds instead of
    becoming a no-op. Findings are mapped back to the fragment's own line
    numbers.
    """
    if not added:
        return []
    lines = text.split("\n")
    findings = []
    for run in _contiguous_runs(added):
        block = [lines[number - 1] for number in run if 1 <= number <= len(lines)]
        if not block or not any(line.strip() for line in block):
            continue
        body = textwrap.dedent("\n".join(block)) + "\n"
        indent = min(len(line) - len(line.lstrip()) for line in block if line.strip())
        tree = _fragment_statement_tree(body, indented=indent > 0)
        if tree is None:
            continue
        for finding in _scan_tree(path, tree):
            position = finding["line"]
            if 1 <= position <= len(run):
                findings.append(
                    Finding(kind=finding["kind"], path=path, line=run[position - 1])
                )
    return findings


def _fragment_line_findings(
    path: str, text: str, added: Sequence[int] | None
) -> list[Finding]:
    """Findings for the added lines when no scope could repair the fragment.

    The per-added-line rules are exactly the rules the line-based scan applies
    (so ``os._exit`` still fires), and the tree-only module-level shapes are
    evaluated on each run of consecutive added lines, so a fragment that
    cannot be parsed is not a no-op.
    """
    if not added:
        return []
    lines = text.splitlines()
    findings = []
    for number in added:
        if 1 <= number <= len(lines):
            findings.extend(_added_line_findings(path, lines[number - 1], number))
    findings.extend(_fragment_run_findings(path, text, added))
    return _dedupe(findings)


def _unparsed_findings(
    path: str,
    text: str,
    exc: SyntaxError | ValueError,
    *,
    recover: bool,
    added: Sequence[int] | None,
) -> list[Finding]:
    """Findings for content that does not parse as a file.

    ``recover`` marks a partial input - the lines a hunk carries. Such a
    fragment is not required to be syntactically self-contained: once repaired
    it is scanned, and when it cannot be repaired the added lines are still
    checked line by line and as statement runs. A complete file (a created
    file, or any input at all in a repo-backed scan) keeps the ``parse_error``
    finding.
    """
    error = [_finding("parse_error", path, getattr(exc, "lineno", 1) or 1)]
    tree = _repair_python(text)
    if tree is not None:
        kept = [] if recover else error
        return _dedupe([*kept, *_scan_tree(path, tree)])
    if not recover:
        return error
    return _fragment_line_findings(path, text, added)


def _python_findings(
    path: str,
    content: bytes,
    *,
    recover: bool = False,
    added: Sequence[int] | None = None,
) -> list[Finding]:
    try:
        text = content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return [_finding("parse_error", path)]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        return _unparsed_findings(path, text, exc, recover=recover, added=added)
    return _scan_tree(path, tree)


def _content_findings(
    path: str,
    content: bytes,
    *,
    recover: bool = False,
    added: Sequence[int] | None = None,
) -> list[Finding]:
    name = path.rsplit("/", 1)[-1].lower()
    findings = []
    if name in _CONFIG_NAMES:
        findings.extend(_config_findings(path, content, recover=recover))
    if name.endswith(".py"):
        findings.extend(_python_findings(path, content, recover=recover, added=added))
    return findings


class _StatementShape(NamedTuple):
    """One statement's source span: line range, first-line indent, code text."""

    start: int
    end: int
    indent: int
    text: str


def _decoded(content: bytes) -> str | None:
    try:
        return content.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None


def _parsed(text: str) -> ast.AST | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _code_line(line: str) -> str | None:
    """The line with trailing whitespace dropped, or ``None`` if it has no code."""

    stripped = line.rstrip()
    body = stripped.lstrip()
    return stripped if body and not body.startswith("#") else None


def _statement_shape(node: ast.AST, lines: Sequence[str]) -> _StatementShape | None:
    """The shape of one statement node, or ``None`` if it has no code span."""

    if not isinstance(node, ast.stmt):
        return None
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None or end is None:
        return None
    if not 1 <= start <= end <= len(lines):
        return None
    segment = []
    for number in range(start, end + 1):
        line = _code_line(lines[number - 1])
        if line is not None:
            segment.append(line)
    if not segment:
        return None
    first = segment[0]
    body = first.lstrip()
    return _StatementShape(
        start=start,
        end=end,
        indent=len(first) - len(body),
        text="\n".join([body, *segment[1:]]),
    )


def _statement_shapes(content: bytes) -> list[_StatementShape] | None:
    """Full statement segments of *content*, for line-independent identity.

    ``ast`` reports a finding at the line of the assignment target or the
    call, which for a multi-line statement is its first line. Two shapes can
    share that line while the statement does something else entirely (a patch
    can rewrite the continuation of a pre-existing ``sys.modules`` statement),
    so the whole statement segment is the shape: its first-line indentation (a
    dedent out of a guard is a code change) plus every code line of the span.
    Blank and comment-only lines inside the span are dropped, so a comment
    added inside a pre-existing statement is still the same shape. ``None``
    means the content does not parse; nothing is subtracted then.
    """

    text = _decoded(content)
    if text is None:
        return None
    tree = _parsed(text)
    if tree is None:
        return None
    lines = text.split("\n")
    shapes = []
    for node in ast.walk(tree):
        shape = _statement_shape(node, lines)
        if shape is not None:
            shapes.append(shape)
    return shapes


def _finding_shape(
    shapes: Sequence[_StatementShape] | None, finding: Finding
) -> tuple[str, int, str] | None:
    """Line-independent identity of a Python finding, or ``None`` if unknown.

    The shape is the rule id plus the full source segment of the statement the
    finding's node belongs to, with its indentation kept and trailing
    whitespace dropped. Line numbers are deliberately absent: a hunk that
    shifts a pre-existing statement down must not resurrect it as "new".
    Indentation is kept because a patch that dedents a statement out of its
    guard is a code change, not a line shift, and has to be reported. The
    whole segment (not just its first line) is part of the identity so a patch
    that rewrites the continuation of a multi-line ``sys.modules`` statement
    cannot hide behind the line it did not touch. ``parse_error`` and content
    that does not parse are never subtracted (fail closed).
    """

    if finding["kind"] == "parse_error" or not shapes:
        return None
    line = finding["line"]
    candidates = [shape for shape in shapes if shape.start <= line <= shape.end]
    if not candidates:
        return None
    # The innermost statement that starts closest to the finding: a statement
    # nested in a block (or a body inside a compound statement) wins over the
    # compound it sits in.
    shape = min(candidates, key=lambda item: (item.end - item.start, -item.start))
    return finding["kind"], shape.indent, shape.text


def _subtract_base_findings(
    base: list[Finding],
    base_content: bytes,
    after: list[Finding],
    after_content: bytes,
) -> list[Finding]:
    """Drop post-image findings whose shape is already present at the base.

    Counted, not just matched: a patch that adds a second copy of a shape the
    base already carried still keeps one finding. Shapes that cannot be
    identified are never subtracted (fail closed).
    """

    base_shapes = _statement_shapes(base_content)
    after_shapes = _statement_shapes(after_content)
    remaining = Counter(
        shape
        for finding in base
        if (shape := _finding_shape(base_shapes, finding)) is not None
    )
    kept = []
    for finding in after:
        shape = _finding_shape(after_shapes, finding)
        if shape is not None and remaining[shape]:
            remaining[shape] -= 1
            continue
        kept.append(finding)
    return kept


def _change_findings(
    change: _Change,
    before: bytes,
    after: bytes,
    *,
    recover: bool = False,
    subtract_base: bool = False,
    added: Sequence[int] | None = None,
) -> list[Finding]:
    """Content findings for one changed file, minus what the base already had.

    Only Python content findings are subtracted. A document-level config
    finding (the INI rules report the whole ``pytest.ini``/``tox.ini``/
    ``setup.cfg`` document at line 1) has no per-line shape that could tell a
    pre-existing ``addopts`` plugin from a newly added one, so those keep the
    fail-closed whole-file behaviour; ``pyproject.toml`` findings are already
    attributed to added lines by ``_path_findings``. A new file (no pre-image)
    and a file whose pre-image is empty have nothing to subtract.
    """

    tolerance = _fragment_tolerant(change, recover)
    after_findings = _content_findings(change.path, after, recover=tolerance, added=added)
    is_python = change.path.lower().endswith(".py")
    if not subtract_base or not is_python or not before:
        return after_findings
    base_path = change.old_path or change.path
    # The base scan gets no ``added`` lines: they are post-image numbers, and
    # nothing subtracted from an unparseable base fragment keeps the check
    # fail-closed.
    base_findings = _content_findings(base_path, before, recover=tolerance)
    return _subtract_base_findings(base_findings, before, after_findings, after)


def _fragment_tolerant(change: _Change, recover: bool) -> bool:
    """Whether an unparseable post-image is fragment noise, not evidence.

    A patch-only scan rebuilds a modified file's post-image from the hunk
    lines alone, so it is a fragment of the real file. A created file's
    post-image is the whole file, so a parse error there is real evidence and
    stays a finding even in a fragment-tolerant scan.
    """
    if not recover:
        return False
    return not change.status.startswith("A")


def _changed_content(
    repo: Path,
    tree: str,
    changes: list[_Change],
    entries: dict[str, str],
    env: dict[str, str],
    *,
    recover: bool = False,
    subtract_base: bool = False,
) -> list[Finding]:
    active = [change for change in changes if not change.status.startswith("D")]
    try:
        oids = [entries[change.path] for change in active]
    except KeyError as exc:
        raise _ScanFailure() from exc
    blobs = _batch_blobs(repo, env, oids)
    findings = []
    for change in active:
        after = blobs[entries[change.path]]
        before = _preimage(repo, tree, change)
        if change.status.startswith(("M", "T")) and before == after:
            continue
        added = _added_lines(change, before, after)
        findings.extend(_path_findings(change, added, after))
        findings.extend(
            _change_findings(
                change,
                before,
                after,
                recover=recover,
                subtract_base=subtract_base,
                added=added,
            )
        )
    return findings


def _scan_change_tree(
    patch: str,
    repo_root: Path,
    source_rev: str,
    *,
    recover: bool = False,
    subtract_base: bool = False,
) -> list[Finding]:
    tree = _resolve_tree(repo_root, source_rev)
    with tempfile.TemporaryDirectory(prefix="bundlebees-subversion-") as scratch:
        env = _git_env(Path(scratch) / "index")
        _git(repo_root, ["read-tree", tree], env=env)
        _git(
            repo_root,
            ["apply", "--cached", "--whitespace=nowarn", "-"],
            env=env,
            input_bytes=patch.encode("utf-8"),
        )
        diff = _git(
            repo_root,
            ["diff", "--cached", "--name-status", "-M", "--no-ext-diff", "-z", tree],
            env=env,
        )
        changes = _changed_paths(diff)
        listing = _git(repo_root, ["ls-files", "-s", "-z"], env=env)
        entries = _index_entries(listing)
        return _changed_content(
            repo_root,
            tree,
            changes,
            entries,
            env,
            recover=recover,
            subtract_base=subtract_base,
        )


def scan_change(patch: str, repo_root: str | Path, source_rev: str) -> list[Finding]:
    """Repo-backed scan: whole-file findings minus the ones the base already had.

    The patch-only scan (``scan_patch``) keeps its own reconstruction semantics;
    only this path has a real base revision to subtract.
    """

    try:
        findings = _scan_change_tree(
            patch, Path(repo_root), str(source_rev), subtract_base=True
        )
        return ScanFindings(_dedupe(findings))
    except _ScanFailure as exc:
        return _input_finding(exc.path)
    except (OSError, UnicodeError, ValueError, TypeError):
        return _input_finding()


class _PreimageBuilder:
    def __init__(self) -> None:
        self.path: str | None = None
        self.old_line: int | None = None
        self.in_hunk = False
        self.absent: set[str] = set()
        self.lines: dict[str, dict[int, str]] = {}

    def _section(self, raw: str) -> None:
        match = _DIFF_GIT_RE.match(raw)
        self.path = None
        self.old_line = None
        self.in_hunk = False
        if match:
            value = _path_value(match.group(1))
            if value is not None:
                self.path = value[2:] if value.startswith("a/") else value
                self.lines.setdefault(self.path, {})

    def _old_header(self, raw: str) -> None:
        value = _path_value(raw[4:])
        if value is None:
            if self.path is not None:
                self.absent.add(self.path)
            return
        self.path = value[2:] if value.startswith("a/") else value
        self.absent.discard(self.path)
        self.lines.setdefault(self.path, {})

    def _hunk(self, raw: str) -> None:
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", raw)
        self.old_line = int(match.group(1)) if match else None
        self.in_hunk = match is not None

    def _body(self, raw: str) -> None:
        if self.old_line is None or self.path is None or raw[:1] not in {" ", "-"}:
            return
        self.lines.setdefault(self.path, {})[self.old_line] = raw[1:]
        self.old_line += 1

    def _rename(self, raw: str) -> None:
        self.path = _path_value(raw[12:])
        if self.path is not None:
            self.lines.setdefault(self.path, {})

    def feed(self, raw: str) -> None:
        if raw.startswith("diff --git "):
            self._section(raw)
        elif raw.startswith("@@"):
            self._hunk(raw)
        elif self.in_hunk:
            self._body(raw)
        elif raw.startswith("--- "):
            self._old_header(raw)
        elif raw.startswith("rename from "):
            self._rename(raw)
        elif raw.startswith("new file mode") and self.path is not None:
            self.absent.add(self.path)

    def render(self) -> dict[str, bytes]:
        documents = {}
        for path, lines in self.lines.items():
            if path in self.absent:
                continue
            last = max(lines, default=0)
            text = "".join(f"{lines.get(number, '')}\n" for number in range(1, last + 1))
            documents[path] = text.encode("utf-8")
        return documents


def _preimages(patch: str) -> dict[str, bytes]:
    builder = _PreimageBuilder()
    for raw in patch.splitlines():
        builder.feed(raw)
    return builder.render()


def _write_preimages(repo: Path, documents: Mapping[str, bytes]) -> None:
    for name, content in documents.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise _ScanFailure()
        target = repo.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


def _scan_patch_only(patch: str) -> list[Finding]:
    if not patch.strip():
        return []
    with tempfile.TemporaryDirectory(prefix="bundlebees-patch-scan-") as scratch:
        repo = Path(scratch) / "repo"
        repo.mkdir()
        _git(repo, ["init", "-q"])
        _write_preimages(repo, _preimages(patch))
        _git(repo, ["add", "-A"])
        tree = _git(repo, ["write-tree"]).decode("ascii", "strict").strip()
        return _scan_change_tree(patch, repo, tree, recover=True)


def _finding_key(finding: Finding) -> tuple[str, str, int]:
    return finding["kind"], finding["path"], finding["line"]


def _counter_delta(left: list[Finding], right: list[Finding]) -> list[Finding]:
    remaining = Counter(_finding_key(finding) for finding in right)
    delta = []
    for finding in left:
        key = _finding_key(finding)
        if remaining[key]:
            remaining[key] -= 1
        else:
            delta.append(finding)
    return delta


def _differential(patch: str, v2: list[Finding]) -> dict[str, Any] | None:
    if dt.datetime.now(dt.timezone.utc).date() >= _DIFFERENTIAL_END:
        return None
    try:
        v1 = _scan_patch_v1(patch)
    except Exception:
        _LOGGER.warning("subversion V1 differential failed; V2 decision is unchanged")
        return None
    v1_only = _counter_delta(v1, v2)
    v2_only = _counter_delta(v2, v1)
    if v1_only or v2_only:
        _LOGGER.warning(
            "subversion scanner differential: v1=%d v2=%d v1_only=%d v2_only=%d",
            len(v1),
            len(v2),
            len(v1_only),
            len(v2_only),
        )
    return {
        "v1": v1,
        "v2": list(v2),
        "delta": {"v1_only": v1_only, "v2_only": v2_only},
        "match": not v1_only and not v2_only,
    }


def _with_differential(patch: str, findings: list[Finding]) -> ScanFindings:
    return ScanFindings(findings, differential=_differential(patch, findings))


def scan_patch(
    patch: str,
    repo_root: str | Path | None = None,
    source_rev: str | None = None,
) -> list[Finding]:
    """Scan a patch for test-runner subversion.

    With ``repo_root`` and ``source_rev`` the patch is applied to that tree and
    the *whole* post-image file is scanned, minus the content findings the base
    revision already carried. Without them the changed files are rebuilt from
    the hunks the patch carries, so a partial hunk is only a fragment of a
    file: ``ast`` cannot parse it and the scan reports ``parse_error`` on a
    file that is valid once the patch applies. Verify always has the repository
    and the dispatched source revision, so it passes both; a patch that does
    not apply to that tree falls back to the patch-only scan and is refused
    later by the revision checks.
    """

    if repo_root is not None and source_rev:
        try:
            findings = _scan_change_tree(
                patch, Path(repo_root), str(source_rev), subtract_base=True
            )
        except _ScanFailure:
            findings = None
        except (OSError, UnicodeError, ValueError, TypeError):
            findings = None
        if findings is not None:
            return _with_differential(patch, findings)
    elif (repo_root is None) != (source_rev is None):
        return _with_differential(patch, _input_finding())
    try:
        findings = _scan_patch_only(patch)
    except _ScanFailure as exc:
        findings = _input_finding(exc.path)
    except (OSError, UnicodeError, ValueError, TypeError):
        findings = _input_finding()
    return _with_differential(patch, findings)


def scan_for_verify(
    patch: str,
    repo_root: str | Path | None = None,
    source_rev: str | None = None,
) -> list[Finding]:
    return scan_patch(patch, repo_root, source_rev)
