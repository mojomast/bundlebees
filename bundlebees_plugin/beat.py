"""The beat flow: the heartbeat's duties, with the reconciliation sweep first.

The beat is the coordinator's recurring heartbeat turn (the catch-up cron).
Its duties used to be implicit and sequenced by whatever the turn happened to
be busy with — which is how the gap happened: while the beats were working the
merge wave, completed bees whose branches were never pushed sat unprocessed
for 30+ minutes because nothing diffed the launcher's attempt list against
origin. This module makes completed-attempt reconciliation a *first-class*
duty: every beat runs :func:`beat_reconciliation.sweep` — the launcher-side
diff the no-LLM scout structurally cannot do — before and regardless of the
merge stream, and reports both duties even when one fails or is busy.

The scout's origin-delta detection is unchanged: :func:`scout_tick` performs
exactly the same read-only classification the sweep uses, writes nothing, and
still leaves the acting to the beat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from . import beat_reconciliation
from .security import scrub

SCHEMA = "rizzstack.beat-report/1"

#: The beat's duties, in the order the beat runs them. The reconciliation
#: sweep is first-class: it is never skipped because merge work is pending.
DUTY_SWEEP = "reconcile_completed_attempts"
DUTY_MERGE = "merge_stream"

_MAX_TEXT = 300


def _clip(text: str) -> str:
    return str(text)[:_MAX_TEXT]


def beat(
    state_dir: Path,
    *,
    launcher: Any,
    origin_branches: Any,
    open_prs: Any,
    merge_stream: Callable[[], Any] | None = None,
    processor: Any = None,
    branch_of: Callable[[Mapping[str, Any]], Any] | None = None,
    now: Any = None,
    wait_s: float = 0.0,
) -> dict[str, Any]:
    """Run one beat: the reconciliation sweep first, then the merge stream.

    ``merge_stream`` is the beat's existing wave work (integration ticks and
    the like); a busy or failing merge stream never gates or eats the sweep.
    The report carries every duty's own status, and the beat's overall status
    is ``busy``/``partial``/``ok`` accordingly.
    """

    duties: dict[str, Any] = {}
    duties[DUTY_SWEEP] = beat_reconciliation.sweep(
        Path(state_dir),
        launcher=launcher,
        origin_branches=origin_branches,
        open_prs=open_prs,
        processor=processor,
        branch_of=branch_of,
        now=now,
        wait_s=wait_s,
    )
    if merge_stream is not None:
        try:
            outcome = merge_stream()
        except Exception as exc:  # noqa: BLE001 - the beat outlives its duties
            duties[DUTY_MERGE] = {
                "status": "error",
                "error": _clip(scrub(f"{type(exc).__name__}: {exc}")),
            }
        else:
            duties[DUTY_MERGE] = (
                dict(outcome)
                if isinstance(outcome, Mapping)
                else {"status": "ok", "result": _clip(scrub(str(outcome)))}
            )
    statuses = [str(duty.get("status") or "ok") for duty in duties.values()]
    # The merge stream being busy is normal operation, not a beat failure: the
    # overall status reflects a busy sweep, a failing duty, or success.
    overall = (
        "busy"
        if str(duties[DUTY_SWEEP].get("status") or "") == "busy"
        else ("partial" if "error" in statuses else "ok")
    )
    return {
        "schema": SCHEMA,
        "at": beat_reconciliation.stamp(now),
        "status": overall,
        "duties": duties,
    }


def scout_tick(
    state_dir: Path,
    *,
    launcher: Any,
    origin_branches: Any,
    open_prs: Any,
    branch_of: Callable[[Mapping[str, Any]], Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """The no-LLM scout tick: diff the launcher attempt list against origin.

    Read-only end to end: the same classification the sweep uses, no
    processing, no claim files, no durable writes. The scout keeps reporting;
    the beat keeps acting.
    """

    return beat_reconciliation.sweep(
        Path(state_dir),
        launcher=launcher,
        origin_branches=origin_branches,
        open_prs=open_prs,
        processor=None,
        branch_of=branch_of,
        now=now,
        record=False,
        claim=False,
    )


__all__ = ["DUTY_MERGE", "DUTY_SWEEP", "SCHEMA", "beat", "scout_tick"]
