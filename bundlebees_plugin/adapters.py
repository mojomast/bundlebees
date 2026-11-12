"""Pluggable worker backends behind one narrow, auditable interface.

Threat model
------------
A worker runtime is *untrusted execution*. It receives a brief, runs an agent,
and reports back text and a diff. Nothing a worker says is authority: it cannot
grant itself a file lease, widen its scope, choose its own base revision, or
approve its own work. The trusted coordinator keeps the brief validation, the
immutable lease, the revision check, the isolated verification, and wave
integration regardless of which adapter is selected. Adapters only translate
the coordinator's calls into a runtime's transport and translate the runtime's
replies back into the result contract below.

Credentials never travel through an adapter. A launcher token stays with the
coordinator and is read through Hermes's secret scope by ``client.py``; a stub
adapter has no credential parameter at all. Adapter errors are scrubbed before
they can reach a log or a model-visible envelope.

Agent-agnostic contract
-----------------------
The core verifier and contracts never import or assume a specific agent,
model or launcher. Any runtime (DeepSeek bees through the Orbit/Hermes
launcher, OpenCode, Codex, a shell script) plugs in by implementing two calls:

* **Input** - ``create(brief, *, model, kanban_task_id)`` receives the
  coordinator-validated brief (``objective``, full ``source_rev``,
  ``assigned_files``, ``acceptance``, optional ``branch``/``tests``). The same
  fields are frozen in the coordinator's signed lease before verify runs.
* **Output** - ``result(attempt_id)`` returns whatever the runtime produced;
  :func:`normalize_result` reduces it to the result contract below
  (a patch plus worker-claimed evidence).

``WorkerBackend`` is that required minimum. ``WorkerAdapter`` adds optional
capabilities (health, projects, attempts, get, events, cancel, review) that
the Hermes tools use when present; a backend without them gets a clear
``unsupported_operation`` error from those tool ops, never a fake success.

Result contract
---------------
Every adapter result is reduced to exactly::

    {
        "attempt_id": str | None,
        "patch": str | None,       # unified diff against source_rev, or None
        "base_rev": str | None,    # full hex base the worker *declares*, not proof
        "usage": {"tokens": int | None, "cost_usd": float | None},
        "evidence": {"checks": [{"name", "command", "passed"}]},  # worker claims
        "state": str | None,
        "untrusted": True,
    }

Anything else the worker sent (``assigned_files``, ``lease``, ``brief``,
``scope``, ``worker_claims``, tokens, hosts) is dropped. A declared
``base_rev`` is re-checked by the coordinator against the immutable lease,
and worker ``evidence`` is informational only: the coordinator regenerates
trusted evidence by running the tests in its own sandbox. ``patch`` is not
scrubbed because a diff is data to be verified, not prose to display.
Runtime-specific result shapes (for example the Orbit launcher's
``source.repo_rev`` and ``observed.usage``) are mapped here and nowhere else.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .client import client_from_config
from .security import scrub

_ADAPTER_DOC = "docs/ADAPTERS.md"
_FULL_HEX_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_KNOWN_ADAPTERS = ("orbit-loopback", "launcher", "opencode", "codex")
_LAUNCHER_ALIASES = frozenset({"direct", "http", "https"})
_IMPLEMENTED = {
    "orbit-loopback": True,
    "launcher": True,
    "opencode": False,
    "codex": False,
}
_PATCH_KEYS = ("patch", "unified_diff", "diff")
_BASE_KEYS = ("base_rev", "base_sha", "base", "source_rev", "repo_rev")
_BASE_CONTAINERS = ("observed", "source")
_MAX_CHECKS = 50


class UnsupportedAdapter(Exception):
    """A requested adapter or operation has no implementation.

    Carries the stable ``unsupported_adapter`` code plus a scrubbed ``error``
    and ``fix`` so a tool envelope can be built without leaking worker text.
    """

    code = "unsupported_adapter"

    def __init__(self, error: str, fix: str | None = None) -> None:
        self.error = str(scrub(str(error)))
        self.fix = str(
            scrub(
                str(
                    fix
                    or (
                        "Choose the orbit-loopback or launcher adapter, or "
                        f"implement the WorkerAdapter contract in {_ADAPTER_DOC}."
                    )
                )
            )
        )
        super().__init__(self.error)

    def as_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "code": self.code,
            "fix": self.fix,
        }


class WorkerBackend(Protocol):
    """The required minimum any agent runtime implements (input -> output)."""

    name: str

    def create(
        self,
        brief: dict[str, Any],
        *,
        model: str,
        kanban_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Start work on a validated brief; return at least ``attempt_id``."""
        ...

    def result(self, attempt_id: str) -> dict[str, Any]:
        """Return the runtime's raw output; reduced by :func:`normalize_result`."""
        ...


class WorkerAdapter(WorkerBackend, Protocol):
    """A backend plus the optional capabilities the Hermes tools can use."""

    def health(self) -> dict[str, Any]:
        ...

    def projects(self) -> list[dict[str, Any]]:
        ...

    def attempts(self) -> list[dict[str, Any]]:
        ...

    def get(self, attempt_id: str) -> dict[str, Any]:
        ...

    def events(
        self, attempt_id: str, cursor: int | None = None
    ) -> list[dict[str, Any]]:
        ...

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        ...

    def review(
        self,
        attempt_id: str,
        *,
        accepted: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        ...


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flatten(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Use one nested ``result``/``data`` mapping, keeping outer fallbacks."""
    for key in ("result", "data"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            for outer_key, outer_value in raw.items():
                merged.setdefault(outer_key, outer_value)
            return merged
    return raw


def _normalize_usage(raw: Mapping[str, Any]) -> dict[str, int | float | None]:
    usage: Any = raw.get("usage")
    if not isinstance(usage, Mapping):
        observed = raw.get("observed")
        usage = observed.get("usage") if isinstance(observed, Mapping) else None
    if not isinstance(usage, Mapping):
        usage = {}
    tokens = usage.get("tokens", usage.get("total_tokens"))
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        tokens = None
    cost = usage.get("cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None
    else:
        cost = float(cost)
    return {"tokens": tokens, "cost_usd": cost}


def _patch_text(value: Any) -> str | None:
    text = _optional_text(value)
    if text is not None:
        return text
    if isinstance(value, Mapping):  # e.g. {"diff": "..."} or {"patch.diff": "..."}
        for key in ("diff", "patch.diff", "unified_diff", "patch"):
            text = _optional_text(value.get(key))
            if text is not None:
                return text
    return None


def _declared_base(data: Mapping[str, Any]) -> str | None:
    for container in (data, *(data.get(key) for key in _BASE_CONTAINERS)):
        if not isinstance(container, Mapping):
            continue
        for key in _BASE_KEYS:
            value = container.get(key)
            if isinstance(value, str) and _FULL_HEX_RE.match(value.strip()):
                return value.strip().lower()
    return None


def _worker_checks(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = data.get("checks")
    if not isinstance(checks, list):
        return []
    out: list[dict[str, Any]] = []
    for item in checks[:_MAX_CHECKS]:
        if not isinstance(item, Mapping):
            continue
        out.append(
            {
                "name": str(scrub(str(item.get("name") or "")))[:200],
                "command": str(scrub(str(item.get("command") or "")))[:400],
                "passed": item.get("passed") is True,
            }
        )
    return out


def normalize_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce arbitrary worker output to the documented result contract.

    Unknown keys are dropped rather than forwarded: worker metadata never
    carries lease or scope authority. Only the first non-empty known alias for
    each field is used, ``base_rev`` must be a full hex commit id, and
    ``untrusted`` is forced to true.
    """
    data = _flatten(raw if isinstance(raw, Mapping) else {})

    patch: str | None = None
    for key in _PATCH_KEYS:
        patch = _patch_text(data.get(key))
        if patch is not None:
            break

    state = _optional_text(data.get("state")) or _optional_text(data.get("status"))

    return {
        "attempt_id": _optional_id(data.get("attempt_id") or data.get("id")),
        "patch": patch,
        "base_rev": _declared_base(data),
        "usage": _normalize_usage(data),
        "evidence": {"checks": _worker_checks(data)},
        "state": state,
        "untrusted": True,
    }


class StubAdapter:
    """Base for documented, non-functional adapters.

    Every operation fails closed with ``UnsupportedAdapter`` so selecting a
    stub can never silently fall back to a different runtime or pretend work
    succeeded.
    """

    name = "stub"

    def _unsupported(self) -> UnsupportedAdapter:
        return UnsupportedAdapter(
            f"the {self.name} adapter is a documented stub, not an implementation",
            (
                f"Use orbit-loopback or launcher, or implement the WorkerAdapter "
                f"contract for {self.name} as described in {_ADAPTER_DOC}."
            ),
        )

    def health(self) -> dict[str, Any]:
        raise self._unsupported()

    def projects(self) -> list[dict[str, Any]]:
        raise self._unsupported()

    def attempts(self) -> list[dict[str, Any]]:
        raise self._unsupported()

    def create(
        self,
        brief: dict[str, Any],
        *,
        model: str,
        kanban_task_id: str | None = None,
    ) -> dict[str, Any]:
        raise self._unsupported()

    def get(self, attempt_id: str) -> dict[str, Any]:
        raise self._unsupported()

    def events(
        self, attempt_id: str, cursor: int | None = None
    ) -> list[dict[str, Any]]:
        raise self._unsupported()

    def result(self, attempt_id: str) -> dict[str, Any]:
        raise self._unsupported()

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        raise self._unsupported()

    def review(
        self,
        attempt_id: str,
        *,
        accepted: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        raise self._unsupported()


class OpenCodeAdapter(StubAdapter):
    """Documented stub for the OpenCode runtime.

    A real implementation must: create a disposable checkout at the coordinator
    supplied ``source_rev``; create and check out a feature branch there (never
    ``main`` or ``master``); run OpenCode against the brief with the brief's
    ``assigned_files`` as its only writable scope; return the unified diff from
    ``git diff <source_rev>`` plus ``base_rev=<source_rev>`` and the runtime's
    reported token/cost usage; and leave the coordinator's credentials outside
    the runtime. It must never push, merge, tag, force-push, or accept a token,
    host, or lease from worker output. Map runtime replies through
    ``normalize_result`` before returning them.
    """

    name = "opencode"


class CodexAdapter(StubAdapter):
    """Documented stub for the Codex runtime.

    A real implementation must: create a disposable checkout at the coordinator
    supplied ``source_rev``; create and check out a feature branch there (never
    ``main`` or ``master``); run Codex non-interactively against the brief with
    the brief's ``assigned_files`` as its only writable scope; return the
    unified diff from ``git diff <source_rev>`` plus ``base_rev=<source_rev>``
    and the runtime's reported token/cost usage; and keep coordinator
    credentials out of the runtime. It must never push, merge, tag, force-push,
    or treat worker metadata as lease or scope authority. Map runtime replies
    through ``normalize_result`` before returning them.
    """

    name = "codex"


def _client_adapter(name: str) -> Callable[..., WorkerAdapter]:
    """Build a factory that returns the existing client for ``name``."""

    def build(
        config: Mapping[str, Any] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        *,
        state_dir: Path | None = None,
    ) -> WorkerAdapter:
        settings = dict(config or {})
        settings["adapter"] = name
        client = client_from_config(
            settings, token_provider=token_provider, state_dir=state_dir
        )
        client.name = name
        return client

    return build


def _stub_adapter(adapter_cls: type[StubAdapter]) -> Callable[..., WorkerAdapter]:
    def build(
        config: Mapping[str, Any] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        *,
        state_dir: Path | None = None,
    ) -> WorkerAdapter:
        return adapter_cls()

    return build


ADAPTERS: dict[str, Callable[..., WorkerAdapter]] = {
    "orbit-loopback": _client_adapter("orbit-loopback"),
    "launcher": _client_adapter("launcher"),
    "opencode": _stub_adapter(OpenCodeAdapter),
    "codex": _stub_adapter(CodexAdapter),
}


def adapter_from_config(
    config: Mapping[str, Any] | None = None,
    *,
    token_provider: Callable[[], str | None] | None = None,
    state_dir: Path | None = None,
) -> WorkerAdapter:
    """Pick the configured adapter, defaulting to the loopback Orbit client."""
    settings = config or {}
    name = str(settings.get("adapter") or "orbit-loopback").strip().lower()
    if name in _LAUNCHER_ALIASES:
        name = "launcher"
    factory = ADAPTERS.get(name)
    if factory is None:
        known = ", ".join(sorted(ADAPTERS))
        aliases = ", ".join(sorted(_LAUNCHER_ALIASES))
        raise UnsupportedAdapter(
            f"unknown worker adapter: {name or '(empty)'}; known adapters: {known}",
            (
                f"Choose one of: {known}. The aliases {aliases} select launcher. "
                f"See {_ADAPTER_DOC}."
            ),
        )
    return factory(settings, token_provider, state_dir=state_dir)


def available_adapters() -> list[dict[str, Any]]:
    """List adapter names with default/implemented flags, in stable order."""
    return [
        {
            "name": name,
            "default": name == "orbit-loopback",
            "implemented": _IMPLEMENTED[name],
        }
        for name in _KNOWN_ADAPTERS
    ]
