"""RED for wave A slice A6: the lease-overlap check fails closed.

If the live attempts cannot be read, dispatch must refuse with
``launcher_unreadable`` instead of treating the list as empty, and it must
never reach ``create``.
"""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeCtx, load_plugin
from tests.test_tools import FakeClient, _brief


def _ctx():
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    return ctx


def _dispatch(client):
    ctx = _ctx()
    ctx.bundlebees_client = client
    return json.loads(ctx.tools["bundlebees_dispatch"]["handler"]({"brief": _brief()}))


class RawUnreadable(FakeClient):
    def attempts_raw(self):
        raise OSError("connection reset")


class ListUnreadable(FakeClient):
    """A client without attempts_raw whose attempts() raises."""

    attempts_raw = None  # type: ignore[assignment]

    def __getattribute__(self, name):
        if name == "attempts_raw":
            raise AttributeError(name)
        return super().__getattribute__(name)

    def attempts(self):
        raise RuntimeError("launcher returned garbage")


class NotAList(FakeClient):
    def attempts_raw(self):
        return {"unexpected": "shape"}


@pytest.mark.parametrize("client_cls", [RawUnreadable, ListUnreadable, NotAList])
def test_unreadable_attempts_refuse_dispatch(client_cls):
    client = client_cls()
    payload = _dispatch(client)
    assert payload["ok"] is False
    assert payload["code"] == "launcher_unreadable"
    assert "lease" in payload["error"].lower()
    assert not any(call[0] == "create" for call in client.calls)


def test_readable_empty_attempts_still_dispatch():
    client = FakeClient()
    payload = _dispatch(client)
    assert payload["ok"] is True
    assert any(call[0] == "create" for call in client.calls)
