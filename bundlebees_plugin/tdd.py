"""TDD evidence for ACDD contracts: classify runs, record proof, check it.

Threat model
------------
A test result, a JUnit file, and a stored evidence record all come from an
untrusted worker or a disposable sandbox. Anything read here can be wrong,
hostile, oversized, or edited after the fact. The module therefore:

* classifies one run with the fixed, first-match table in ``docs/ACDD.md`` §4
  so a broken environment is never reported as honest RED;
* parses JUnit only after refusing oversized files and any DOCTYPE/ENTITY
  declaration, and scrubs every message through
  :func:`bundlebees_plugin.security.scrub`;
* fingerprints approved test files by content, refusing absolute paths,
  ``..`` escapes, symlinks, and missing files;
* signs every record with HMAC-SHA256 over canonical JSON and verifies the
  signature on load, so a rewritten classification is ``evidence_tampered``;
* writes records append-only as ``0600`` files inside a ``0700`` directory;
* reports a required phase that never ran as ``unrun``, never as passed.

The contract is read as a plain mapping (``acdd/1`` shape); this module
deliberately does not import ``bundlebees_plugin.contract``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .security import scrub

EVIDENCE_VERSION = "tdd-evidence/1"

CLASSIFICATIONS: tuple[str, ...] = (
    "behavioral_failure",
    "passed",
    "syntax_error",
    "collection_error",
    "environment_error",
    "unrelated_failure",
    "timeout",
    "not_run",
)

FAILURE_CODES: tuple[str, ...] = (
    "contract_invalid",
    "contract_not_frozen",
    "contract_changed",
    "red_invalid",
    "red_incomplete",
    "red_missing",
    "stale_revision",
    "tests_tampered",
    "scope_violation",
    "green_failed",
    "refactor_failed",
    "integration_failed",
    "review_failed",
    "security_failed",
    "evidence_tampered",
    "evidence_invalid",
    "unrun",
)

_PHASES = ("red", "green", "refactor", "integration", "review")
_ROLES = ("verifier", "worker", "reviewer", "human")
_CRITERIA_STATUS = ("passed", "failed", "not_run", "review_required")
_CONTRACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REV_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AC_ID_RE = re.compile(r"^AC-[0-9]{3,}$")
_XML_DANGER_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_ERROR_LINE_RE = re.compile(
    r"^E?\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b",
    re.MULTILINE,
)
_MAX_JUNIT_BYTES = 2_000_000
_MAX_MESSAGE = 500
_DEFAULT_FIX = (
    "Rerun the approved tests in the sandbox and record verifier evidence for "
    "the frozen contract revision."
)


class TddError(Exception):
    """A rejected evidence operation with a documented failure code and fix."""

    def __init__(self, code: str, error: str, fix: str = _DEFAULT_FIX) -> None:
        self.code = str(code)
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        super().__init__(self.error)


# ---------------------------------------------------------------------------
# Test-id matching and classification
# ---------------------------------------------------------------------------


def matches(case_id: str, test_id: str) -> bool:
    """True when two test ids are equal or one is a ``::`` / ``[`` prefix."""
    left = str(case_id)
    right = str(test_id)
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    if not longer.startswith(shorter):
        return False
    rest = longer[len(shorter):]
    return rest.startswith("::") or rest.startswith("[")


def _in_red_set(case_id: str, red_set: Iterable[str]) -> bool:
    return any(matches(case_id, test) or matches(test, case_id) for test in red_set)


def _is_missing_interface(case: Mapping[str, Any], added: Sequence[str]) -> bool:
    error_type = str(case.get("error_type") or "")
    if error_type == "NotImplementedError":
        return True
    if error_type not in ("ImportError", "ModuleNotFoundError", "AttributeError"):
        return False
    message = str(case.get("message") or "")
    for name in added:
        candidates = {str(name), str(name).rsplit(".", 1)[-1]}
        if any(f"'{candidate}'" in message for candidate in candidates):
            return True
    return False


def classify(
    result: Mapping[str, Any] | None,
    red_set: Sequence[str],
    added: Sequence[str],
) -> str:
    """Classify a run with the docs/ACDD.md §4 table, first match wins."""
    if result is None:
        return "not_run"
    if bool(result.get("timed_out")):
        return "timeout"

    cases = [case for case in (result.get("cases") or []) if isinstance(case, Mapping)]
    errored = [case for case in cases if case.get("outcome") == "error"]
    failing = [
        case for case in cases if case.get("outcome") in ("failed", "error")
    ]
    exit_code = result.get("exit_code")

    if any(
        case.get("error_type") in ("SyntaxError", "IndentationError", "TabError")
        for case in errored
    ):
        return "syntax_error"

    if exit_code in (3, 4, 5):
        return "environment_error"

    for case in failing:
        if not _in_red_set(str(case.get("id") or ""), red_set):
            return "unrelated_failure"

    for case in errored:
        if not _in_red_set(str(case.get("id") or ""), red_set):
            continue
        if _is_missing_interface(case, added):
            continue
        return "collection_error" if case.get("stage") == "collection" else "environment_error"

    if exit_code == 0 and not failing:
        return "passed"

    if exit_code != 0 and not failing:
        return "environment_error"

    return "behavioral_failure"


# ---------------------------------------------------------------------------
# JUnit parsing
# ---------------------------------------------------------------------------


def _junit_id(classname: str, name: str) -> tuple[str, str]:
    """Return ``(test_id, stage)`` for one JUnit ``testcase``."""
    if not classname:
        return name.replace(".", "/") + ".py", "collection"
    parts = classname.split(".")
    module_parts = 0
    for index in range(1, len(parts) + 1):
        if parts[index - 1].startswith("test"):
            module_parts = index
    if module_parts:
        module = "/".join(parts[:module_parts]) + ".py"
        classes = parts[module_parts:]
    else:
        module = classname.replace(".", "/") + ".py"
        classes = []
    return "::".join([module, *classes, name]), "call"


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext())


def _error_type(element: ET.Element | None, text: str) -> str | None:
    if element is None:
        return None
    declared = element.get("type")
    if declared:
        return declared.rsplit(".", 1)[-1]
    found = _ERROR_LINE_RE.findall(text)
    if not found:
        return None
    return found[-1].rsplit(".", 1)[-1]


def _error_message(element: ET.Element | None, text: str) -> str:
    if element is not None:
        found = list(_ERROR_LINE_RE.finditer(text))
        if found:
            matched = found[-1]
            rest = text[matched.end():].split("\n", 1)[0]
            rest = rest.lstrip().lstrip(":").strip()
            if rest:
                return str(scrub(rest))[:_MAX_MESSAGE]
        declared = element.get("message")
        if declared:
            return str(scrub(declared))[:_MAX_MESSAGE]
    first = text.strip().split("\n", 1)[0].strip()
    return str(scrub(first))[:_MAX_MESSAGE] if first else ""


def _case_outcome(testcase: ET.Element) -> tuple[str, ET.Element | None]:
    failure = testcase.find("failure")
    if failure is not None:
        return "failed", failure
    error = testcase.find("error")
    if error is not None:
        return "error", error
    if testcase.find("skipped") is not None:
        return "skipped", testcase.find("skipped")
    return "passed", None


def parse_junit_cases(path: os.PathLike[str] | str, *, limit: int = 2000) -> list[dict]:
    """Parse pytest's JUnit XML into bounded, scrubbed per-case outcomes."""
    target = Path(path)
    try:
        if target.is_symlink() or not target.is_file():
            raise TddError("evidence_invalid", "junit file is missing or not a regular file")
        size = target.stat().st_size
        if size > _MAX_JUNIT_BYTES:
            raise TddError("evidence_invalid", "junit file exceeds the 2 MB limit")
        raw = target.read_bytes()
    except TddError:
        raise
    except OSError as exc:
        raise TddError("evidence_invalid", f"junit file could not be read: {exc}") from exc
    if len(raw) > _MAX_JUNIT_BYTES:
        raise TddError("evidence_invalid", "junit file exceeds the 2 MB limit")
    text = raw.decode("utf-8", errors="replace")
    if _XML_DANGER_RE.search(text):
        raise TddError("evidence_invalid", "junit file declares DOCTYPE or ENTITY")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TddError("evidence_invalid", f"junit file is not valid XML: {exc}") from exc

    cases: list[dict] = []
    if limit <= 0:
        return cases
    for testcase in root.iter("testcase"):
        outcome, element = _case_outcome(testcase)
        classname = testcase.get("classname") or ""
        name = testcase.get("name") or ""
        test_id, stage = _junit_id(classname, name)
        body = _element_text(element if element is not None else testcase)
        if outcome in ("failed", "error") and stage != "collection":
            declared_message = (element.get("message") or "") if element is not None else ""
            stripped = body.strip()
            if (
                declared_message.strip().startswith("failed on setup")
                or declared_message.strip().startswith("failed on teardown")
                or stripped.startswith("failed on setup")
                or stripped.startswith("failed on teardown")
            ):
                stage = "setup"
        case: dict[str, Any] = {"id": test_id, "outcome": outcome, "stage": stage}
        if element is not None:
            error_type = _error_type(element, body)
            if error_type:
                case["error_type"] = error_type
            message = _error_message(element, body)
            if message:
                case["message"] = message
        cases.append(case)
        if len(cases) >= limit:
            break
    return cases


# ---------------------------------------------------------------------------
# Test-file fingerprints
# ---------------------------------------------------------------------------


def _tampered(error: str) -> TddError:
    return TddError(
        "tests_tampered",
        error,
        "Fingerprint the approved test files exactly as listed in scope.test_files; "
        "do not add, remove, edit, or symlink them.",
    )


def fingerprint_files(workdir: os.PathLike[str] | str, files: Iterable[str]) -> dict[str, str]:
    """Return SHA-256 for every approved test file, refusing unsafe paths."""
    root = Path(workdir)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise _tampered(f"workdir is not readable: {exc}") from exc
    fingerprints: dict[str, str] = {}
    for value in files:
        text = str(value)
        candidate = Path(text)
        if not text or "\x00" in text or candidate.is_absolute() or ".." in candidate.parts:
            raise _tampered(f"test file path is not a safe relative path: {text!r}")
        current = root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise _tampered(f"test file is a symlink: {text}")
        try:
            resolved = (root / candidate).resolve(strict=True)
        except OSError as exc:
            raise _tampered(f"test file is missing or unreadable: {text}") from exc
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise _tampered(f"test file escapes the worktree: {text}") from exc
        if not resolved.is_file():
            raise _tampered(f"test file is not a regular file: {text}")
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            raise _tampered(f"test file is unreadable: {text}") from exc
        fingerprints[text] = hashlib.sha256(raw).hexdigest()
    return fingerprints


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _red_set(contract: Mapping[str, Any]) -> list[str]:
    red: list[str] = []
    for criterion in contract.get("acceptance_criteria") or []:
        if not isinstance(criterion, Mapping):
            continue
        if criterion.get("verification") != "test":
            continue
        if criterion.get("regression") is True:
            continue
        for test in criterion.get("tests") or []:
            text = str(test)
            if text not in red:
                red.append(text)
    return red


def _added_interfaces(contract: Mapping[str, Any]) -> list[str]:
    added: list[str] = []
    for interface in contract.get("interfaces") or []:
        if isinstance(interface, Mapping) and interface.get("change") == "add":
            name = str(interface.get("name") or "")
            if name:
                added.append(name)
    return added


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _zero_result() -> dict[str, Any]:
    return {
        "exit_code": None,
        "tests": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "timed_out": False,
    }


def _copy_result(result: Mapping[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {
        "exit_code": result.get("exit_code"),
        "tests": int(result.get("tests") or 0),
        "failures": int(result.get("failures") or 0),
        "errors": int(result.get("errors") or 0),
        "skipped": int(result.get("skipped") or 0),
        "timed_out": bool(result.get("timed_out")),
    }
    cases: list[dict[str, Any]] = []
    for case in result.get("cases") or []:
        if not isinstance(case, Mapping):
            continue
        item = dict(case)
        if "message" in item:
            item["message"] = str(scrub(item["message"]))[:_MAX_MESSAGE]
        cases.append(item)
    if cases:
        copied["cases"] = cases
    return copied


def _case_matches(case_id: str, tests: Sequence[str]) -> bool:
    return any(matches(case_id, test) or matches(test, case_id) for test in tests)


def _criteria(
    contract: Mapping[str, Any],
    phase: str,
    result: Mapping[str, Any] | None,
    classification: str,
) -> dict[str, str]:
    cases = [
        case
        for case in ((result or {}).get("cases") or [])
        if isinstance(case, Mapping)
    ]
    statuses: dict[str, str] = {}
    for criterion in contract.get("acceptance_criteria") or []:
        if not isinstance(criterion, Mapping):
            continue
        ac_id = str(criterion.get("id") or "")
        if not ac_id:
            continue
        verification = criterion.get("verification")
        if verification == "test":
            tests = [str(test) for test in criterion.get("tests") or []]
            matched = [
                case
                for case in cases
                if _case_matches(str(case.get("id") or ""), tests)
            ]
            if not matched or all(case.get("outcome") == "skipped" for case in matched):
                statuses[ac_id] = "not_run"
            elif any(case.get("outcome") in ("failed", "error") for case in matched):
                statuses[ac_id] = "failed"
            else:
                statuses[ac_id] = "passed"
        elif verification == "integration":
            if phase == "integration":
                statuses[ac_id] = "passed" if classification == "passed" else "failed"
            else:
                statuses[ac_id] = "not_run"
        else:
            statuses[ac_id] = "review_required"
    return statuses


def _validate_record(record: Mapping[str, Any]) -> None:
    def bad(error: str) -> TddError:
        return TddError("evidence_invalid", error, "Build the record with make_record().")

    if record.get("evidence_version") != EVIDENCE_VERSION:
        raise bad("evidence_version must be tdd-evidence/1")
    if not _CONTRACT_ID_RE.match(str(record.get("contract_id") or "")):
        raise bad("contract_id is not a safe identifier")
    if not _SHA256_RE.match(str(record.get("contract_sha256") or "")):
        raise bad("contract_sha256 must be 64 lower-case hex characters")
    if record.get("phase") not in _PHASES:
        raise bad("phase is not a tdd-evidence/1 phase")
    for name in ("revision", "base_rev"):
        if not _REV_RE.match(str(record.get(name) or "")):
            raise bad(f"{name} must be a full 40- or 64-character hex revision")
    fingerprints = record.get("test_fingerprints")
    if not isinstance(fingerprints, Mapping):
        raise bad("test_fingerprints must be an object")
    for path, digest in fingerprints.items():
        if not isinstance(path, str) or not _SHA256_RE.match(str(digest)):
            raise bad("test_fingerprints values must be 64 lower-case hex characters")
    if not isinstance(record.get("command"), list) or not all(
        isinstance(item, str) for item in record["command"]
    ):
        raise bad("command must be a list of strings")
    result = record.get("result")
    if not isinstance(result, Mapping):
        raise bad("result must be an object")
    for name in ("tests", "failures", "errors", "skipped"):
        value = result.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise bad(f"result.{name} must be a non-negative integer")
    if not isinstance(result.get("timed_out"), bool):
        raise bad("result.timed_out must be a boolean")
    exit_code = result.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        raise bad("result.exit_code must be an integer or null")
    if record.get("classification") not in CLASSIFICATIONS:
        raise bad("classification is not a tdd-evidence/1 value")
    criteria = record.get("criteria")
    if not isinstance(criteria, Mapping):
        raise bad("criteria must be an object")
    for ac_id, status in criteria.items():
        if not _AC_ID_RE.match(str(ac_id)) or status not in _CRITERIA_STATUS:
            raise bad("criteria must map AC-NNN ids to evidence statuses")
    produced_by = record.get("produced_by")
    if not isinstance(produced_by, Mapping) or produced_by.get("role") not in _ROLES:
        raise bad("produced_by.role is not verifier, worker, reviewer, or human")
    if not isinstance(record.get("test_first"), bool):
        raise bad("test_first must be a boolean")
    if not isinstance(record.get("at"), str) or not record.get("at"):
        raise bad("at must be a non-empty timestamp")


def make_record(
    contract: Mapping[str, Any],
    contract_sha256: str,
    *,
    phase: str,
    revision: str,
    test_fingerprints: Mapping[str, str],
    command: Sequence[str],
    result: Mapping[str, Any] | None,
    produced_by: Mapping[str, Any],
    test_first: bool,
    environment: Mapping[str, Any] | None = None,
    notes: str | None = None,
    now: str | None = None,
) -> dict:
    """Build one signed-ready ``tdd-evidence/1`` record for a contract phase."""
    if result is None:
        stored_result = _zero_result()
        classification = "not_run"
    else:
        stored_result = _copy_result(result)
        classification = classify(
            stored_result, _red_set(contract), _added_interfaces(contract)
        )
    record: dict[str, Any] = {
        "evidence_version": EVIDENCE_VERSION,
        "contract_id": str(contract.get("id") or ""),
        "contract_sha256": str(contract_sha256),
        "phase": str(phase),
        "revision": str(revision),
        "base_rev": str(contract.get("source_rev") or ""),
        "test_fingerprints": {str(path): str(digest) for path, digest in test_fingerprints.items()},
        "command": [str(item) for item in command],
        "result": stored_result,
        "classification": classification,
        "criteria": _criteria(contract, str(phase), stored_result, classification),
        "produced_by": dict(produced_by),
        "test_first": bool(test_first),
        "at": str(now) if now else _now_utc(),
    }
    if environment is not None:
        record["environment"] = dict(environment)
    if notes is not None:
        record["notes"] = str(notes)
    _validate_record(record)
    return record


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _unsigned(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key != "signature" and not str(key).startswith("_")
    }


def sign_record(record: Mapping[str, Any], key: bytes) -> dict:
    """Return a copy of *record* with an HMAC-SHA256 ``signature``."""
    data = dict(record)
    digest = hmac.new(bytes(key), _canonical(_unsigned(data)), hashlib.sha256).hexdigest()
    data["signature"] = digest
    return data


def verify_signature(record: Mapping[str, Any], key: bytes) -> bool:
    """True only when the stored signature matches the canonical record."""
    if not isinstance(record, Mapping):
        return False
    signature = record.get("signature")
    if not isinstance(signature, str) or not _SHA256_RE.match(signature):
        return False
    expected = hmac.new(
        bytes(key), _canonical(_unsigned(record)), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ---------------------------------------------------------------------------
# Append-only store
# ---------------------------------------------------------------------------


def _check_id(contract_id: str) -> None:
    if not _CONTRACT_ID_RE.match(str(contract_id)):
        raise TddError(
            "evidence_invalid",
            "contract_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
            "Use the contract's own id, never a path fragment.",
        )


def _private_dir(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise TddError("evidence_invalid", f"evidence directory is unsafe: {path.name}")
        os.chmod(path, 0o700)
    except TddError:
        raise
    except OSError as exc:
        raise TddError("evidence_invalid", f"could not create evidence directory: {exc}") from exc
    return path


def write_record(state_dir: os.PathLike[str] | str, record: Mapping[str, Any], key: bytes) -> Path:
    """Append one signed record under ``evidence/<contract_id>/NNNN-phase.json``."""
    contract_id = str(record.get("contract_id") or "")
    _check_id(contract_id)
    phase = str(record.get("phase") or "")
    if phase not in _PHASES:
        raise TddError("evidence_invalid", "phase is not a tdd-evidence/1 phase")
    root = Path(state_dir)
    contract_dir = _private_dir(_private_dir(root / "evidence") / contract_id)
    existing = sorted(
        child
        for child in contract_dir.iterdir()
        if child.name.endswith(".json") and (child.is_file() or child.is_symlink())
    )
    number = len(existing) + 1
    path = contract_dir / f"{number:04d}-{phase}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise TddError("evidence_invalid", "evidence record already exists") from exc
    except OSError as exc:
        raise TddError("evidence_invalid", f"could not create evidence record: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(sign_record(record, key), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise TddError("evidence_invalid", f"could not write evidence record: {exc}") from exc
    return path


def load_records(state_dir: os.PathLike[str] | str, contract_id: str, key: bytes) -> list[dict]:
    """Load signed records in filename order; any bad record is tampering."""
    _check_id(contract_id)
    contract_dir = Path(state_dir) / "evidence" / contract_id
    if contract_dir.is_symlink():
        raise TddError("evidence_tampered", "evidence directory is a symlink")
    if not contract_dir.is_dir():
        return []
    loaded: list[dict] = []
    for path in sorted(contract_dir.iterdir(), key=lambda item: item.name):
        if not path.name.endswith(".json"):
            continue
        if path.is_symlink() or not path.is_file():
            raise TddError("evidence_tampered", f"evidence record is unsafe: {path.name}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TddError("evidence_tampered", f"evidence record is unreadable: {path.name}") from exc
        if not isinstance(data, dict) or not verify_signature(data, key):
            raise TddError("evidence_tampered", f"evidence record signature is invalid: {path.name}")
        record = dict(data)
        record["_path"] = str(path)
        loaded.append(record)
    return loaded


# ---------------------------------------------------------------------------
# RED / GREEN / REFACTOR checks
# ---------------------------------------------------------------------------


def _stale(contract: Mapping[str, Any], contract_sha256: str, record: Mapping[str, Any], what: str) -> TddError:
    return TddError(
        "stale_revision",
        f"{what} evidence does not match the frozen contract revision or hash",
        "Re-run the check against the contract's source_rev and frozen contract_sha256.",
    )


def _has_failing_case(record: Mapping[str, Any], test_id: str) -> bool:
    for case in (record.get("result") or {}).get("cases") or []:
        if not isinstance(case, Mapping):
            continue
        if case.get("outcome") not in ("failed", "error"):
            continue
        if matches(str(case.get("id") or ""), test_id) or matches(
            test_id, str(case.get("id") or "")
        ):
            return True
    return False


def _check_identity(contract: Mapping[str, Any], contract_sha256: str, record: Mapping[str, Any], what: str) -> None:
    if str(record.get("contract_sha256") or "") != str(contract_sha256):
        raise _stale(contract, contract_sha256, record, what)
    if str(record.get("base_rev") or "") != str(contract.get("source_rev") or ""):
        raise _stale(contract, contract_sha256, record, what)


def _check_verifier(record: Mapping[str, Any], code: str, what: str) -> None:
    role = (record.get("produced_by") or {}).get("role")
    if role != "verifier":
        raise TddError(
            code,
            f"{what} evidence must be produced by the verifier, not {role!r}",
            "Have the independent verifier run the check; worker output never authorizes.",
        )


def check_red(contract: Mapping[str, Any], contract_sha256: str, record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Raise a named failure unless RED is a verifier behavioural failure."""
    _check_identity(contract, contract_sha256, record, "RED")
    _check_verifier(record, "red_invalid", "RED")
    classification = str(record.get("classification") or "")
    red = _red_set(contract)
    incomplete = classification == "passed" or any(
        not _has_failing_case(record, test) for test in red
    )
    if incomplete:
        raise TddError(
            "red_incomplete",
            "some RED-set test passed or never ran on the baseline",
            "Ensure every approved failing test fails for the missing behavior before implementing.",
        )
    if classification != "behavioral_failure":
        raise TddError(
            "red_invalid",
            f"RED classified as {classification}, expected behavioral_failure",
            "Fix the test environment so only the missing behavior fails at baseline.",
        )
    expected = set(str(path) for path in (contract.get("scope") or {}).get("test_files") or [])
    actual = set(str(path) for path in (record.get("test_fingerprints") or {}))
    if actual != expected:
        raise TddError(
            "tests_tampered",
            "RED fingerprints do not cover exactly scope.test_files",
            "Fingerprint every approved test file and leave the set unchanged.",
        )
    return record


def check_green(
    contract: Mapping[str, Any],
    contract_sha256: str,
    red: Mapping[str, Any] | None,
    green: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Raise a named failure unless GREEN follows valid RED with identical tests."""
    _check_identity(contract, contract_sha256, green, "GREEN")
    test_first = (contract.get("tdd") or {}).get("test_first") is not False
    if red is None:
        if test_first:
            raise TddError(
                "red_missing",
                "GREEN has no RED record but the contract requires test-first",
                "Record a valid RED against source_rev, or set tdd.test_first: false honestly.",
            )
    else:
        check_red(contract, contract_sha256, red)
    _check_verifier(green, "green_failed", "GREEN")
    if red is not None:
        if (green.get("test_fingerprints") or {}) != (red.get("test_fingerprints") or {}):
            raise TddError(
                "tests_tampered",
                "GREEN test fingerprints differ from RED",
                "Run the same approved test files unchanged; do not edit or substitute them.",
            )
    if green.get("classification") != "passed":
        raise TddError(
            "green_failed",
            f"GREEN classified as {green.get('classification')}, expected passed",
            "Make the implementation pass the approved tests without changing them.",
        )
    return green


def check_refactor(
    contract: Mapping[str, Any],
    contract_sha256: str,
    red: Mapping[str, Any] | None,
    green: Mapping[str, Any],
    refactor: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Raise a named failure unless REFACTOR keeps identical tests green."""
    check_green(contract, contract_sha256, red, green)
    _check_identity(contract, contract_sha256, refactor, "REFACTOR")
    _check_verifier(refactor, "refactor_failed", "REFACTOR")
    if (refactor.get("test_fingerprints") or {}) != (green.get("test_fingerprints") or {}):
        raise TddError(
            "tests_tampered",
            "REFACTOR test fingerprints differ from GREEN",
            "Refactor only scope.assigned_files; approved tests stay byte-identical.",
        )
    if refactor.get("classification") != "passed":
        raise TddError(
            "refactor_failed",
            f"REFACTOR classified as {refactor.get('classification')}, expected passed",
            "Restore green behavior without touching the approved tests.",
        )
    return refactor


# ---------------------------------------------------------------------------
# Acceptance report
# ---------------------------------------------------------------------------


def _last_phase(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        phase = str(record.get("phase") or "")
        if phase in _PHASES:
            latest[phase] = record
    return latest


def _code_for(error: TddError) -> str:
    return error.code


def _green_status(
    contract: Mapping[str, Any],
    contract_sha256: str,
    latest: Mapping[str, Mapping[str, Any]],
) -> str:
    green = latest.get("green")
    if green is None:
        return "unrun"
    red = latest.get("red")
    if red is None and (contract.get("tdd") or {}).get("test_first") is not False:
        return "red_missing"
    try:
        check_green(contract, contract_sha256, red, green)
    except TddError as exc:
        return _code_for(exc)
    return "valid"


def _phase_status(
    phase: str,
    contract: Mapping[str, Any],
    contract_sha256: str,
    latest: Mapping[str, Mapping[str, Any]],
) -> str:
    record = latest.get(phase)
    if record is None:
        return "unrun"
    if phase == "red":
        try:
            check_red(contract, contract_sha256, record)
        except TddError as exc:
            return _code_for(exc)
        return "valid"
    if phase == "green":
        return _green_status(contract, contract_sha256, latest)
    if phase == "refactor":
        red = latest.get("red")
        green = latest.get("green")
        if green is None:
            return "green_failed"
        try:
            check_refactor(contract, contract_sha256, red, green, record)
        except TddError as exc:
            return _code_for(exc)
        return "valid"
    if phase == "integration":
        return "valid" if record.get("classification") == "passed" else "integration_failed"
    if phase == "review":
        role = (record.get("produced_by") or {}).get("role")
        return (
            "valid"
            if role in ("reviewer", "human") and record.get("classification") == "passed"
            else "review_failed"
        )
    return "valid" if record.get("classification") == "passed" else "security_failed"


def _criteria_report(
    contract: Mapping[str, Any],
    latest: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    sources = [latest.get(phase) for phase in ("integration", "refactor", "green")]
    report: dict[str, dict[str, Any]] = {}
    for criterion in contract.get("acceptance_criteria") or []:
        if not isinstance(criterion, Mapping):
            continue
        ac_id = str(criterion.get("id") or "")
        if not ac_id:
            continue
        status = "not_run"
        evidence = None
        for record in sources:
            if record is None:
                continue
            values = record.get("criteria")
            if isinstance(values, Mapping) and ac_id in values:
                status = str(values[ac_id])
                evidence = record.get("_path")
                break
        report[ac_id] = {"status": status, "evidence": evidence}
    return report


def _test_first(
    contract: Mapping[str, Any],
    contract_sha256: str,
    records: Sequence[Mapping[str, Any]],
) -> bool:
    if (contract.get("tdd") or {}).get("test_first") is False:
        return False
    if any(record.get("test_first") is False for record in records):
        return False
    green_index = None
    for index, record in enumerate(records):
        if record.get("phase") == "green":
            green_index = index
    if green_index is None:
        return False
    for index, record in enumerate(records):
        if index >= green_index or record.get("phase") != "red":
            continue
        try:
            check_red(contract, contract_sha256, record)
        except TddError:
            continue
        return True
    return False


def acceptance_report(
    contract: Mapping[str, Any],
    contract_sha256: str,
    records: Sequence[Mapping[str, Any]],
) -> dict:
    """Summarize each required phase and criterion without inflating unrun."""
    latest = _last_phase(records)
    required = [str(phase) for phase in contract.get("evidence_required") or []]
    phases = {
        phase: _phase_status(phase, contract, contract_sha256, latest)
        for phase in required
    }
    criteria = _criteria_report(contract, latest)
    phases_ok = all(status == "valid" for status in phases.values())
    review_valid = phases.get("review") == "valid"
    criteria_ok = all(
        item["status"] == "passed"
        or (item["status"] == "review_required" and review_valid)
        for item in criteria.values()
    )
    return {
        "contract_id": str(contract.get("id") or ""),
        "contract_sha256": str(contract_sha256),
        "mode": "contract",
        "test_first": _test_first(contract, contract_sha256, records),
        "phases": phases,
        "criteria": criteria,
        "accepted": bool(phases_ok and criteria_ok),
    }
