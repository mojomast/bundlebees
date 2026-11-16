"""Run worker-produced checks in a disposable, network-less sandbox.

Threat model
------------
The untrusted input is an entire worktree plus the command that runs inside it
(for example a worker's pytest invocation). A compromised test must not be able
to read the coordinator's home directory, credentials or agent sockets, reach
the network or a metadata service, mutate the real worktree, or persist after
the run. The trusted process only ever reads back a small, scrubbed tail of
output and a size-bounded JUnit summary; it never executes the work in place.

Strategy
--------
* Copy the worktree to a fresh ``mkdtemp`` directory (symlinks copied, never
  followed), bind that copy and a throwaway home into a filesystem/namespace
  wrapper, and delete the whole tree in ``finally``.
* ``auto`` only ever picks a *strong* backend: bubblewrap, then firejail.
  Detection runs ``true`` through the wrapper, so a host with user namespaces
  disabled is treated as unavailable instead of silently running "sandboxed"
  code.
* ``unshare`` is never auto-selected. It runs only when ``verify_isolation``
  is explicitly ``unshare`` *and* the weak-isolation override is on (the
  ``verify_allow_weak_unshare`` setting or ``BUNDLEBEES_ALLOW_WEAK_UNSHARE=1``).
* Fail closed when no allowed wrapper works. The only other escape hatch is
  the explicit ``verify_allow_unsafe_local`` setting, which still uses a
  scrubbed environment and the disposable copy but provides no kernel
  isolation.
* Every child gets ``sandbox_env``: an exact, from-scratch environment. No
  ``os.environ`` value (token, SSH agent, git config, proxy) is inherited.
* ``unshare`` is the weakest backend: it isolates network, IPC, UTS and PID
  namespaces with a scrubbed environment, but the coordinator's filesystem
  (home, credentials, ``.env`` files) remains readable as the invoking uid.
  It is not a filesystem sandbox and is UNSAFE for untrusted worker tests on
  any machine holding credentials.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .security import scrub

BACKENDS: tuple[str, ...] = ("bwrap", "firejail", "unshare")
#: Backends ``auto`` may pick: both hide the coordinator's filesystem.
STRONG_BACKENDS: tuple[str, ...] = ("bwrap", "firejail")
#: Namespace-only backends that need an explicit, documented-unsafe override.
WEAK_BACKENDS: tuple[str, ...] = ("unshare",)
#: Environment override that permits an explicitly requested weak backend.
WEAK_OVERRIDE_ENV = "BUNDLEBEES_ALLOW_WEAK_UNSHARE"

_IGNORED_COPY_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "node_modules",
    }
)

_PROBE_ENV: Mapping[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

_PROBE_TIMEOUT = 10
_MAX_EVIDENCE_BYTES = 2_000_000
_MAX_OUTPUT_TAIL = 4000
_FIX_UNAVAILABLE = (
    "Install bubblewrap (the bubblewrap package) or firejail so a strong "
    "sandbox backend is available. unshare is never picked automatically "
    "because it does not hide the coordinator's filesystem. Only on a "
    "disposable machine with no credentials: set verify_allow_unsafe_local=true "
    "in the bundlebees plugin settings to run without kernel isolation."
)
_FIX_WEAK_REFUSED = (
    "Use verify_isolation=auto (bubblewrap or firejail). unshare does not hide "
    "the coordinator's filesystem; to accept that risk on a disposable host, "
    "set verify_allow_weak_unshare=true or BUNDLEBEES_ALLOW_WEAK_UNSHARE=1."
)
_XML_DANGER_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_EVIDENCE_NAME = ".bundlebees-evidence"
_LOOPBACK_V4_RE = re.compile(r"(?<![\d.])127\.0\.0\.1(?![\d.])")
_LOOPBACK_V6_RE = re.compile(r"(?<![\w:])::1(?![\w:])")
_LOOPBACK_MARKERS = (
    (_LOOPBACK_V4_RE, "127.0.0.1", "__BUNDLEBEES_LOOPBACK_V4__"),
    (_LOOPBACK_V6_RE, "::1", "__BUNDLEBEES_LOOPBACK_V6__"),
)
_TIKTOKEN_CACHE_INSIDE = "/opt/bundlebees/tiktoken-cache"
_ETC_HOSTS = "127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n"
_ETC_NSSWITCH = "hosts: files\n"
_FIX_CACHE_DIR = (
    "Set sandbox_tiktoken_cache to an existing directory outside your home "
    "directory (for example a shared, read-only encoding cache)."
)

Which = Callable[[str], str | None]
Probe = Callable[[str], bool]


class SandboxUnavailable(Exception):
    """Raised when no isolation backend is usable and unsafe local is disabled."""

    code = "sandbox_unavailable"

    def __init__(
        self, error: str, fix: str = _FIX_UNAVAILABLE, code: str | None = None
    ) -> None:
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        self.code = code or type(self).code
        super().__init__(self.error)


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------


def _probe_command(backend: str) -> list[str]:
    if backend == "bwrap":
        return [
            "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "/bin/true",
        ]
    if backend == "firejail":
        return [
            "firejail",
            "--quiet",
            "--noprofile",
            "--net=none",
            "--private-tmp",
            "/bin/true",
        ]
    return [
        "unshare",
        "--user",
        "--net",
        "--ipc",
        "--uts",
        "--pid",
        "--fork",
        "--kill-child",
        "/bin/true",
    ]


def _default_probe(backend: str) -> bool:
    """Run ``true`` through *backend* to prove it works on this host."""
    try:
        proc = subprocess.run(
            _probe_command(backend),
            env=dict(_PROBE_ENV),
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def weak_override_enabled(
    setting: bool = False, environ: Mapping[str, str] | None = None
) -> bool:
    """True when the weak-backend override is on (setting or env var)."""
    if setting is True:
        return True
    env = os.environ if environ is None else environ
    return str(env.get(WEAK_OVERRIDE_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def detect_backend(
    preferred: str = "auto",
    *,
    allow_weak: bool = False,
    which: Which = shutil.which,
    probe: Probe | None = None,
) -> str | None:
    """Return a usable backend name, or ``None`` when none is usable here.

    ``auto`` only considers :data:`STRONG_BACKENDS`; a weak backend is never
    auto-selected. A weak backend named explicitly is returned only when
    *allow_weak* is true; otherwise ``None`` (fail closed).
    """
    if preferred != "auto" and preferred not in BACKENDS:
        raise ValueError(f"unknown sandbox backend: {preferred!r}")
    if preferred in WEAK_BACKENDS and not allow_weak:
        return None
    check = probe or _default_probe
    names: Sequence[str] = STRONG_BACKENDS if preferred == "auto" else (preferred,)
    for name in names:
        if not which(name):
            continue
        try:
            if check(name):
                return name
        except (OSError, subprocess.SubprocessError):
            continue
    return None


# ---------------------------------------------------------------------------
# Environment and wrapper commands
# ---------------------------------------------------------------------------


def sandbox_env(home: Path, tmp: Path) -> dict[str, str]:
    """Build a fresh, exact environment; nothing is copied from ``os.environ``."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": os.fspath(home),
        "TMPDIR": os.fspath(tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
        "CI": "1",
    }


def _python_prefixes() -> list[str]:
    candidates: list[str] = []
    for value in (sys.prefix, sys.base_prefix, sys.exec_prefix):
        if value:
            candidates.append(value)
    try:
        candidates.extend(site.getsitepackages())
    except (AttributeError, OSError):
        pass
    # Follow the interpreter's symlink chain (venv -> uv/pyenv/system python):
    # bind each hop's install prefix at its unresolved path so the exact
    # sys.executable path resolves inside the sandbox.
    hop = sys.executable
    for _ in range(8):
        if not hop:
            break
        candidates.append(str(Path(hop).parent.parent))
        if not os.path.islink(hop):
            break
        target = os.readlink(hop)
        hop = target if os.path.isabs(target) else os.path.join(os.path.dirname(hop), target)

    real_home = Path.home().resolve()
    seen: set[str] = set()
    prefixes: list[str] = []
    for value in candidates:
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            continue
        resolved = path.resolve()
        if resolved == Path("/") or resolved == real_home:
            continue
        for text in (os.path.normpath(str(path)), str(resolved)):
            if text in seen or text in ("/", str(real_home)):
                continue
            seen.add(text)
            prefixes.append(text)
    return prefixes


def _resolve_tiktoken_cache(
    value: Path | str | None, *, backend: str | None = None
) -> Path | None:
    """Resolve an operator tiktoken cache, refusing unsafe locations.

    The masked-root refusal is backend-scoped: only a backend whose wrapper replaces the masked
    tmp roots (firejail ``--private-tmp``) makes a cache under them unusable, so only such a
    backend -- and, failing closed, a call that names no known backend id -- is refused one.
    bwrap ro-binds the individual cache directory and unshare masks nothing, so both accept the
    cache's host path.
    """
    if value is None:
        return None
    raw = os.fspath(value)
    if not str(raw).strip():
        return None
    cache = Path(raw).expanduser().resolve()
    real_home = Path.home().resolve()
    if cache == Path("/") or cache == real_home or real_home in cache.parents:
        raise SandboxUnavailable(
            "sandbox_tiktoken_cache must be outside the coordinator home directory",
            _FIX_CACHE_DIR,
            code="unsafe_cache_dir",
        )
    if backend is None or backend not in BACKENDS or backend in _TMP_MASKING_BACKENDS:
        # Fail closed without a backend id, for an unknown id, and for ``auto`` (a selection
        # policy, not a backend id: it may still pick the masking backend); only a known
        # non-masking backend accepts a cache under the masked roots.
        for root in _MASKED_TMP_ROOTS:
            masked = Path(root)
            if cache == masked or masked in cache.parents:
                raise SandboxUnavailable(
                    f"sandbox_tiktoken_cache must not live under {root}: a backend that masks "
                    "that root would stage a copy of the whole masked root into the sandbox copy",
                    _FIX_CACHE_DIR,
                    code="unsafe_cache_dir",
                )
    if not cache.is_dir():
        raise SandboxUnavailable(
            "sandbox_tiktoken_cache is not an existing directory",
            _FIX_CACHE_DIR,
            code="unsafe_cache_dir",
        )
    return cache


def _bwrap_command(
    copy_dir: Path,
    home: Path,
    argv: list[str],
    *,
    extra_ro: Sequence[tuple[str, str]] = (),
) -> list[str]:
    cmd = [
        "bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]

    read_only: list[str] = []
    seen: set[str] = set()
    real_home = str(Path.home().resolve())

    def ro(path: str) -> None:
        if path == "/" or path == real_home or path in seen:
            return
        seen.add(path)
        read_only.extend(["--ro-bind-try", path, path])

    for path in ("/usr", "/etc/alternatives", "/etc/ld.so.cache", "/etc/localtime", "/etc/ssl"):
        ro(path)
    for path in _python_prefixes():
        ro(path)
    cmd.extend(read_only)

    for path in ("/bin", "/lib", "/lib64", "/sbin"):
        target = Path(path)
        if target.is_symlink():
            try:
                link = os.readlink(path)
            except OSError:
                cmd.extend(["--ro-bind-try", path, path])
            else:
                cmd.extend(["--symlink", link, path])
        else:
            cmd.extend(["--ro-bind-try", path, path])

    for source, target in extra_ro:
        cmd.extend(["--ro-bind", str(source), str(target)])

    cmd.extend(
        [
            "--bind",
            str(copy_dir),
            "/work",
            "--bind",
            str(home),
            "/home/sandbox",
            "--chdir",
            "/work",
            *argv,
        ]
    )
    return cmd


def build_command(
    backend: str,
    copy_dir: Path,
    home: Path,
    argv: list[str],
    *,
    extra_ro: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """Return the wrapper argv for *backend* around *argv*.

    ``copy_dir`` is the only writable worktree visible to the child; ``home`` is
    a throwaway home. The real ``/`` and the real home are never bound. The
    ``unshare`` backend is namespace-only: the filesystem is still the host's.
    ``extra_ro`` adds read-only bind mounts ``(source, target)`` for bwrap.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown sandbox backend: {backend!r}")
    if backend == "bwrap":
        return _bwrap_command(
            Path(copy_dir), Path(home), list(argv), extra_ro=extra_ro
        )
    if backend == "firejail":
        return [
            "firejail",
            "--quiet",
            "--noprofile",
            "--net=none",
            f"--private={copy_dir}",
            "--private-tmp",
            "--caps.drop=all",
            "--nonewprivs",
            "--seccomp",
            "--nogroups",
            "--nosound",
            "--no3d",
            *argv,
        ]
    return [
        "unshare",
        "--user",
        "--net",
        "--ipc",
        "--uts",
        "--pid",
        "--fork",
        "--kill-child",
        *argv,
    ]


#: Environment override for the interpreter the sandbox starts (same meaning as ``verify_python``).
PYTHON_ENV = "BUNDLEBEES_VERIFY_PYTHON"
#: Where a staged interpreter lives inside the sandbox copy.
_SANDBOX_PYTHON_DIR = ".bundlebees-python"
#: Where a staged tiktoken cache lives inside the sandbox copy.
_SANDBOX_TIKTOKEN_DIR = ".bundlebees-tiktoken"
#: Backends whose wrapper remaps ``$HOME``, so a path inside the coordinator home does not exist.
HOME_HIDING_BACKENDS: tuple[str, ...] = ("firejail",)
#: Filesystem roots ``firejail --private-tmp`` replaces with a fresh tmpfs.
_MASKED_TMP_ROOTS: tuple[str, ...] = ("/tmp", "/var/tmp")
#: Backends whose wrapper replaces the masked tmp roots; only these refuse a cache under one.
_TMP_MASKING_BACKENDS: frozenset[str] = frozenset({"firejail"})
_FIX_PYTHON = (
    "Set verify_python (or BUNDLEBEES_VERIFY_PYTHON) to an absolute interpreter path outside the "
    "coordinator home that has pytest installed (for example a venv under /var/tmp), or use "
    "verify_isolation=bwrap on a host that allows unprivileged user namespaces."
)


def resolve_sandbox_python(python: str | None = None) -> str | None:
    """Validated ``verify_python`` interpreter, or None to start the coordinator's own.

    ``verify_python`` / ``BUNDLEBEES_VERIFY_PYTHON`` names the interpreter the sandbox runs.
    Whether it can be reached is a property of the chosen backend, so ``run_in_sandbox`` probes
    it there instead of guessing from the path.
    """
    explicit = str(python or os.environ.get(PYTHON_ENV) or "").strip()
    if not explicit:
        return None
    path = Path(explicit).expanduser()
    if not path.is_absolute():
        raise SandboxUnavailable(
            f"verify_python must be an absolute path, got {explicit!r}",
            _FIX_PYTHON,
            code="invalid_argument",
        )
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise SandboxUnavailable(
            f"verify_python is not an executable file: {path}",
            _FIX_PYTHON,
            code="invalid_argument",
        )
    return str(path)


def interpreter_visible_in_sandbox(
    backend: str,
    copy_dir: Path,
    home: Path,
    interpreter: str,
    env: dict[str, str],
) -> bool:
    """Probe the real backend for one thing: can the sandbox exec *interpreter*?

    A path heuristic cannot answer this. ``firejail --private=<copy>`` masks the coordinator home
    but leaves separate mounts under it reachable, so ``~/.hermes/tools/python-*`` vanishes while
    ``~/.cache/uv/...`` does not -- and ``--private-tmp`` masks ``/tmp`` and ``/var/tmp`` as well.
    Ask the wrapper instead.
    """
    probe = ["/bin/sh", "-c", f"test -x {shlex.quote(str(interpreter))}"]
    try:
        proc = subprocess.run(
            build_command(backend, copy_dir, home, probe),
            cwd=str(copy_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def stage_interpreter(copy_dir: Path, host_python: str) -> str:
    """Copy *host_python*'s install tree into the sandbox copy; return the sandbox-visible path.

    The returned path is **relative** (``./.bundlebees-python/bin/python``): a wrapper that remaps
    the home mounts the copy over ``$HOME`` and may not expose the copy's host path at all, while
    the child's cwd is always the copy. ``firejail --private=<copy>`` behaves exactly that way
    (a host path under the copy answers ENOENT inside the jail, the same file answers fine as
    ``./…`` or ``$HOME/…``).

    The copy is disposable, a venv's ``bin/python`` symlink still resolves to the visible base
    interpreter, and ``python -m pytest`` never depends on installed console-script shebangs.
    """
    host = Path(host_python).expanduser()
    root = host.parent.parent  # <venv>/bin/python -> <venv>
    destination = Path(copy_dir) / _SANDBOX_PYTHON_DIR
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(root, destination, symlinks=True)
    staged = destination / host.relative_to(root)
    if not staged.is_symlink():
        # A venv's bin/python is usually a symlink to the base interpreter; chmod would follow it
        # and fail with EPERM on a root-owned target. Only a regular file needs the execute bit.
        staged.chmod(staged.stat().st_mode | 0o500)
    return f"./{_SANDBOX_PYTHON_DIR}/{host.relative_to(root).as_posix()}"


def swap_interpreter(argv: list[str], interpreter: str | None) -> list[str]:
    """Return a copy of *argv* whose interpreter position is *interpreter* when one was resolved."""
    argv = list(argv)
    if not interpreter or not argv:
        return argv
    if not _is_coordinator_interpreter(argv[0]):
        return argv
    return [interpreter, *argv[1:]]


def _cache_masked_in_sandbox(cache: Path) -> bool:
    """True when the firejail wrapper masks *cache*, so its host path is ENOENT inside the jail.

    ``--private-tmp`` mounts a fresh tmpfs over ``/tmp`` and ``/var/tmp``, and ``--private=<copy>``
    mounts the copy over the coordinator's home; a cache under either cannot be addressed by its
    host path inside the jail and tiktoken would silently rebuild (or fail offline).
    """
    resolved = Path(cache).resolve()
    for root in _MASKED_TMP_ROOTS:
        masked = Path(root)
        if resolved == masked or masked in resolved.parents:
            return True
    real_home = Path.home().resolve()
    return resolved == real_home or real_home in resolved.parents


def stage_tiktoken_cache(copy_dir: Path, cache: Path) -> str:
    """Copy an operator cache into the sandbox copy; return the sandbox-visible relative path.

    Mirrors :func:`stage_interpreter`: the returned path is **relative** because the jail's cwd is
    the copy while the copy's host path does not exist inside the jail. The copy is disposable and
    deleted with the rest of the sandbox; symlinks are copied, never followed.
    """
    destination = Path(copy_dir) / _SANDBOX_TIKTOKEN_DIR
    if destination.is_symlink() or destination.is_file():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(cache, destination, symlinks=True)
    return f"./{_SANDBOX_TIKTOKEN_DIR}"


def _is_coordinator_interpreter(path: str) -> bool:
    """True when *path* is the interpreter this coordinator runs (a hidden one needs a substitute)."""
    if not path:
        return False
    try:
        # realpath("python3") resolves a file in cwd, not a PATH entry.
        # Only substitute a bare name when it resolves to this interpreter;
        # another venv or an arbitrary executable must retain its identity.
        resolved = path if os.path.dirname(path) else shutil.which(path)
        return bool(resolved) and os.path.realpath(resolved) == os.path.realpath(sys.executable)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Evidence and execution
# ---------------------------------------------------------------------------


def pytest_argv(targets: list[str]) -> list[str]:
    """Build a pytest invocation that writes JUnit evidence into the sandbox."""
    return [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--junitxml={evidence}/junit.xml",
        *targets,
    ]


def _ignore_copy(_directory: str, names: Iterable[str]) -> set[str]:
    return {name for name in names if name in _IGNORED_COPY_NAMES}


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _output_tail(stdout: object, stderr: object) -> str:
    text = (_as_text(stdout) + _as_text(stderr)).replace("\x00", "")[-_MAX_OUTPUT_TAIL:]
    protected = text
    for pattern, _literal, marker in _LOOPBACK_MARKERS:
        protected = pattern.sub(marker, protected)
    cleaned = str(scrub(protected))
    for _pattern, literal, marker in _LOOPBACK_MARKERS:
        cleaned = cleaned.replace(marker, literal)
    return cleaned.strip()[-_MAX_OUTPUT_TAIL:]


def _as_int(value: str | None) -> int:
    if value is None:
        return 0
    try:
        number = int(value.strip())
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _junit_summary(evidence: Path) -> dict[str, int] | None:
    path = evidence / "junit.xml"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > _MAX_EVIDENCE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_EVIDENCE_BYTES:
        return None
    text = raw.decode("utf-8", errors="replace")
    if _XML_DANGER_RE.search(text):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = [child for child in list(root) if child.tag == "testsuite"]
        if not suites:
            suites = [root]
    else:
        return None

    return {
        "tests": sum(_as_int(suite.get("tests")) for suite in suites),
        "failures": sum(_as_int(suite.get("failures")) for suite in suites),
        "errors": sum(_as_int(suite.get("errors")) for suite in suites),
        "skipped": sum(_as_int(suite.get("skipped")) for suite in suites),
    }


def _replace_evidence(argv: Iterable[str], evidence: str) -> list[str]:
    return [str(argument).replace("{evidence}", evidence) for argument in argv]


def _fresh_dir(path: Path) -> None:
    """Create an empty directory, discarding anything the copy already had."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def run_in_sandbox(
    workdir: Path,
    argv: list[str],
    *,
    backend: str = "auto",
    allow_unsafe_local: bool = False,
    allow_weak: bool = False,
    timeout: int = 900,
    which: Which = shutil.which,
    probe: Probe | None = None,
    tiktoken_cache: Path | str | None = None,
    collect_file: str | None = None,
    python: str | None = None,
) -> dict:
    """Copy *workdir*, run *argv* in isolation, and return only evidence.

    With *collect_file*, the JSON object the child wrote to
    ``{evidence}/<collect_file>`` is returned as ``collected`` (bounded,
    regular file only, ``None`` when absent or malformed).

    The real worktree is never modified or executed in place. The disposable
    copy is always deleted, even on timeout or an unavailable backend.
    ``allow_weak`` (or ``BUNDLEBEES_ALLOW_WEAK_UNSHARE=1``) is required before an
    explicitly requested ``unshare`` backend runs; ``auto`` never picks it.
    ``tiktoken_cache`` names an operator-owned cache exposed as ``TIKTOKEN_CACHE_DIR``; it must be
    an existing directory outside home. bwrap binds it read-only at a fixed inside path; a backend
    that masks its host path (firejail's ``--private-tmp`` over ``/tmp`` and ``/var/tmp``, or the
    private home mount) stages a copy of it into the sandbox copy and names the relative path.
    ``python`` (``verify_python``) starts the child with an interpreter that
    exists inside the sandbox; a backend that hides the coordinator home refuses
    an interpreter living in it instead of failing inside the wrapper.
    """
    cache = _resolve_tiktoken_cache(tiktoken_cache, backend=backend)
    weak_ok = weak_override_enabled(allow_weak)
    source = Path(workdir)
    root = Path(tempfile.mkdtemp(prefix="bundlebees-verify-"))
    copy_dir = root / "work"
    home = root / "home"
    tmp = root / "tmp"
    try:
        shutil.copytree(source, copy_dir, symlinks=True, ignore=_ignore_copy)
        home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)

        if backend in WEAK_BACKENDS and not weak_ok:
            raise SandboxUnavailable(
                f"verify_isolation={backend} is a weak, namespace-only backend that "
                "does not hide the coordinator's filesystem; refusing without the "
                "explicit weak-isolation override",
                _FIX_WEAK_REFUSED,
            )
        chosen = detect_backend(backend, allow_weak=weak_ok, which=which, probe=probe)
        if chosen is None and not allow_unsafe_local:
            raise SandboxUnavailable(
                "no strong sandbox backend (bubblewrap or firejail) is usable on "
                "this host; refusing to run worker-produced code"
            )

        evidence_host = copy_dir / _EVIDENCE_NAME
        _fresh_dir(evidence_host)

        extra_ro: list[tuple[str, str]] = []
        if chosen == "bwrap":
            evidence_inside = f"/work/{_EVIDENCE_NAME}"
            (home / "home").mkdir(parents=True, exist_ok=True)
            (home / "tmp").mkdir(parents=True, exist_ok=True)
            env = sandbox_env(
                Path("/home/sandbox/home"), Path("/home/sandbox/tmp")
            )
            hosts_file = root / "etc-hosts"
            hosts_file.write_text(_ETC_HOSTS, encoding="utf-8")
            nsswitch_file = root / "etc-nsswitch"
            nsswitch_file.write_text(_ETC_NSSWITCH, encoding="utf-8")
            extra_ro = [
                (str(hosts_file), "/etc/hosts"),
                (str(nsswitch_file), "/etc/nsswitch.conf"),
            ]
            if cache is not None:
                extra_ro.append((str(cache), _TIKTOKEN_CACHE_INSIDE))
                env["TIKTOKEN_CACHE_DIR"] = _TIKTOKEN_CACHE_INSIDE
        elif chosen == "firejail":
            # firejail --private=<copy> mounts the copy OVER $HOME, so the copy's host path does not
            # exist inside the jail even though the same directory is the child's cwd. Evidence must be
            # addressed relatively, exactly like a staged interpreter.
            evidence_inside = f"./{_EVIDENCE_NAME}"
            # The copy is mounted OVER the coordinator's home, so the jail-visible home is the real
            # home path, not the copy's host path (which does not exist inside).
            env = sandbox_env(Path.home(), Path("/tmp"))
            if cache is not None:
                if _cache_masked_in_sandbox(cache):
                    # --private-tmp replaces /tmp and /var/tmp (and the private mount replaces the
                    # coordinator home), so the cache's host path is ENOENT inside the jail; stage it
                    # into the sandbox copy and name it relative to the child's cwd, exactly like a
                    # staged interpreter. The copy is disposable and deleted with the sandbox.
                    env["TIKTOKEN_CACHE_DIR"] = stage_tiktoken_cache(copy_dir, cache)
                else:
                    # A cache outside the masked roots is genuinely visible inside the jail.
                    env["TIKTOKEN_CACHE_DIR"] = str(cache)
        else:
            evidence_inside = str(evidence_host)
            env = sandbox_env(home, tmp)
            if cache is not None:
                env["TIKTOKEN_CACHE_DIR"] = str(cache)

        command_argv = _replace_evidence(argv, evidence_inside)
        if command_argv and _is_coordinator_interpreter(command_argv[0]):
            configured = resolve_sandbox_python(python)
            target = configured or sys.executable
            if chosen in HOME_HIDING_BACKENDS and not interpreter_visible_in_sandbox(
                chosen, copy_dir, home, target, env
            ):
                if not configured:
                    raise SandboxUnavailable(
                        f"{chosen} hides {target} from the sandbox, so the worker's tests cannot "
                        "start; the wrapper only says 'no suitable ... executable found'",
                        _FIX_PYTHON,
                        code="sandbox_interpreter_hidden",
                    )
                staged = stage_interpreter(copy_dir, configured)
                if not interpreter_visible_in_sandbox(chosen, copy_dir, home, staged, env):
                    raise SandboxUnavailable(
                        f"{chosen} hides {configured} even after staging it inside the sandbox "
                        "copy; point verify_python at a self-contained interpreter",
                        _FIX_PYTHON,
                        code="sandbox_interpreter_hidden",
                    )
                command_argv = swap_interpreter(command_argv, staged)
            elif configured:
                command_argv = swap_interpreter(command_argv, configured)
        if chosen is None:
            label = "none-unsafe-local"
            command = command_argv
        else:
            label = chosen
            command = build_command(
                chosen, copy_dir, home, command_argv, extra_ro=extra_ro
            )

        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                command,
                cwd=str(copy_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            exit_code = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout, stderr = exc.stdout, exc.stderr
        except OSError as exc:
            exit_code = 127
            stdout, stderr = "", str(exc)
        duration = time.monotonic() - started

        cases: list[dict] = []
        try:
            from . import tdd as _tdd

            junit_path = evidence_host / "junit.xml"
            if junit_path.is_file() and not junit_path.is_symlink():
                cases = _tdd.parse_junit_cases(junit_path)
        except Exception:
            cases = []

        result = {
            "backend": label,
            "exit_code": int(exit_code),
            "timed_out": timed_out,
            "duration_s": float(duration),
            "output_tail": _output_tail(stdout, stderr),
            "junit": _junit_summary(evidence_host),
            "cases": cases,
        }
        if collect_file:
            result["collected"] = _read_collected(evidence_host / collect_file)
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _read_collected(path: Path) -> dict | None:
    """Parse a child-written JSON object, refusing symlinks and oversized files."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_EVIDENCE_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
