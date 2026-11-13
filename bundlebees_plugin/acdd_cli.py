"""ACDD + TDD commands and verify hooks for the user-run ``hermes bundlebees`` CLI.

``contract validate|freeze`` check and freeze ``acdd/1`` contracts.
``tdd red`` runs the approved tests at the contract's ``source_rev`` in the
disposable sandbox and records a signed RED. ``tdd refactor`` reruns the same
read-only tests on a later descendant revision (or records ``--none``) and
refuses edited tests, out-of-scope paths, and revisions that do not descend
from GREEN. ``tdd review`` records a signed REVIEW from a named reviewer or
human, targeting GREEN or the required REFACTOR revision.
``tdd report`` prints the acceptance report. ``verify`` calls
:func:`verify_preflight` and
:func:`record_green` when the coordinator lease carries a contract; briefs
without one keep the legacy path. Evidence is signed with a key derived from
the coordinator's lease key and is never produced by the worker.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import contract as acdd
from . import leases, tdd
from .security import child_env, scrub

LEGACY_NOTE = "no ACDD contract; TDD evidence not required"
_MAX_CONTRACT_BYTES = 1024 * 1024
_MAX_COLLECTED_IDS = 20_000
_MAX_ID_CHARS = 1024
_COLLECT_FILE = "collected.json"
_HEX = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$|^[0-9a-f]{64}$")
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
            match = pat.search(text)
            errors.append({"nodeid": report.nodeid, "error_type": match.group(1).rsplit(".", 1)[-1] if match else None})
    def pytest_collection_finish(self, session):
        ids.extend(item.nodeid for item in session.items)
code = pytest.main(["-p", "no:cacheprovider", "-q", "--collect-only",
                    "--continue-on-collection-errors", *targets], plugins=[Collector()])
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump({"exit_code": int(code), "ids": ids, "errors": errors}, handle)
sys.exit(0)
"""


def _cli():
    from . import cli  # late import: cli imports this module

    return cli


def _error(code: str, error: str, fix: str) -> Exception:
    return _cli().CliError(code, error, fix)


def _print_hex(label: str, value: str) -> None:
    """Print a validated digest without scrub() mistaking it for a secret."""
    if isinstance(value, str) and _HEX.match(value):
        print(f"{label}: {value}")
    else:
        print(f"{label}: unavailable")


def _as_error(exc: Exception) -> Exception:
    if isinstance(exc, (acdd.ContractError, tdd.TddError)):
        return _error(exc.code, exc.error, exc.fix)
    return exc


def load_contract_file(value: Any) -> dict[str, Any]:
    path = Path(str(value or "")).expanduser()
    try:
        if path.is_symlink() or not path.is_file():
            raise _error("contract_invalid", "contract path is not a regular file", "Pass the path to a contract JSON file.")
        if path.stat().st_size > _MAX_CONTRACT_BYTES:
            raise _error("contract_invalid", "contract file is larger than 1 MB", "Keep contracts small; link large context instead.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _error("contract_invalid", f"contract file could not be read as JSON: {exc}", "Fix the JSON and retry.") from exc
    if not isinstance(data, dict):
        raise _error("contract_invalid", "contract must be a JSON object", "Write the contract as one JSON object.")
    return data


def _frozen(contract: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    try:
        validated = acdd.validate_contract(contract)
        return validated, acdd.check_frozen(contract)
    except acdd.ContractError as exc:
        raise _as_error(exc) from exc


def _test_files(contract: Mapping[str, Any]) -> list[str]:
    scope = contract.get("scope") if isinstance(contract.get("scope"), Mapping) else {}
    return [str(item) for item in scope.get("test_files") or [] if isinstance(item, str)]


def _test_first(contract: Mapping[str, Any]) -> bool:
    block = contract.get("tdd") if isinstance(contract.get("tdd"), Mapping) else {}
    return block.get("test_first") is not False


def _result(evidence: Mapping[str, Any]) -> dict[str, Any]:
    junit = evidence.get("junit") if isinstance(evidence.get("junit"), Mapping) else {}
    cases = [case for case in evidence.get("cases") or [] if isinstance(case, Mapping)]
    return {
        "exit_code": evidence.get("exit_code"),
        "tests": int(junit.get("tests") or len(cases)),
        "failures": int(junit.get("failures") or 0),
        "errors": int(junit.get("errors") or 0),
        "skipped": int(junit.get("skipped") or 0),
        "timed_out": bool(evidence.get("timed_out")),
        "cases": cases,
    }


def _environment(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"isolation": str(evidence.get("backend") or "unknown"), "runner": "pytest (bundlebees sandbox)"}


def _latest(records: list[dict], phase: str, sha: str) -> dict | None:
    found = [r for r in records if r.get("phase") == phase and r.get("contract_sha256") == sha]
    return found[-1] if found else None


def _collection_requirements(contract: Mapping[str, Any]) -> dict[str, list[str]]:
    """Map test criteria to their references; empty when TDD does not apply."""
    block = contract.get("tdd") if isinstance(contract.get("tdd"), Mapping) else {}
    if block.get("applicable") is not True:
        return {}
    out: dict[str, list[str]] = {}
    for index, criterion in enumerate(contract.get("acceptance_criteria") or []):
        if not isinstance(criterion, Mapping) or criterion.get("verification") != "test":
            continue
        refs = [str(item) for item in criterion.get("tests") or [] if isinstance(item, str)]
        if refs:
            out[str(criterion.get("id") or f"#{index}")] = refs
    return out


def _needs_collection(contract: Mapping[str, Any]) -> bool:
    return any(_collection_requirements(contract).values())


def _bounded_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        raise _error("tests_uncollectable", "the test collector returned no id list",
                     "Check that pytest is installed in the sandbox and retry.")
    if len(values) > _MAX_COLLECTED_IDS:
        raise _error("tests_uncollectable", f"the test collector returned more than {_MAX_COLLECTED_IDS} ids",
                     "Narrow scope.test_files to the approved tests for this contract.")
    out: list[str] = []
    for value in values:
        if (not isinstance(value, str) or not value or len(value) > _MAX_ID_CHARS
                or any(ord(char) < 32 for char in value)):
            raise _error("tests_uncollectable", "the test collector returned a malformed test id",
                         "Rename the test so its id is plain text, then freeze again.")
        out.append(value)
    return out


def _static_ids(path: str, source: str) -> list[str]:
    """Test ids read from a file's syntax tree, without importing it."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise _error("tests_uncollectable",
                     f"approved test file {path} has a syntax error: {scrub(exc.msg)} (line {exc.lineno})",
                     "Fix the approved test file and freeze again.") from exc
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


def _collect_argv(test_files: list[str]) -> list[str]:
    return [sys.executable, "-B", "-c", _COLLECTOR, "{evidence}/" + _COLLECT_FILE, *test_files]


def _collect_manifest(ctx: Any, contract: Mapping[str, Any], sha: str, workdir: Path) -> dict[str, Any]:
    """Collect the approved tests at ``source_rev`` in the sandbox (bounded)."""
    from . import sandbox

    cli = _cli()
    options = cli._sandbox_options(cli._config(ctx))
    options["timeout"] = min(int(options.get("timeout") or 300), 300)
    tests = _test_files(contract)
    with tempfile.TemporaryDirectory(prefix="bundlebees-collect-") as tmp:
        repo = _checkout(workdir, str(contract["source_rev"]), Path(tmp))
        try:
            fingerprints = tdd.fingerprint_files(repo, tests)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        try:
            evidence = sandbox.run_in_sandbox(
                repo, _collect_argv(tests), collect_file=_COLLECT_FILE, **options
            )
        except sandbox.SandboxUnavailable as exc:
            raise _error(exc.code, exc.error, exc.fix) from exc
        except ValueError as exc:
            raise _error("invalid_argument", f"unknown verify_isolation backend: {exc}",
                         "Set verify_isolation to auto, bwrap or firejail (unshare needs the weak override).") from exc

        if evidence.get("timed_out"):
            raise _error("tests_uncollectable", "test collection timed out in the sandbox",
                         "Make the approved tests importable quickly, or raise verify_timeout_s.")
        collected = evidence.get("collected")
        if not isinstance(collected, Mapping):
            raise _error(
                "tests_uncollectable",
                f"test collection produced no manifest (exit {evidence.get('exit_code')}): "
                f"{str(evidence.get('output_tail') or '')[-300:]}",
                "Run pytest --collect-only on the approved tests at source_rev and fix the error.",
            )
        ids = _bounded_ids(collected.get("ids"))
        methods = {path: "pytest" for path in tests}
        errors = collected.get("errors") if isinstance(collected.get("errors"), list) else []
        importable_errors = ("ImportError", "ModuleNotFoundError", "AttributeError", "NameError")
        for item in errors:
            if not isinstance(item, Mapping):
                continue
            nodeid = str(item.get("nodeid") or "")
            error_type = str(item.get("error_type") or "")
            path = nodeid.split("::", 1)[0]
            if path not in methods:
                raise _error("tests_uncollectable",
                             f"collection failed outside the approved tests: {nodeid or 'session'}",
                             "Fix conftest or package imports so the approved tests can be collected.")
            if error_type not in importable_errors:
                raise _error("tests_uncollectable",
                             f"approved test file {path} failed to collect ({error_type or 'unknown error'})",
                             "Only a missing interface (an import-type error) is allowed at source_rev; fix the test file.")
            try:
                source = (repo / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise _error("tests_uncollectable", f"approved test file {path} is unreadable",
                             "Commit the approved test file at source_rev.") from exc
            ids.extend(_static_ids(path, source))
            methods[path] = "static"
        ids = sorted(set(_bounded_ids(ids)))

    resolved: dict[str, dict[str, list[str]]] = {}
    for criterion_id, refs in _collection_requirements(contract).items():
        resolved[criterion_id] = {}
        for ref in refs:
            matches = tdd.resolve_test_ref(ref, ids)
            if not matches:
                why = ("names a whole file; name exact test ids (path::Class::test)" if "::" not in ref
                       else "matches no collected test id")
                raise _error("test_unresolved", f"{criterion_id} test {ref!r} {why}",
                             "Fix the reference to an exact collected id, or commit the missing approved test, then freeze again.")
            resolved[criterion_id][ref] = matches
    return {
        "collection_version": tdd.COLLECTION_VERSION,
        "contract_id": str(contract.get("id") or ""),
        "contract_sha256": str(sha),
        "source_rev": str(contract.get("source_rev") or ""),
        "test_fingerprints": {str(path): str(digest) for path, digest in fingerprints.items()},
        "methods": methods,
        "collected_ids": ids,
        "resolved": resolved,
        "environment": {"isolation": str(evidence.get("backend") or "unknown"),
                        "runner": "pytest --collect-only (bundlebees sandbox)"},
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------- parser


def add_parsers(subparsers: Any) -> None:
    contract_cmd = subparsers.add_parser("contract", help="Validate or freeze an acdd/1 contract.")
    contract_sub = contract_cmd.add_subparsers(dest="contract_command", required=True)
    validate = contract_sub.add_parser("validate", help="Check a contract against the schema and the ACDD checks.")
    validate.add_argument("path", help="Contract JSON file.")
    freeze = contract_sub.add_parser("freeze", help="Approve a contract and bind it to its sha256.")
    freeze.add_argument("path", help="Contract JSON file.")
    freeze.add_argument("--approved-by", required=True, help="Who approved the contract.")
    freeze.add_argument("--out", default=None, help="Where to write the frozen contract (default: overwrite path).")
    freeze.add_argument("--workdir", default=None,
                        help="Git repository with source_rev; its approved tests are collected in the sandbox first.")
    freeze.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    contract_cmd.set_defaults(command="contract")

    tdd_cmd = subparsers.add_parser("tdd", help="Record RED evidence or print the acceptance report.")
    tdd_sub = tdd_cmd.add_subparsers(dest="tdd_command", required=True)
    red = tdd_sub.add_parser("red", help="Run the approved tests at source_rev in the sandbox and record RED.")
    red.add_argument("path", help="Frozen contract JSON file.")
    red.add_argument("--workdir", default=None, help="Git repository that contains source_rev.")
    red.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    report = tdd_sub.add_parser("report", help="Print the acceptance report for a frozen contract.")
    report.add_argument("path", help="Frozen contract JSON file.")
    report.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    report.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    refactor = tdd_sub.add_parser("refactor", help="Record REFACTOR evidence after a valid GREEN.")
    refactor.add_argument("path", help="Frozen contract JSON file.")
    refactor.add_argument("--workdir", default=None, help="Git repository that contains the refactor revision.")
    refactor.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    target = refactor.add_mutually_exclusive_group(required=True)
    target.add_argument("--rev", default=None, help="Later commit that keeps the approved tests green.")
    target.add_argument("--none", action="store_true", help="Record explicitly that no refactor is needed.")
    refactor.add_argument("--note", default=None, help="Note stored with the refactor record.")
    review = tdd_sub.add_parser("review", help="Record signed REVIEW evidence from a reviewer or human.")
    review.add_argument("path", help="Frozen contract JSON file.")
    review.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    review.add_argument("--reviewer", required=True, help="Name of the reviewer or human.")
    review.add_argument("--role", choices=("reviewer", "human"), default="reviewer",
                        help="Whether the reviewer is a reviewer or a human (default: reviewer).")
    verdict = review.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true", help="Accept the change.")
    verdict.add_argument("--reject", action="store_true", help="Reject the change.")
    review.add_argument("--note", required=True, help="Why the change was approved or rejected.")
    tdd_cmd.set_defaults(command="tdd")


# --------------------------------------------------------------------------- commands


def cmd_contract(ctx: Any, args: Any) -> int:
    data = load_contract_file(getattr(args, "path", None))
    sub = getattr(args, "contract_command", None)
    if sub == "validate":
        try:
            validated = acdd.validate_contract(data)
        except acdd.ContractError as exc:
            raise _as_error(exc) from exc
        print("bundlebees contract")
        print(f"contract: {scrub(str(validated.get('id')))} valid")
        _print_hex("contract_sha256", acdd.contract_sha256(validated))
        try:
            acdd.check_frozen(data)
            print("frozen: yes")
        except acdd.ContractError as exc:
            print(f"frozen: no ({exc.code})")
        return 0
    if sub == "freeze":
        approved_by = str(getattr(args, "approved_by", "") or "")
        try:
            validated = acdd.validate_contract(data)
        except acdd.ContractError as exc:
            raise _as_error(exc) from exc
        test_collection = None
        manifest = None
        if _needs_collection(validated):
            workdir_value = getattr(args, "workdir", None) or _cli()._config(ctx).get("worktree")
            if not workdir_value:
                raise _error("collection_required",
                             "this contract names tests; they must be collected at source_rev before freezing",
                             "Pass --workdir with the repository that contains source_rev.")
            sha = acdd.contract_sha256(validated)
            state_dir = _cli()._state_dir(ctx, args)
            manifest = _collect_manifest(ctx, validated, sha, Path(str(workdir_value)).expanduser().resolve())
            try:
                _path, digest = tdd.write_collection(state_dir, manifest, leases.evidence_key(state_dir))
            except tdd.TddError as exc:
                raise _as_error(exc) from exc
            except OSError as exc:
                raise _error("tests_not_collected", f"could not store the collected-test manifest: {exc}",
                             "Check the plugin state directory.") from exc
            test_collection = {
                "manifest_sha256": digest,
                "source_rev": str(validated["source_rev"]),
                "collected": len(manifest["collected_ids"]),
            }
        try:
            frozen = acdd.freeze_contract(data, approved_by=approved_by, test_collection=test_collection)
        except acdd.ContractError as exc:
            raise _as_error(exc) from exc
        target = Path(str(getattr(args, "out", None) or getattr(args, "path"))).expanduser()
        if target.is_symlink():
            raise _error("contract_invalid", "refusing to write through a symlink", "Choose a regular output path.")
        fd, tmp = tempfile.mkstemp(prefix=".contract-", dir=str(target.parent or Path(".")))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(frozen, handle, indent=2, sort_keys=False)
                handle.write("\n")
            os.replace(tmp, target)
        except OSError as exc:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise _error("contract_invalid", f"could not write the frozen contract: {exc}", "Check the output path.") from exc
        print("bundlebees contract freeze")
        print(f"contract: {scrub(str(frozen.get('id')))} approved")
        _print_hex("contract_sha256", frozen["review"]["contract_sha256"])
        if manifest is not None:
            static = sorted(path for path, how in manifest["methods"].items() if how == "static")
            print(f"tests collected: {len(manifest['collected_ids'])} ids at source_rev, every AC reference resolved")
            if static:
                print("read statically (import-type collection error at source_rev): " + scrub(", ".join(static)))
        print(f"written: {scrub(str(target))}")
        return 0
    raise _error("invalid_argument", "unknown contract command", "Use: contract validate|freeze.")


def _checkout(workdir: Path, rev: str, into: Path) -> Path:
    target = into / "repo"
    env = child_env()
    for argv in (
        ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(workdir), str(target)],
        ["git", "-C", str(target), "checkout", "--quiet", "--detach", rev],
    ):
        proc = subprocess.run(argv, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise _error("stale_revision", f"could not check out source_rev: {scrub(proc.stderr.strip())[:300]}",
                         "Fetch the commit named by contract.source_rev into the workdir and retry.")
    return target


def _changed_paths(repo: Path, older: str, newer: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", older, newer],
            capture_output=True, text=True, env=child_env(), timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise _error("stale_revision", "git diff did not finish for the refactor revision",
                     "Retry with a smaller revision range.") from exc
    if proc.returncode != 0:
        raise _error("stale_revision", f"could not diff the refactor revision: {scrub(proc.stderr.strip())[:300]}",
                     "Fetch both revisions into the workdir and retry.")
    return [line for line in proc.stdout.splitlines() if line]


def _check_descends(repo: Path, older: str, newer: str) -> None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
            capture_output=True, text=True, env=child_env(), timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise _error("stale_revision", "git merge-base did not finish for the refactor revision",
                     "Retry with a smaller revision range.") from exc
    if proc.returncode != 0:
        raise _error("stale_revision", "refactor revision does not descend from GREEN",
                     "Pass a commit that is a descendant of the GREEN revision.")


def _check_refactor_scope(contract: Mapping[str, Any], changed: list[str]) -> None:
    approved = set(_test_files(contract))
    tampered = [path for path in changed if path in approved]
    if tampered:
        raise _error("tests_tampered", "the refactor revision edits approved test files: " + ", ".join(tampered),
                     "Approved tests are read-only; refactor only scope.assigned_files.")
    scope = contract.get("scope") if isinstance(contract.get("scope"), Mapping) else {}
    assigned = {str(item) for item in scope.get("assigned_files") or [] if isinstance(item, str)}
    outside = [path for path in changed if path not in assigned or acdd.is_prohibited(path, contract)]
    if outside:
        raise _error("scope_violation", "the refactor revision touches files outside scope.assigned_files: "
                     + ", ".join(outside),
                     "Refactor only the paths in scope.assigned_files.")


def _cmd_refactor(ctx: Any, args: Any, contract: Mapping[str, Any], sha: str,
                  state_dir: Path, key: bytes) -> int:
    cli = _cli()
    try:
        records = tdd.load_records(state_dir, str(contract["id"]), key)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    red = _latest(records, "red", sha)
    green = _latest(records, "green", sha)
    if green is None:
        raise _error("green_failed", "no GREEN evidence for this contract",
                     "Run `hermes bundlebees verify` on the implementation revision first.")
    try:
        tdd.check_green(contract, sha, red, green)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc

    if getattr(args, "none", False):
        note = str(getattr(args, "note", None) or "")
        record = tdd.make_record(
            contract, sha, phase="refactor", revision=str(green["revision"]),
            test_fingerprints=green["test_fingerprints"], command=["none"],
            result=green["result"], produced_by={"role": "verifier", "name": "bundlebees tdd refactor"},
            test_first=green["test_first"], environment=green.get("environment"),
            notes="no refactor needed: " + note,
        )
        try:
            tdd.write_record(state_dir, record, key)
            tdd.check_refactor(contract, sha, red, green, record)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        print(scrub("refactor: valid (no refactor needed)"))
        return 0

    rev = str(getattr(args, "rev", None) or "")
    if not _HEX.match(rev):
        raise _error("invalid_argument",
                     "refactor --rev must be a full 40- or 64-character lower-case hex revision",
                     "Pass the later commit id exactly as git prints it.")
    workdir_value = getattr(args, "workdir", None) or cli._config(ctx).get("worktree")
    if not workdir_value:
        raise _error("invalid_argument", "no workdir", "Pass --workdir with the repository that contains the refactor revision.")
    workdir = Path(str(workdir_value)).expanduser().resolve()
    tests = _test_files(contract)
    with tempfile.TemporaryDirectory(prefix="bundlebees-refactor-") as tmp:
        repo = _checkout(workdir, rev, Path(tmp))
        _check_descends(repo, str(green["revision"]), rev)
        _check_refactor_scope(contract, _changed_paths(repo, str(green["revision"]), rev))
        evidence = cli._run_sandboxed(repo, list(tests), cli._config(ctx))
        try:
            fingerprints = tdd.fingerprint_files(repo, tests)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
    record = tdd.make_record(
        contract, sha, phase="refactor", revision=rev, test_fingerprints=fingerprints,
        command=["pytest", *tests], result=_result(evidence),
        produced_by={"role": "verifier", "name": "bundlebees tdd refactor"},
        test_first=green["test_first"], environment=_environment(evidence),
    )
    try:
        tdd.write_record(state_dir, record, key)
        tdd.check_refactor(contract, sha, red, green, record)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    print("bundlebees tdd refactor")
    print(f"contract: {scrub(str(contract['id']))}")
    _print_hex("revision", rev)
    print("refactor: valid")
    return 0


def _cmd_review(ctx: Any, args: Any, contract: Mapping[str, Any], sha: str,
                state_dir: Path, key: bytes) -> int:
    reviewer = str(getattr(args, "reviewer", "") or "").strip()
    note = str(getattr(args, "note", "") or "").strip()
    if not reviewer or not note:
        raise _error("invalid_argument", "review needs a non-empty reviewer name and note",
                     "Pass --reviewer NAME and --note TEXT with real content.")
    role = str(getattr(args, "role", None) or "reviewer")
    approve = bool(getattr(args, "approve", False))

    try:
        records = tdd.load_records(state_dir, str(contract["id"]), key)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    red = _latest(records, "red", sha)
    green = _latest(records, "green", sha)
    if green is None:
        raise _error("green_failed", "no GREEN evidence for this contract",
                     "Run `hermes bundlebees verify` on the implementation revision first.")
    try:
        tdd.check_green(contract, sha, red, green)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc

    refactor = None
    if "refactor" in [str(phase) for phase in contract.get("evidence_required") or []]:
        refactor = _latest(records, "refactor", sha)
        if refactor is None:
            raise _error(
                "refactor_failed",
                "no REFACTOR evidence for this contract",
                "Record it with `hermes bundlebees tdd refactor --rev <sha>` or "
                "`--none --note <why>`.",
            )
        try:
            tdd.check_refactor(contract, sha, red, green, refactor)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc

    target = refactor or green
    # C6: this is a review the coordinator *recorded*. The HMAC proves the
    # coordinator wrote it, not who the reviewer is, so the record never
    # carries role "human"; the reviewer and claimed kind are unverified labels.
    record = tdd.make_record(
        contract, sha, phase="review", revision=str(target["revision"]),
        test_fingerprints=target["test_fingerprints"], command=["review"],
        result=None, produced_by={"role": "reviewer", "name": "bundlebees tdd review (coordinator-recorded)"},
        test_first=green["test_first"], notes=scrub(note),
        review_outcome="passed" if approve else "failed",
        attestation=tdd.coordinator_review_attestation(scrub(reviewer), role),
    )
    try:
        tdd.write_record(state_dir, record, key)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    if not approve:
        raise _error("review_failed", "the reviewer rejected the change",
                     "Address the review note and record a new GREEN/REFACTOR, then review again.")
    print("bundlebees tdd review")
    print(f"contract: {scrub(str(contract['id']))}")
    _print_hex("revision", str(target["revision"]))
    print("review: valid (coordinator-recorded; reviewer identity not authenticated)")
    return 0


def cmd_tdd(ctx: Any, args: Any) -> int:
    cli = _cli()
    data = load_contract_file(getattr(args, "path", None))
    contract, sha = _frozen(data)
    state_dir = cli._state_dir(ctx, args)
    key = leases.evidence_key(state_dir)
    sub = getattr(args, "tdd_command", None)
    if sub == "report":
        try:
            records = tdd.load_records(state_dir, str(contract["id"]), key)
            report = tdd.acceptance_report(contract, sha, records)
            report["report_version"] = "acceptance_report/1"
            report["generated_at"] = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            signed = tdd.sign_record(report, key)
            tdd.write_report(state_dir, signed)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        if getattr(args, "json", False):
            print(json.dumps(signed, indent=2, sort_keys=True))
        else:
            print("bundlebees tdd report")
            print(f"contract: {scrub(str(contract['id']))}")
            _print_hex("contract_sha256", sha)
            for phase, status in report["phases"].items():
                print(f"{phase}: {'not run' if status == 'unrun' else status}")
            for cid, item in report["criteria"].items():
                print(f"{cid}: {'not run' if item['status'] in ('unrun', 'not_run') else item['status']}")
            print(f"test-first: {'yes' if report['test_first'] else 'no'}")
            if report.get("review_identity"):
                print(f"review identity: {report['review_identity']}")
            print(f"human authorization: {report['human_authorization']['status']}")
            print(f"accepted: {'yes' if report['accepted'] else 'no'}")
            print(f"report: reports/{contract['id']}.json")
        return 0 if report["accepted"] else 1
    if sub == "refactor":
        return _cmd_refactor(ctx, args, contract, sha, state_dir, key)
    if sub == "review":
        return _cmd_review(ctx, args, contract, sha, state_dir, key)
    if sub != "red":
        raise _error("invalid_argument", "unknown tdd command", "Use: tdd red|report|refactor|review.")
    if not _test_first(contract):
        raise _error("red_invalid", "the contract says tdd.test_first: false; there is no RED to record",
                     "Record GREEN through verify; the report will say the tests came after the code.")
    workdir_value = getattr(args, "workdir", None) or cli._config(ctx).get("worktree")
    if not workdir_value:
        raise _error("invalid_argument", "no workdir", "Pass --workdir with the repository that contains source_rev.")
    workdir = Path(str(workdir_value)).expanduser().resolve()
    tests = _test_files(contract)
    manifest = None
    review_block = contract.get("review") if isinstance(contract.get("review"), Mapping) else {}
    if isinstance(review_block.get("test_collection"), Mapping):
        try:
            manifest = tdd.load_collection(state_dir, str(contract["id"]), sha, key)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
    with tempfile.TemporaryDirectory(prefix="bundlebees-red-") as tmp:
        repo = _checkout(workdir, str(contract["source_rev"]), Path(tmp))
        try:
            fingerprints = tdd.fingerprint_files(repo, tests)
            if manifest is not None and dict(manifest.get("test_fingerprints") or {}) != fingerprints:
                raise _error("tests_tampered",
                             "approved test files differ from the ones collected at freeze",
                             "Re-freeze the contract so the new tests are collected and approved.")
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        evidence = cli._run_sandboxed(repo, list(tests), cli._config(ctx))
    collected = {str(c.get("id")) for c in evidence.get("cases") or [] if isinstance(c, Mapping)}
    unknown = sorted({
        str(ref) for ac in contract.get("acceptance_criteria") or [] if isinstance(ac, Mapping)
        for ref in ac.get("tests") or []
        if "::" in str(ref) and str(ref) not in collected
        and not any(cid.startswith(str(ref) + "[") or cid.startswith(str(ref) + "::") for cid in collected)
    })
    if unknown:
        raise _error("test_ref_unknown",
                     "acceptance criteria name tests pytest did not collect: " + ", ".join(unknown[:20]),
                     "Fix the test ids in the contract (re-freeze it) or add the tests on source_rev.")
    record = tdd.make_record(
        contract, sha, phase="red", revision=str(contract["source_rev"]),
        test_fingerprints=fingerprints, command=["pytest", *tests], result=_result(evidence),
        produced_by={"role": "verifier", "name": "bundlebees tdd red"}, test_first=True,
        environment=_environment(evidence),
    )
    try:
        tdd.write_record(state_dir, record, key)
        tdd.check_red(contract, sha, record)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    print("bundlebees tdd red")
    print(f"contract: {scrub(str(contract['id']))}")
    _print_hex("source_rev", str(contract["source_rev"]))
    print(f"classification: {record['classification']}")
    print("red: valid")
    return 0


# --------------------------------------------------------------------------- verify hooks


def verify_preflight(record: Mapping[str, Any], state_dir: Path, patch_files: list[str]) -> dict[str, Any] | None:
    """Contract checks before any patch is applied; ``None`` means legacy mode."""
    brief = record.get("brief") if isinstance(record.get("brief"), Mapping) else {}
    raw = acdd.contract_from_brief(brief)
    if raw is None:
        return None
    contract, sha = _frozen(raw)
    try:
        acdd.check_brief_against_contract(brief, contract)
    except acdd.ContractError as exc:
        raise _as_error(exc) from exc
    approved = set(_test_files(contract))
    touched = sorted(f for f in patch_files if f in approved)
    if touched:
        raise _error("tests_tampered", "the worker patch edits approved test files: " + ", ".join(touched),
                     "Approved tests are read-only for the implementer; nothing was applied.")
    prohibited = sorted(f for f in patch_files if acdd.is_prohibited(f, contract))
    if prohibited:
        raise _error("scope_violation", "the worker patch touches prohibited paths: " + ", ".join(prohibited),
                     "Keep the change inside scope.assigned_files; nothing was applied.")
    key = leases.evidence_key(state_dir)
    try:
        records = tdd.load_records(state_dir, str(contract["id"]), key)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    red = _latest(records, "red", sha)
    if red is None and _test_first(contract):
        raise _error("red_missing", "no RED evidence for this contract",
                     "Run `hermes bundlebees tdd red <contract>` on source_rev first.")
    if red is not None:
        try:
            tdd.check_red(contract, sha, red)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
    return {"contract": contract, "sha": sha, "red": red, "key": key, "tests": _test_files(contract)}


def record_green(state_dir: Path, acdd_state: Mapping[str, Any], workdir: Path, evidence: Mapping[str, Any],
                 revision: str, *, note: str | None = None) -> dict[str, Any]:
    """Write a signed GREEN for the run in *evidence*; raise unless it is valid."""
    contract, sha = acdd_state["contract"], acdd_state["sha"]
    tests = list(acdd_state["tests"])
    try:
        fingerprints = tdd.fingerprint_files(workdir, tests)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    record = tdd.make_record(
        contract, sha, phase="green", revision=revision, test_fingerprints=fingerprints,
        command=["pytest", *tests], result=_result(evidence),
        produced_by={"role": "verifier", "name": "bundlebees verify"}, test_first=_test_first(contract),
        environment=_environment(evidence), notes=note,
    )
    try:
        tdd.write_record(state_dir, record, acdd_state["key"])
        tdd.check_green(contract, sha, acdd_state["red"], record)
    except tdd.TddError as exc:
        raise _as_error(exc) from exc
    return record


def print_mode(acdd_state: Mapping[str, Any] | None, green: Mapping[str, Any] | None = None) -> None:
    if acdd_state is None:
        print(f"acdd: {LEGACY_NOTE}")
        return
    print(f"contract: {scrub(str(acdd_state['contract']['id']))}")
    _print_hex("contract_sha256", acdd_state["sha"])
    if green is not None:
        print("green: valid")
    if not _test_first(acdd_state["contract"]):
        print("test-first: no (tests were written after the code)")


# --------------------------------------------------------------------------- integrate hooks


def integrate_preflight(state_dir: Path, attempt_ids: list[str]) -> list[dict[str, Any]]:
    """For each contract attempt, require a valid signed GREEN before the wave runs."""
    out: list[dict[str, Any]] = []
    key = None
    for attempt_id in attempt_ids:
        try:
            record = leases.load_lease(state_dir, attempt_id)
        except leases.LeaseError:
            continue  # integrate itself reports unverified or missing leases
        brief = record.get("brief") if isinstance(record.get("brief"), Mapping) else {}
        raw = acdd.contract_from_brief(brief)
        if raw is None:
            continue
        contract, sha = _frozen(raw)
        key = key or leases.evidence_key(state_dir)
        try:
            records = tdd.load_records(state_dir, str(contract["id"]), key)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        red, green = _latest(records, "red", sha), _latest(records, "green", sha)
        if green is None:
            raise _error("green_failed", f"no GREEN evidence for contract {contract['id']} ({attempt_id})",
                         "Verify the attempt with `hermes bundlebees verify` before integrating.")
        try:
            tdd.check_green(contract, sha, red, green)
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        if "refactor" in [str(phase) for phase in contract.get("evidence_required") or []]:
            refactor = _latest(records, "refactor", sha)
            if refactor is None:
                raise _error(
                    "refactor_failed",
                    f"no REFACTOR evidence for contract {contract['id']} ({attempt_id})",
                    "Record it with `hermes bundlebees tdd refactor --rev <sha>` or "
                    "`--none --note <why>`.",
                )
            try:
                tdd.check_refactor(contract, sha, red, green, refactor)
            except tdd.TddError as exc:
                raise _as_error(exc) from exc
        out.append({"attempt_id": attempt_id, "contract": contract, "sha": sha, "key": key,
                    "tests": _test_files(contract)})
    return out


def record_integration(state_dir: Path, items: list[dict[str, Any]], workdir: Path,
                       suite: Mapping[str, Any] | None, head: Any) -> list[tuple[str, str]]:
    """Write one signed integration record per contract; return (id, status) pairs."""
    statuses: list[tuple[str, str]] = []
    revision = str(head) if isinstance(head, str) and _HEX.match(head) else None
    for item in items:
        contract, sha = item["contract"], item["sha"]
        if revision is None:
            statuses.append((str(contract["id"]), "unrun"))
            continue
        fingerprints: dict[str, str] = {}
        missing: list[str] = []
        for test_file in item["tests"]:  # every approved test file must be in the evidence
            try:
                fingerprints.update(tdd.fingerprint_files(workdir, [test_file]))
            except tdd.TddError:
                missing.append(str(test_file))
        targets = [str(t) for t in (suite or {}).get("targets") or []]
        record = tdd.make_record(
            contract, sha, phase="integration", revision=revision, test_fingerprints=fingerprints,
            command=["pytest", *targets] if targets else ["pytest", "(default suite on the combined branch)"],
            result=_result(suite) if suite else None,
            produced_by={"role": "verifier", "name": "bundlebees integrate"}, test_first=_test_first(contract),
            environment=_environment(suite or {}),
        )
        suites = (suite or {}).get("suites")
        if isinstance(suites, Mapping):
            # Per-category status (regression / contract / security), signed with the record.
            record["suites"] = {str(k): dict(v) for k, v in suites.items() if isinstance(v, Mapping)}
            if (suite or {}).get("blocking") and record.get("classification") == "passed":
                record["classification"] = "failed"
        if missing:
            record["missing_test_files"] = sorted(missing)
            if record.get("classification") == "passed":
                record["classification"] = "failed"
        try:
            tdd.write_record(state_dir, record, item["key"])
        except tdd.TddError as exc:
            raise _as_error(exc) from exc
        statuses.append((str(contract["id"]), "valid" if record["classification"] == "passed" else "integration_failed"))
    return statuses
