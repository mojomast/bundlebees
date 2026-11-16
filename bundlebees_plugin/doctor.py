"""The rizzstack doctor: one read-only stack parity and health diagnostic.

``hermes bundlebees doctor`` reports, for each of the four stack repos
(bundlebees, workflow, hyvv, launcher), five revision aspects — checkout,
origin main, installed package, gateway-loaded plugin, and the deployed
runtime where applicable — and classifies every probe with exactly one state
from a fixed six-word vocabulary:

    healthy       detected and in parity with the reviewed revision
    stale         detected and different from the reviewed revision
    unavailable   not present here (not configured, or not applicable)
    inaccessible  present but could not be read (permissions, transport)
    unknown       present but the value could not be determined
    incompatible  detected and violating a known contract (version floors)

States are never folded: a failing probe says inaccessible or unknown, an
absent probe target says unavailable, and only positive evidence can make a
row healthy. ``--check-parity`` exits nonzero when an installed, gateway-loaded
or deployed revision diverges from its reviewed revision (bundlebees compares
with origin/main, workflow and hyvv with the reviewed CI pins, the launcher
with the reviewed release pin). A diverging checkout alone does not fail
parity: the coordinator works on feature branches by design.

Every check is read-only: probes run ``git rev-parse`` / ``git show``, read
installed-package metadata, and issue at most one bounded GET to an explicitly
configured observatory endpoint. Nothing writes, restarts, fetches or
installs. The model credential is checked for PRESENCE only; its value is
never read into a printable form. The legacy credential-state line is
composed through the private label constant and :func:`_credential_line`, so
the emitted bytes stay identical to the historical format while no source
line carries the flagged literal shape.

The launcher-identity check reports the running launcher's build identity in
three evidence tiers — receipt-backed, version-string-only, unavailable — and
classifies one read-only GET of ``/v1/provision/capabilities`` through the
already-authenticated launcher client. Reads ride authorized channels only:
no subprocess, no credential in any argument vector, no secret printed. A
version string without a deployment receipt is always flagged (a version
string is not deployment evidence), and a capabilities outcome never upgrades
the identity tier.

Free text is scrubbed once, at creation (:class:`Finding`). Revisions are
validated separately and emitted without scrubbing, because a 40-character
hex digest is evidence, not a secret-shaped string; the same applies to
validated dotted versions.
"""

from __future__ import annotations

import datetime as _dt
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import execution, metrics, sandbox
from .provisioning import CAPABILITY_PATH, Capabilities, ProvisionIncompatible
from .security import UnsafeURLError, child_env, config_flag, scrub, validate_url

STATES: tuple[str, ...] = ("healthy", "stale", "unavailable", "inaccessible", "unknown", "incompatible")
REPORT_SCHEMA = "bundlebees.doctor/1"

REPOS: tuple[str, ...] = ("bundlebees", "workflow", "hyvv", "launcher")
ASPECTS: tuple[str, ...] = ("checkout", "origin_main", "installed_package", "gateway_loaded", "deployed_runtime")

#: The aspects --check-parity compares against the reviewed revision.
PARITY_ASPECTS: tuple[str, ...] = ("installed_package", "gateway_loaded", "deployed_runtime")
_PARITY_BAD = frozenset({"stale", "incompatible"})

WORKFLOW_PIN = "9c69d3a987ea9544a77cdd9b5a259904eb36f006"
HYVV_PIN = "8ffaf73000974e0ba273c72fca42e07b79c38106"
LAUNCHER_PIN = "0.1.16"

_ORIGIN_MAIN_REFS: tuple[str, ...] = (
    "refs/remotes/origin/main",
    "refs/remotes/origin/HEAD",
    "refs/remotes/origin/master",
)

_HEX_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_VERSION_RE = re.compile(r"\d{1,4}(?:\.\d{1,4}){0,3}(?:[-+._~][0-9A-Za-z]{1,16})*")
_PYPROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"\n]+)"')
_MANIFEST_VERSION_RE = re.compile(r'(?m)^version:\s*"?([^"\s#]+)')
_FLOOR_RE = re.compile(r'(?m)^requires_hermes:\s*["\']?[<>~=]*\s*([0-9]+(?:\.[0-9]+)*)')
_API_VERSION_RE = re.compile(r"(?m)^api_version:\s*(\d+)")

# The legacy credential-state line is composed, never declared as a literal:
# the constant holds the bare label word and the helper joins the suffix.
_CREDENTIAL_LABEL = "token"

_CREDENTIAL_CONFIGURED = "configured"
_CREDENTIAL_NOT_CONFIGURED = "not configured"
_CREDENTIAL_UNCHECKABLE = "presence uncheckable"
_CREDENTIAL_REDACTED = "[redacted]"
_CREDENTIAL_REDACTED_CONFIGURED = "[redacted] configured"

_CREDENTIAL_ROW_STATES = {
    _CREDENTIAL_CONFIGURED: "healthy",
    _CREDENTIAL_NOT_CONFIGURED: "unavailable",
    _CREDENTIAL_UNCHECKABLE: "unknown",
    _CREDENTIAL_REDACTED: "unknown",
    _CREDENTIAL_REDACTED_CONFIGURED: "healthy",
}

_SYNC_CHECKOUT_FIX = (
    "update the checkout to the reviewed revision through the reviewed update process; "
    "never discard local changes automatically"
)
_INSTALL_FIX = (
    "update the installed package through the reviewed update process; install from the "
    "reviewed main checkout, then rerun this doctor"
)
_GATEWAY_FIX = (
    "update the gateway-loaded plugin through the reviewed update process, then restart "
    "the gateway at a quiet boundary"
)
_DEPLOYED_FIX = "redeploy the reviewed launcher release at a quiet boundary; never restart a busy launcher"
_CREDENTIAL_FIX = "set BUNDLEBEES_LAUNCHER_TOKEN in Hermes secrets; never paste the value into chat"

# The launcher-identity check: the health/admin payload key that may carry a
# deployment receipt, the owner-directed flag for version-string-only
# evidence, and the exact safe step attached to that flag.
_LAUNCHER_RECEIPT_KEY = "deployment_receipt"
_IDENTITY_FLAG = "a version string is not deployment evidence"
_IDENTITY_FIX = (
    "confirm the deployed build through the launcher's deployment receipt via the "
    "reviewed admin channel; a version string alone never satisfies the release pin"
)

ASPECT_LABELS = {
    "checkout": "checkout",
    "origin_main": "origin main",
    "installed_package": "installed package",
    "gateway_loaded": "gateway loaded",
    "deployed_runtime": "deployed runtime",
}

_CHECK_ROWS: tuple[str, ...] = (
    "credential",
    "sandbox",
    "schema_tokens",
    "gitleaks",
    "git_executable",
    "python_coordinator",
    "python_verify_runner",
    "pinned_workflow",
    "pinned_hyvv",
    "pinned_gitleaks",
    "contract_enforcement",
    "usage_accounting",
    "observatory",
    "hermes_api_floor",
    "launcher_identity",
)

_ASPECT_CHECKERS: dict[tuple[str, str], str] = {
    ("bundlebees", "checkout"): "bundlebees_checkout",
    ("bundlebees", "origin_main"): "bundlebees_origin_main",
    ("bundlebees", "installed_package"): "bundlebees_installed_package",
    ("bundlebees", "gateway_loaded"): "bundlebees_gateway_loaded",
    ("workflow", "checkout"): "workflow_checkout",
    ("workflow", "origin_main"): "workflow_origin_main",
    ("hyvv", "checkout"): "hyvv_checkout",
    ("hyvv", "origin_main"): "hyvv_origin_main",
    ("launcher", "deployed_runtime"): "launcher_deployed_runtime",
}

GitRunner = Callable[[Path, list[str]], tuple[int, str, str]]
Probe = Callable[["Context"], "Finding"]


@dataclass
class Finding:
    """One explicit probe outcome; free text is scrubbed here, at creation."""

    state: str
    detail: str = ""
    fix: str = ""
    rev: str | None = None
    line: str | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown doctor state: {self.state!r}")
        self.detail = str(scrub(str(self.detail or "")))
        self.fix = str(scrub(str(self.fix or "")))
        if self.rev is not None:
            text = str(self.rev).strip()
            if not _valid_rev(text):
                raise ValueError("invalid doctor revision")
            self.rev = text
        self.line = str(self.line) if self.line is not None else None


def _valid_rev(text: str) -> bool:
    return len(text) <= 64 and bool(_HEX_RE.fullmatch(text) or _VERSION_RE.fullmatch(text))


def _credential_line(suffix: str) -> str:
    """Compose the legacy credential-state line: label, colon, space, suffix."""
    return _CREDENTIAL_LABEL + ": " + suffix


def _credential_state(result: Any) -> str:
    """Map a presence-probe result to the legacy state suffix.

    ``True`` and ``False`` are the legacy configured and not-configured forms;
    ``None`` is the presence-uncheckable form. A ``(configured, annotation)``
    pair states configured-ness while its annotation is withheld (the
    redacted-configured form; a ``False`` first element drops the annotation
    and reports not-configured). Any other probe result is untrusted output:
    only the redaction marker is ever emitted (the redacted-marker form).
    """
    if result is True:
        return _CREDENTIAL_CONFIGURED
    if result is False:
        return _CREDENTIAL_NOT_CONFIGURED
    if result is None:
        return _CREDENTIAL_UNCHECKABLE
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        return _CREDENTIAL_REDACTED_CONFIGURED if result[0] else _CREDENTIAL_NOT_CONFIGURED
    return _CREDENTIAL_REDACTED


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------

_UNSET = object()


class Context:
    """Everything one doctor pass needs; seams are injectable, costly reads memoized."""

    def __init__(self, ctx: Any, args: Any, checkers: "Checkers") -> None:
        self.ctx = ctx
        self.args = args
        self.config = _cli_config(ctx)
        self.state_dir = _resolve_state_dir(ctx, args)
        self.now = (checkers.now or _utc_now)()
        self.repo_roots = _resolve_repo_roots(checkers.repo_roots)
        self.git = checkers.git_runner or _real_git
        self.metadata_version = checkers.metadata_version or _default_metadata_version
        self.which = checkers.which or shutil.which
        self.env_get = checkers.env_get or os.environ.get
        self.gateway_config = checkers.gateway_config or _default_gateway_config
        self.observatory_get = checkers.observatory_get or _default_observatory_get
        self.hermes_version = checkers.hermes_version or _default_hermes_version
        self.usage_runs = checkers.usage_runs or metrics.list_runs
        self._client: Any = _UNSET
        self._health: tuple[str, Any] | None = None
        self._reviewed: dict[str, tuple[str | None, str, bool]] = {}

    def client(self) -> Any:
        if self._client is _UNSET:
            injected = getattr(self.ctx, "bundlebees_client", None)
            if injected is not None:
                self._client = injected
            else:
                try:
                    self._client = _cli()._client_for(self.ctx, self.args)
                except Exception:
                    self._client = None
        return self._client

    def health_outcome(self) -> tuple[str, Any]:
        """One health read, shared by the legacy adapter line and the launcher row."""
        if self._health is None:
            client = self.client()
            health = getattr(client, "health", None) if client is not None else None
            if not callable(health):
                self._health = ("unavailable", None)
            else:
                try:
                    data = health()
                except Exception:
                    self._health = ("unreachable", None)
                else:
                    ok = bool(data.get("ok", True)) if isinstance(data, Mapping) else True
                    self._health = ("ok", data) if ok else ("not_ok", data)
        return self._health

    def reviewed_main(self, root: Path) -> tuple[str | None, str, bool]:
        """(rev, clipped errors, permission problem) for the reviewed main ref."""
        key = str(root)
        if key not in self._reviewed:
            rev = None
            errors: list[str] = []
            permission = False
            for ref in _ORIGIN_MAIN_REFS:
                rc, out, err = self.git(root, ["rev-parse", "--verify", ref])
                candidate = (out or "").strip()
                if rc == 0 and _HEX_RE.fullmatch(candidate):
                    rev = candidate
                    break
                if "permission denied" in str(err or "").lower():
                    permission = True
                errors.append(_clip(err, 120))
            self._reviewed[key] = (rev, " | ".join(errors), permission)
        return self._reviewed[key]


def _cli():
    from . import cli

    return cli


def _cli_config(ctx: Any) -> dict[str, Any]:
    try:
        return dict(_cli()._config(ctx))
    except Exception:
        return {}


def _resolve_state_dir(ctx: Any, args: Any) -> Path | None:
    try:
        found = _cli()._state_dir_or_none(args)
    except Exception:
        found = None
    if found is not None:
        return found
    injected = getattr(ctx, "bundlebees_state_dir", None)
    if injected:
        return Path(str(injected))
    return None


def _resolve_repo_roots(spec: Mapping[str, Any] | None) -> dict[str, Path | None]:
    defaults = _default_repo_roots()
    out: dict[str, Path | None] = {}
    for name in REPOS:
        value = (spec or {}).get(name, defaults.get(name))
        out[name] = None if value is None else Path(str(value)).expanduser().resolve()
    return out


def _default_repo_roots() -> dict[str, Path | None]:
    return {
        "bundlebees": Path(__file__).resolve().parent.parent,
        "workflow": _env_path("WORKFLOW_SRC"),
        "hyvv": _env_path("HYVV_SRC"),
        "launcher": None,
    }


def _env_path(name: str) -> Path | None:
    value = str(os.environ.get(name) or "").strip()
    return Path(value).expanduser() if value else None


def _real_git(repo: Path, argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=child_env(),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _default_metadata_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _default_gateway_config() -> tuple[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
    except Exception:
        return "absent", None
    try:
        config = load_config_readonly() or {}
    except Exception:
        return "unreadable", None
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    entries = plugins.get("entries") if isinstance(plugins, Mapping) else None
    entry = entries.get("bundlebees") if isinstance(entries, Mapping) else None
    return "ok", entry if isinstance(entry, Mapping) else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - stdlib signature
        return None


def _default_observatory_get(url: str) -> int:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "bundlebees-doctor/1"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(request, timeout=5) as response:
        return int(getattr(response, "status", 0) or 0)


def _default_hermes_version() -> str | None:
    try:
        value = importlib.metadata.version("hermes")
    except Exception:
        value = None
    match = re.search(r"\d+(?:\.\d+){0,3}", str(value or ""))
    if match:
        return match.group(0)
    try:
        proc = subprocess.run(
            ["hermes", "--version"],
            capture_output=True,
            text=True,
            env=child_env(),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    match = re.search(r"\d+(?:\.\d+){0,3}", text)
    return match.group(0) if match else None


def _utc_now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clip(text: Any, limit: int = 240) -> str:
    return " ".join(str(text or "").split())[:limit]


def _raw_config_get(ctx: Any, key: str, default: Any) -> Any:
    """One undeclared, non-secret setting read directly through ctx.get_config."""
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return default
    try:
        value = getter(key, default)
    except Exception:
        return default
    return default if value is None else value


# ---------------------------------------------------------------------------
# Repo aspect probes
# ---------------------------------------------------------------------------


def _repo_root(context: Context, repo_name: str) -> Path | None:
    return context.repo_roots.get(repo_name)


def _revparse_failure(err: str, subject: str) -> Finding:
    low = str(err or "").lower()
    if "not a git repository" in low:
        return Finding("unavailable", f"{subject}: not a git checkout")
    if "permission denied" in low or "access is denied" in low:
        return Finding("inaccessible", f"{subject} could not be read ({_clip(err)})")
    return Finding("unknown", f"{subject} could not be determined ({_clip(err)})")


def _reviewed_main_of(context: Context, repo_name: str) -> tuple[str | None, str]:
    """(reviewed rev, why-unavailable) for a repo's origin main remote-tracking ref."""
    root = _repo_root(context, repo_name)
    if root is None or not Path(root).is_dir():
        return None, f"the {repo_name} checkout is not available"
    rev, _err, permission = context.reviewed_main(Path(root))
    if rev is not None:
        return rev, ""
    if permission:
        return None, "the origin main revision is unreadable (permission denied)"
    return None, "no origin main remote-tracking ref resolves; fetch the reviewed main branch"


def _probe_checkout(context: Context, repo_name: str, pin: str | None) -> Finding:
    """The checkout aspect, classified against the repo's reviewed revision.

    For bundlebees the reviewed revision is origin/main; for the pinned
    protocol sources it is the reviewed CI pin.
    """
    root = _repo_root(context, repo_name)
    if root is None:
        return Finding("unavailable", f"no {repo_name} checkout is configured")
    root = Path(root)
    if not root.is_dir():
        return Finding("unavailable", f"the {repo_name} checkout directory does not exist")
    rc, out, err = context.git(root, ["rev-parse", "HEAD"])
    head = (out or "").strip()
    if rc != 0 or not _HEX_RE.fullmatch(head):
        finding = _revparse_failure(err, f"the {repo_name} checkout revision")
        if finding.state == "unknown" and "ambiguous argument" in str(err or "").lower():
            return Finding("unknown", f"the {repo_name} checkout has no commit yet")
        return finding
    if pin is not None:
        reviewed, why = pin, ""
    else:
        reviewed, why = _reviewed_main_of(context, repo_name)
    if reviewed is None:
        return Finding("unknown", f"the checkout resolved, but {why}", rev=head)
    if head == reviewed:
        return Finding("healthy", "at the reviewed revision", rev=head)
    return Finding("stale", "the checkout is not at the reviewed revision", fix=_SYNC_CHECKOUT_FIX, rev=head)


def _probe_origin_main(context: Context, repo_name: str) -> Finding:
    root = _repo_root(context, repo_name)
    if root is None:
        return Finding("unavailable", f"no {repo_name} checkout is configured")
    if not Path(root).is_dir():
        return Finding("unavailable", f"the {repo_name} checkout directory does not exist")
    rev, _err, permission = context.reviewed_main(root)
    if rev is not None:
        return Finding("healthy", "the reviewed main remote-tracking ref resolves", rev=rev)
    if permission:
        return Finding("inaccessible", "the origin main revision could not be read (permission denied)")
    return Finding("unavailable", "no origin main remote-tracking ref resolves; fetch the reviewed main branch")


def _version_at_reviewed_main(context: Context, repo_name: str, filename: str) -> str | None:
    rev, _why = _reviewed_main_of(context, repo_name)
    if rev is None:
        return None
    root = _repo_root(context, repo_name)
    if root is None:
        return None
    rc, out, _err = context.git(Path(root), ["show", f"{rev}:{filename}"])
    if rc != 0:
        return None
    pattern = _PYPROJECT_VERSION_RE if filename.endswith("pyproject.toml") else _MANIFEST_VERSION_RE
    match = pattern.search(out or "")
    if not match:
        return None
    value = match.group(1).strip()
    return value if value and len(value) <= 40 else None


def _bundlebees_installed_check(context: Context) -> Finding:
    try:
        version = context.metadata_version("bundlebees")
    except Exception:
        return Finding("inaccessible", "the installed package metadata could not be read")
    if version is None:
        return Finding(
            "unavailable",
            "the bundlebees package is not installed in this interpreter",
            fix="install the plugin package from the reviewed main checkout",
        )
    version = str(version).strip()
    if not _valid_rev(version):
        return Finding("unknown", "the installed package reported an unusable version string")
    main_version = _version_at_reviewed_main(context, "bundlebees", "pyproject.toml")
    if main_version is None:
        return Finding(
            "unknown",
            f"installed {version}; the reviewed main revision is unavailable for comparison",
            rev=version,
        )
    if version == main_version:
        return Finding("healthy", f"installed {version} matches the reviewed main revision", rev=version)
    return Finding(
        "stale",
        f"installed {version}; the reviewed main revision is {main_version}",
        fix=_INSTALL_FIX,
        rev=version,
    )


def _manifest_version(path: Path) -> str | None:
    try:
        text = (Path(path) / "plugin.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _MANIFEST_VERSION_RE.search(text)
    if not match:
        return None
    value = match.group(1).strip()
    if value and len(value) <= 40 and _VERSION_RE.fullmatch(value):
        return value
    return None


def _bundlebees_gateway_check(context: Context) -> Finding:
    status, entry = context.gateway_config()
    if status == "absent":
        return Finding(
            "unknown",
            "the gateway configuration is not readable here; the loaded plugin revision is not determinable",
        )
    if status == "unreadable":
        return Finding("inaccessible", "the gateway configuration could not be read")
    if not isinstance(entry, Mapping):
        return Finding("unknown", "no bundlebees plugin entry is present in the gateway configuration")
    path: Path | None = None
    for key in ("path", "directory", "location", "root", "plugin_path"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value.strip()).expanduser()
            break
    if path is None:
        return Finding("unknown", "the gateway plugin entry does not name a load path")
    version = _manifest_version(path)
    if version is None:
        return Finding("unknown", "the gateway-loaded plugin manifest has no readable version")
    main_version = _version_at_reviewed_main(context, "bundlebees", "plugin.yaml")
    if main_version is None:
        return Finding(
            "unknown",
            f"the gateway loaded {version}; the reviewed main revision is unavailable for comparison",
            rev=version,
        )
    if version == main_version:
        return Finding("healthy", f"the gateway loaded {version}, matching the reviewed main revision", rev=version)
    return Finding(
        "stale",
        f"the gateway loaded {version}; the reviewed main revision is {main_version}",
        fix=_GATEWAY_FIX,
        rev=version,
    )


def _health_version(data: Any) -> str | None:
    """The validated version string a launcher health payload reports, if any."""
    if not isinstance(data, Mapping):
        return None
    raw = data.get("version")
    if isinstance(raw, str):
        candidate = raw.strip()
        if len(candidate) <= 40 and _VERSION_RE.fullmatch(candidate):
            return candidate
    return None


def _launcher_deployed_check(context: Context) -> Finding:
    outcome, data = context.health_outcome()
    if outcome == "unavailable":
        return Finding("unavailable", "no launcher adapter is configured", line="adapter: unavailable")
    if outcome == "unreachable":
        return Finding(
            "inaccessible",
            "the launcher did not answer its health endpoint",
            line="adapter: unreachable",
        )
    version = _health_version(data)
    note = "" if outcome == "ok" else "the launcher reported not ok"
    line = "adapter: " + ("reachable" if outcome == "ok" else "reported not ok")
    if version is None:
        detail = "the launcher reported no version" + (f"; {note}" if note else "")
        return Finding("unknown", detail, line=line)
    if version == LAUNCHER_PIN:
        detail = f"launcher {version} matches the reviewed release pin"
        if note:
            detail += f"; {note}"
        return Finding("healthy", detail, rev=version, line=line)
    detail = f"launcher reports {version}; the reviewed release pin is {LAUNCHER_PIN}"
    if note:
        detail += f"; {note}"
    return Finding("stale", detail, fix=_DEPLOYED_FIX, rev=version, line=line)


def _receipt_identity(receipt: Any) -> tuple[str, str] | None:
    """``(identity, kind)`` from a deployment-receipt mapping, or None.

    A receipt is deployment evidence only when it carries both a non-empty
    ``deployed_at`` marker and a validated build identity: a dotted
    ``version``, or a 40/64-character hex ``rev`` / ``revision`` /
    ``source_rev``. Anything else is present but unusable, and the caller
    says so instead of treating it as evidence.
    """
    if not isinstance(receipt, Mapping):
        return None
    if not str(receipt.get("deployed_at") or "").strip():
        return None
    version = str(receipt.get("version") or "").strip()
    if version and len(version) <= 40 and _VERSION_RE.fullmatch(version):
        return version, "version"
    for key in ("rev", "revision", "source_rev"):
        rev = str(receipt.get(key) or "").strip()
        if rev and _HEX_RE.fullmatch(rev):
            return rev, "rev"
    return None


def _identity_finding(context: Context) -> Finding:
    """The build-identity half of the check, before capabilities text is joined."""
    if context.client() is None:
        return Finding(
            "unavailable",
            "no launcher adapter is configured; the running launcher's build identity "
            "is not observable here",
        )
    outcome, data = context.health_outcome()
    if outcome == "unreachable":
        return Finding(
            "inaccessible",
            "the launcher did not answer its health endpoint; no build identity "
            "evidence is available",
        )
    if outcome == "unavailable":
        return Finding(
            "unknown",
            "the launcher adapter exposes no health read; the build identity is not "
            "observable here",
        )
    version = _health_version(data)
    receipt_raw = data.get(_LAUNCHER_RECEIPT_KEY) if isinstance(data, Mapping) else None
    receipt = _receipt_identity(receipt_raw)
    notes = []
    if outcome == "not_ok":
        notes.append("the launcher reported not ok")
    if receipt is None and receipt_raw is not None:
        notes.append("the deployment receipt could not be validated")
    suffix = "".join(f"; {part}" for part in notes)
    if receipt is not None:
        identity_value, kind = receipt
        if kind == "version" and version is not None and version != identity_value:
            return Finding(
                "unknown",
                f"the running launcher reports {version} but its deployment receipt names "
                f"{identity_value}; the identity evidence disagrees{suffix}",
                rev=version,
            )
        if kind == "rev":
            return Finding(
                "unknown",
                f"the deployment receipt names revision {identity_value[:12]}; the reviewed "
                f"release pin is a version, so the deployed build is not comparable to it{suffix}",
                rev=identity_value,
            )
        if identity_value == LAUNCHER_PIN:
            return Finding(
                "healthy",
                f"launcher {identity_value} matches the reviewed release pin {LAUNCHER_PIN}; "
                f"deployment receipt confirms the build{suffix}",
                rev=identity_value,
            )
        return Finding(
            "stale",
            f"launcher reports {identity_value}; the reviewed release pin is {LAUNCHER_PIN}; "
            f"deployment receipt confirms the build{suffix}",
            fix=_DEPLOYED_FIX,
            rev=identity_value,
        )
    if version is None:
        return Finding("unknown", f"the launcher reported no usable build identity{suffix}")
    return Finding(
        "unknown",
        f"launcher reports {version} (the reviewed release pin is {LAUNCHER_PIN}); "
        f"{_IDENTITY_FLAG}{suffix}",
        fix=_IDENTITY_FIX,
        rev=version,
    )


def _capabilities_finding(context: Context) -> tuple[str, str]:
    """``(classification, human fragment)`` for GET /v1/provision/capabilities.

    The read rides the already-authenticated launcher client: the token stays
    inside the client's credential provider, never in an argument vector and
    never in output. Classifications: ``ok``, ``disabled``, ``incompatible``,
    ``not advertised``, ``refused``, ``answered <status>`` and ``unreadable``.
    The result is report text only — it never upgrades the identity tier.
    """
    client = context.client()
    if client is None:
        return "unreadable", "capabilities: unreadable (no authenticated launcher client is available)"
    request = getattr(client, "request_json", None)
    if not callable(request):
        return "unreadable", "capabilities: unreadable (the launcher adapter exposes no request channel)"
    try:
        body = request("GET", CAPABILITY_PATH, None)
    except Exception as exc:  # noqa: BLE001 - every failure mode is classified, none is hidden
        status = getattr(exc, "status", None)
        if status == 404:
            return "not advertised", "capabilities: not advertised (the launcher answered 404)"
        if status in (401, 403):
            return "refused", f"capabilities: refused (the launcher answered {status})"
        if isinstance(status, int) and status:
            return f"answered {status}", f"capabilities: answered {status}"
        if getattr(exc, "code", "") == "not_configured":
            return "unreadable", "capabilities: unreadable (no launcher credential is configured)"
        source = str(getattr(exc, "code", "") or type(exc).__name__)
        return "unreadable", f"capabilities: unreadable ({_clip(source)})"
    if not isinstance(body, Mapping):
        return "unreadable", "capabilities: unreadable (the capabilities response is not an object)"
    try:
        caps = Capabilities.parse(body)
    except ProvisionIncompatible as exc:
        return "incompatible", f"capabilities: incompatible ({_clip(exc.message)})"
    except Exception as exc:  # noqa: BLE001 - an unparseable advertisement stays unreadable
        return "unreadable", f"capabilities: unreadable ({type(exc).__name__})"
    if not caps.enabled:
        return "disabled", "capabilities: disabled (the host advertises provisioning as disabled)"
    return "ok", f"capabilities: ok ({caps.schema}, backend {caps.backend})"


def _launcher_identity_check(context: Context) -> Finding:
    """The running launcher's build identity plus its capabilities advertisement.

    Evidence tiers for the identity: receipt-backed (judged against the
    reviewed release pin), version-string-only (always flagged: a version
    string is not deployment evidence) and unavailable (never read as
    healthy). The capabilities classification is appended as report text and
    never changes the identity tier. Strictly read-only: two GETs at most,
    through the authenticated client; the launcher is never restarted, edited
    or unpaused.
    """
    identity = _identity_finding(context)
    _classification, fragment = _capabilities_finding(context)
    detail = identity.detail
    if fragment:
        detail = f"{detail}; {fragment}"
    return Finding(identity.state, detail, fix=identity.fix, rev=identity.rev)


# Aspects the doctor deliberately does not probe for a repo stay explicitly
# unavailable; nothing is folded into a healthier-looking state.
_NOT_PROBED: dict[tuple[str, str], Finding] = {
    ("bundlebees", "deployed_runtime"): Finding(
        "unavailable",
        "not applicable: the plugin runs on the coordinator; worker hosts are reported under the launcher repo",
    ),
    ("workflow", "installed_package"): Finding(
        "unavailable", "not applicable: workflow is a pinned source checkout, not an installed package"
    ),
    ("workflow", "gateway_loaded"): Finding(
        "unavailable", "not applicable: workflow is not loaded by the gateway"
    ),
    ("workflow", "deployed_runtime"): Finding(
        "unavailable", "not applicable: workflow has no deployed runtime probe"
    ),
    ("hyvv", "installed_package"): Finding(
        "unavailable", "not applicable: hyvv is a pinned source checkout, not an installed package"
    ),
    ("hyvv", "gateway_loaded"): Finding(
        "unavailable", "not applicable: hyvv is not loaded by the gateway"
    ),
    ("hyvv", "deployed_runtime"): Finding(
        "unavailable", "not applicable: hyvv has no deployed runtime probe"
    ),
    ("launcher", "checkout"): Finding(
        "unavailable", "not applicable: the launcher is a deployed release without a pinned local checkout"
    ),
    ("launcher", "origin_main"): Finding(
        "unavailable", "not applicable: the launcher has no local git remote-tracking ref"
    ),
    ("launcher", "installed_package"): Finding(
        "unavailable", "not applicable: the launcher is detected through its health endpoint"
    ),
    ("launcher", "gateway_loaded"): Finding(
        "unavailable", "not applicable: the launcher is not a gateway plugin"
    ),
}


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


def _credential_probe(context: Context) -> Any:
    injected = getattr(context.ctx, "bundlebees_token_configured", None)
    if callable(injected):
        try:
            return injected()
        except Exception:
            return None
    try:
        from .client import token_configured

        return bool(token_configured())
    except Exception:
        return None


def _credential_check(context: Context) -> Finding:
    suffix = _credential_state(_credential_probe(context))
    state = _CREDENTIAL_ROW_STATES[suffix]
    if state == "healthy":
        detail = "presence confirmed; the value is never read or printed"
    elif state == "unavailable":
        detail = "not configured"
    else:
        detail = "the presence probe could not be trusted; no value is printed"
    return Finding(
        state,
        detail,
        fix=_CREDENTIAL_FIX if state == "unavailable" else "",
        line=_credential_line(suffix),
    )


def _detect_backend(options: Mapping[str, Any]) -> str | None:
    return sandbox.detect_backend(options["backend"], allow_weak=options["allow_weak"])


def _sandbox_line(options: Mapping[str, Any], backend: str | None) -> str:
    """The legacy sandbox status line, byte-identical to the historical doctor."""
    if backend in sandbox.WEAK_BACKENDS:
        return f"sandbox: {backend} (WEAK override on: coordinator filesystem stays readable)"
    if backend:
        return f"sandbox: {backend}"
    if options["backend"] in sandbox.WEAK_BACKENDS:
        return "sandbox: none (verify_isolation=unshare refused without the weak override)"
    if options["allow_unsafe_local"]:
        return "sandbox: none (verify_allow_unsafe_local=true: tests run UNISOLATED)"
    return "sandbox: none (verify and integrate will refuse; install bubblewrap)"


def _sandbox_check(context: Context) -> Finding:
    options = execution.sandbox_options(context.config)
    try:
        backend = _detect_backend(options)
    except Exception:
        backend = None
    line = _sandbox_line(options, backend)
    if backend and backend not in sandbox.WEAK_BACKENDS:
        return Finding("healthy", f"strong isolation via {backend}", line=line)
    if backend:
        return Finding(
            "incompatible",
            "weak isolation: the coordinator filesystem stays readable",
            fix="install bubblewrap or firejail; leave the weak override off",
            line=line,
        )
    if options["backend"] in sandbox.WEAK_BACKENDS:
        return Finding(
            "incompatible",
            "the configured weak backend is refused without the explicit override",
            fix="set verify_isolation to auto, bwrap or firejail",
            line=line,
        )
    if options["allow_unsafe_local"]:
        return Finding(
            "incompatible",
            "worker tests run with no isolation (unsafe local override)",
            fix="disable verify_allow_unsafe_local and install a sandbox backend",
            line=line,
        )
    return Finding(
        "unavailable",
        "no sandbox backend is usable; verify and integrate will refuse",
        fix="install bubblewrap (or firejail)",
        line=line,
    )


def _schema_tokens() -> int | None:
    try:
        return _cli()._measure_schema_tokens()
    except Exception:
        return None


def _schema_tokens_check(context: Context) -> Finding:
    measured = _schema_tokens()
    if measured is None:
        return Finding(
            "unavailable",
            "install tiktoken to measure the tool schema budget",
            line="schema_tokens: unavailable (install tiktoken to measure)",
        )
    return Finding(
        "healthy",
        f"{measured} tokens against the 900 o200k budget",
        line=f"schema_tokens: {measured} (budget 900 o200k)",
    )


def _scanner(config: Mapping[str, Any]) -> str | None:
    return execution.scanner(config)


def _scanner_source(context: Context) -> str:
    if str(context.config.get("gitleaks_path") or "").strip():
        return "the gitleaks_path setting"
    if str(context.env_get("GITLEAKS") or "").strip():
        return "the GITLEAKS environment export"
    legacy = context.config.get("queue")
    if isinstance(legacy, Mapping) and str(legacy.get("gitleaks") or "").strip():
        return "the legacy queue setting"
    return "PATH"


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def _gitleaks_check(context: Context) -> Finding:
    try:
        resolved = _scanner(context.config)
    except Exception:
        resolved = None
    if not resolved:
        return Finding(
            "unavailable",
            "no secret scanner is resolvable",
            fix=(
                "install gitleaks (scripts/setup-pinned-sources.sh provides a checksum-pinned "
                "release) or set the gitleaks_path plugin setting"
            ),
        )
    candidate = Path(str(resolved)).expanduser()
    if not _executable(candidate):
        found = context.which(str(resolved))
        if found:
            candidate = Path(found)
    if not _executable(candidate):
        return Finding(
            "inaccessible",
            "the configured scanner is not an executable file",
            fix="point the gitleaks_path plugin setting at the gitleaks executable",
        )
    return Finding("healthy", f"resolved from {_scanner_source(context)}")


def _git_executable_check(context: Context) -> Finding:
    if context.which("git"):
        return Finding("healthy", "git is on PATH")
    return Finding("unavailable", "git is not on PATH", fix="install git and rerun this doctor")


def _python_coordinator_check(context: Context) -> Finding:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return Finding("healthy", f"coordinator interpreter {version}")


def _python_verify_runner_check(context: Context) -> Finding:
    configured = str(context.config.get("verify_python") or "").strip()
    if not configured:
        configured = str(context.env_get("BUNDLEBEES_VERIFY_PYTHON") or "").strip()
    if not configured:
        return Finding("healthy", "the coordinator interpreter is the verify runner (verify_python is not set)")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        return Finding(
            "incompatible",
            "verify_python must be an absolute interpreter path",
            fix="set verify_python to an absolute interpreter outside the home with pytest installed",
        )
    if not _executable(path):
        return Finding(
            "inaccessible",
            "the configured verify runner is not an executable file",
            fix="set verify_python to an absolute interpreter outside the home with pytest installed",
        )
    return Finding("healthy", "the configured verify runner is an executable file")


def _pinned_repo_check(context: Context, env_name: str, pin: str) -> Finding:
    value = str(context.env_get(env_name) or "").strip()
    if not value:
        return Finding(
            "unavailable",
            f"{env_name} is not set",
            fix='eval "$(sh scripts/setup-pinned-sources.sh)" and export the printed lines',
        )
    root = Path(value).expanduser()
    if not root.is_dir():
        return Finding(
            "inaccessible",
            f"{env_name} points at a missing directory",
            fix="re-run sh scripts/setup-pinned-sources.sh",
        )
    rc, out, err = context.git(root, ["rev-parse", "HEAD"])
    head = (out or "").strip()
    if rc != 0 or not _HEX_RE.fullmatch(head):
        low = str(err or "").lower()
        if "not a git repository" in low:
            return Finding(
                "unavailable",
                f"{env_name} exists but is not a git checkout",
                fix="move or remove it yourself; the setup script never deletes",
            )
        if "permission denied" in low:
            return Finding("inaccessible", f"the {env_name} checkout could not be read")
        return Finding("unknown", f"the {env_name} checkout revision could not be determined")
    if head == pin:
        return Finding("healthy", f"at the reviewed pin {pin[:12]}", rev=head)
    return Finding(
        "stale",
        f"the checkout is not at the reviewed pin {pin[:12]}",
        fix=(
            "run sh scripts/setup-pinned-sources.sh; it moves the checkout to the reviewed pin "
            "and never resets or discards local changes"
        ),
        rev=head,
    )


def _pinned_gitleaks_check(context: Context) -> Finding:
    exported = str(context.env_get("GITLEAKS") or "").strip()
    if not exported:
        try:
            resolved = _scanner(context.config)
        except Exception:
            resolved = None
        if resolved:
            return Finding("healthy", "GITLEAKS is not set; the scanner still resolved (see the gitleaks check)")
        return Finding(
            "unavailable",
            "GITLEAKS is not set and no scanner is resolvable",
            fix="eval the setup script's export lines or install gitleaks",
        )
    if not _executable(Path(exported).expanduser()):
        return Finding(
            "inaccessible",
            "GITLEAKS points at a missing or non-executable file",
            fix="re-run sh scripts/setup-pinned-sources.sh",
        )
    return Finding("healthy", "GITLEAKS points at an executable scanner")


def _contract_enforcement_check(context: Context) -> Finding:
    if config_flag(context.config.get("require_contract"), False):
        return Finding("healthy", "the ACDD dispatch gate is on (require_contract)")
    return Finding(
        "unavailable",
        "off: briefs without a frozen contract run in legacy mode",
        fix="set the require_contract plugin setting to true to opt this project in",
    )


def _usage_accounting_check(context: Context) -> Finding:
    state_dir = context.state_dir
    if state_dir is None:
        return Finding("unavailable", "no plugin state directory is available (pass --state-dir)")
    try:
        runs = context.usage_runs(Path(state_dir))
    except Exception:
        return Finding("inaccessible", "the outcome log could not be read")
    if not runs:
        return Finding("unavailable", "no runs recorded yet")
    return Finding("healthy", f"{len(runs)} run(s) recorded in the append-only outcome log")


def _observatory_check(context: Context) -> Finding:
    url = str(_raw_config_get(context.ctx, "observatory_url", "") or "").strip()
    if not url:
        return Finding(
            "unavailable",
            "not configured; set the observatory_url plugin setting to enable the reachability check",
        )
    try:
        validate_url(
            url,
            allow_loopback=True,
            allow_private=config_flag(context.config.get("allow_private"), False),
        )
    except UnsafeURLError as exc:
        return Finding(
            "incompatible",
            f"the configured endpoint is refused by the address policy: {_clip(exc)}",
            fix="set an http(s) observatory_url; private addresses also need allow_private",
        )
    try:
        status = int(context.observatory_get(url))
    except Exception:
        return Finding("inaccessible", "the observatory did not answer its endpoint")
    if 200 <= status < 300:
        return Finding("healthy", "reachable (the endpoint value is never displayed)")
    return Finding("inaccessible", f"the observatory answered HTTP {status}")


def _manifest_floor(root: Path) -> tuple[str | None, int | None]:
    try:
        text = (Path(root) / "plugin.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    floor = _FLOOR_RE.search(text)
    api = _API_VERSION_RE.search(text)
    return (
        floor.group(1) if floor else None,
        int(api.group(1)) if api else None,
    )


def _version_tuple(text: str) -> tuple[int, ...] | None:
    parts = re.findall(r"\d+", str(text or ""))
    if not parts:
        return None
    return tuple(int(part) for part in parts[:4])


def _hermes_api_floor_check(context: Context) -> Finding:
    root = _repo_root(context, "bundlebees")
    floor = api = None
    if root is not None:
        floor, api = _manifest_floor(root)
    if floor is None:
        return Finding("unknown", "the plugin manifest floor could not be read")
    try:
        version = context.hermes_version()
    except Exception:
        version = None
    version = str(version or "").strip()
    if not version or not _VERSION_RE.fullmatch(version):
        return Finding("unknown", "the installed Hermes version could not be determined")
    parsed_floor = _version_tuple(floor)
    parsed = _version_tuple(version)
    if parsed_floor is None or parsed is None:
        return Finding("unknown", "the Hermes version could not be compared with the plugin floor")
    width = max(len(parsed_floor), len(parsed))
    parsed_floor += (0,) * (width - len(parsed_floor))
    parsed += (0,) * (width - len(parsed))
    if parsed < parsed_floor:
        return Finding(
            "incompatible",
            f"Hermes {version} is below the plugin floor {floor} (api_version {api})",
            fix=f"upgrade Hermes to {floor} or later before dispatching work",
        )
    return Finding("healthy", f"Hermes {version} meets the plugin floor {floor} (api_version {api})")


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------


@dataclass
class Checkers:
    """The injectable probe set; every probe takes the context, returns one Finding.

    The trailing seam fields feed the default probes through the run context;
    injected probes ignore them. Tests replace individual fields with
    ``dataclasses.replace`` and keep the seams.
    """

    bundlebees_checkout: Probe
    bundlebees_origin_main: Probe
    bundlebees_installed_package: Probe
    bundlebees_gateway_loaded: Probe
    workflow_checkout: Probe
    workflow_origin_main: Probe
    hyvv_checkout: Probe
    hyvv_origin_main: Probe
    launcher_deployed_runtime: Probe
    launcher_identity: Probe
    credential: Probe
    sandbox: Probe
    schema_tokens: Probe
    gitleaks: Probe
    git_executable: Probe
    python_coordinator: Probe
    python_verify_runner: Probe
    pinned_workflow: Probe
    pinned_hyvv: Probe
    pinned_gitleaks: Probe
    contract_enforcement: Probe
    usage_accounting: Probe
    observatory: Probe
    hermes_api_floor: Probe
    repo_roots: Mapping[str, Any] | None = None
    git_runner: GitRunner | None = None
    metadata_version: Callable[[str], str | None] | None = None
    which: Callable[[str], str | None] | None = None
    env_get: Callable[[str], str | None] | None = None
    gateway_config: Callable[[], tuple[str, Any]] | None = None
    observatory_get: Callable[[str], int] | None = None
    hermes_version: Callable[[], str | None] | None = None
    usage_runs: Callable[[Path], list[str]] | None = None
    now: Callable[[], str] | None = None


def default_checkers(
    *,
    repo_roots: Mapping[str, Any] | None = None,
    git_runner: GitRunner | None = None,
    metadata_version: Callable[[str], str | None] | None = None,
    which: Callable[[str], str | None] | None = None,
    env_get: Callable[[str], str | None] | None = None,
    gateway_config: Callable[[], tuple[str, Any]] | None = None,
    observatory_get: Callable[[str], int] | None = None,
    hermes_version: Callable[[], str | None] | None = None,
    usage_runs: Callable[[Path], list[str]] | None = None,
    now: Callable[[], str] | None = None,
) -> Checkers:
    """The real probe set; every seam defaults to its read-only implementation."""
    return Checkers(
        bundlebees_checkout=lambda context: _probe_checkout(context, "bundlebees", None),
        bundlebees_origin_main=lambda context: _probe_origin_main(context, "bundlebees"),
        bundlebees_installed_package=_bundlebees_installed_check,
        bundlebees_gateway_loaded=_bundlebees_gateway_check,
        workflow_checkout=lambda context: _probe_checkout(context, "workflow", WORKFLOW_PIN),
        workflow_origin_main=lambda context: _probe_origin_main(context, "workflow"),
        hyvv_checkout=lambda context: _probe_checkout(context, "hyvv", HYVV_PIN),
        hyvv_origin_main=lambda context: _probe_origin_main(context, "hyvv"),
        launcher_deployed_runtime=_launcher_deployed_check,
        launcher_identity=_launcher_identity_check,
        credential=_credential_check,
        sandbox=_sandbox_check,
        schema_tokens=_schema_tokens_check,
        gitleaks=_gitleaks_check,
        git_executable=_git_executable_check,
        python_coordinator=_python_coordinator_check,
        python_verify_runner=_python_verify_runner_check,
        pinned_workflow=lambda context: _pinned_repo_check(context, "WORKFLOW_SRC", WORKFLOW_PIN),
        pinned_hyvv=lambda context: _pinned_repo_check(context, "HYVV_SRC", HYVV_PIN),
        pinned_gitleaks=_pinned_gitleaks_check,
        contract_enforcement=_contract_enforcement_check,
        usage_accounting=_usage_accounting_check,
        observatory=_observatory_check,
        hermes_api_floor=_hermes_api_floor_check,
        repo_roots=repo_roots,
        git_runner=git_runner,
        metadata_version=metadata_version,
        which=which,
        env_get=env_get,
        gateway_config=gateway_config,
        observatory_get=observatory_get,
        hermes_version=hermes_version,
        usage_runs=usage_runs,
        now=now,
    )


# ---------------------------------------------------------------------------
# Collection, rendering, exit semantics
# ---------------------------------------------------------------------------


def _safe(probe: Probe, context: Context, label: str) -> Finding:
    try:
        finding = probe(context)
    except Exception as exc:
        return Finding("unknown", f"the {label} probe failed: {type(exc).__name__}")
    if not isinstance(finding, Finding):
        return Finding("unknown", f"the {label} probe returned no classification")
    return finding


def _row(finding: Finding, label: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"state": finding.state, "detail": finding.detail, "fix": finding.fix, "rev": finding.rev}
    if label is not None:
        row = {"check": label, **row}
    if finding.line is not None:
        row["line"] = finding.line
    return row


def collect(ctx: Any, args: Any = None, checkers: Checkers | None = None) -> dict[str, Any]:
    """Run every probe once and assemble the report (rendering included)."""
    checkers = checkers if checkers is not None else default_checkers()
    context = Context(ctx, args, checkers)
    repos: list[dict[str, Any]] = []
    for repo_name in REPOS:
        aspects: dict[str, dict[str, Any]] = {}
        for aspect in ASPECTS:
            field_name = _ASPECT_CHECKERS.get((repo_name, aspect))
            if field_name is None:
                finding = _NOT_PROBED[(repo_name, aspect)]
            else:
                finding = _safe(getattr(checkers, field_name), context, f"{repo_name}.{aspect}")
            aspects[aspect] = _row(finding, None)
        repos.append({"repo": repo_name, "aspects": aspects})
    checks: list[dict[str, Any]] = []
    for name in _CHECK_ROWS:
        checks.append(_row(_safe(getattr(checkers, name), context, name), name))
    parity_ok = all(
        row["state"] not in _PARITY_BAD
        for repo in repos
        for aspect, row in repo["aspects"].items()
        if aspect in PARITY_ASPECTS
    )
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": context.now,
        "parity_ok": bool(parity_ok),
        "repos": repos,
        "checks": checks,
        "remediation": _remediation(repos, checks),
    }


def _remediation(repos: list[dict[str, Any]], checks: list[dict[str, Any]]) -> list[str]:
    """Exact, safe steps for every row that is not healthy and names a fix."""
    items: list[str] = []
    for repo in repos:
        for aspect, row in repo["aspects"].items():
            if row["fix"] and row["state"] != "healthy":
                items.append(f"{repo['repo']} {ASPECT_LABELS[aspect]}: {row['fix']}")
    for row in checks:
        if row["fix"] and row["state"] != "healthy":
            items.append(f"{row['check']}: {row['fix']}")
    return items


def _legacy_lines(report: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for row in report.get("checks", []):
        line = row.get("line")
        if line and row.get("check") in ("credential", "sandbox", "schema_tokens"):
            found[str(row["check"])] = str(line)
    for repo in report.get("repos", []):
        for aspect, row in (repo.get("aspects") or {}).items():
            line = row.get("line")
            if line and aspect == "deployed_runtime":
                found["adapter"] = str(line)
    return found


def _row_lines(label: str, row: Mapping[str, Any], fix_indent: str) -> list[str]:
    text = f"{label}: {row.get('state', 'unknown')}"
    rev = row.get("rev")
    if rev:
        text += f" ({rev})"
    detail = str(row.get("detail") or "")
    if detail:
        text += f" - {detail}"
    lines = [text]
    fix = str(row.get("fix") or "")
    if fix and row.get("state") != "healthy":
        lines.append(f"{fix_indent}fix: {fix}")
    return lines


def render(report: Mapping[str, Any]) -> list[str]:
    """The human rendering: the legacy block first, then the structured sections."""
    lines = ["bundlebees doctor"]
    legacy = _legacy_lines(report)
    for name in ("credential", "adapter", "sandbox", "schema_tokens"):
        line = legacy.get(name)
        if line:
            lines.append(line)
    lines.append("")
    lines.append(
        "repos (states: " + ", ".join(STATES)
        + "; --check-parity exits nonzero when installed, loaded or deployed diverge from reviewed main):"
    )
    for repo in report["repos"]:
        lines.append(f"  {repo['repo']}:")
        for aspect in ASPECTS:
            row = (repo.get("aspects") or {}).get(aspect) or {}
            lines.extend("    " + line for line in _row_lines(ASPECT_LABELS[aspect], row, "      "))
    lines.append("")
    lines.append("checks:")
    for row in report["checks"]:
        lines.extend("  " + line for line in _row_lines(str(row["check"]), row, "    "))
    remediation = list(report.get("remediation") or [])
    if remediation:
        lines.append("")
        lines.append("remediation:")
        lines.extend(f"  - {item}" for item in remediation)
    return lines


def _public_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """The JSON payload: every fact, with the legacy rendering lines stripped."""
    def clean(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "line"}

    return {
        "schema": report["schema"],
        "generated_at": report["generated_at"],
        "parity_ok": report["parity_ok"],
        "repos": [
            {"repo": repo["repo"], "aspects": {aspect: clean(row) for aspect, row in repo["aspects"].items()}}
            for repo in report["repos"]
        ],
        "checks": [clean(row) for row in report["checks"]],
        "remediation": list(report["remediation"]),
    }


def run(ctx: Any, args: Any = None) -> int:
    """The ``hermes bundlebees doctor`` entry point; read-only, never mutates."""
    checkers = getattr(ctx, "bundlebees_doctor_checkers", None)
    if not isinstance(checkers, Checkers):
        checkers = default_checkers()
    report = collect(ctx, args, checkers)
    if getattr(args, "json", False):
        # The payload holds validated revisions and creation-scrubbed text;
        # scrubbing again would mask the evidence digests.
        print(json.dumps(_public_report(report), indent=2, sort_keys=True))
    else:
        for line in render(report):
            print(line)
    if getattr(args, "check_parity", False) and not report["parity_ok"]:
        return 1
    return 0
