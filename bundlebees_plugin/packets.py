"""Runtime enforcement of the pinned workflow feature-packet protocol.

The protocol lives in `mojomast/workflow`. Bundlebees does not re-implement it:
it loads the validator from the pinned checkout (`WORKFLOW_SRC`) and refuses to
admit a workflow-native job when that checkout is unavailable. Legacy jobs are
untouched and are never given invented packet evidence.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


class PacketError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def workflow_src() -> Path:
    value = os.environ.get("WORKFLOW_SRC", "")
    path = Path(value) if value else None
    required = os.environ.get("REQUIRE_WORKFLOW") == "1"
    if path is None or not (path / "tools" / "feature_packet.py").exists():
        if required:
            raise PacketError("workflow_unavailable",
                              "the pinned workflow checkout is required but missing",
                              "Check out the pinned workflow revision and set WORKFLOW_SRC.")
        raise PacketError("workflow_unavailable",
                          "no pinned workflow checkout is configured",
                          "Set WORKFLOW_SRC to the pinned workflow revision.")
    return path


def _validator():
    src = workflow_src()
    module_path = src / "tools" / "feature_packet.py"
    spec = importlib.util.spec_from_file_location("workflow_feature_packet", module_path)
    if spec is None or spec.loader is None:
        raise PacketError("workflow_unavailable", f"cannot load {module_path}", "Fix the pinned checkout.")
    module = importlib.util.module_from_spec(spec)
    sys.modules["workflow_feature_packet"] = module
    spec.loader.exec_module(module)
    return module


def digest(packet: Mapping[str, Any]) -> str:
    canonical = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def validate(packet: Mapping[str, Any]) -> str:
    """Validate against the pinned protocol and return the packet digest."""

    try:
        module = _validator()
        module.validate(packet, workflow_repo=workflow_src(), check_protocol_commit=True)
    except PacketError:
        raise
    except Exception as exc:  # noqa: BLE001 - the pinned validator's refusal is a refusal
        code = getattr(exc, "code", "packet_invalid")
        raise PacketError(str(code), str(exc), "Refuse the packet; do not relax the protocol.") from exc
    return digest(packet)
