"""Local-only verification: verify a worker patch and commit it locally.

`verify --commit-local` never pushes and never touches the caller's working tree.
It builds an isolated worktree at the pinned base, refuses anything the lease does
not authorise, proves the patch is RED before and GREEN after, runs the regression
suite and gitleaks, and only then creates a local commit on ``bb-verify/<attempt>``.

The resulting commit is what integration consumes: its parent is the pinned base
and its file set is exactly the patch's, so `integrate_wave`'s ancestry checks are
satisfied without rewriting history.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from importlib import metadata as _metadata
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import leases, sandbox, test_receipts as receipts

RECORD_DIR = "verify-results"
DEFAULT_SANDBOX_TIMEOUT_S = 600
_MAX_SANDBOX_TIMEOUT_S = 7200
_COORDINATOR_ENV_PREFIXES = ("BUNDLEBEES_", "HERMES_")

#: A test runner receives the worktree and the exact argv, and returns the
#: sandbox's evidence mapping (``exit_code``/``timed_out``/``output_tail``).
#: Tests on hosts without a real isolation backend inject one; production uses
#: :func:`sandbox_runner` so no child silently inherits the coordinator env.
TestRunner = Callable[[Path, Sequence[str]], Mapping[str, Any]]

_TREE_DOMAIN = b"bundlebees-source-tree:v1\x00"
_TEST_SOURCES_DOMAIN = b"bundlebees-test-sources:v1\x00"
_DEPENDENCIES_DOMAIN = b"bundlebees-dependencies:v1\x00"
_IGNORED_TREE_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
)
_DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "Pipfile",
        "Pipfile.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "requirements.txt",
    }
)


class VerifyError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def _child_env() -> dict[str, str]:
    """Environment for trusted git/gitleaks children: no coordinator secrets.

    The ambient environment is preserved for normal tooling, but every
    ``BUNDLEBEES_*`` and ``HERMES_*`` value (launcher tokens, signing keys,
    gateway keys) is dropped rather than inherited.
    """

    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_COORDINATOR_ENV_PREFIXES)
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(args: Sequence[str], cwd: Path, *, timeout: float = 600.0, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
    )


def sandbox_runner(
    workdir: Path,
    argv: Sequence[str],
    *,
    backend: str = "auto",
    allow_unsafe_local: bool = False,
    allow_weak: bool = False,
    timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    which: Any = None,
    probe: Any = None,
) -> dict[str, Any]:
    """Run *argv* in the enforced sandbox and return its evidence.

    The sandbox supplies the entire child environment (nothing from
    ``os.environ`` reaches it, including any ``BUNDLEBEES_*``/``HERMES_*``
    coordinator secret) and runs with its cwd inside a disposable copy of
    *workdir*. ``SandboxUnavailable`` propagates so callers can fail closed with
    their own named error.
    """

    bounded = max(1, min(int(timeout), _MAX_SANDBOX_TIMEOUT_S))
    options: dict[str, Any] = {
        "backend": backend,
        "allow_unsafe_local": allow_unsafe_local,
        "allow_weak": allow_weak,
        "timeout": bounded,
    }
    if which is not None:
        options["which"] = which
    if probe is not None:
        options["probe"] = probe
    return sandbox.run_in_sandbox(Path(workdir), list(argv), **options)


def _test_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise VerifyError(
            "sandbox_evidence",
            "the test runner returned no evidence",
            "Check the sandbox runner and retry.",
        )
    try:
        exit_code = int(evidence.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = 1
    tail = evidence.get("output_tail")
    if tail is None:
        tail = evidence.get("tail", "")
    return {
        "exit_code": exit_code,
        "timed_out": bool(evidence.get("timed_out")),
        "tail": str(tail or "").strip()[-300:],
    }


def _sha256(domain: bytes, payload: bytes) -> str:
    digest = hashlib.sha256(domain)
    digest.update(payload)
    return digest.hexdigest()


def _tree_digest(repo: Path, rev: str) -> str:
    """A stable digest of every tracked path/blob in ``rev``'s tree."""

    proc = _run(["git", "ls-tree", "-r", "--full-tree", rev], repo)
    if proc.returncode != 0:
        raise VerifyError(
            "git_error",
            f"cannot read the tree of {rev}: {proc.stderr.strip()[:200]}",
            "Inspect the repository.",
        )
    return _sha256(_TREE_DOMAIN, proc.stdout.encode("utf-8"))


def _is_test_source(name: str) -> bool:
    return (
        name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _test_sources_digest(worktree: Path, tests: Sequence[str]) -> str:
    """Digest the named tests plus every discovered test source file."""

    root = Path(worktree)
    entries: dict[str, Path] = {}
    for name in tests:
        rel = str(name).replace("\\", "/")
        path = root / rel
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() or child.is_symlink():
                    entries[child.relative_to(root).as_posix()] = child
        else:
            entries[rel] = path

    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _IGNORED_TREE_NAMES
        )
        for filename in sorted(filenames):
            if not _is_test_source(filename):
                continue
            path = Path(directory) / filename
            entries.setdefault(path.relative_to(root).as_posix(), path)

    digest = hashlib.sha256(_TEST_SOURCES_DOMAIN)
    for rel in sorted(entries):
        path = entries[rel]
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        try:
            if path.is_symlink():
                digest.update(b"link:")
                digest.update(os.readlink(path).encode("utf-8"))
            elif path.is_file():
                digest.update(b"file:")
                digest.update(path.read_bytes())
            else:
                digest.update(b"missing")
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\x00")
    return digest.hexdigest()


def _installed_dependency_identity() -> dict[str, Any]:
    """Identity of the packages actually installed in this runtime.

    Manifest text alone is not evidence: two environments with the same
    ``pyproject.toml`` can resolve to different package versions.  Hash the
    installed distribution names and versions so a changed execution
    environment invalidates reuse even when every manifest is byte-identical.
    """

    names: set[str] = set()
    try:
        for distribution in _metadata.distributions():
            try:
                name = distribution.metadata.get("Name") or getattr(
                    distribution, "name", None
                )
            except Exception:  # noqa: BLE001 - a broken distribution is still identity
                continue
            if name:
                names.add(str(name))
    except Exception:  # noqa: BLE001 - a broken install must not break verification
        return {"available": False, "sha256": "unknown"}
    entries = {
        f"{name.lower()}=={_package_version(name)}" for name in sorted(names)
    }
    payload = "\n".join(sorted(entries)).encode("utf-8")
    return {
        "available": True,
        "count": len(entries),
        "sha256": _sha256(_DEPENDENCIES_DOMAIN, payload),
    }


def _dependencies_digest(worktree: Path) -> str:
    """Digest every dependency manifest that could change the test outcome."""

    root = Path(worktree)
    entries: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _IGNORED_TREE_NAMES
        )
        for filename in sorted(filenames):
            if filename in _DEPENDENCY_FILES or (
                filename.startswith("requirements") and filename.endswith(".txt")
            ):
                path = Path(directory) / filename
                entries[path.relative_to(root).as_posix()] = path
    digest = hashlib.sha256(_DEPENDENCIES_DOMAIN)
    for rel in sorted(entries):
        path = entries[rel]
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\x00")
    return digest.hexdigest()


def _package_version(name: str) -> str:
    try:
        return _metadata.version(name)
    except _metadata.PackageNotFoundError:
        return "unknown"
    except Exception:  # noqa: BLE001 - a broken install must not break verification
        return "unknown"


def _stack_identity() -> dict[str, Any]:
    return {
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
        "pytest": _package_version("pytest"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "dependency_environment": _installed_dependency_identity(),
    }


_SANDBOX_ENVIRONMENT: Mapping[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "<sandbox-home>",
    "TMPDIR": "<sandbox-tmp>",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONHASHSEED": "0",
    "CI": "1",
}


def _environment_policy(runner: TestRunner | None) -> dict[str, Any]:
    policy = dict(_SANDBOX_ENVIRONMENT)
    if runner is None:
        policy["runner"] = "sandbox"
    else:
        policy["runner"] = str(
            getattr(runner, "__qualname__", type(runner).__name__)
        )
    return policy


def _sandbox_policy(
    runner: TestRunner | None,
    *,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
) -> dict[str, Any]:
    return {
        "backend": str(backend),
        "allow_unsafe_local": bool(allow_unsafe_local),
        "allow_weak": bool(allow_weak),
        "timeout": int(timeout),
        "runner": "injected" if runner is not None else "sandbox",
    }


def _receipt_key_inputs(
    worktree: Path,
    *,
    command: Sequence[str],
    stage: str,
    source_tree_sha256: str,
    patch_sha256: str,
    tests: Sequence[str],
    runner: TestRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
) -> dict[str, Any]:
    return {
        "source_tree_sha256": source_tree_sha256,
        "patch_sha256": patch_sha256,
        "test_sources_sha256": _test_sources_digest(worktree, tests),
        "command": [str(argument) for argument in command],
        "dependencies": _dependencies_digest(worktree),
        "stack": _stack_identity(),
        "environment": _environment_policy(runner),
        "sandbox": _sandbox_policy(
            runner,
            backend=backend,
            allow_unsafe_local=allow_unsafe_local,
            allow_weak=allow_weak,
            timeout=timeout,
        ),
        "stage": str(stage),
    }


def _test_command(
    tests: Sequence[str], suite_command: Sequence[str] | None
) -> list[str] | None:
    """The exact argv used for the named-test RED/GREEN executions."""

    if not list(tests):
        return None
    base = list(suite_command) if suite_command else ["python", "-m", "pytest", "-q"]
    return [str(argument) for argument in base] + [str(test) for test in tests]


def _gitleaks_identity(require_gitleaks: bool, gitleaks: str | None) -> dict[str, Any]:
    return {
        "required": bool(require_gitleaks),
        "tool": None if gitleaks is None else str(gitleaks),
    }


def _verification_identity(
    worktree: Path,
    *,
    base_rev: str,
    source_tree_sha256: str,
    patch_sha256: str,
    tests: Sequence[str],
    suite_command: Sequence[str] | None,
    runner: TestRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
    require_gitleaks: bool,
    gitleaks: str | None,
) -> dict[str, Any]:
    """Everything that can change the observed verification result.

    The aggregate verify record may only be reused when this identity matches
    exactly.  It deliberately includes both the named-test argv (which embeds
    the suite command) and the standalone suite argv, so changing either one
    re-earns the record instead of inheriting an accepted result.
    """

    return {
        "base_rev": str(base_rev),
        "source_tree_sha256": str(source_tree_sha256),
        "patch_sha256": str(patch_sha256),
        "test_sources_sha256": _test_sources_digest(worktree, tests),
        "tests": [str(test) for test in tests],
        "command": _test_command(tests, suite_command),
        "suite_command": (
            None if suite_command is None else [str(item) for item in suite_command]
        ),
        "dependencies": _dependencies_digest(worktree),
        "dependency_environment": _installed_dependency_identity(),
        "stack": _stack_identity(),
        "environment": _environment_policy(runner),
        "sandbox": _sandbox_policy(
            runner,
            backend=backend,
            allow_unsafe_local=allow_unsafe_local,
            allow_weak=allow_weak,
            timeout=timeout,
        ),
        "gitleaks": _gitleaks_identity(require_gitleaks, gitleaks),
    }


def _receipt_evidence(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a stored result; partial evidence is a refusal, never a pass."""

    result = receipt.get("result")
    if not isinstance(result, Mapping):
        raise VerifyError(
            "receipt_corrupt",
            "the stored test receipt has no complete result",
            "Remove the corrupt receipt only by intending to rerun the tests.",
        )
    try:
        exit_code = int(result.get("exit_code"))
    except (TypeError, ValueError) as exc:
        raise VerifyError(
            "receipt_corrupt",
            "the stored test receipt has no exit code",
            "Remove the corrupt receipt only by intending to rerun the tests.",
        ) from exc
    timed_out = result.get("timed_out", False)
    if not isinstance(timed_out, bool):
        raise VerifyError(
            "receipt_corrupt",
            "the stored test receipt has an invalid timeout flag",
            "Remove the corrupt receipt only by intending to rerun the tests.",
        )
    tail = result.get("tail", "")
    if tail is None:
        tail = ""
    if not isinstance(tail, str):
        raise VerifyError(
            "receipt_corrupt",
            "the stored test receipt has an invalid output tail",
            "Remove the corrupt receipt only by intending to rerun the tests.",
        )
    evidence: dict[str, Any] = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "tail": tail,
        "receipt": {
            "key": receipt.get("key"),
            "hit": True,
            "duration_s": receipt.get("duration_s"),
        },
    }
    if "junit" in result:
        evidence["junit"] = result.get("junit")
    if "backend" in result:
        evidence["backend"] = result.get("backend")
    return evidence


def _assert_sandbox_usable(
    runner: TestRunner | None,
    *,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
) -> None:
    """Re-check the sandbox policy even when a receipt supplies the result."""

    if runner is not None or allow_unsafe_local:
        return
    try:
        chosen = sandbox.detect_backend(
            backend, allow_weak=sandbox.weak_override_enabled(allow_weak)
        )
    except ValueError as exc:
        raise VerifyError(
            "invalid_argument",
            f"unknown verify_isolation backend: {exc}",
            "Set verify_isolation to auto, bwrap or firejail (unshare needs the weak override).",
        ) from exc
    if chosen is None:
        raise VerifyError(
            "sandbox_unavailable",
            "no strong sandbox backend is usable on this host; refusing to reuse a cached result",
            sandbox._FIX_UNAVAILABLE,
        )


def _execute_test_command(
    workdir: Path,
    argv: Sequence[str],
    *,
    runner: TestRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
) -> dict[str, Any]:
    try:
        if runner is not None:
            evidence = runner(Path(workdir), list(argv))
        else:
            evidence = sandbox_runner(
                Path(workdir),
                list(argv),
                backend=backend,
                allow_unsafe_local=allow_unsafe_local,
                allow_weak=allow_weak,
                timeout=timeout,
            )
    except sandbox.SandboxUnavailable as exc:
        raise VerifyError(exc.code, exc.error, exc.fix) from exc
    except ValueError as exc:
        raise VerifyError(
            "invalid_argument",
            f"unknown verify_isolation backend: {exc}",
            "Set verify_isolation to auto, bwrap or firejail (unshare needs the weak override).",
        ) from exc
    return _test_evidence(evidence)


def _run_test_command(
    workdir: Path,
    argv: Sequence[str],
    *,
    runner: TestRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
    receipt_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    execute = lambda: _execute_test_command(  # noqa: E731 - local closure keeps the call site small
        workdir,
        argv,
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
    )
    if receipt_context is None:
        return execute()

    context = receipt_context
    stats = context["stats"]
    state_dir = context["state_dir"]
    cacheable = bool(context.get("cacheable", True))
    key_inputs = _receipt_key_inputs(
        workdir,
        command=argv,
        stage=str(context["stage"]),
        source_tree_sha256=str(context["source_tree_sha256"]),
        patch_sha256=str(context["patch_sha256"]),
        tests=context.get("tests", ()),
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
    )

    try:
        hit = receipts.lookup_receipt(state_dir, key_inputs, cacheable=cacheable)
    except receipts.ReceiptError as exc:
        raise VerifyError(exc.code, exc.error, exc.fix) from exc
    if hit is not None:
        _assert_sandbox_usable(
            runner,
            backend=backend,
            allow_unsafe_local=allow_unsafe_local,
            allow_weak=allow_weak,
        )
        stats["hits"] += 1
        return _receipt_evidence(hit)

    if not cacheable:
        stats["misses"] += 1
        stats["executions"] += 1
        return execute()

    try:
        claim = receipts.claim_receipt(state_dir, key_inputs, cacheable=True)
    except receipts.ReceiptError as exc:
        raise VerifyError(exc.code, exc.error, exc.fix) from exc
    try:
        try:
            hit = receipts.lookup_receipt(state_dir, key_inputs)
        except receipts.ReceiptError as exc:
            raise VerifyError(exc.code, exc.error, exc.fix) from exc
        if hit is not None:
            _assert_sandbox_usable(
                runner,
                backend=backend,
                allow_unsafe_local=allow_unsafe_local,
                allow_weak=allow_weak,
            )
            stats["hits"] += 1
            return _receipt_evidence(hit)

        stats["misses"] += 1
        started = time.monotonic()
        evidence = execute()
        stats["executions"] += 1
        duration = max(0.0, time.monotonic() - started)
        if not evidence.get("timed_out"):
            try:
                receipts.record_receipt(state_dir, key_inputs, evidence, duration)
            except receipts.ReceiptError as exc:
                raise VerifyError(exc.code, exc.error, exc.fix) from exc
        return evidence
    finally:
        claim.release()


def record_path(state_dir: Path, attempt_id: str) -> Path:
    return Path(state_dir) / RECORD_DIR / f"{attempt_id}.json"


def read_record(state_dir: Path, attempt_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(record_path(state_dir, attempt_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(state_dir: Path, attempt_id: str, record: Mapping[str, Any]) -> Path:
    path = record_path(state_dir, attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(dict(record), sort_keys=True, indent=2).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _branch(attempt_id: str) -> str:
    return f"bb-verify/{attempt_id}"


def _files_in_patch(patch: str) -> list[str]:
    found = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                found.add(parts[3][2:] if parts[3].startswith("b/") else parts[3])
    return sorted(found)


def _test_cache_stats() -> dict[str, int]:
    return {"executions": 0, "hits": 0, "misses": 0}


def _verify_request_lock(
    attempt_id: str,
    *,
    patch: str,
    base_rev: str,
    tests: Sequence[str],
    suite_command: Sequence[str] | None,
) -> str:
    payload = {
        "attempt_id": str(attempt_id),
        "base_rev": str(base_rev),
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "tests": [str(test) for test in tests],
        "suite_command": [str(item) for item in (suite_command or ())],
    }
    return "verify-request:" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def verify_and_commit(
    state_dir: Path,
    repo: Path,
    attempt_id: str,
    *,
    patch: str,
    declared_files: Sequence[str],
    base_rev: str,
    tests: Sequence[str] = (),
    suite_command: Sequence[str] | None = None,
    gitleaks: str | None = None,
    require_gitleaks: bool = True,
    tests_required: bool = True,
    expected_patch_sha256: str | None = None,
    acdd_preflight: Any = None,
    contract_declared: bool = False,
    worktree_root: Path | None = None,
    identity: Sequence[str] = ("-c", "user.name=bundlebees", "-c", "user.email=bees@localhost"),
    runner: TestRunner | None = None,
    sandbox_backend: str = "auto",
    sandbox_allow_unsafe_local: bool = False,
    sandbox_allow_weak: bool = False,
    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    cacheable: bool = True,
) -> dict[str, Any]:
    """Verify one patch and commit it locally. Returns the verify record.

    Every worker-produced test/suite invocation runs through
    :func:`sandbox_runner` (or the explicitly injected *runner*) with a bounded
    timeout, a minimal environment and a cwd inside the disposable worktree
    copy. When the sandbox is unavailable this fails closed with
    ``VerifyError("sandbox_unavailable")``.

    Identical requests are serialized by a durable single-flight claim, and each
    exact test invocation is looked up in the receipt ledger before it runs.
    """

    try:
        claim = receipts.claim_key(
            state_dir,
            _verify_request_lock(
                attempt_id,
                patch=patch,
                base_rev=base_rev,
                tests=tests,
                suite_command=suite_command,
            ),
        )
    except receipts.ReceiptError as exc:
        raise VerifyError(exc.code, exc.error, exc.fix) from exc
    try:
        return _verify_and_commit_locked(
            state_dir,
            repo,
            attempt_id,
            patch=patch,
            declared_files=declared_files,
            base_rev=base_rev,
            tests=tests,
            suite_command=suite_command,
            gitleaks=gitleaks,
            require_gitleaks=require_gitleaks,
            tests_required=tests_required,
            expected_patch_sha256=expected_patch_sha256,
            acdd_preflight=acdd_preflight,
            contract_declared=contract_declared,
            worktree_root=worktree_root,
            identity=identity,
            runner=runner,
            sandbox_backend=sandbox_backend,
            sandbox_allow_unsafe_local=sandbox_allow_unsafe_local,
            sandbox_allow_weak=sandbox_allow_weak,
            sandbox_timeout=sandbox_timeout,
            cacheable=cacheable,
            stats=_test_cache_stats(),
        )
    finally:
        claim.release()


def _verify_and_commit_locked(
    state_dir: Path,
    repo: Path,
    attempt_id: str,
    *,
    patch: str,
    declared_files: Sequence[str],
    base_rev: str,
    tests: Sequence[str] = (),
    suite_command: Sequence[str] | None = None,
    gitleaks: str | None = None,
    require_gitleaks: bool = True,
    tests_required: bool = True,
    expected_patch_sha256: str | None = None,
    acdd_preflight: Any = None,
    contract_declared: bool = False,
    worktree_root: Path | None = None,
    identity: Sequence[str] = ("-c", "user.name=bundlebees", "-c", "user.email=bees@localhost"),
    runner: TestRunner | None = None,
    sandbox_backend: str = "auto",
    sandbox_allow_unsafe_local: bool = False,
    sandbox_allow_weak: bool = False,
    sandbox_timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    cacheable: bool = True,
    stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    repo = Path(repo)
    stats = stats if stats is not None else _test_cache_stats()

    # Fail closed on missing evidence rather than recording a hollow pass.
    if tests_required and not list(tests):
        raise VerifyError(
            "tests_required",
            "this contract needs named tests and none were given",
            "Pass the contract's test targets; an empty list is not a pass.",
        )
    if require_gitleaks and not gitleaks:
        raise VerifyError(
            "security_tool_missing",
            "gitleaks is required for verification but is not available",
            "Install gitleaks, or record an explicit exemption in the contract.",
        )
    if expected_patch_sha256:
        actual = hashlib.sha256(patch.encode()).hexdigest()
        if actual != expected_patch_sha256:
            raise VerifyError(
                "artifact_mismatch",
                f"patch sha256 {actual[:12]} does not match the declared {expected_patch_sha256[:12]}",
                "Reject this artifact; its bytes changed after dispatch.",
            )
    if acdd_preflight is not None or contract_declared:
        if acdd_preflight is None:
            raise VerifyError(
                "contract_missing",
                "the lease declares a frozen contract but no preflight was supplied",
                "Refusing to fall back to legacy mode for a contracted task.",
            )
        try:
            outcome = acdd_preflight(list(declared_files))
        except Exception as exc:  # noqa: BLE001 - any contract refusal is fatal
            raise VerifyError(
                "contract_violation",
                f"the frozen contract refused this patch: {exc}",
                "Do not verify a patch that changes its own acceptance criteria.",
            ) from exc
        if contract_declared and outcome is None:
            raise VerifyError(
                "contract_missing",
                "the task declares a frozen contract but the preflight found none",
                "Refusing to verify a contracted task in legacy mode.",
            )

    # 1. The lease is the authority; worker claims never widen it.
    try:
        lease = leases.load_lease(state_dir, attempt_id)
    except leases.LeaseError as exc:
        raise VerifyError(exc.code, str(exc), exc.fix) from exc
    lease_files = sorted(leases.lease_files(lease))
    declared = sorted(str(path) for path in declared_files)
    if not declared:
        declared = _files_in_patch(patch)
    if declared != lease_files:
        raise VerifyError(
            "lease_scope_mismatch",
            f"patch files {declared} do not match the lease {lease_files}",
            "Do not verify a patch that touches files outside its lease.",
        )

    # 2. Source revision binding.
    if str(lease.get("source_rev") or "") != str(base_rev):
        raise VerifyError(
            "source_rev_mismatch",
            "the lease and the result disagree about the source revision",
            "Re-dispatch against the pinned revision; do not verify this artifact.",
        )

    patch_sha256 = hashlib.sha256(patch.encode()).hexdigest()
    source_tree_sha256 = _tree_digest(repo, base_rev)

    # 3. Idempotent restart: an accepted record with a real commit is reused.
    # Reuse is proof about this exact execution identity, never mere presence.
    existing = read_record(state_dir, attempt_id)
    if existing and existing.get("ok") is True:
        if _record_still_valid(
            existing,
            repo,
            base_rev=base_rev,
            files=declared,
            tests=list(tests),
            patch_sha256=patch_sha256,
            patch=patch,
            require_gitleaks=require_gitleaks,
            suite_command=suite_command,
            runner=runner,
            backend=sandbox_backend,
            allow_unsafe_local=sandbox_allow_unsafe_local,
            allow_weak=sandbox_allow_weak,
            timeout=sandbox_timeout,
            gitleaks=gitleaks,
            source_tree_sha256=source_tree_sha256,
        ):
            _assert_sandbox_usable(
                runner,
                backend=sandbox_backend,
                allow_unsafe_local=sandbox_allow_unsafe_local,
                allow_weak=sandbox_allow_weak,
            )
            reused = dict(existing)
            # Counters describe this invocation only: a reuse executed nothing.
            reused["test_cache"] = dict(stats)
            return reused
        # Present-but-invalid evidence is not evidence: re-run every check.
        existing = None

    def receipt_context(stage: str) -> dict[str, Any]:
        return {
            "state_dir": state_dir,
            "stage": stage,
            "source_tree_sha256": source_tree_sha256,
            "patch_sha256": patch_sha256,
            "tests": list(tests),
            "cacheable": cacheable,
            "stats": stats,
        }

    branch = _branch(attempt_id)
    root = Path(worktree_root) if worktree_root else repo.parent / f".bb-verify-{attempt_id}"
    worktree = root / "wt"
    if worktree.exists():
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)

    # A commit left by a crashed run is not a verified result: it is rechecked
    # from scratch below, and replaced if it does not survive the checks.
    if _branch_exists(repo, branch):
        _run(["git", "branch", "-D", branch], repo)

    _run(["git", "worktree", "add", "--detach", str(worktree), base_rev], repo)
    try:
        # 4. RED before the patch, when tests are named.
        red: dict[str, Any] | None = None
        if tests:
            red = _run_tests(
                worktree,
                tests,
                suite_command,
                runner=runner,
                backend=sandbox_backend,
                allow_unsafe_local=sandbox_allow_unsafe_local,
                allow_weak=sandbox_allow_weak,
                timeout=sandbox_timeout,
                receipt_context=receipt_context("verify.red"),
            )
            if red["timed_out"]:
                raise VerifyError(
                    "tests_timed_out",
                    "the named tests timed out before the patch",
                    "Increase the sandbox timeout or fix the test; a timeout is not RED evidence.",
                )
            if red["exit_code"] == 0:
                raise VerifyError(
                    "red_missing",
                    "the named tests pass before the patch, so there is no RED evidence",
                    "Fix the contract; a change that changes nothing cannot be accepted.",
                )

        # 5. Apply the patch.
        check = _run(["git", "apply", "--check", "--whitespace=nowarn", "-"], worktree, input_text=patch)
        if check.returncode != 0:
            raise VerifyError("patch_conflict", f"the patch does not apply: {check.stderr.strip()[:300]}", "Re-dispatch against the pinned base.")
        applied = _run(["git", "apply", "--whitespace=nowarn", "-"], worktree, input_text=patch)
        if applied.returncode != 0:
            raise VerifyError("patch_conflict", f"the patch failed to apply: {applied.stderr.strip()[:300]}", "Re-dispatch against the pinned base.")

        # -uall lists every new file instead of collapsing a new directory to
        # ``smoke/``, which would fail the declared-file comparison.
        touched = [line[3:] for line in _run(["git", "status", "--porcelain", "-uall"], worktree).stdout.splitlines()]
        # Test runs create bytecode/caches; they are not part of the worker's patch.
        touched = [path for path in touched
                   if not (path.endswith(".pyc") or "__pycache__" in path.split("/") or ".pytest_cache" in path.split("/"))]
        touched = sorted({path.split(" -> ")[-1] for path in touched if path})
        if touched != declared:
            raise VerifyError(
                "patch_scope_mismatch",
                f"the applied patch touched {touched}, expected {declared}",
                "Reject this artifact; it changed more than its lease allows.",
            )

        execution_identity = _verification_identity(
            worktree,
            base_rev=base_rev,
            source_tree_sha256=source_tree_sha256,
            patch_sha256=patch_sha256,
            tests=tests,
            suite_command=suite_command,
            runner=runner,
            backend=sandbox_backend,
            allow_unsafe_local=sandbox_allow_unsafe_local,
            allow_weak=sandbox_allow_weak,
            timeout=sandbox_timeout,
            require_gitleaks=require_gitleaks,
            gitleaks=gitleaks,
        )

        # 6. GREEN after the patch.
        green = _run_tests(
            worktree,
            tests,
            suite_command,
            runner=runner,
            backend=sandbox_backend,
            allow_unsafe_local=sandbox_allow_unsafe_local,
            allow_weak=sandbox_allow_weak,
            timeout=sandbox_timeout,
            receipt_context=receipt_context("verify.green"),
        )
        if green["timed_out"]:
            raise VerifyError(
                "tests_timed_out",
                "the named tests timed out after the patch",
                "Increase the sandbox timeout or fix the test; a timeout is not a pass.",
            )
        if green["exit_code"] != 0:
            raise VerifyError("tests_failed", f"named tests failed after the patch: {green.get('tail', '')}", "Reject and re-dispatch.")

        suite = None
        if suite_command:
            suite = _run_suite(
                worktree,
                suite_command,
                runner=runner,
                backend=sandbox_backend,
                allow_unsafe_local=sandbox_allow_unsafe_local,
                allow_weak=sandbox_allow_weak,
                timeout=sandbox_timeout,
                receipt_context=receipt_context("verify.suite"),
            )
            if suite["timed_out"]:
                raise VerifyError(
                    "suite_timed_out",
                    "the regression suite timed out after the patch",
                    "Increase the sandbox timeout or investigate the hang.",
                )
            if suite["exit_code"] != 0:
                raise VerifyError("suite_failed", f"regression suite failed: {suite.get('tail', '')}", "Reject and re-dispatch.")

        scan = None
        if gitleaks:
            scan = _run([gitleaks, "stdin", "--no-banner", "--redact"], worktree, input_text=patch)
            if scan.returncode != 0:
                raise VerifyError("secrets_found", "gitleaks reported a finding in the patch", "Reject this artifact.")

        # 7. Commit locally, on a branch, never pushed.
        _run(["git"] + list(identity) + ["checkout", "-b", branch], worktree)
        _run(["git", "add", "--", *declared], worktree)
        commit = _run(["git"] + list(identity) + ["commit", "-q", "-m", f"verify: {attempt_id}"], worktree)
        if commit.returncode != 0:
            raise VerifyError("commit_failed", f"the verification commit failed: {commit.stderr.strip()[:200]}", "Inspect the worktree.")
        sha = _rev(worktree, "HEAD")
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)

    # 8. Prove the commit is what we say it is.
    parent = _rev(repo, f"{sha}^")
    if parent != base_rev:
        raise VerifyError("commit_parent_mismatch", f"{sha} is not a child of {base_rev}", "Reject this verification.")
    diff_files = _diff_files(repo, base_rev, sha)
    if diff_files != declared:
        raise VerifyError("commit_scope_mismatch", f"commit touches {diff_files}, expected {declared}", "Reject this verification.")

    record = _record(
        attempt_id,
        sha,
        base_rev,
        declared,
        tests,
        red=red,
        suite=suite,
        gitleaks=scan,
        patch_sha256=patch_sha256,
        test_cache=stats,
        execution_identity=execution_identity,
    )
    _write_record(state_dir, attempt_id, record)
    return record


def _record(attempt_id, sha, base_rev, files, tests, *, red, suite, gitleaks, patch_sha256=None, resumed=False, test_cache=None, execution_identity=None) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "ok": True,
        "sha": sha,
        "branch": _branch(attempt_id),
        "base_rev": base_rev,
        "files": list(files),
        "tests": list(tests),
        "red": None if red is None else {"exit_code": red["exit_code"], "timed_out": bool(red.get("timed_out"))},
        "suite": None if suite is None else {"exit_code": suite.get("exit_code"), "timed_out": bool(suite.get("timed_out"))},
        "gitleaks": None if gitleaks is None else {"exit_code": gitleaks.returncode},
        "mode": "commit-local",
        "patch_sha256": patch_sha256,
        "execution_identity": (
            None if execution_identity is None else dict(execution_identity)
        ),
        "resumed": resumed,
        "test_cache": dict(test_cache or _test_cache_stats()),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _identity_for_commit(
    repo: Path,
    sha: str,
    *,
    base_rev: str,
    source_tree_sha256: str,
    patch_sha256: str,
    tests: Sequence[str],
    suite_command: Sequence[str] | None,
    runner: TestRunner | None,
    backend: str,
    allow_unsafe_local: bool,
    allow_weak: bool,
    timeout: int,
    require_gitleaks: bool,
    gitleaks: str | None,
) -> dict[str, Any] | None:
    """Recompute the execution identity from the *committed* verified tree."""

    scratch = repo.parent / f".bb-identity-{os.getpid()}-{time.time_ns()}"
    if scratch.exists():
        _run(["git", "worktree", "remove", "--force", str(scratch)], repo)
    try:
        added = _run(["git", "worktree", "add", "--detach", str(scratch), sha], repo)
        if added.returncode != 0:
            return None
        return _verification_identity(
            scratch,
            base_rev=base_rev,
            source_tree_sha256=source_tree_sha256,
            patch_sha256=patch_sha256,
            tests=tests,
            suite_command=suite_command,
            runner=runner,
            backend=backend,
            allow_unsafe_local=allow_unsafe_local,
            allow_weak=allow_weak,
            timeout=timeout,
            require_gitleaks=require_gitleaks,
            gitleaks=gitleaks,
        )
    finally:
        _run(["git", "worktree", "remove", "--force", str(scratch)], repo)


def _record_still_valid(
    record: Mapping[str, Any],
    repo: Path,
    *,
    base_rev: str,
    files: Sequence[str],
    tests: Sequence[str],
    patch_sha256: str,
    patch: str,
    require_gitleaks: bool,
    suite_command: Sequence[str] | None = None,
    runner: TestRunner | None = None,
    backend: str = "auto",
    allow_unsafe_local: bool = False,
    allow_weak: bool = False,
    timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    gitleaks: str | None = None,
    source_tree_sha256: str | None = None,
) -> bool:
    """Reuse only a record whose *results* still apply to this exact artifact.

    Presence is not proof: a record claiming RED exit 0, a failed suite, a
    mismatched patch hash, a different test list, base, command, runtime or
    sandbox policy is stale and must be re-earned.
    """

    sha = str(record.get("sha") or "")
    if not sha or not _commit_exists(repo, sha):
        return False
    if record.get("base_rev") != base_rev:
        return False
    if record.get("patch_sha256") != patch_sha256:
        return False
    if sorted(record.get("files") or []) != sorted(files):
        return False
    if sorted(record.get("tests") or []) != sorted(tests):
        return False
    if not _commit_matches(repo, sha, base_rev, files):
        return False
    if not _commit_contains_patch(repo, sha, base_rev, patch):
        return False

    red = record.get("red")
    if not isinstance(red, Mapping) or red.get("exit_code") in (None, 0) or red.get("timed_out"):
        return False
    suite = record.get("suite")
    if not isinstance(suite, Mapping) or suite.get("exit_code") != 0 or suite.get("timed_out"):
        return False
    if require_gitleaks:
        scan = record.get("gitleaks")
        if not isinstance(scan, Mapping) or scan.get("exit_code") != 0:
            return False

    stored = record.get("execution_identity")
    if not isinstance(stored, Mapping):
        return False
    if source_tree_sha256 is None:
        try:
            source_tree_sha256 = _tree_digest(repo, base_rev)
        except VerifyError:
            return False
    expected = _identity_for_commit(
        repo,
        sha,
        base_rev=base_rev,
        source_tree_sha256=str(source_tree_sha256),
        patch_sha256=patch_sha256,
        tests=tests,
        suite_command=suite_command,
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
        require_gitleaks=require_gitleaks,
        gitleaks=gitleaks,
    )
    if expected is None:
        return False
    return dict(stored) == expected


def _commit_contains_patch(repo: Path, sha: str, base_rev: str, patch: str) -> bool:
    """The committed tree must equal the tree produced by applying this patch."""

    try:
        committed = _rev(repo, f"{sha}^{{tree}}")
    except VerifyError:
        return False
    scratch = repo.parent / f".bb-tree-check-{os.getpid()}-{time.time_ns()}"
    if scratch.exists():
        _run(["git", "worktree", "remove", "--force", str(scratch)], repo)
    try:
        if _run(["git", "worktree", "add", "--detach", str(scratch), base_rev], repo).returncode != 0:
            return False
        if _run(["git", "apply", "--whitespace=nowarn", "-"], scratch, input_text=patch).returncode != 0:
            return False
        if _run(["git", "add", "-A"], scratch).returncode != 0:
            return False
        tree = _run(["git", "write-tree"], scratch)
        return tree.returncode == 0 and tree.stdout.strip() == committed
    finally:
        _run(["git", "worktree", "remove", "--force", str(scratch)], repo)


def _run_tests(
    worktree: Path,
    tests: Sequence[str],
    suite_command: Sequence[str] | None,
    *,
    runner: TestRunner | None = None,
    backend: str = "auto",
    allow_unsafe_local: bool = False,
    allow_weak: bool = False,
    timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    receipt_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if suite_command:
        base = list(suite_command)
    else:
        base = ["python", "-m", "pytest", "-q"]
    cmd = base + list(tests)
    return _run_test_command(
        worktree,
        cmd,
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
        receipt_context=receipt_context,
    )


def _run_suite(
    worktree: Path,
    suite_command: Sequence[str],
    *,
    runner: TestRunner | None = None,
    backend: str = "auto",
    allow_unsafe_local: bool = False,
    allow_weak: bool = False,
    timeout: int = DEFAULT_SANDBOX_TIMEOUT_S,
    receipt_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _run_test_command(
        worktree,
        list(suite_command),
        runner=runner,
        backend=backend,
        allow_unsafe_local=allow_unsafe_local,
        allow_weak=allow_weak,
        timeout=timeout,
        receipt_context=receipt_context,
    )


def _rev(cwd: Path, ref: str) -> str:
    proc = _run(["git", "rev-parse", ref], cwd)
    if proc.returncode != 0:
        raise VerifyError("git_error", f"cannot resolve {ref}: {proc.stderr.strip()[:200]}", "Inspect the repository.")
    return proc.stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    return _run(["git", "rev-parse", "--verify", "--quiet", branch], repo).returncode == 0


def _commit_exists(repo: Path, sha: str) -> bool:
    return _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo).returncode == 0


def _commit_matches(repo: Path, sha: str, base_rev: str, files: Sequence[str]) -> bool:
    try:
        return _rev(repo, f"{sha}^") == base_rev and _diff_files(repo, base_rev, sha) == sorted(files)
    except VerifyError:
        return False


def _diff_files(repo: Path, base_rev: str, sha: str) -> list[str]:
    proc = _run(["git", "diff", "--name-only", base_rev, sha], repo)
    if proc.returncode != 0:
        raise VerifyError("git_error", f"cannot diff {base_rev}..{sha}", "Inspect the repository.")
    return sorted(line for line in proc.stdout.splitlines() if line.strip())
