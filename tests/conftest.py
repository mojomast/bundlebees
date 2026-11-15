"""Test-only terminal seam.

The production authorization code now requires ``sys.stdin.isatty()`` even when
a caller passes ``interactive=True``; that flag is not a bypass. Tests exercise
the human path by presenting a fake terminal, never by claiming one.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _fake_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
