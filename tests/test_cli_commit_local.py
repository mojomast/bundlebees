"""RED test for `verify --commit-local` with the DEFAULT workdir path.

Drives ``_cmd_verify_commit_local`` directly with minimal stub ctx/args
objects and no ``--workdir`` in args, so the function must evaluate the
default workdir line itself. The stub client serves an envelope with no
patch, so once the workdir line executes the function proceeds to the
envelope read and raises the controlled ``CliError`` with code
``no_result_yet`` — proving the default-workdir path is no longer dead
code.

Before the fix, the workdir line references ``os`` without importing it
and the command dies with ``NameError: name 'os' is not defined`` before
the client is ever contacted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bundlebees_plugin import cli

ATTEMPT_ID = "att_f9e680d217bb458b"


class _StubClient:
    """Result-envelope stub: records the fetch, serves no patch."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def result_envelope(self, attempt_id: str) -> dict:
        self.calls.append(attempt_id)
        # A real "worker still running" envelope: a Mapping with no patch
        # anywhere in it, so _result_patch() comes back empty.
        return {"attempt_id": attempt_id, "status": "running", "result": {}}


def test_commit_local_default_workdir_reaches_envelope(tmp_path):
    client = _StubClient()
    ctx = SimpleNamespace(bundlebees_client=client)
    # No `workdir` attribute: the command must resolve the default workdir
    # itself. `state_dir` is set so _state_dir() needs no plugin data dir.
    args = SimpleNamespace(
        attempt_id=ATTEMPT_ID,
        state_dir=str(tmp_path / "state"),
    )

    with pytest.raises(cli.CliError) as excinfo:
        cli._cmd_verify_commit_local(ctx, args)

    # The controlled refusal, not a crash: the command got past the
    # default-workdir line, built its client, and read the envelope.
    assert excinfo.value.code == "no_result_yet"
    assert client.calls == [ATTEMPT_ID]
