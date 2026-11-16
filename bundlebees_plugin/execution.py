"""One verifier configuration for CLI, durable jobs and integration.

This is a trusted runner over the existing sandbox, not another backend.
Its explicit policy participates in test receipt identity.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import sandbox
from .security import config_flag


def sandbox_options(config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        timeout = int(config.get("verify_timeout_s") or 900)
    except (TypeError, ValueError):
        timeout = 900
    return {
        "backend": str(config.get("verify_isolation") or "auto").strip().lower(),
        "allow_unsafe_local": config_flag(config.get("verify_allow_unsafe_local"), False),
        "allow_weak": sandbox.weak_override_enabled(
            config_flag(config.get("verify_allow_weak_unshare"), False)),
        "timeout": max(30, min(timeout, 7200)),
        "tiktoken_cache": str(config.get("sandbox_tiktoken_cache") or "").strip() or None,
        "python": str(config.get("verify_python") or "").strip() or None,
    }


def scanner(config: Mapping[str, Any], explicit: str | None = None) -> str | None:
    """Explicit flag, declared setting, legacy queue config, environment, PATH."""
    selected = (explicit or config.get("gitleaks_path")
                or (config.get("queue") or {}).get("gitleaks") or os.environ.get("GITLEAKS"))
    return str(selected) if selected else shutil.which("gitleaks")


def _content_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SandboxRunner:
    def __init__(self, config: Mapping[str, Any]):
        self.options = sandbox_options(config)

    def __call__(self, workdir: Path, argv: Sequence[str]) -> dict[str, Any]:
        return sandbox.run_in_sandbox(Path(workdir), list(argv), **self.options)

    def check_available(self) -> None:
        options = self.options
        chosen = sandbox.detect_backend(options["backend"], allow_weak=options["allow_weak"])
        if chosen is None and not options["allow_unsafe_local"]:
            raise sandbox.SandboxUnavailable("no strong sandbox backend is usable on this host")
        sandbox.resolve_sandbox_python(options["python"])
        sandbox._resolve_tiktoken_cache(options["tiktoken_cache"], backend=options["backend"])

    def receipt_identity(self) -> dict[str, Any]:
        # Recompute on each use: changing a configured executable/cache must
        # invalidate receipts even when the Python runner object is reused.
        configured = sandbox.resolve_sandbox_python(self.options["python"])
        executable = Path(configured or sys.executable).expanduser()
        identity: dict[str, Any] = {"options": dict(self.options),
                                    "executable": _content_digest(executable)}
        environment = executable.parent.parent
        manifests = [environment / "pyvenv.cfg"]
        for pattern in ("lib/python*/site-packages/*.dist-info/RECORD", "Lib/site-packages/*.dist-info/RECORD"):
            manifests.extend(environment.glob(pattern))
        identity["dependencies"] = {p.relative_to(environment).as_posix(): _content_digest(p)
                                    for p in sorted(manifests) if p.is_file()}
        cache = self.options["tiktoken_cache"]
        if cache:
            # Validate before traversal, especially for an accidental '/' or
            # coordinator-home setting. Never fingerprint arbitrary roots.
            root = sandbox._resolve_tiktoken_cache(cache, backend=self.options["backend"])
            identity["cache"] = {p.relative_to(root).as_posix(): _content_digest(p)
                                  for p in sorted(root.rglob("*")) if p.is_file()}
        return identity
