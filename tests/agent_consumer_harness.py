"""Test harness for the agent consumer's lane-B ``adopt_proposal`` dependency.

M4.3 lane A owns the consumer; lane B owns ``hermes_adapter.adopt_proposal``.
Until that function lands, this harness supplies a faithful local implementation
built only from the adapter's existing primitives, then delegates to the real
CLI. It also replaces ``decide_with_hermes`` with a loud failure so the restart
test proves the model is never re-invoked for a persisted proposal.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from bundlebees_plugin import hermes_adapter, job as jobs, recovery


def _stub_adopt(
    state_dir: Path,
    request_id: str,
    *,
    repo: Path | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Compile a persisted proposal record into a canonical decision."""

    del repo
    state = Path(state_dir)
    request = recovery.load_request(state, request_id)
    path = hermes_adapter.proposal_path(state, request)
    record = json.loads(path.read_text(encoding="utf-8"))
    parsed = record.get("parsed")
    if not isinstance(parsed, Mapping):
        raise recovery.RecoveryError(
            "proposal_invalid",
            "the persisted proposal has no parsed answer to adopt",
            "A malformed proposal is evidence of a refusal; it is not re-rolled.",
        )
    job = jobs.load_job(state, str(request.get("job_id") or ""), recover=False)
    task = next(
        (
            item
            for item in job.get("tasks") or []
            if str(item.get("task_id") or "") == str(request.get("task_id") or "")
        ),
        None,
    )
    if task is None:
        raise recovery.RecoveryError("proposal_invalid", "the request's task no longer exists")
    decided_at = str(record.get("recorded_at") or "") or hermes_adapter._decided_at(now)
    decision = hermes_adapter.compile_decision(
        request,
        parsed,
        adapter_name=str(record.get("adapter") or ""),
        decided_at=decided_at,
        task=task,
    )
    attempts, corrections = recovery._ledger(job, task)
    module = recovery.protocol_validator()
    module.validate_pair(request, decision, attempts_used=attempts, corrections_used=corrections)
    stored = recovery.record_decision(
        state, request_id, decision, adapter=str(record.get("adapter") or "hermes")
    )
    return {"request_id": request_id, "proposal": str(path), "decision": stored}


_original_adopt = getattr(hermes_adapter, "adopt_proposal", None) or _stub_adopt


def _logged_adopt(*args: Any, **kwargs: Any) -> dict[str, Any]:
    log = os.environ.get("ADOPT_LOG")
    if log:
        request_id = str(args[1] if len(args) > 1 else kwargs.get("request_id") or "")
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(request_id + "\n")
    return _original_adopt(*args, **kwargs)


hermes_adapter.adopt_proposal = _logged_adopt  # type: ignore[attr-defined]


def _forbidden_decide(*args: Any, **kwargs: Any) -> dict[str, Any]:
    log = os.environ.get("DECIDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("called\n")
    raise AssertionError("the model was invoked even though a proposal already existed")


hermes_adapter.decide_with_hermes = _forbidden_decide  # type: ignore[assignment]

from bundlebees_plugin.agent_consumer import main  # noqa: E402

raise SystemExit(main())
