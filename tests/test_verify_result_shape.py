"""Regression tests for the launcher result envelope.

The real envelope from launcher 0.1.16 (schema ``orbit.local-worker.result/v1``)
carries the patch as an object — ``{"patch": {"diff": "...", "files": [...]}}`` —
while the public, model-facing ``result()`` strips it entirely. ``verify`` needs
the patch, so it reads the envelope through ``result_envelope`` and accepts both
shapes. These tests pin that behaviour using the recorded structure.
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import cli
from bundlebees_plugin.cli import CliError


DIFF = (
    "diff --git a/bundlebees_plugin/reconcile.py b/bundlebees_plugin/reconcile.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/bundlebees_plugin/reconcile.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+# reconcile\n"
    "+VALUE = 1\n"
)


def real_envelope() -> dict:
    """Trimmed copy of the structure launcher 0.1.16 actually returned."""

    return {
        "schema": "orbit.local-worker.result/v1",
        "attempt_id": "att_d1368ff77cba4de3",
        "source": {
            "project": "bundlebees",
            "repo_rev": "1b23f61e27c3fce240ca5718c5d04fa911144a80",
            "launcher": "0.1.16",
        },
        "observed": {"status": "idle", "stop_reason": "end_turn"},
        "patch": {
            "diff": DIFF,
            "files": [
                {
                    "path": "bundlebees_plugin/reconcile.py",
                    "status": "A",
                    "sha256": "dc12f8d509f9f2abcc202256674e02cfa4858d60ac88b46f5c59c11d81503608",
                    "change": "added",
                    "in_assigned_scope": True,
                }
            ],
        },
        "checks": [{"name": "tests", "passed": True}],
        "note": "worker_claims are self-reported by the model",
    }


class EnvelopeClient:
    """Launcher client that exposes the unredacted envelope."""

    def __init__(self, envelope):
        self.envelope = envelope
        self.calls: list[str] = []

    def result_envelope(self, attempt_id: str) -> dict:
        self.calls.append(attempt_id)
        return dict(self.envelope)


class RedactingClient:
    """Client shaped like the model-facing surface: no patch, no envelope."""

    def result(self, attempt_id: str) -> dict:
        data = real_envelope()
        data.pop("patch")
        return data


def test_result_patch_accepts_the_real_object_shape():
    assert cli._result_patch(real_envelope()) == DIFF


def test_result_patch_still_accepts_a_bare_string():
    assert cli._result_patch({"patch": DIFF}) == DIFF


def test_fetch_result_uses_the_envelope_and_finds_the_patch():
    client = EnvelopeClient(real_envelope())

    data = cli._fetch_result(client, "att_d1368ff77cba4de3")

    assert client.calls == ["att_d1368ff77cba4de3"]
    assert cli._result_patch(data) == DIFF
    assert data["source"]["repo_rev"].startswith("1b23f61")


def test_fetch_result_unwraps_a_nested_result_envelope():
    client = EnvelopeClient({"result": real_envelope()})

    data = cli._fetch_result(client, "att_d1368ff77cba4de3")

    assert cli._result_patch(data) == DIFF


def test_a_redacted_client_fails_closed_instead_of_inventing_a_patch():
    with pytest.raises(CliError) as exc:
        cli._fetch_result(RedactingClient(), "att_d1368ff77cba4de3")

    assert exc.value.code == "no_result_yet"


def test_an_empty_patch_object_is_not_a_patch():
    envelope = real_envelope()
    envelope["patch"] = {"diff": "   ", "files": []}

    assert cli._result_patch(envelope) is None
