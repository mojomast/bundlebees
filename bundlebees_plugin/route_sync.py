"""Record the route each locally leased attempt runs on, as the launcher reports it.

The launcher picks the model (and with it the provider route) when it claims a
bee, so the coordinator's lease cannot know it at dispatch. This module reads
the launcher's own attempt listing (``GET /v1/attempts``, public fields only)
and writes one small observation per attempt this coordinator leased::

    <state>/attempt-routes/<attempt_id>.json   (0600, directory 0700)

The route is taken only from an explicit provider prefix on the reported model
(``nous:hy4-flash`` -> ``nous``). An unprefixed model keeps its model but no
route: a route is never inferred from a model name. These are launcher
*reports*, not verified attribution; ``attempts`` lets a verified attribution
intent outrank them and never copies a reported model into
``attributed_model``.

Run from a timer (see ``contrib/systemd/bundlebees-route-sync.*``)::

    python -m bundlebees_plugin.route_sync --state-dir DIR

The launcher URL comes from ``--launcher-url`` or ``BUNDLEBEES_LAUNCHER_URL``
and the credential only from ``BUNDLEBEES_LAUNCHER_TOKEN`` in the environment
(the factory driver's convention). Nothing is printed but counts and a generic
error code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "bundlebees.attempt-route/1"
ROUTES = ("go", "deepseek", "zoo", "kimi", "nous")
DIRNAME = "attempt-routes"
MAX_RECORD = 4096
_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_STAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def _attempt(value: Any) -> str | None:
    return value if isinstance(value, str) and _ATTEMPT_RE.fullmatch(value) else None


def _model(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _MODEL_RE.fullmatch(text) and ".." not in text else None


def route_of(model: str | None) -> str | None:
    """The provider route an explicit ``<route>:<model>`` prefix names, else None."""
    if not model or ":" not in model:
        return None
    prefix, _, rest = model.partition(":")
    return prefix if prefix in ROUTES and rest else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _leased(state: Path, attempt_id: str) -> bool:
    try:
        info = os.lstat(state / "leases" / f"{attempt_id}.json")
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _store(state: Path) -> Path:
    root = state / DIRNAME
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        os.mkdir(root, 0o700)
        info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise OSError("route store is not a private directory")
    if info.st_mode & 0o077:
        os.chmod(root, 0o700)
    return root


def _read(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD:
            return None
        raw = os.read(fd, MAX_RECORD + 1)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_route(state_dir: Path | str, attempt_id: str) -> tuple[str | None, str | None]:
    """``(route, model)`` reported for ``attempt_id``; ``(None, None)`` when unusable."""
    attempt = _attempt(attempt_id)
    if attempt is None:
        return None, None
    data = _read(Path(state_dir) / DIRNAME / f"{attempt}.json")
    if not data or data.get("schema") != SCHEMA or data.get("attempt_id") != attempt:
        return None, None
    model = _model(data.get("model"))
    route = data.get("route")
    if route is not None and (route not in ROUTES or route != route_of(model)):
        return None, None
    return route, model


def _write(root: Path, attempt_id: str, body: Mapping[str, Any]) -> None:
    payload = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".route-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, root / f"{attempt_id}.json")
    except OSError:
        os.unlink(tmp)
        raise


def sync(state_dir: Path | str, launcher: Any, *, now: str | None = None) -> dict[str, int]:
    """Write route observations for every locally leased attempt the launcher lists."""
    state = Path(state_dir)
    rows: Iterable[Any] = launcher.attempts_raw()
    stamp = now if isinstance(now, str) and _STAMP_RE.fullmatch(now) else _utc_now()
    counts = {"seen": 0, "recorded": 0, "unchanged": 0, "skipped": 0}
    root: Path | None = None
    for row in rows:
        counts["seen"] += 1
        data = row if isinstance(row, Mapping) else {}
        attempt = _attempt(data.get("attempt_id") or data.get("id"))
        model = _model(data.get("model"))
        if attempt is None or model is None or not _leased(state, attempt):
            counts["skipped"] += 1
            continue
        route = route_of(model)
        current = read_route(state, attempt)
        if current == (route, model) and (state / DIRNAME / f"{attempt}.json").is_file():
            counts["unchanged"] += 1
            continue
        if root is None:
            root = _store(state)
        _write(root, attempt, {
            "schema": SCHEMA, "attempt_id": attempt, "model": model, "route": route,
            "observed_at": stamp, "source": "launcher",
        })
        counts["recorded"] += 1
    return counts


def _fail(code: str, exit_code: int) -> int:
    print(json.dumps({"ok": False, "error": {"code": code}}))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record launcher-reported routes for leased attempts.")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--launcher-url", default=os.environ.get("BUNDLEBEES_LAUNCHER_URL", ""))
    args = parser.parse_args(argv)
    state = Path(args.state_dir)
    token = os.environ.get("BUNDLEBEES_LAUNCHER_TOKEN", "")
    if not token or not args.launcher_url or not state.is_absolute() or not state.is_dir():
        return _fail("not_configured", 2)
    from .client import ClientError, LauncherClient

    try:
        launcher = LauncherClient(args.launcher_url, token=token)
        counts = sync(state, launcher)
    except ClientError:
        return _fail("launcher_unavailable", 3)
    except OSError:
        return _fail("store_unavailable", 3)
    print(json.dumps({"ok": True, **counts}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
