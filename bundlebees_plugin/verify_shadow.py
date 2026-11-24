"""The one production entry into the verify-worker module: SHADOW mode only.

``BUNDLEBEES_VERIFY_WORKERS=shadow`` makes the pipeline observe verification
with the worker lane alongside the ordinary in-beat verify. The in-beat verdict
is authoritative: :func:`observe` always returns the exact verdict object it was
given, never re-runs the in-beat verifier, and records any shadow failure as one
JSON line instead of raising it. Mode ``on`` is not wired and is refused.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import verify_workers

ENV_MODE = "BUNDLEBEES_VERIFY_WORKERS"

MODE_IN_BEAT = "in-beat"
MODE_SHADOW = "shadow"
MODE_ON = "on"

ERROR_SCHEMA = "bundlebees.verify-shadow-error/1"

__all__ = [
    "ENV_MODE",
    "MODE_IN_BEAT",
    "MODE_SHADOW",
    "MODE_ON",
    "ERROR_SCHEMA",
    "mode_from_env",
    "observe",
]


def mode_from_env(environ: Mapping[str, str] | None = None) -> str:
    """Resolve the verify-workers mode from the environment.

    Unset or empty means ``in-beat`` (the default). ``shadow`` is the only mode
    wired into production; ``on`` is refused as ``mode_not_wired`` and any other
    value is refused as ``mode_invalid``. Matching is case- and
    whitespace-insensitive.
    """

    source = os.environ if environ is None else environ
    raw = source.get(ENV_MODE, "") if isinstance(source, Mapping) else ""
    text = str(raw).strip().lower()
    if text in ("", MODE_IN_BEAT):
        return MODE_IN_BEAT
    if text == MODE_SHADOW:
        return MODE_SHADOW
    if text == MODE_ON:
        raise verify_workers.VerifyWorkersError(
            "mode_not_wired",
            "verify-workers mode 'on' is not wired; only in-beat and shadow are available",
            "Set the flag to in-beat or shadow.",
        )
    raise verify_workers.VerifyWorkersError(
        "mode_invalid",
        f"verify-workers mode {raw!r} is not one of {MODE_IN_BEAT}, {MODE_SHADOW}, {MODE_ON}",
        "Set the flag to in-beat or shadow.",
    )


def _iso(now: Any) -> str:
    value = now() if callable(now) else now
    if value is None:
        moment = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
    else:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shadow_dir(state_dir: Any) -> Path:
    root = Path(state_dir) / "verify-workers"
    path = root / "shadow"
    path.mkdir(parents=True, exist_ok=True)
    for directory in (root, path):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return path


def _record_error(state_dir: Any, *, attempt_id: str, code: str, now: Any) -> None:
    try:
        record = {
            "schema": ERROR_SCHEMA,
            "at": _iso(now),
            "attempt_id": attempt_id,
            "code": code,
        }
        path = _shadow_dir(state_dir) / "errors.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:  # noqa: BLE001 - recording an error may never raise
        pass


def observe(
    state_dir: Any,
    repo: Any,
    *,
    mode: str,
    request_fields: Mapping[str, Any],
    in_beat_verdict: Any,
    worker: Any = None,
    key: Any = None,
    now: Any = None,
) -> Any:
    """Observe one verification in shadow mode, returning the in-beat verdict.

    ``in-beat`` does nothing and writes nothing. ``shadow`` builds the pinned
    request and dispatches the worker lane with the already-computed in-beat
    verdict as its authoritative answer. Any failure is swallowed and recorded
    as one ``errors.jsonl`` line; the in-beat verdict is always returned and is
    never recomputed.
    """

    if mode != MODE_SHADOW:
        return in_beat_verdict

    fields = request_fields if isinstance(request_fields, Mapping) else {}
    attempt_id = str(fields.get("attempt_id") or "")
    try:
        request = verify_workers.build_request(**dict(fields))
        verify_workers.dispatch_verification(
            state_dir,
            repo,
            request,
            key=key or b"",
            worker=worker,
            in_beat=lambda: in_beat_verdict,
            mode=MODE_SHADOW,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - shadow may never change the verdict
        code = exc.code if isinstance(exc, verify_workers.VerifyWorkersError) else type(exc).__name__
        try:
            _record_error(state_dir, attempt_id=attempt_id, code=str(code), now=now)
        except Exception:  # noqa: BLE001 - recording must never break the verdict guarantee
            pass
    return in_beat_verdict
