"""Named fault checkpoints for crash-recovery tests. Inert in production.

A *checkpoint* is a place in production code where a test may want the process to
die: after a dispatch was acknowledged, after a wave record was persisted, after
a publication landed. ``checkpoint(name, **payload)`` is the only call site
production code needs; it costs one dict lookup and does nothing at all unless a
hook was registered for that exact name **in this process**.

Why process-local registration rather than an environment variable read here:
a crash test registers its hook in the child process it is about to kill, and the
restart is a fresh process with no hooks. Nothing survives in the environment, so
a restarted coordinator can never re-trigger the fault, and no shipped
configuration can arm a checkpoint by accident.

Hooks receive the payload as keyword arguments. A hook that returns normally
lets execution continue; a hook that raises simulates a failure; a hook may also
terminate the process (the crash tests use ``os._exit`` after writing a marker).

The module deliberately has no imports from the rest of the package: production
modules import it lazily and must keep working when it is absent.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

__all__ = [
    "FaultError",
    "Hook",
    "checkpoint",
    "clear",
    "register",
    "registered",
    "unregister",
]

#: A checkpoint hook. Called as ``hook(**payload)``.
Hook = Callable[..., None]

_HOOKS: dict[str, Hook] = {}
_LOCK = threading.RLock()


class FaultError(Exception):
    """A checkpoint name or hook is invalid. A test bug, never a runtime state."""


def _checked_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text:
        raise FaultError("a checkpoint name must be a non-empty string")
    if any(ch.isspace() for ch in text):
        raise FaultError(f"checkpoint name {text!r} must not contain whitespace")
    return text


def register(name: str, hook: Hook) -> Hook:
    """Arm ``name`` in this process. Returns the hook for convenient cleanup."""

    key = _checked_name(name)
    if not callable(hook):
        raise FaultError(f"hook for {key!r} is not callable")
    with _LOCK:
        existing = _HOOKS.get(key)
        if existing is not None and existing is not hook:
            raise FaultError(f"checkpoint {key!r} already has a hook registered in this process")
        _HOOKS[key] = hook
    return hook


def unregister(name: str) -> Hook | None:
    """Disarm ``name``; returns the hook that was registered, if any."""

    try:
        key = _checked_name(name)
    except FaultError:
        return None
    with _LOCK:
        return _HOOKS.pop(key, None)


def clear() -> None:
    """Disarm every checkpoint in this process (test teardown)."""

    with _LOCK:
        _HOOKS.clear()


def registered() -> tuple[str, ...]:
    """The names armed in this process, sorted. Diagnostics only."""

    with _LOCK:
        return tuple(sorted(_HOOKS))


def checkpoint(name: str, **payload: Any) -> None:
    """Fire ``name`` if — and only if — a hook is armed for it.

    Production passes whatever durable context it already has at that point
    (ids, refs, digests). The payload is not validated: with no hook armed the
    call does not even iterate it.
    """

    try:
        key = _checked_name(name)
    except FaultError:
        return
    with _LOCK:
        hook = _HOOKS.get(key)
    if hook is None:
        return
    hook(**payload)
