"""Security helpers (stub). Wave A / BB01 replaces this with the real implementations."""

from __future__ import annotations

from typing import Any, Mapping


def scrub(value: Any) -> Any:
    """Redact secrets, hosts, and credential-shaped strings. BB01 implements this."""
    return value


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal exact-name environment for a subprocess. BB01 implements this."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C"}
    if extra:
        env.update(extra)
    return env


def contain_path(path: str, root: str) -> str:
    """Resolve path and reject escapes from root. BB01 implements this."""
    return path
