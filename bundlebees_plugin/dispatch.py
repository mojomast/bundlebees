"""Serve the lease payload: the pinned sources a sandboxed verify suite consumes.

A worker's verify suite reads the pinned protocol sources through
``WORKFLOW_SRC`` / ``HYVV_SRC`` environment variables (see
``docs/pinned-sources.md``). The checkouts live in the coordinator home, which
every strong sandbox wrapper hides: ``firejail --private=<copy>`` mounts the
disposable copy over the coordinator home and ``--private-tmp`` replaces
``/tmp`` and ``/var/tmp``, so a payload addressed at its absolute host path is
ENOENT inside the jail and serves nothing. That is the failure this module
exists to prevent, and the reason serving is *staging*, not env transport.

Dispatch therefore turns the coordinator's configured pinned sources into a
lease payload:

* :func:`collect_payload` reads only the coordinator's own environment, and
  only variables in :data:`PAYLOAD_ENV_VARS`. A variable that is unset or empty
  serves nothing; one that is set but does not name an existing directory is a
  fail-closed :class:`PayloadError`, never a silent gap.
* :func:`payload_digest` binds the payload to exact bytes, so a lease (or a
  verify run) can prove later that what was served is what was dispatched.
* :func:`lease_payload` checks a lease's recorded payload manifest against the
  collected sources, so a lease cannot be served against different trees.
* The serving half lives in :mod:`bundlebees_plugin.sandbox`:
  :func:`sandbox.stage_payload` copies each source into the sandbox copy --
  exactly like a staged interpreter or tiktoken cache -- and
  :func:`sandbox.run_in_sandbox` hands the child the returned **relative**
  paths (``./.bundlebees-payload/<name>``). The child's cwd is always the
  copy, so the relative path resolves inside every backend; the host checkout
  path never appears in the child env or in the wrapper argv, and on a masking
  wrapper it does not even exist inside the jail. Code that runs in the child
  and calls back into dispatch (:func:`collect_payload`) is handed that same
  relative form; :func:`resolve_source` resolves exactly that staged entry
  under the child's own payload directory and keeps refusing every other
  relative path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import sandbox

#: The pinned-source variables a lease payload may serve.
PAYLOAD_ENV_VARS: tuple[str, ...] = ("WORKFLOW_SRC", "HYVV_SRC")

_NAME_RE = sandbox.PAYLOAD_NAME_RE

_FIX_SOURCE = sandbox._FIX_PAYLOAD
_FIX_LEASE = (
    "Re-dispatch the attempt so its lease records the payload the coordinator "
    "actually serves; do not verify against a source the lease did not pin."
)


class PayloadError(Exception):
    """A configured lease-payload source cannot be served; dispatch fails closed."""

    def __init__(self, code: str, error: str, fix: str = "") -> None:
        super().__init__(error)
        self.code = code
        self.error = error
        self.fix = fix


def resolve_source(name: str, value: str) -> Path:
    """Validate one configured payload source and return its absolute host path.

    The value must be an absolute path to an existing directory; anything else
    is a named refusal, because a payload the coordinator cannot serve must
    fail closed rather than leave the suite without its pinned sources.

    The one exception is the served form a sandbox child is given:
    ``./.bundlebees-payload/<name>`` (see :func:`sandbox.stage_payload`). It is
    accepted only when it names this payload's staged entry and resolves under
    the child's own payload directory, so code that runs inside the sandbox
    can record a lease against the payload it was actually served while every
    other relative path stays refused.
    """
    if not _NAME_RE.fullmatch(str(name)):
        raise PayloadError("payload_invalid_name", f"invalid payload name: {name!r}")
    raw = str(value).strip()
    if not raw:
        raise PayloadError(
            "payload_source_missing", f"{name} is set but empty", _FIX_SOURCE
        )
    if "\x00" in raw:
        raise PayloadError(
            "payload_source_invalid", f"{name} contains a NUL byte", _FIX_SOURCE
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return _resolve_served_payload_source(name, raw)
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise PayloadError(
            "payload_source_invalid",
            f"{name} cannot be resolved: {exc}",
            _FIX_SOURCE,
        ) from exc
    if not resolved.is_dir():
        raise PayloadError(
            "payload_source_missing",
            f"{name} does not name an existing directory: {resolved}",
            _FIX_SOURCE,
        )
    return resolved


def _resolve_served_payload_source(name: str, raw: str) -> Path:
    """Resolve the sandbox-served relative form to the staged entry it names.

    The child env inside a sandbox names a staged payload relatively
    (``./.bundlebees-payload/<name>``) because the jail's cwd is the disposable
    copy and the copy's host path does not exist inside it. Accepting that one
    shape lets a test running in the sandbox call the dispatch chokepoint with
    the env it was given; nothing else loosens: the value must be exactly the
    staged entry for *name*, and its resolved path must stay under this
    process's own payload directory (no ``..``, no other name, no symlink out,
    no bare relative path). Every other relative value keeps the named
    absolute-path refusal.
    """
    if Path(raw).parts != (sandbox._SANDBOX_PAYLOAD_DIR, str(name)):
        raise PayloadError(
            "payload_source_invalid",
            f"{name} must be an absolute path, got {raw!r}",
            _FIX_SOURCE,
        )
    try:
        root = Path.cwd().resolve()
        resolved = (root / sandbox._SANDBOX_PAYLOAD_DIR / str(name)).resolve()
    except OSError as exc:
        raise PayloadError(
            "payload_source_invalid",
            f"{name} cannot be resolved against the sandbox copy: {exc}",
            _FIX_SOURCE,
        ) from exc
    payload_root = root / sandbox._SANDBOX_PAYLOAD_DIR
    if (
        resolved.name != str(name)
        or resolved.parent != payload_root
        or not resolved.is_relative_to(root)
        or not resolved.is_dir()
    ):
        raise PayloadError(
            "payload_source_invalid",
            f"{name} does not resolve to the staged payload "
            f"({sandbox._SANDBOX_PAYLOAD_DIR}/{name}) of this sandbox copy",
            _FIX_SOURCE,
        )
    return resolved


def collect_payload(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Collect the lease payload from the coordinator environment.

    Returns a mapping of payload name to absolute host path for every
    :data:`PAYLOAD_ENV_VARS` entry that is configured. The coordinator's
    environment is read-only input here: none of it reaches the child, which
    only ever sees the staged, relative paths :func:`sandbox.stage_payload`
    returns.
    """
    env = os.environ if environ is None else environ
    payload: dict[str, str] = {}
    for name in PAYLOAD_ENV_VARS:
        value = str(env.get(name, "") or "")
        if not value.strip():
            continue
        payload[name] = str(resolve_source(name, value))
    return payload


def payload_digest(payload: Mapping[str, str]) -> dict[str, str]:
    """Digest each payload source tree; the digests bind dispatch to serve.

    The digest covers every served file's relative path and bytes (symlinks by
    their target), skipping the same names :func:`sandbox.stage_payload` leaves
    out, so the digest of a source tree equals the digest a child computes over
    the staged copy of it.
    """
    digests: dict[str, str] = {}
    for name, source in payload.items():
        digests[str(name)] = _tree_digest(Path(source))
    return digests


def lease_payload(
    record: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The lease payload for *record*, collected from the coordinator environment.

    When *record* is a lease whose brief records a ``payload`` manifest of
    ``{name: sha256}``, the collected sources must match every recorded digest
    exactly; a missing or differing source is a :class:`PayloadError`, so a
    lease can never be verified against trees it did not pin. A lease without
    a recorded manifest binds nothing extra.
    """
    payload = collect_payload(environ)
    if record is None:
        return payload
    brief = record.get("brief")
    manifest = brief.get("payload") if isinstance(brief, Mapping) else None
    if manifest is None:
        return payload
    if not isinstance(manifest, Mapping):
        raise PayloadError(
            "payload_lease_mismatch",
            "the lease's recorded payload is not a name-to-digest manifest",
            _FIX_LEASE,
        )
    digests = payload_digest(payload)
    for name, expected in manifest.items():
        actual = digests.get(str(name))
        if actual is None:
            raise PayloadError(
                "payload_lease_mismatch",
                f"the lease pins {name} but the coordinator does not serve it",
                _FIX_LEASE,
            )
        if actual != expected:
            raise PayloadError(
                "payload_lease_mismatch",
                f"the served {name} does not match the digest recorded in the lease",
                _FIX_LEASE,
            )
    return payload


def serve_in_sandbox(
    workdir: Path,
    argv: Sequence[str],
    *,
    backend: str = "auto",
    payload: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    **run_kwargs: Any,
) -> dict:
    """Run *argv* in the sandbox with the lease payload served.

    *payload* defaults to :func:`collect_payload` over *environ*. The payload
    is staged into the disposable sandbox copy and the child env names each
    entry relative to its cwd; the host checkout path is never handed to the
    child. All other arguments are forwarded to
    :func:`bundlebees_plugin.sandbox.run_in_sandbox`.
    """
    resolved = dict(payload) if payload is not None else collect_payload(environ)
    return sandbox.run_in_sandbox(
        Path(workdir),
        list(argv),
        backend=backend,
        payload=resolved or None,
        **run_kwargs,
    )


def _tree_digest(root: Path) -> str:
    """A stable digest of *root*'s served file set (paths, bytes, symlink targets)."""
    entries: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in sandbox.PAYLOAD_IGNORED_NAMES
        )
        for filename in sorted(filenames):
            if filename in sandbox.PAYLOAD_IGNORED_NAMES:
                continue
            path = Path(directory) / filename
            entries.append((path.relative_to(root).as_posix(), path))
    digest = hashlib.sha256(b"bundlebees-lease-payload:v1\x00")
    for rel, path in entries:
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
                digest.update(b"unreadable")
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\x00")
    return digest.hexdigest()
