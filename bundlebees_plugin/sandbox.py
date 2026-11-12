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

import os
import re
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


def _resolve_tiktoken_cache(value: Path | str | None) -> Path | None:
    """Resolve an operator tiktoken cache, refusing unsafe locations."""
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
) -> dict:
    """Copy *workdir*, run *argv* in isolation, and return only evidence.

    The real worktree is never modified or executed in place. The disposable
    copy is always deleted, even on timeout or an unavailable backend.
    ``allow_weak`` (or ``BUNDLEBEES_ALLOW_WEAK_UNSHARE=1``) is required before an
    explicitly requested ``unshare`` backend runs; ``auto`` never picks it.
    ``tiktoken_cache`` names an operator-owned cache bound read-only and exposed
    as ``TIKTOKEN_CACHE_DIR``; it must be an existing directory outside home.
    """
    cache = _resolve_tiktoken_cache(tiktoken_cache)
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
            evidence_inside = str(evidence_host)
            env = sandbox_env(copy_dir, Path("/tmp"))
            if cache is not None:
                env["TIKTOKEN_CACHE_DIR"] = str(cache)
        else:
            evidence_inside = str(evidence_host)
            env = sandbox_env(home, tmp)
            if cache is not None:
                env["TIKTOKEN_CACHE_DIR"] = str(cache)

        command_argv = _replace_evidence(argv, evidence_inside)
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

        return {
            "backend": label,
            "exit_code": int(exit_code),
            "timed_out": timed_out,
            "duration_s": float(duration),
            "output_tail": _output_tail(stdout, stderr),
            "junit": _junit_summary(evidence_host),
            "cases": cases,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
