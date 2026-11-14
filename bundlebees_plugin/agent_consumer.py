"""Out-of-band decision consumer for durable agent work items (M4.3 lane A).

This is a separate process, not an inference call inside the queue tick::

    python -m bundlebees_plugin.agent_consumer --state DIR --consumer NAME \
        [--adapter hermes|deterministic] [--once | --interval N]

One invocation claims at most one item and then:

1. if a canonical decision already exists, completes the item idempotently;
2. else if a proposal is already persisted, asks
   ``hermes_adapter.adopt_proposal`` to compile it — the model is never asked
   again for a request that already has an answer;
3. else invokes the selected adapter (Hermes or the deterministic adapter).

A failure releases the item for one bounded retry; the second failed attempt
refuses it (``MAX_ATTEMPTS`` claims in total). The consumer never dispatches a
bee and never touches a job's task state; applying a decision stays with the
mechanical reconciler.

Test-only crash boundary
------------------------
When ``BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL=1`` is set in the consumer, the
Hermes path installs a guard around ``recovery.record_decision``. The guard runs
after ``decide_with_hermes`` has persisted the proposal and immediately before
the decision would be recorded: it writes a fsynced marker under ``recovery/``
and exits with status 97. A restart therefore sees a proposal with no decision,
adopts it, and never re-invokes the model.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import agent_work, hermes_adapter, job as jobs, recovery

CRASH_ENV = "BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL"
CRASH_EXIT_CODE = 97
CRASH_SCHEMA = "rizzstack.agent-crash/1"

ADAPTERS: tuple[str, ...] = ("hermes", "deterministic")


class ConsumerError(Exception):
    """The consumer could not finish an item; the caller bounds the retry."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clock(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ConsumerError("clock_invalid", "now must be epoch seconds or an ISO timestamp") from exc


def _error_text(exc: BaseException) -> str:
    """A short, agent-agnostic refusal: a code, never a prompt or transcript."""

    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    if isinstance(exc, ConsumerError) and exc.code:
        return exc.code
    return type(exc).__name__


def _relative(state_dir: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(state_dir).resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _request_for(state_dir: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    request = recovery.load_request(Path(state_dir), str(item.get("request_id") or ""))
    if str(request.get("request_id") or "") != str(item.get("request_id") or ""):
        raise ConsumerError("work_request_mismatch", "the request id does not match the work item")
    if str(request.get("request_digest") or "") != str(item.get("request_digest") or ""):
        raise ConsumerError(
            "work_request_mismatch",
            "the durable request digest does not match the work item",
            "Refuse: a work item that points at another request must not be decided.",
        )
    return request


def _task_for(state_dir: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    job = jobs.load_job(Path(state_dir), str(request.get("job_id") or ""), recover=False)
    task_id = str(request.get("task_id") or "")
    for task in job.get("tasks") or []:
        if str(task.get("task_id") or "") == task_id:
            return dict(task)
    raise ConsumerError("work_request_mismatch", f"job {request.get('job_id')!r} has no task {task_id!r}")


def _proposal_path(state_dir: Path, request: Mapping[str, Any]) -> Path:
    return hermes_adapter.proposal_path(Path(state_dir), request)


def _marker_path(state_dir: Path, request: Mapping[str, Any]) -> Path:
    job_id = str(request.get("job_id") or "")
    task_id = str(request.get("task_id") or "")
    ordinal = recovery.request_ordinal(Path(state_dir), str(request.get("request_id") or ""))
    return recovery.recovery_dir(Path(state_dir), job_id) / f"{task_id}.{ordinal}.proposal.crash.json"


def _write_fsync(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dfd = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _crash_guard(state_dir: Path, item: Mapping[str, Any]):
    """The test-only boundary: persist a marker, then die before the decision."""

    state = Path(state_dir)
    work_id = str(item.get("work_id") or "")

    def guarded(
        call_state: Path,
        request_id: str,
        decision: Mapping[str, Any],
        *,
        adapter: str = "deterministic",
        now: Any = None,
    ) -> dict[str, Any]:
        del decision, adapter, now
        request = recovery.load_request(Path(call_state), str(request_id))
        proposal = _proposal_path(Path(call_state), request)
        marker = _marker_path(Path(call_state), request)
        try:
            _write_fsync(
                marker,
                {
                    "schema": CRASH_SCHEMA,
                    "work_id": work_id,
                    "request_id": str(request.get("request_id") or ""),
                    "request_digest": str(request.get("request_digest") or ""),
                    "proposal": _relative(state, proposal),
                    "decision_recorded": False,
                    "pid": os.getpid(),
                    "at": _iso(time.time()),
                },
            )
        finally:
            # The crash is the point of the hook: never fall through to the
            # real recorder, whether or not the marker reached the disk.
            os._exit(CRASH_EXIT_CODE)
        raise AssertionError("unreachable")  # pragma: no cover

    return guarded


def _decide_with_hermes(
    state_dir: Path,
    item: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    repo: Path | None,
    timeout_s: int,
    model: str | None,
    provider: str | None,
    now: Any,
) -> dict[str, Any]:
    original = recovery.record_decision
    if os.environ.get(CRASH_ENV) == "1":
        recovery.record_decision = _crash_guard(Path(state_dir), item)  # type: ignore[assignment]
    try:
        return hermes_adapter.decide_with_hermes(
            Path(state_dir),
            str(request.get("request_id") or ""),
            repo=repo,
            timeout_s=timeout_s,
            model=model,
            provider=provider,
            now=now,
        )
    finally:
        recovery.record_decision = original


def _decision_from(outcome: Any, fallback: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], Any]:
    """Accept the adapter's documented outcome shape without guessing semantics."""

    if isinstance(outcome, Mapping):
        decision = outcome.get("decision")
        if isinstance(decision, Mapping):
            return dict(decision), outcome.get("proposal") or (fallback or {}).get("proposal")
        if "decision" in outcome and "schema" in outcome and "adapter" in outcome:
            return dict(outcome), (fallback or {}).get("proposal")
    raise ConsumerError(
        "adapter_outcome_invalid",
        "the adapter returned no canonical decision",
        "The adapter must return {'decision': <canonical decision>, 'proposal': <path>}.",
    )


def _adopt(
    state_dir: Path,
    item: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    repo: Path | None,
    now: Any,
) -> dict[str, Any]:
    """Adopt a persisted proposal; this function must never ask the model."""

    adopt = getattr(hermes_adapter, "adopt_proposal", None)
    if not callable(adopt):
        raise ConsumerError(
            "adopt_unavailable",
            "hermes_adapter.adopt_proposal is not available; a persisted proposal cannot be adopted",
            "Install the lane-B adapter that adopts a recorded proposal before running the consumer.",
        )
    return adopt(Path(state_dir), str(request.get("request_id") or ""), repo=repo, now=now)


def _proposal_ref(state_dir: Path, value: Any, fallback: Path | None = None) -> str | None:
    """Store only a state-relative proposal path, whatever the adapter returned."""

    if value not in (None, ""):
        path = Path(str(value))
        return _relative(state_dir, path) if path.is_absolute() else str(value)
    if fallback is not None and Path(fallback).exists():
        return _relative(state_dir, Path(fallback))
    return None


def _summary(
    action: str,
    item: Mapping[str, Any],
    *,
    consumer: str = "",
    decision: Mapping[str, Any] | None = None,
    proposal: Any = None,
) -> dict[str, Any]:
    return {
        "consumer": consumer,
        "work_id": str(item.get("work_id") or ""),
        "request_id": str(item.get("request_id") or ""),
        "state": str(item.get("state") or ""),
        "action": action,
        "proposal": proposal,
        "decision": dict(decision) if isinstance(decision, Mapping) else None,
    }


def _run_item(
    state_dir: Path,
    item: Mapping[str, Any],
    *,
    consumer: str,
    adapter: str | None,
    repo: Path | None,
    timeout_s: int,
    model: str | None,
    provider: str | None,
    now: Any,
) -> dict[str, Any]:
    state = Path(state_dir)
    work_id = str(item.get("work_id") or "")
    pid = os.getpid()
    request = _request_for(state, item)
    proposal_path = _proposal_path(state, request)
    proposal_exists = proposal_path.exists()
    decision = recovery.read_decision(state, request)

    if decision is not None:
        completed = agent_work.complete(
            state,
            work_id,
            consumer=consumer,
            pid=pid,
            proposal=_relative(state, proposal_path) if proposal_exists else None,
            decision=decision,
            now=now,
        )
        return _summary(
            "already_decided",
            completed,
            consumer=consumer,
            decision=decision,
            proposal=_relative(state, proposal_path) if proposal_exists else None,
        )

    if proposal_exists:
        outcome = _adopt(state, item, request, repo=repo, now=now)
        adopted, proposal_ref = _decision_from(outcome, {"proposal": _relative(state, proposal_path)})
        proposal_ref = _proposal_ref(state, proposal_ref, proposal_path)
        completed = agent_work.complete(
            state,
            work_id,
            consumer=consumer,
            pid=pid,
            proposal=proposal_ref,
            decision=adopted,
            now=now,
        )
        return _summary("adopted", completed, consumer=consumer, decision=adopted, proposal=proposal_ref)

    chosen = str(adapter or item.get("adapter") or "hermes")
    if chosen not in ADAPTERS:
        raise ConsumerError(
            "adapter_unknown",
            f"adapter {chosen!r} is not one of {', '.join(ADAPTERS)}",
            "Use --adapter hermes or --adapter deterministic.",
        )
    # A claim beyond the budget may still adopt a proposal or observe a decision
    # (both are inference-free), but it must never call an adapter again.
    claims = int(item.get("claims") or item.get("attempts") or 0)
    if claims > agent_work.MAX_ATTEMPTS:
        raise ConsumerError(
            "attempts_exhausted",
            f"work item {work_id!r} has been claimed {claims} times; no adapter is invoked",
        )

    if chosen == "deterministic":
        task = _task_for(state, request)
        proposed = recovery.deterministic_adapter(request, task=task)
        stored = recovery.record_decision(
            state, str(request.get("request_id") or ""), proposed, adapter="deterministic", now=now
        )
        completed = agent_work.complete(
            state, work_id, consumer=consumer, pid=pid, decision=stored, now=now
        )
        return _summary("decided", completed, consumer=consumer, decision=stored)

    outcome = _decide_with_hermes(
        state,
        item,
        request,
        repo=repo,
        timeout_s=timeout_s,
        model=model,
        provider=provider,
        now=now,
    )
    decided, proposal_ref = _decision_from(outcome, {"proposal": _relative(state, proposal_path)})
    proposal_ref = _proposal_ref(state, proposal_ref, proposal_path)
    completed = agent_work.complete(
        state,
        work_id,
        consumer=consumer,
        pid=pid,
        proposal=proposal_ref,
        decision=decided,
        now=now,
    )
    return _summary("decided", completed, consumer=consumer, decision=decided, proposal=proposal_ref)


def consume_once(
    state_dir: Path,
    *,
    consumer: str,
    adapter: str | None = None,
    repo: Path | None = None,
    timeout_s: int = 300,
    model: str | None = None,
    provider: str | None = None,
    now: Any = None,
) -> dict[str, Any] | None:
    """Claim and process at most one item. ``None`` means nothing was claimable."""

    name = str(consumer or "").strip()
    if not name:
        raise ConsumerError("consumer_invalid", "the consumer must have a name")
    pid = os.getpid()
    for candidate in agent_work.list(Path(state_dir)):
        if str(candidate.get("state") or "") not in {"pending", "claimed"}:
            continue
        claimed = agent_work.claim(
            Path(state_dir),
            str(candidate.get("work_id") or ""),
            consumer=name,
            pid=pid,
            now=now,
        )
        if claimed is None:
            continue
        try:
            return _run_item(
                Path(state_dir),
                claimed,
                consumer=name,
                adapter=adapter,
                repo=Path(repo) if repo is not None else None,
                timeout_s=int(timeout_s),
                model=model,
                provider=provider,
                now=now,
            )
        except agent_work.WorkError as exc:
            if exc.code == "claim_not_held":
                # Another consumer adopted the stale claim; its turn owns the item.
                return {"consumer": name, "work_id": claimed.get("work_id"), "state": "lost_claim", "error": exc.code}
            return _fail(Path(state_dir), claimed, name, pid, exc, now)
        except Exception as exc:  # noqa: BLE001 - every adapter refusal is bounded
            return _fail(Path(state_dir), claimed, name, pid, exc, now)
    return None


def _fail(
    state_dir: Path,
    item: Mapping[str, Any],
    consumer: str,
    pid: int,
    exc: BaseException,
    now: Any,
) -> dict[str, Any]:
    """Release the first failure, refuse the last; never retry unbounded."""

    work_id = str(item.get("work_id") or "")
    error = _error_text(exc)
    attempts = int(item.get("attempts") or 0)
    try:
        if attempts >= agent_work.MAX_ATTEMPTS:
            stored = agent_work.refuse(
                state_dir, work_id, consumer=consumer, pid=pid, error=error, now=now
            )
            action = "refused"
        else:
            stored = agent_work.release(
                state_dir, work_id, consumer=consumer, pid=pid, error=error, now=now
            )
            action = "released"
    except agent_work.WorkError as lost:
        return {
            "consumer": consumer,
            "work_id": work_id,
            "state": "lost_claim" if lost.code == "claim_not_held" else "error",
            "action": "none",
            "error": lost.code,
        }
    return {
        "consumer": consumer,
        "work_id": work_id,
        "request_id": str(item.get("request_id") or ""),
        "state": str(stored.get("state") or ""),
        "action": action,
        "error": error,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bundlebees_plugin.agent_consumer",
        description="Consume recovery work items outside the mechanical tick.",
    )
    parser.add_argument("--state", required=True, help="Bundlebees state directory.")
    parser.add_argument("--consumer", required=True, help="Consumer name recorded on the claim.")
    parser.add_argument("--adapter", choices=ADAPTERS, default=None,
                        help="Decision adapter; defaults to the item's hint, then hermes.")
    parser.add_argument("--once", action="store_true", help="Process at most one item and exit.")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between attempts; without --once the consumer loops.")
    parser.add_argument("--repo", default=None, help="Repository for the Hermes evidence package.")
    parser.add_argument("--timeout-s", type=int, default=300, help="One Hermes turn timeout (default 300).")
    parser.add_argument("--model", default=None, help="Hermes model override.")
    parser.add_argument("--provider", default=None, help="Hermes provider override.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.once and args.interval is not None:
        print(json.dumps({"error": "argument_invalid", "message": "--once and --interval are mutually exclusive"}))
        return 2
    interval = args.interval
    if interval is not None and int(interval) <= 0:
        print(json.dumps({"error": "argument_invalid", "message": "--interval must be positive"}))
        return 2
    if interval is None and not args.once:
        args.once = True

    state = Path(args.state)
    while True:
        try:
            result = consume_once(
                state,
                consumer=str(args.consumer),
                adapter=args.adapter,
                repo=Path(args.repo) if args.repo else None,
                timeout_s=int(args.timeout_s),
                model=args.model,
                provider=args.provider,
            )
        except Exception as exc:  # noqa: BLE001 - the CLI reports a refusal, never a traceback
            print(json.dumps({"error": _error_text(exc), "message": str(exc)}))
            return 1
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        if args.once:
            return 0
        time.sleep(float(interval))


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
