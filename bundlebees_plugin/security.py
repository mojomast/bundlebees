"""Security helpers: scrubbing, child environments, URL guards, path confinement.

Nothing here reads ambient environment variables for credentials. ``child_env``
builds a fresh exact-name environment instead of copying ``os.environ``.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    """Raised when a URL is not allowed by the loopback/public address policy."""


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1[redacted]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd|authorization)"
            r"(\s*[:=]\s*)([^\s,;\"']+)"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[redacted]"),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
        "[redacted]",
    ),
)

_URL_RE = re.compile(r"(?i)\b(?:https?|ssh|git|ftp)://[^\s\"'<>]+")
_SCP_RE = re.compile(r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\S+)?")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![:.\w])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}(?![:.\w])"
    r"|\[[0-9A-Fa-f:]{2,}\]"
)
_HOSTPORT_RE = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}:\d{2,5}\b")
_LONG_KEY_RE = re.compile(
    r"\b(?=[A-Za-z0-9+/=_-]{32,}\b)(?=[^\s]*[0-9])(?=[^\s]*[A-Za-z])"
    r"[A-Za-z0-9+/=_-]{32,}\b"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|passw|auth|api[_-]?key|private[_-]?key|credential|"
    r"bind|address|host|hostname|token_file|key_file|ssh|url|uri|local_path)"
)


def _scrub_str(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _URL_RE.sub("[redacted-url]", text)
    text = _SCP_RE.sub("[redacted-host]", text)
    text = _IPV4_RE.sub("[redacted-ip]", text)
    text = _IPV6_RE.sub("[redacted-ip]", text)
    text = _HOSTPORT_RE.sub("[redacted-host]", text)
    text = _LONG_KEY_RE.sub("[redacted]", text)
    return text


def scrub(value: Any) -> Any:
    """Recursively redact secrets, URLs, IPs, and host-shaped strings.

    Non-string scalars are returned unchanged. Untrusted text keeps its shape so
    callers can still show it as quoted third-party data.
    """
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                out[key] = "[redacted]"
            else:
                out[key] = scrub(item)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, set):
        return {scrub(item) for item in value}
    return value


def looks_like_secret(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return any(pattern.search(text) for pattern, _ in _SECRET_PATTERNS)


def contains_address(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return bool(
        _URL_RE.search(text)
        or _SCP_RE.search(text)
        or _IPV4_RE.search(text)
        or _IPV6_RE.search(text)
        or _HOSTPORT_RE.search(text)
    )


# ---------------------------------------------------------------------------
# Child environment
# ---------------------------------------------------------------------------

# Exact-name allowlist. Anything else is rejected rather than silently copied.
_SAFE_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
    }
)
_SAFE_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a fresh minimal environment for a subprocess.

    ``extra`` may only set names from the exact-name allowlist. ``os.environ``
    is never consulted.
    """
    env = dict(_SAFE_BASE_ENV)
    if not extra:
        return env
    for name, value in extra.items():
        if not isinstance(name, str) or not _ENV_NAME_RE.match(name):
            raise ValueError(f"invalid environment variable name: {name!r}")
        if name not in _SAFE_ENV_NAMES:
            raise ValueError(f"environment variable is not allowed: {name}")
        if value is None:
            continue
        env[name] = str(value)
    return env


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def contain_path(path: str, root: str) -> str:
    """Resolve *path* (following symlinks) and ensure it stays under *root*.

    Relative paths are interpreted relative to *root*. Raises ``ValueError`` on
    a NUL byte, an unreadable root, or any escape including a symlink escape.
    """
    if not isinstance(path, str) or not isinstance(root, str):
        raise ValueError("path and root must be strings")
    if "\x00" in path or "\x00" in root:
        raise ValueError("path contains a NUL byte")
    root_path = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError:
        raise ValueError("path escapes the configured root") from None
    return str(resolved)


# ---------------------------------------------------------------------------
# URL / SSRF guards
# ---------------------------------------------------------------------------

_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.google.internal.",
        "instance-data",
        "metadata.azure.internal",
    }
)
_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _check_ip(
    address: ipaddress._BaseAddress,
    *,
    allow_loopback: bool,
    allow_private: bool,
) -> None:
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    if address in _METADATA_IPS:
        raise UnsafeURLError("metadata service addresses are never allowed")
    if address.is_loopback:
        if not allow_loopback:
            raise UnsafeURLError("loopback addresses are not allowed here")
        return
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        raise UnsafeURLError("link-local, multicast, and unspecified addresses are not allowed")
    if address.is_global:
        return
    if address.is_private or address.is_reserved:
        if not allow_private:
            raise UnsafeURLError("private or reserved addresses require explicit configuration")
        return
    raise UnsafeURLError("address is not a public unicast address")


def validate_url(
    url: str,
    *,
    allow_loopback: bool = True,
    allow_private: bool = False,
    resolve: bool = True,
) -> str:
    """Validate a launcher/fetch URL against the address policy.

    Loopback is allowed by default (the Orbit adapter is loopback-only). Private
    binds must be opted into from explicit user configuration, never from tool
    arguments. Metadata, link-local, multicast, credentials, and non-HTTP
    schemes are always rejected.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("URL must be a non-empty string")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError("URL could not be parsed") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError("only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")
    if not parts.hostname:
        raise UnsafeURLError("URL is missing a host")
    if parts.fragment:
        raise UnsafeURLError("URL fragments are not allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("URL has an invalid port") from exc

    host = parts.hostname.lower()
    if host in _METADATA_HOSTS:
        raise UnsafeURLError("metadata service hostnames are never allowed")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        _check_ip(literal, allow_loopback=allow_loopback, allow_private=allow_private)
    elif resolve:
        try:
            infos = socket.getaddrinfo(
                host,
                port or _DEFAULT_PORTS[parts.scheme.lower()],
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise UnsafeURLError("endpoint host could not be resolved") from exc
        addresses = set()
        for info in infos:
            raw = str(info[4][0]).split("%", 1)[0]
            try:
                addresses.add(ipaddress.ip_address(raw))
            except ValueError:
                raise UnsafeURLError("endpoint resolved to an invalid address") from None
        if not addresses:
            raise UnsafeURLError("endpoint host did not resolve to an address")
        for address in addresses:
            _check_ip(address, allow_loopback=allow_loopback, allow_private=allow_private)
    return url


def loopback_guard(url: str) -> str:
    return validate_url(url, allow_loopback=True, allow_private=False)


def public_fetch_guard(url: str) -> str:
    return validate_url(url, allow_loopback=False, allow_private=False)


def is_public_git_url(url: str) -> bool:
    """True only for a public-looking HTTPS git URL with no credentials.

    DNS is deliberately not resolved here so validation works offline and does
    not turn a prompt builder into a network oracle.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if parts.query or parts.fragment:
        return False
    host = (parts.hostname or "").lower()
    if not host or host in _METADATA_HOSTS:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    if "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    try:
        port = parts.port
    except ValueError:
        return False
    if port not in (None, 443):
        return False
    path = parts.path.strip("/")
    return bool(path)
