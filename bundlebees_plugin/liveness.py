"""Shared pid-liveness check for durable claim protocols.

Every claim record in the plugin stores the owning process id.  Adoption and
recovery must agree on exactly one answer to "is that process still running?",
so the check lives here instead of being reimplemented per module.
"""

from __future__ import annotations

import os


def pid_alive(pid: object) -> bool:
    """Whether ``pid`` names a live process; a malformed pid is not alive."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows host
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, OSError):
        return False
    return True
