"""Regression tests for the launcher result envelope.

The real envelopes from launcher 0.1.17 (the pinned release) and the older 0.1.16
(schema ``orbit.local-worker.result/v1``) carry the patch as an object — ``{"patch": {"diff": "...", "files": [...]}}`` —
while the public, model-facing ``result()`` strips it entirely. ``verify`` needs
the patch, so it reads the envelope through ``result_envelope`` and accepts both
shapes. These tests pin that behaviour against both recorded structures.
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


def recorded_0116_envelope() -> dict:
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


# Recorded from launcher 0.1.17 (hyvv 1fcf1039), attempt att_f6084b60ee8547cd (applied as bundlebees #190).
# Trimmed to the envelope fields bundlebees reads: schema, attempt_id, source (project, repo_rev,
# launcher), observed (status, stop_reason), patch (diff, files), checks (name, passed), note. The diff
# keeps the real first file header and one real hunk; patch.files is the full real list. Launcher-side
# observation detail (host, sandbox, workdir, artifacts, environment, usage, worker_claims) is dropped.
DIFF_0117 = (
    'diff --git a/bundlebees_plugin/queue.py b/bundlebees_plugin/queue.py\n'
    'index f8c91bb..378d493 100644\n'
    '--- a/bundlebees_plugin/queue.py\n'
    '+++ b/bundlebees_plugin/queue.py\n'
    '@@ -856,6 +919,7 @@ def run_once(state_dir: Path, queue: QueueAdapter, *, tick: Any, on_status: Any\n'
    '             return {\n'
    '                 "action": "idle",\n'
    '                 "feature": None,\n'
    '+                "refused": refused,\n'
    '                 "orphaned": orphaned,\n'
    '                 "orphaned_live": orphaned_live,\n'
    '             }\n'
)


def real_envelope() -> dict:
    """Trimmed copy of the structure launcher 0.1.17 (the pinned release) actually returned."""

    return {
        "schema": "orbit.local-worker.result/v1",
        "attempt_id": "att_f6084b60ee8547cd",
        "source": {
            "project": "bundlebees",
            "repo_rev": "aeda8f8b7ceb7e0b451ede230245d0cd7b96c805",
            "launcher": "0.1.18"
        },
        "observed": {
            "status": "idle",
            "stop_reason": "end_turn"
        },
        "patch": {
            "diff": DIFF_0117,
            "files": [
                {
                    "path": "bundlebees_plugin/queue.py",
                    "status": "M",
                    "sha256": "6c3e5ae62922beacc76340ec21230fef482f4a372aacd761824298c8d05c3012",
                    "change": "changed",
                    "in_assigned_scope": True
                },
                {
                    "path": "tests/test_queue.py",
                    "status": "M",
                    "sha256": "08375c9c3e2af156797f5cfd336990832febc66d2e8c3b69f29535273399fe1e",
                    "change": "changed",
                    "in_assigned_scope": True
                }
            ]
        },
        "checks": [
            {
                "name": "queue suite RED on base",
                "passed": False
            },
            {
                "name": "queue suite after fix",
                "passed": True
            },
            {
                "name": "full suite after fix",
                "passed": False
            }
        ],
        "note": "worker_claims are self-reported by the model; only 'observed' was measured by the launcher"
    }


RECORDINGS = {
    "0.1.17": (real_envelope, DIFF_0117),
    "0.1.16": (recorded_0116_envelope, DIFF),
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


LAUNCHERS = pytest.mark.parametrize("launcher", sorted(RECORDINGS))


@LAUNCHERS
def test_result_patch_accepts_the_real_object_shape(launcher):
    envelope, diff = RECORDINGS[launcher]
    assert envelope()["source"]["launcher"] == launcher
    assert cli._result_patch(envelope()) == diff


def test_result_patch_still_accepts_a_bare_string():
    assert cli._result_patch({"patch": DIFF}) == DIFF


@LAUNCHERS
def test_fetch_result_uses_the_envelope_and_finds_the_patch(launcher):
    envelope, diff = RECORDINGS[launcher]
    client = EnvelopeClient(envelope())
    attempt = envelope()["attempt_id"]

    data = cli._fetch_result(client, attempt)

    assert client.calls == [attempt]
    assert cli._result_patch(data) == diff
    assert data["source"]["repo_rev"] == envelope()["source"]["repo_rev"]


@LAUNCHERS
def test_fetch_result_unwraps_a_nested_result_envelope(launcher):
    envelope, diff = RECORDINGS[launcher]
    client = EnvelopeClient({"result": envelope()})

    data = cli._fetch_result(client, envelope()["attempt_id"])

    assert cli._result_patch(data) == diff


def test_a_redacted_client_fails_closed_instead_of_inventing_a_patch():
    with pytest.raises(CliError) as exc:
        cli._fetch_result(RedactingClient(), "att_f6084b60ee8547cd")

    assert exc.value.code == "no_result_yet"


@LAUNCHERS
def test_an_empty_patch_object_is_not_a_patch(launcher):
    envelope = RECORDINGS[launcher][0]()
    envelope["patch"] = {"diff": "   ", "files": []}

    assert cli._result_patch(envelope) is None

