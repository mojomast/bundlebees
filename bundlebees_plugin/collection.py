"""Collected-test-id manifests for ACDD contracts (``test-collection/1``).

A contract's ``acceptance_criteria[].tests`` are references. Checking only that
each reference names a file in ``scope.test_files`` lets a typo, a renamed test
or a whole-file reference stand in for a criterion. Before a contract is frozen
or dispatched, the coordinator runs a bounded ``pytest --collect-only`` of the
approved test files at the contract's ``source_rev`` (the approved test
revision) inside the disposable sandbox, and every reference must resolve to at
least one collected test id.

The result is a manifest bound to the contract hash, ``source_rev`` and the
test-file fingerprints, HMAC-signed with the coordinator's evidence key and
kept in the coordinator state dir. ``tdd red``, ``verify`` and
``bundlebees_dispatch`` refuse a contract whose manifest is missing, unsigned,
stale or unresolved.

Test-first slices often import an interface that does not exist yet, so a
test module can fail to import at ``source_rev``. Only for an import-type
collection error (ImportError, ModuleNotFoundError, AttributeError, NameError)
the ids of that file are read statically from its syntax tree (top-level
``test*`` functions, and ``test*`` methods of ``Test*`` or ``*TestCase``
classes). Those ids are labelled ``static`` per file in the manifest; any other
collection error refuses the contract.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

COLLECTION_VERSION = "test-collection/1"
COLLECT_FILE = "collected.json"
MAX_IDS = 20_000
MAX_ID_CHARS = 1024
MAX_MANIFEST_BYTES = 4_000_000
STATIC_ERRORS = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")

_REV_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ERROR_RE = re.compile(r"^E\s+([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b", re.MULTILINE)

# Runs inside the sandbox; writes {ids, errors} as JSON to argv[1].
_COLLECTOR = r"""
import json, re, sys
import pytest
out_path, targets = sys.argv[1], sys.argv[2:]
ids, errors = [], []
pat = re.compile(r"^E\s+([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))\b", re.M)
class Collector:
    def pytest_collectreport(self, report):
        if report.failed:
            text = str(report.longrepr or "")
            m = pat.search(text)
            errors.append({"nodeid": report.nodeid, "error_type": m.group(1).rsplit(".", 1)[-1] if m else None})
    def pytest_collection_finish(self, session):
        ids.extend(item.nodeid for item in session.items)
code = pytest.main(["-p", "no:cacheprovider", "-q", "--collect-only",
                    "--continue-on-collection-errors", *targets], plugins=[Collector()])
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({"exit_code": int(code), "ids": ids, "errors": errors}, handle)
sys.exit(0)
"""


class CollectionError(Exception):
    """A contract's test references could not be proven to exist."""

    def __init__(self, code: str, error: str, fix: str) -> None:
        super().__init__(error)
        self.code = code
        self.error = error
        self.fix = fix


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def collect_argv(python: str, test_files: Iterable[str]) -> list[str]:
    """argv for the sandboxed collector; ``{evidence}`` is filled by the sandbox."""
    return [python, "-B", "-c", _COLLECTOR, "{evidence}/" + COLLECT_FILE, *[str(item) for item in test_files]]


def requirements(contract: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{criterion_id: [test refs]}`` for test criteria; empty when TDD is off."""
    tdd = contract.get("tdd") if isinstance(contract.get("tdd"), Mapping) else {}
    if tdd.get("applicable") is not True:
        return {}
    out: dict[str, list[str]] = {}
    for index, criterion in enumerate(contract.get("acceptance_criteria") or []):
        if not isinstance(criterion, Mapping) or criterion.get("verification") != "test":
            continue
        refs = [str(item) for item in criterion.get("tests") or [] if isinstance(item, str)]
        out[str(criterion.get("id") or f"#{index}")] = refs
    return out


def needs_collection(contract: Mapping[str, Any]) -> bool:
    return any(requirements(contract).values())


def _test_files(contract: Mapping[str, Any]) -> list[str]:
    scope = contract.get("scope") if isinstance(contract.get("scope"), Mapping) else {}
    return [str(item) for item in scope.get("test_files") or [] if isinstance(item, str)]


def static_ids(path: str, source: str) -> list[str]:
    """Test ids read from a file's syntax tree, without importing it."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise CollectionError(
            "tests_uncollectable",
            f"approved test file {path} has a syntax error: {exc.msg} (line {exc.lineno})",
            "Fix the approved test file and freeze again.",
        ) from exc
    ids: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            ids.append(f"{path}::{node.name}")
        elif isinstance(node, ast.ClassDef):
            bases = [getattr(base, "attr", getattr(base, "id", "")) for base in node.bases]
            if node.name.startswith("Test") or any(str(name).endswith("TestCase") for name in bases):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        ids.append(f"{path}::{node.name}::{item.name}")
    return ids


def resolve(ref: str, ids: Iterable[str]) -> list[str]:
    """Collected ids a reference names: the exact id, or its parametrized or
    class children. A bare file reference never resolves (it is too broad)."""
    if "::" not in ref:
        return []
    return sorted(i for i in ids if i == ref or i.startswith(ref + "[") or i.startswith(ref + "::"))


def _safe_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise CollectionError("tests_uncollectable", "the collector returned no id list",
                              "Check that pytest is installed in the sandbox and retry.")
    if len(values) > MAX_IDS:
        raise CollectionError("tests_uncollectable", f"the collector returned more than {MAX_IDS} ids",
                              "Narrow scope.test_files to the approved tests for this contract.")
    out = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > MAX_ID_CHARS or any(ord(ch) < 32 for ch in value):
            raise CollectionError("tests_uncollectable", "the collector returned a malformed test id",
                                  "Rename the test so its id is plain text, then freeze again.")
        out.append(value)
    return out


def build_manifest(
    contract: Mapping[str, Any],
    contract_sha256: str,
    repo: Path,
    run: Callable[[Path, list[str]], Mapping[str, Any]],
    *,
    python: str,
    fingerprints: Mapping[str, str],
    now: str | None = None,
) -> dict[str, Any]:
    """Collect at the checked-out ``source_rev`` in *repo* and resolve every ref.

    *run* is ``(repo, argv) -> sandbox evidence`` and must return the parsed
    collector output under ``collected``. Raises :class:`CollectionError` when
    collection fails or any reference does not resolve.
    """
    files = _test_files(contract)
    evidence = run(repo, collect_argv(python, files))
    if evidence.get("timed_out"):
        raise CollectionError("tests_uncollectable", "test collection timed out in the sandbox",
                              "Make the approved tests importable quickly, or raise verify_timeout_s.")
    collected = evidence.get("collected")
    if not isinstance(collected, Mapping):
        raise CollectionError("tests_uncollectable",
                              "test collection produced no manifest (exit "
                              f"{evidence.get('exit_code')}): {str(evidence.get('output_tail') or '')[-300:]}",
                              "Run pytest --collect-only on the approved tests at source_rev and fix the error.")
    ids = _safe_ids(collected.get("ids"))
    methods = {path: "pytest" for path in files}
    errors = collected.get("errors") if isinstance(collected.get("errors"), list) else []
    for item in errors:
        nodeid = str((item or {}).get("nodeid") or "") if isinstance(item, Mapping) else ""
        error_type = str((item or {}).get("error_type") or "") if isinstance(item, Mapping) else ""
        path = nodeid.split("::", 1)[0]
        if path not in methods:
            raise CollectionError("tests_uncollectable", f"collection failed outside the approved tests: {nodeid or 'session'}",
                                  "Fix conftest or package imports so the approved tests can be collected.")
        if error_type not in STATIC_ERRORS:
            raise CollectionError("tests_uncollectable",
                                  f"approved test file {path} failed to collect ({error_type or 'unknown error'})",
                                  "Only a missing interface (an import-type error) is allowed at source_rev; fix the test file.")
        try:
            source = (repo / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CollectionError("tests_uncollectable", f"approved test file {path} is unreadable",
                                  "Commit the approved test file at source_rev.") from exc
        ids.extend(static_ids(path, source))
        methods[path] = "static"
    ids = sorted(set(_safe_ids(ids)))

    resolved: dict[str, dict[str, list[str]]] = {}
    for criterion_id, refs in requirements(contract).items():
        resolved[criterion_id] = {}
        for ref in refs:
            matches = resolve(ref, ids)
            if not matches:
                why = ("names a whole file; name exact test ids (path::Class::test)" if "::" not in ref
                       else "matches no collected test id")
                raise CollectionError(
                    "test_unresolved",
                    f"{criterion_id} test {ref!r} {why}",
                    "Fix the reference to an exact collected id, or commit the missing approved test, then freeze again.",
                )
            resolved[criterion_id][ref] = matches
    return {
        "collection_version": COLLECTION_VERSION,
        "contract_id": str(contract.get("id") or ""),
        "contract_sha256": str(contract_sha256),
        "source_rev": str(contract.get("source_rev") or ""),
        "test_fingerprints": {str(k): str(v) for k, v in fingerprints.items()},
        "methods": methods,
        "collected_ids": ids,
        "resolved": resolved,
        "environment": {"isolation": str(evidence.get("backend") or "unknown"), "runner": "pytest --collect-only (bundlebees sandbox)"},
        "at": now or _now(),
    }


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """sha256 of the signed manifest (signature included)."""
    return hashlib.sha256(_canonical({k: v for k, v in manifest.items() if not str(k).startswith("_")})).hexdigest()


def sign(manifest: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    body = {k: v for k, v in manifest.items() if k != "signature" and not str(k).startswith("_")}
    body["signature"] = hmac.new(bytes(key), _canonical(body), hashlib.sha256).hexdigest()
    return body


def _signature_ok(manifest: Mapping[str, Any], key: bytes) -> bool:
    signature = manifest.get("signature")
    if not isinstance(signature, str) or not _SHA256_RE.match(signature):
        return False
    body = {k: v for k, v in manifest.items() if k != "signature" and not str(k).startswith("_")}
    expected = hmac.new(bytes(key), _canonical(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def manifest_path(state_dir: Path | str, contract_id: str, contract_sha256: str) -> Path:
    if not _ID_RE.match(str(contract_id)) or not _SHA256_RE.match(str(contract_sha256)):
        raise CollectionError("tests_not_collected", "contract id or hash is not a safe identifier",
                              "Use the contract's own id and frozen hash.")
    return Path(state_dir) / "collections" / str(contract_id) / f"{contract_sha256}.json"


def write_manifest(state_dir: Path | str, manifest: Mapping[str, Any], key: bytes) -> tuple[Path, str]:
    """Sign and store *manifest*; return its path and digest."""
    signed = sign(manifest, key)
    path = manifest_path(state_dir, signed["contract_id"], signed["contract_sha256"])
    for directory in (path.parent.parent, path.parent):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise CollectionError("tests_not_collected", "collection directory is a symlink",
                                  "Remove the symlink from the plugin state directory.")
    tmp = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(tmp, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(signed, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)
    return path, manifest_digest(signed)


def load_manifest(
    state_dir: Path | str,
    contract: Mapping[str, Any],
    contract_sha256: str,
    key: bytes,
) -> dict[str, Any]:
    """Return the verified manifest for this exact contract, or raise.

    Checks the signature, the contract hash and ``source_rev`` binding, that
    every current reference is resolved in it, and, when the frozen contract
    records ``review.test_collection.manifest_sha256``, that digest too.
    """
    fix = "Run `hermes bundlebees contract freeze <contract> --workdir <repo>` (it collects the tests at source_rev)."
    path = manifest_path(state_dir, str(contract.get("id") or ""), contract_sha256)
    if path.is_symlink() or not path.is_file():
        raise CollectionError("tests_not_collected", "no collected-test manifest for this contract hash", fix)
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise CollectionError("tests_not_collected", "collected-test manifest is too large", fix)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectionError("tests_not_collected", "collected-test manifest is unreadable", fix) from exc
    if not isinstance(data, dict) or not _signature_ok(data, key):
        raise CollectionError("evidence_tampered", "collected-test manifest signature is invalid", fix)
    if (data.get("collection_version") != COLLECTION_VERSION
            or data.get("contract_sha256") != contract_sha256
            or data.get("contract_id") != contract.get("id")
            or data.get("source_rev") != contract.get("source_rev")
            or not _REV_RE.match(str(data.get("source_rev") or ""))):
        raise CollectionError("tests_not_collected", "collected-test manifest is for a different contract or revision", fix)
    review = contract.get("review") if isinstance(contract.get("review"), Mapping) else {}
    recorded = review.get("test_collection") if isinstance(review.get("test_collection"), Mapping) else None
    if recorded is not None and recorded.get("manifest_sha256") != manifest_digest(data):
        raise CollectionError("tests_not_collected",
                              "collected-test manifest does not match review.test_collection.manifest_sha256", fix)
    ids = data.get("collected_ids") if isinstance(data.get("collected_ids"), list) else []
    for criterion_id, refs in requirements(contract).items():
        for ref in refs:
            if not resolve(ref, ids):
                raise CollectionError("test_unresolved", f"{criterion_id} test {ref!r} matches no collected test id", fix)
    return data


def check_fingerprints(manifest: Mapping[str, Any], fingerprints: Mapping[str, str]) -> None:
    """The tests run now must be byte-identical to the ones collected."""
    if dict(manifest.get("test_fingerprints") or {}) != dict(fingerprints):
        raise CollectionError(
            "tests_tampered",
            "approved test files differ from the ones collected at freeze",
            "Re-freeze the contract so the new tests are collected and approved.",
        )
