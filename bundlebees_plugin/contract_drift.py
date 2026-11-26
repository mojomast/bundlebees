"""Report-only drift findings for the frozen rizzstack brief contract."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from bundlebees_plugin.brief import brief_sha256
from bundlebees_plugin.rizzstack_validators import validate_brief

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
_MODE = "report_only"


def check_brief_drift(
    document: Any, *, schema_dir: str | Path | None = None
) -> list[str]:
    directory = _SCHEMA_DIR if schema_dir is None else schema_dir
    try:
        return validate_brief(document, schema_dir=directory)
    except Exception as exc:  # noqa: BLE001 - report-only: a failed check must never raise
        return [
            "$: the frozen brief schema check could not be completed "
            f"({type(exc).__name__})"
        ]


def record_brief_schema_drift(
    state_dir: str | Path | None, brief: Mapping[str, Any], problems: list[str]
) -> dict[str, Any]:
    findings = list(problems)
    try:
        digest = brief_sha256(brief)
    except Exception:
        digest = ""
    block: dict[str, Any] = {
        "mode": _MODE,
        "count": len(findings),
        "brief_sha256": digest,
        "problems": findings,
        "recorded": False,
    }
    if state_dir is None or not findings:
        return block
    try:
        directory = Path(state_dir) / "brief-schema-drift"
        path = directory / "report-only.jsonl"
        os.makedirs(directory, mode=0o700, exist_ok=True)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "mode": _MODE,
            "count": len(findings),
            "brief_sha256": digest,
            "project": brief.get("project"),
            "problems": findings,
        }
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        block["recorded"] = True
    except Exception as exc:  # noqa: BLE001 - recording is best effort; never abort a dispatch
        block["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return block
