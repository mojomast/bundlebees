"""The one source of truth for bundlebees feature flags.

The factory driver is a systemd user service; the Hermes gateway loads this
plugin into a different process. A flag must mean one thing in both, so both
read the same names from the same shared env file:

``~/.config/bundlebees/flags.env`` (override: ``BUNDLEBEES_FLAGS_FILE``)

The file carries flag names only -- no credential value ever belongs in it.
Its format is one ``NAME=VALUE`` per line, ``#`` comments and blank lines
ignored, a later duplicate winning, the way a systemd ``EnvironmentFile``
behaves.

Resolution order for one flag:

1. the process environment, so a host can override a single run;
2. the shared file, so both processes agree on the durable setting.

When a caller passes an explicit mapping (tests, scoped calls) that mapping is
the whole environment and the file is not read at all. A flag is on only when
its resolved value is exactly ``1`` -- the rule the recall and board-stock
modules already used, now applied once.

``log_startup`` prints one line naming every effective flag. It prints the
0/1 outcome and the file path, never a value from the file, so a log line
cannot leak anything.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, TextIO

__all__ = [
    "DEFAULT_FLAGS_FILE",
    "FLAG_BOARD_STOCK",
    "FLAG_MEMORY",
    "FLAGS",
    "FLAGS_FILE_ENV",
    "effective_flags",
    "enabled",
    "flags_path",
    "log_startup",
    "read_flags_file",
    "startup_line",
]

#: Lease-time memory recall. Dark until the owner turns it on.
FLAG_MEMORY = "MEMORY_ENABLED"
#: Ready-column stocking in the factory driver.
FLAG_BOARD_STOCK = "BUNDLEBEES_BOARD_STOCK"
#: Every flag the startup line prints, in this order.
FLAGS = (FLAG_MEMORY, FLAG_BOARD_STOCK)

#: Names the shared file both processes read.
FLAGS_FILE_ENV = "BUNDLEBEES_FLAGS_FILE"
DEFAULT_FLAGS_FILE = "~/.config/bundlebees/flags.env"


def flags_path(environ: Mapping[str, str] | None = None) -> Path:
    """The shared flag file: ``BUNDLEBEES_FLAGS_FILE`` or the default."""
    source = os.environ if environ is None else environ
    override = str(source.get(FLAGS_FILE_ENV) or "").strip()
    return Path(override or DEFAULT_FLAGS_FILE).expanduser()


def _parse_line(line: str):
    """One ``NAME=VALUE`` line as ``(name, value)``, or ``(None, None)``."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None, None
    if text.startswith("export "):
        text = text[len("export ") :].strip()
    name, separator, value = text.partition("=")
    name = name.strip()
    if not separator or not name or not name.replace("_", "").isalnum() or name[0].isdigit():
        return None, None
    return name, _unquote(value.strip())


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_flags_file(
    path: Path | str | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Parse the shared file. A missing or unreadable file is an empty mapping."""
    target = flags_path(environ) if path is None else Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, value = _parse_line(line)
        if name is not None and value is not None:
            values[name] = value
    return values


def _on(raw) -> bool:
    return raw is not None and str(raw).strip() == "1"


def _view(environ: Mapping[str, str] | None):
    """``(environment, file values)`` for one resolution pass."""
    if environ is not None:
        return environ, {}
    return os.environ, read_flags_file()


def _raw(name: str, environ: Mapping[str, str] | None):
    environment, file_values = _view(environ)
    if name in environment:
        return environment[name]
    return file_values.get(name)


def enabled(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """True only when the shared source resolves *name* to exactly ``1``."""
    if name not in FLAGS:
        raise ValueError(f"unknown bundlebees flag: {name}")
    return _on(_raw(name, environ))


def effective_flags(environ: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Every registered flag's effective value, in ``FLAGS`` order."""
    environment, file_values = _view(environ)
    resolved = {}
    for name in FLAGS:
        raw = environment.get(name) if name in environment else file_values.get(name)
        resolved[name] = _on(raw)
    return resolved


def startup_line(environ: Mapping[str, str] | None = None) -> str:
    """One line naming every effective flag and the file it came from."""
    resolved = effective_flags(environ=environ)
    states = " ".join(f"{name}={int(resolved[name])}" for name in FLAGS)
    path = flags_path(environ)
    presence = "present" if path.is_file() else "missing"
    return f"bundlebees flags: {states} flags_file={path} ({presence})"


def log_startup(stream: TextIO | None = None, environ: Mapping[str, str] | None = None) -> str:
    """Write the startup flag line once; a log failure never stops startup."""
    line = startup_line(environ=environ)
    try:
        print(line, file=sys.stderr if stream is None else stream, flush=True)
    except (OSError, ValueError):
        return line
    return line
