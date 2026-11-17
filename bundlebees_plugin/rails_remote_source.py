"""Read-only Rails export reader for one enrolled peer host (R09).

Transport policy
----------------
Only the peer base URL resolved from explicit configuration or the process
environment is contacted. The only request path is the fixed export path
(default ``/v1/rails/events``) on the enrolled origin; every other URL is an
:class:`OffPolicyUrlRefused`. Redirects are refused before they are followed,
including same-origin redirects, and raise :class:`RedirectRefused`. The HTTP
transport is injectable so tests never need a socket.

Wire contract
-------------
The page is ``rizzstack.rails-export/1``:
``{schema, chain_id, after, events, next_cursor, head, has_more}``. ``after``
and ``next_cursor`` are numeric and exclusive. Events are full frozen
``rizzstack.rails/1`` envelopes. Each envelope is canonicalized with the frozen
formula; the resulting bytes must equal the origin bytes that the envelope
hash commits to. Accepted bytes are returned unchanged, never re-authored.
Malformed bytes, a hash mismatch or a peer-supplied digest mismatch are
:class:`OriginBytesRefused`.

Forensics
---------
A missing retained cursor range is reported in ``gaps`` and is never filled.
A peer timestamp older than the policy window (or a locally recorded last-seen
older than the window in :meth:`health`) is ``stale`` and never ``fresh``.
Once a report is committed, replaying from the beginning and finding a shorter
peer cursor/head, a dropped committed sequence or a different committed hash
raises :class:`LossAfterCommit`; it is never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone

try:
    from rizzstack_rails import contracts as rails_contracts
except ModuleNotFoundError:
    import sys

    _CONTRIB = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contrib"
    )
    if _CONTRIB not in sys.path:
        sys.path.insert(0, _CONTRIB)
    from rizzstack_rails import contracts as rails_contracts

ADAPTER = "rails.remote"
ADAPTER_ID = "rails_remote"
SOURCE_VERSION = "rizzstack.rails-source/1"
EXPORT_SCHEMA = "rizzstack.rails-export/1"
DEFAULT_EXPORT_PATH = "/v1/rails/events"
DEFAULT_STALE_AFTER_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_PAGE_LIMIT = 500
MAX_PAGE_LIMIT = 500
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_CHAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_ALLOWED_QUERY_KEYS = frozenset({"after", "limit"})
_CONFIG_BASE_KEYS = (
    "peer_base_url",
    "rails_peer_base_url",
    "base_url",
    "peer_url",
    "peer_base",
)
_CONFIG_PEER_KEYS = ("peer_id", "rails_peer_id")
_ENV_BASE_KEYS = (
    "BUNDLEBEES_RAILS_PEER_BASE_URL",
    "BUNDLEBEES_RAILS_PEER_BASE",
    "BUNDLEBEES_RAILS_PEER_URL",
    "BUNDLEBEES_PEER_BASE_URL",
    "RAILS_PEER_BASE_URL",
    "RAILS_PEER_BASE",
    "RAILS_REMOTE_BASE_URL",
)
_ENV_PEER_KEYS = ("BUNDLEBEES_RAILS_PEER_ID", "RAILS_PEER_ID")
_ORIGIN_KEYS = ("origin_bytes", "origin_text", "bytes")
_EVENT_DRAFT_KEYS = frozenset(
    {
        "kind",
        "source",
        "scope",
        "payload",
        "authority",
        "lane",
        "occurred_at",
        "duration_ms",
        "timing_quality",
        "redaction_policy",
        "dedupe_key",
    }
)


class SourceError(rails_contracts.RailsRefusal):
    """Stable typed source error, also the base of every remote refusal."""

    status_code = 502
    code = "source_error"

    def __init__(self, code, message=None, **details):
        if message is None:
            message = str(code)
            code = "source_error"
        super().__init__(message, code=code)
        self.error = message
        self.details = dict(details)
        for key, value in details.items():
            setattr(self, key, value)


class RemoteSourceRefusal(SourceError):
    """Base typed refusal for the remote peer source."""

    status_code = 502
    code = "remote_source"

    def __init__(self, message, code=None, **details):
        super().__init__(code or type(self).code, message, **details)


class EnrollmentRefused(RemoteSourceRefusal):
    status_code = 403
    code = "not_enrolled"

    def __init__(self, message, **details):
        super().__init__(message, code="not_enrolled", **details)


class OffPolicyUrlRefused(RemoteSourceRefusal):
    status_code = 403
    code = "off_policy_url"

    def __init__(self, message, **details):
        super().__init__(message, code="off_policy_url", **details)


class RedirectRefused(RemoteSourceRefusal):
    status_code = 403
    code = "redirect_refused"

    def __init__(self, message, **details):
        super().__init__(message, code="redirect_refused", **details)


class OriginBytesRefused(RemoteSourceRefusal):
    status_code = 409
    code = "origin_bytes"

    def __init__(self, message, **details):
        super().__init__(message, code="origin_bytes", **details)


class PeerProtocolRefused(RemoteSourceRefusal):
    status_code = 502
    code = "peer_protocol"

    def __init__(self, message, **details):
        super().__init__(message, code="peer_protocol", **details)


class PeerUnavailable(RemoteSourceRefusal):
    status_code = 503
    code = "peer_unavailable"

    def __init__(self, message, **details):
        super().__init__(message, code="peer_unavailable", **details)


class ResetRequired(RemoteSourceRefusal):
    status_code = 409
    code = "reset_required"

    def __init__(self, message, **details):
        super().__init__(message, code="reset_required", **details)


class PeerError(RemoteSourceRefusal):
    status_code = 502
    code = "peer_error"

    def __init__(self, message, peer_code=None, **details):
        super().__init__(message, code="peer_error", peer_code=peer_code, **details)


class ExportGapDetected(RemoteSourceRefusal):
    status_code = 409
    code = "export_gap"

    def __init__(self, message, gaps, report=None, code="export_gap", **details):
        super().__init__(
            message, code=code, gaps=list(gaps), report=report, **details
        )
        self.gap = gaps[0] if gaps else None


class SourceGapRefused(ExportGapDetected):
    def __init__(self, message, gaps=None, report=None, **details):
        super().__init__(
            message,
            gaps or [],
            report=report,
            code="source_gap",
            **details,
        )


class StaleHostDetected(RemoteSourceRefusal):
    status_code = 409
    code = "stale_host"

    def __init__(self, message, report=None, **details):
        super().__init__(message, code="stale_host", report=report, **details)
        if report:
            self.last_seen = report.get("last_seen")
            self.last_seen_age_seconds = report.get("last_seen_age_seconds")
            self.stale_reason = report.get("stale_reason")


class LossAfterCommit(RemoteSourceRefusal):
    status_code = 409
    code = "loss_after_commit"

    def __init__(self, message, loss=None):
        super().__init__(message, code="loss_after_commit", loss=loss or {})
        loss = loss or {}
        self.reason = loss.get("reason")
        self.chain_id = loss.get("chain_id")
        self.committed_cursor = loss.get("committed_cursor")
        self.committed_next_seq = loss.get("committed_next_seq")
        self.peer_cursor = loss.get("peer_cursor")
        self.peer_next_seq = loss.get("peer_next_seq")
        self.missing = list(loss.get("missing") or [])
        self.mismatched = list(loss.get("mismatched") or [])


class HttpResponse:
    def __init__(self, status, body, headers=None, url=None):
        self.status = int(status)
        self.body = body
        self.headers = dict(headers or {})
        self.url = url

    def read(self):
        return self.body


class UrllibTransport:
    """Default GET transport that refuses every redirect before it is followed."""

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(_RefuseRedirectHandler())

    def request(self, url, headers=None, timeout=None):
        request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
        try:
            response = self.opener.open(request, timeout=timeout)
            return HttpResponse(
                status=getattr(response, "status", response.getcode()),
                body=response.read(),
                headers=dict(response.headers.items()),
                url=response.geturl(),
            )
        except urllib.error.HTTPError as exc:
            if 300 <= int(exc.code) < 400:
                raise RedirectRefused("peer answered with a redirect")
            return HttpResponse(
                status=exc.code,
                body=exc.read(),
                headers=dict(exc.headers.items()) if exc.headers else {},
                url=exc.geturl(),
            )
        except RemoteSourceRefusal:
            raise
        except urllib.error.URLError as exc:
            raise PeerUnavailable("peer transport failed: " + type(exc.reason).__name__)
        except OSError as exc:
            raise PeerUnavailable("peer transport failed: " + type(exc).__name__)


class _RefuseRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectRefused("peer redirect refused")

    def http_error_308(self, req, fp, code, msg, headers):
        raise RedirectRefused("peer redirect refused")


def _lookup(config, keys):
    if config is None:
        return None
    for key in keys:
        if isinstance(config, Mapping) and key in config:
            return config.get(key)
        if hasattr(config, key):
            value = getattr(config, key)
            if value is not None:
                return value
    return None


def _first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _origin_of(parsed):
    try:
        port = parsed.port
    except ValueError:
        raise OffPolicyUrlRefused("URL carries an invalid port")
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port)


def _now_epoch(value):
    if callable(value):
        value = value()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return datetime.now(timezone.utc).timestamp()


def _stamp(epoch):
    return (
        datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )
        + "Z"
    )


def _parse_stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _header(headers, name):
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == lowered:
            return value
    return None


def _response_attr(response, name, default=None):
    if isinstance(response, Mapping):
        return response.get(name, default)
    return getattr(response, name, default)


def _digest_of(texts):
    return hashlib.sha256("".join(texts).encode("ascii")).hexdigest()


def _empty_digest():
    return hashlib.sha256(b"").hexdigest()


def _unavailable_revision():
    return hashlib.sha256(b"remote-unavailable").hexdigest()


class RemoteRailSource:
    """Source adapter over one enrolled peer's ``rails-export/1`` pages."""

    def __init__(
        self,
        base_url=None,
        *,
        peer_id=None,
        config=None,
        env=None,
        transport=None,
        stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        now=None,
        state_path=None,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
    ):
        environment = os.environ if env is None else env
        resolved_base = _first_text(
            base_url,
            _lookup(config, _CONFIG_BASE_KEYS),
            *(environment.get(key) for key in _ENV_BASE_KEYS)
        )
        if not resolved_base:
            raise EnrollmentRefused("no enrolled peer base URL is configured")
        self.base_url, self._base_scheme, self._base_netloc, self._base_path = (
            self._normalize_base(resolved_base)
        )
        self._base_origin = _origin_of(urllib.parse.urlsplit(self.base_url))
        self.peer_id = _first_text(
            peer_id,
            _lookup(config, _CONFIG_PEER_KEYS),
            *(environment.get(key) for key in _ENV_PEER_KEYS)
        ) or (urllib.parse.urlsplit(self.base_url).hostname or "peer")
        self._export_path = self._resolve_export_path(config)
        self.transport = transport or UrllibTransport()
        if (
            isinstance(stale_after_seconds, bool)
            or not isinstance(stale_after_seconds, (int, float))
            or stale_after_seconds <= 0
        ):
            raise RemoteSourceRefusal(
                "stale_after_seconds must be a positive number", code="config"
            )
        self.stale_after_seconds = float(stale_after_seconds)
        self._now = now
        self.timeout = timeout
        self.max_response_bytes = int(max_response_bytes)
        self.state_path = os.fspath(state_path) if state_path is not None else None
        self._state = self._load_state()
        self._check_base_url()

    def _normalize_base(self, base):
        parsed = urllib.parse.urlsplit(base.strip())
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise EnrollmentRefused("peer base URL must be http or https")
        if not parsed.hostname:
            raise EnrollmentRefused("peer base URL must carry a host")
        if parsed.username is not None or parsed.password is not None:
            raise EnrollmentRefused("peer base URL must not carry credentials")
        if parsed.query or parsed.fragment:
            raise EnrollmentRefused("peer base URL must not carry a query or fragment")
        try:
            parsed.port
        except ValueError:
            raise EnrollmentRefused("peer base URL carries an invalid port")
        path = parsed.path or ""
        decoded = urllib.parse.unquote(path)
        if "\\" in decoded or any(
            segment in (".", "..") for segment in decoded.split("/")
        ):
            raise EnrollmentRefused("peer base URL path may not traverse")
        clean_path = path.rstrip("/")
        netloc = parsed.netloc
        normalized = urllib.parse.urlunsplit((scheme, netloc, clean_path, "", ""))
        return normalized, scheme, netloc, clean_path

    def _resolve_export_path(self, config):
        configured = _lookup(config, ("export_path",))
        if configured is None:
            return self._base_path + DEFAULT_EXPORT_PATH
        if not isinstance(configured, str) or not configured.startswith("/"):
            raise EnrollmentRefused("export_path must be an absolute path")
        parsed = urllib.parse.urlsplit(configured)
        decoded = urllib.parse.unquote(parsed.path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or "\\" in decoded
            or any(segment in (".", "..") for segment in decoded.split("/"))
        ):
            raise EnrollmentRefused("export_path must be a fixed local path")
        return parsed.path

    def _load_state(self):
        if not self.state_path:
            return {"chains": {}, "peer": {}}
        if not os.path.exists(self.state_path):
            return {"chains": {}, "peer": {}}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RemoteSourceRefusal(
                "remote source state is unreadable: " + type(exc).__name__,
                code="state",
            )
        if not isinstance(state, dict):
            raise RemoteSourceRefusal(
                "remote source state has an unknown shape", code="state"
            )
        state.setdefault("chains", {})
        state.setdefault("peer", {})
        if not isinstance(state["chains"], dict) or not isinstance(state["peer"], dict):
            raise RemoteSourceRefusal(
                "remote source state has an unknown shape", code="state"
            )
        return state

    def _save_state(self):
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        temporary = self.state_path + ".tmp." + str(os.getpid())
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, sort_keys=True, separators=(",", ":"))
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _chain_state(self, chain_id):
        chains = self._state.setdefault("chains", {})
        record = chains.get(chain_id)
        if not isinstance(record, dict):
            record = {"next_seq": 1, "head_hash": None, "hashes": {}}
            chains[chain_id] = record
        if "next_seq" not in record and isinstance(record.get("cursor"), int):
            record["next_seq"] = record["cursor"]
        record.setdefault("next_seq", 1)
        record.setdefault("head_hash", None)
        hashes = record.get("hashes")
        if not isinstance(hashes, dict):
            hashes = {}
            record["hashes"] = hashes
        return record

    def _peer_state(self):
        peer = self._state.setdefault("peer", {})
        if not isinstance(peer, dict):
            peer = {}
            self._state["peer"] = peer
        return peer

    def _touch_peer(self, success, code=None):
        peer = self._peer_state()
        now = _now_epoch(self._now)
        if success:
            peer["last_seen"] = now
            peer["last_seen_stamp"] = _stamp(now)
            peer["last_success"] = peer["last_seen_stamp"]
            peer["last_error"] = None
        else:
            peer["last_error"] = code or "peer_unavailable"
        self._save_state()

    def _check_base_url(self):
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.username is not None or parsed.password is not None:
            raise EnrollmentRefused("peer base URL must not carry credentials")
        if parsed.query or parsed.fragment:
            raise EnrollmentRefused("peer base URL must not carry a query or fragment")
        if _origin_of(parsed) != self._base_origin:
            raise EnrollmentRefused("peer base URL origin is invalid")

    def _check_url(self, url):
        if not isinstance(url, str) or not url:
            raise OffPolicyUrlRefused("URL must be a nonempty string")
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise OffPolicyUrlRefused("URL scheme is outside the peer policy")
        if parsed.username is not None or parsed.password is not None:
            raise OffPolicyUrlRefused("URL may not carry credentials")
        if parsed.fragment:
            raise OffPolicyUrlRefused("URL may not carry a fragment")
        if _origin_of(parsed) != self._base_origin:
            raise OffPolicyUrlRefused("URL origin is not the enrolled peer")
        decoded = urllib.parse.unquote(parsed.path or "")
        if "\\" in decoded or any(
            segment in (".", "..") for segment in decoded.split("/")
        ):
            raise OffPolicyUrlRefused("URL path may not traverse")
        if decoded != self._export_path:
            raise OffPolicyUrlRefused("URL is not the enrolled export endpoint")
        if parsed.query:
            pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            keys = [key for key, _ in pairs]
            if len(keys) != len(set(keys)):
                raise OffPolicyUrlRefused("URL repeats a query parameter")
            if not set(keys).issubset(_ALLOWED_QUERY_KEYS):
                raise OffPolicyUrlRefused("URL query keys are outside the peer policy")
        return parsed

    def export_url(self, chain_id, cursor=0, limit=DEFAULT_PAGE_LIMIT, *, after=None):
        if not isinstance(chain_id, str) or not _CHAIN_RE.match(chain_id):
            raise PeerProtocolRefused("chain_id is not a valid origin chain")
        if cursor is None and after is None:
            cursor = 0
        if after is not None:
            if cursor not in (0, None) and cursor != after:
                raise PeerProtocolRefused("cursor and after arguments disagree")
            cursor = after
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise PeerProtocolRefused("cursor must be a nonnegative integer")
        query = [("after", str(cursor))]
        if limit is not None:
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
                or limit > MAX_PAGE_LIMIT
            ):
                raise PeerProtocolRefused("limit must be between 1 and 500")
            query.append(("limit", str(limit)))
        query = urllib.parse.urlencode(query)
        url = urllib.parse.urlunsplit(
            (self._base_scheme, self._base_netloc, self._export_path, query, "")
        )
        self._check_url(url)
        return url

    def _request(self, url):
        headers = {
            "Accept": "application/json",
            "User-Agent": "bundlebees-rails-remote/1",
        }
        try:
            if hasattr(self.transport, "request"):
                response = self.transport.request(
                    url, headers=headers, timeout=self.timeout
                )
            elif hasattr(self.transport, "get"):
                response = self.transport.get(url, headers=headers, timeout=self.timeout)
            elif callable(self.transport):
                response = self.transport(url)
            else:
                raise PeerUnavailable("transport does not implement a GET operation")
        except RemoteSourceRefusal as exc:
            self._touch_peer(False, exc.code)
            raise
        except Exception as exc:
            self._touch_peer(False, "peer_unavailable")
            raise PeerUnavailable("peer transport failed: " + type(exc).__name__)
        status = int(
            _response_attr(response, "status", _response_attr(response, "status_code", 0))
        )
        headers_out = _response_attr(response, "headers", {}) or {}
        if 300 <= status < 400:
            raise RedirectRefused(
                "peer answered with a redirect",
                status=status,
                location=_header(headers_out, "Location"),
            )
        final_url = _response_attr(response, "url")
        if final_url and final_url != url:
            self._check_url(final_url)
            raise RedirectRefused("transport followed or rewrote the request URL")
        body = _response_attr(response, "body", _response_attr(response, "text", b""))
        if body is None:
            body = b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, (bytes, bytearray)):
            raise PeerProtocolRefused("peer body must be bytes or text")
        if len(body) > self.max_response_bytes:
            raise PeerProtocolRefused("peer body exceeds the response limit")
        return bytes(body), headers_out, status

    def _decode_body(self, body, chain_id, after, status):
        if not body:
            payload = {}
        else:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                raise PeerProtocolRefused("peer body is not UTF-8")
            try:
                payload = json.loads(text)
            except ValueError:
                raise PeerProtocolRefused("peer body is not JSON")
        if not isinstance(payload, dict):
            raise PeerProtocolRefused("peer body must be a JSON object")
        if payload.get("ok") is False or status >= 400:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            if code == "reset_required":
                raise ResetRequired("peer cursor is beyond its head")
            if code == "source_gap":
                raise SourceGapRefused("peer reports a missing retained prefix")
            if status >= 500:
                raise PeerUnavailable("peer returned status " + str(status), status=status)
            if code:
                raise PeerError("peer error: " + str(code), peer_code=code, status=status)
            raise PeerProtocolRefused("peer returned status " + str(status), status=status)
        if payload.get("schema") != EXPORT_SCHEMA:
            raise PeerProtocolRefused("peer export schema is not the frozen export page")
        reported_peer = payload.get("peer_id")
        if (
            reported_peer is not None
            and self.peer_id is not None
            and reported_peer != self.peer_id
        ):
            raise PeerProtocolRefused("peer identity does not match the enrollment")
        reported_chain = payload.get("chain_id")
        if not isinstance(reported_chain, str) or not _CHAIN_RE.match(reported_chain):
            raise PeerProtocolRefused("peer did not identify a valid chain")
        if chain_id is not None and reported_chain != chain_id:
            raise PeerProtocolRefused("peer chain does not match the request")
        reported_after = payload.get("after")
        if isinstance(reported_after, bool) or not isinstance(reported_after, int):
            raise PeerProtocolRefused("export page must carry an integer after")
        if reported_after != after:
            raise PeerProtocolRefused("export page after does not match the request")
        events = payload.get("events")
        if not isinstance(events, list):
            raise PeerProtocolRefused("peer events must be a list")
        if len(events) > MAX_PAGE_LIMIT:
            raise PeerProtocolRefused("peer page exceeds the event limit")
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and (
            isinstance(next_cursor, bool)
            or not isinstance(next_cursor, int)
            or next_cursor < 0
        ):
            raise PeerProtocolRefused("next_cursor must be a nonnegative integer or null")
        has_more = payload.get("has_more")
        if has_more is not None and not isinstance(has_more, bool):
            raise PeerProtocolRefused("has_more must be a boolean")
        head = payload.get("head")
        if head is not None:
            if not isinstance(head, Mapping):
                raise PeerProtocolRefused("head must be an object or null")
            seq = head.get("seq")
            digest = head.get("hash")
            if (
                isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq < 1
                or not isinstance(digest, str)
                or not rails_contracts.HEX64_RE.match(digest)
            ):
                raise PeerProtocolRefused("head shape is outside the export contract")
        return payload, reported_chain

    def _entries(self, raw_events):
        entries = []
        for raw in raw_events:
            expected_hash = None
            expected_sha = None
            if isinstance(raw, str):
                text = raw
            elif isinstance(raw, (bytes, bytearray)):
                try:
                    text = bytes(raw).decode("ascii")
                except UnicodeDecodeError:
                    raise OriginBytesRefused("origin bytes are not ASCII canonical text")
            elif isinstance(raw, Mapping):
                candidate = None
                for key in _ORIGIN_KEYS:
                    if isinstance(raw.get(key), str):
                        candidate = raw[key]
                        break
                if candidate is None:
                    try:
                        candidate = rails_contracts.canonicalize(raw).decode("ascii")
                    except rails_contracts.RailsRefusal as exc:
                        raise OriginBytesRefused(
                            "origin envelope is not canonical: " + exc.code
                        )
                text = candidate
                expected_hash = raw.get("origin_hash")
                expected_sha = raw.get("origin_sha256") or raw.get("bytes_sha256")
            else:
                raise OriginBytesRefused("origin entry must be a frozen event envelope")
            entries.append(
                {
                    "text": text,
                    "expected_hash": expected_hash,
                    "expected_sha": expected_sha,
                }
            )
        return entries

    def _verify_entries(self, entries, after, chain_id):
        verified = []
        seen = set()
        previous_seq = None
        previous_hash = None
        for entry in entries:
            text = entry["text"]
            if not isinstance(text, str):
                raise OriginBytesRefused("origin entry must be canonical text")
            try:
                canonical = rails_contracts.canonicalize_text(text).decode("ascii")
            except rails_contracts.RailsRefusal as exc:
                raise OriginBytesRefused("origin text is not canonical: " + exc.code)
            if canonical != text:
                raise OriginBytesRefused("origin bytes are not canonical")
            try:
                envelope = json.loads(text)
            except ValueError:
                raise OriginBytesRefused("origin entry is not JSON")
            if not isinstance(envelope, dict):
                raise OriginBytesRefused("origin entry must be an object")
            if envelope.get("schema") != rails_contracts.SCHEMA:
                raise OriginBytesRefused("origin schema mismatch")
            if envelope.get("chain_id") != chain_id:
                raise PeerProtocolRefused("origin chain does not match the request")
            seq = envelope.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise OriginBytesRefused("origin seq must be a positive integer")
            if envelope.get("event_id") != chain_id + ":" + str(seq):
                raise OriginBytesRefused("origin event_id mismatch")
            if seq in seen:
                raise OriginBytesRefused("origin seq appears twice in one export")
            without_hash = {
                key: value for key, value in envelope.items() if key != "hash"
            }
            try:
                derived = rails_contracts.rails_hash(
                    envelope.get("prev_hash"), without_hash
                )
            except rails_contracts.RailsRefusal as exc:
                raise OriginBytesRefused("origin hash input refused: " + exc.code)
            if envelope.get("hash") != derived:
                raise OriginBytesRefused("origin hash does not match its bytes")
            if (
                entry["expected_hash"] is not None
                and entry["expected_hash"] != envelope.get("hash")
            ):
                raise OriginBytesRefused("peer-supplied origin hash does not match")
            bytes_sha = hashlib.sha256(text.encode("ascii")).hexdigest()
            if entry["expected_sha"] is not None and entry["expected_sha"] != bytes_sha:
                raise OriginBytesRefused("peer-supplied bytes digest does not match")
            if previous_seq is not None:
                if seq <= previous_seq:
                    raise OriginBytesRefused("origin entries must be strictly ascending")
                if seq == previous_seq + 1 and envelope.get("prev_hash") != previous_hash:
                    raise OriginBytesRefused("origin chain is broken inside the export")
            verified.append(
                {
                    "text": text,
                    "seq": seq,
                    "hash": envelope.get("hash"),
                    "prev_hash": envelope.get("prev_hash"),
                    "bytes_sha256": bytes_sha,
                }
            )
            seen.add(seq)
            previous_seq = seq
            previous_hash = envelope.get("hash")
        if verified:
            first = verified[0]
            record = self._chain_state(chain_id)
            if first["seq"] == 1:
                if first["prev_hash"] != rails_contracts.GENESIS:
                    raise OriginBytesRefused("origin does not start at genesis")
            elif first["seq"] == after + 1:
                known = record["hashes"].get(str(first["seq"] - 1))
                if known is not None and first["prev_hash"] != known:
                    raise LossAfterCommit(
                        "origin does not continue the committed head",
                        {
                            "reason": "prev_hash_mismatch",
                            "chain_id": chain_id,
                            "committed_cursor": record["next_seq"] - 1,
                            "committed_next_seq": record["next_seq"],
                            "peer_cursor": first["seq"],
                            "peer_next_seq": first["seq"] + 1,
                            "missing": [],
                            "mismatched": [first["seq"]],
                        },
                    )
        return verified

    def _check_head(self, payload, verified):
        head = payload.get("head")
        if not isinstance(head, Mapping):
            return
        head_seq = head.get("seq")
        head_hash = head.get("hash")
        if verified and head_seq < verified[-1]["seq"]:
            raise PeerProtocolRefused("export head is behind the returned events")
        for item in verified:
            if item["seq"] == head_seq and item["hash"] != head_hash:
                raise OriginBytesRefused("export head hash does not match its bytes")

    def _next_seq(self, payload, verified, after):
        explicit = payload.get("next_seq")
        if (
            isinstance(explicit, int)
            and not isinstance(explicit, bool)
            and explicit >= 1
        ):
            return explicit
        next_cursor = payload.get("next_cursor")
        if (
            isinstance(next_cursor, int)
            and not isinstance(next_cursor, bool)
            and next_cursor >= 0
        ):
            last_seq = verified[-1]["seq"] if verified else after
            if next_cursor > last_seq:
                return next_cursor
            return next_cursor + 1
        if verified:
            return verified[-1]["seq"] + 1
        return after + 1

    def _declared_gaps(self, payload):
        gaps = []
        declared = payload.get("gaps")
        if isinstance(declared, list):
            for item in declared:
                span = self._span(item)
                if span is not None:
                    gaps.append(span)
        span = self._span(payload.get("gap"))
        if span is not None:
            gaps.append(span)
        return gaps

    def _span(self, item):
        if not isinstance(item, Mapping):
            return None
        start = item.get("start")
        end = item.get("end")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and start >= 1
            and end >= start
        ):
            return {"start": start, "end": end}
        return None

    def _merge_gaps(self, gaps):
        merged = []
        for gap in sorted(gaps, key=lambda item: (item["start"], item["end"])):
            if merged and gap["start"] <= merged[-1]["end"] + 1:
                merged[-1]["end"] = max(merged[-1]["end"], gap["end"])
            else:
                merged.append(dict(gap))
        return merged

    def _gaps(self, payload, verified, after, next_seq, declared):
        gaps = list(declared)
        expected = after + 1
        for item in verified:
            seq = item["seq"]
            if seq < expected:
                continue
            if seq > expected:
                gaps.append({"start": expected, "end": seq - 1})
            expected = seq + 1
        head = payload.get("head")
        has_more = payload.get("has_more") is True
        if not has_more:
            if isinstance(head, Mapping) and isinstance(head.get("seq"), int):
                head_seq = head["seq"]
                if head_seq >= expected:
                    gaps.append({"start": expected, "end": head_seq})
            elif next_seq > expected:
                gaps.append({"start": expected, "end": next_seq - 1})
        return self._merge_gaps(gaps)

    def _loss(self, payload, chain_id, after, next_seq, verified):
        record = self._chain_state(chain_id)
        committed_next = record["next_seq"]
        committed_cursor = committed_next - 1
        peer_cursor = next_seq - 1
        committed_hashes = record["hashes"]
        loss = None
        if payload.get("loss"):
            loss = {
                "reason": "peer_reported_loss",
                "chain_id": chain_id,
                "committed_cursor": committed_cursor,
                "committed_next_seq": committed_next,
                "peer_cursor": peer_cursor,
                "peer_next_seq": next_seq,
                "missing": [],
                "mismatched": [],
                "detail": (
                    payload.get("loss")
                    if isinstance(payload.get("loss"), str)
                    else None
                ),
            }
        if committed_next <= 1 and not committed_hashes:
            return loss
        provided = {item["seq"]: item["hash"] for item in verified}
        missing = []
        mismatched = []
        for seq_text, known_hash in committed_hashes.items():
            seq = int(seq_text)
            if seq in provided and provided[seq] != known_hash:
                mismatched.append(seq)
        if next_seq < committed_next:
            for seq in range(next_seq, committed_next):
                if seq not in provided:
                    missing.append(seq)
        if after < committed_cursor:
            for seq in range(after + 1, committed_next):
                if seq not in provided and seq not in missing:
                    missing.append(seq)
        head = payload.get("head")
        head_behind = False
        if isinstance(head, Mapping) and isinstance(head.get("seq"), int):
            head_seq = head["seq"]
            head_hash = head.get("hash")
            if head_seq < committed_cursor:
                head_behind = True
            known_head = committed_hashes.get(str(head_seq))
            if known_head is not None and head_hash != known_head:
                mismatched.append(head_seq)
            if (
                head_seq == committed_cursor
                and record.get("head_hash")
                and head_hash != record["head_hash"]
            ):
                mismatched.append(head_seq)
        if mismatched:
            reason = "hash_mismatch"
        elif head_behind:
            reason = "head_behind"
        elif next_seq < committed_next:
            reason = "peer_cursor_behind"
        elif missing:
            reason = "missing_committed"
        else:
            return loss
        return {
            "reason": reason,
            "chain_id": chain_id,
            "committed_cursor": committed_cursor,
            "committed_next_seq": committed_next,
            "peer_cursor": peer_cursor,
            "peer_next_seq": next_seq,
            "missing": sorted(set(missing)),
            "mismatched": sorted(set(mismatched)),
        }

    def _stale(self, payload, headers):
        declared = payload.get("last_seen") or payload.get("host_last_seen")
        if declared is None:
            declared = payload.get("last_success")
        if declared is None:
            declared = payload.get("observed_at")
        if declared is None:
            for header_name in ("X-Rails-Last-Seen", "Last-Seen", "X-Last-Seen"):
                declared = _header(headers, header_name)
                if declared is not None:
                    break
        now = _now_epoch(self._now)
        if declared is None:
            return {
                "last_seen": _stamp(now),
                "last_seen_age_seconds": 0.0,
                "stale": False,
                "fresh": True,
                "stale_reason": None,
            }
        parsed = _parse_stamp(declared)
        if parsed is None:
            return {
                "last_seen": declared if isinstance(declared, str) else None,
                "last_seen_age_seconds": None,
                "stale": True,
                "fresh": False,
                "stale_reason": "last_seen_invalid",
            }
        age = now - parsed.timestamp()
        if age < -60:
            stale = True
            reason = "clock_skew"
        elif age > self.stale_after_seconds:
            stale = True
            reason = "age_exceeded"
        else:
            stale = False
            reason = None
        return {
            "last_seen": declared if isinstance(declared, str) else parsed.isoformat(),
            "last_seen_age_seconds": age,
            "stale": stale,
            "fresh": not stale,
            "stale_reason": reason,
        }

    def _evaluate(
        self,
        payload,
        headers,
        chain_id,
        after,
        *,
        strict,
        require_fresh,
        allow_loss,
    ):
        raw_events = payload.get("events", [])
        entries = self._entries(raw_events)
        verified = self._verify_entries(entries, after, chain_id)
        self._check_head(payload, verified)
        next_seq = self._next_seq(payload, verified, after)
        if next_seq <= after:
            raise PeerProtocolRefused("peer page made no forward cursor progress")
        gaps = self._gaps(
            payload, verified, after, next_seq, self._declared_gaps(payload)
        )
        loss = self._loss(payload, chain_id, after, next_seq, verified)
        stale = self._stale(payload, headers)
        texts = [item["text"] for item in verified]
        origin_hashes = [item["hash"] for item in verified]
        first_gap = gaps[0] if gaps else None
        importable = []
        for item in verified:
            if first_gap is not None and item["seq"] >= first_gap["start"]:
                break
            importable.append(item["text"])
        record = self._chain_state(chain_id)
        head = payload.get("head")
        has_more = payload.get("has_more") is True
        status = "ok"
        if stale["stale"]:
            status = "stale"
        if gaps:
            status = "gap"
        if loss:
            status = "loss"
        report = {
            "schema": EXPORT_SCHEMA,
            "adapter": ADAPTER_ID,
            "peer_id": self.peer_id,
            "chain_id": chain_id,
            "requested_cursor": after,
            "requested_after": after,
            "after": after,
            "cursor": verified[-1]["seq"] if verified else after,
            "next_cursor": next_seq - 1,
            "reported_next_cursor": payload.get("next_cursor"),
            "next_seq": next_seq,
            "peer_cursor": next_seq - 1,
            "peer_next_seq": next_seq,
            "committed_cursor": record["next_seq"] - 1,
            "committed_next_seq": record["next_seq"],
            "head": dict(head) if isinstance(head, Mapping) else None,
            "head_hash": (
                head.get("hash")
                if isinstance(head, Mapping) and isinstance(head.get("hash"), str)
                else (origin_hashes[-1] if origin_hashes else None)
            ),
            "has_more": has_more,
            "count": len(verified),
            "range": [verified[0]["seq"], verified[-1]["seq"]] if verified else None,
            "events": texts,
            "origin_bytes": list(texts),
            "origin_hashes": origin_hashes,
            "bytes_sha256": [item["bytes_sha256"] for item in verified],
            "preserved": True,
            "gaps": gaps,
            "gap": first_gap,
            "gap_range": [first_gap["start"], first_gap["end"]] if first_gap else None,
            "filled": False,
            "contiguous": not gaps,
            "complete": not gaps and loss is None and not has_more,
            "importable_events": importable,
            "last_seen": stale["last_seen"],
            "last_seen_age_seconds": stale["last_seen_age_seconds"],
            "stale": stale["stale"],
            "fresh": stale["fresh"],
            "stale_reason": stale["stale_reason"],
            "loss": loss,
            "status": status,
            "committed": False,
        }
        if loss and not allow_loss:
            raise LossAfterCommit(
                "committed origin data was lost or forked on replay", loss
            )
        if gaps and strict:
            raise ExportGapDetected("peer export has a cursor gap", gaps, report=report)
        if stale["stale"] and require_fresh:
            raise StaleHostDetected(
                "peer host is stale: " + str(stale["stale_reason"]), report=report
            )
        return report

    def fetch_url(
        self,
        url,
        *,
        chain_id=None,
        cursor=None,
        limit=None,
        strict=False,
        require_fresh=False,
        allow_loss=False,
        after=None,
    ):
        self._check_url(url)
        if after is not None:
            if cursor is not None and cursor != after:
                raise PeerProtocolRefused("cursor and after arguments disagree")
            cursor = after
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        if chain_id is not None and not _CHAIN_RE.match(chain_id):
            raise PeerProtocolRefused("chain_id is not a valid origin chain")
        raw_after = query.get("after")
        parsed_after = None
        if raw_after is not None:
            try:
                parsed_after = int(raw_after)
            except ValueError:
                raise PeerProtocolRefused("cursor in URL is not an integer")
            if parsed_after < 0:
                raise PeerProtocolRefused("cursor in URL must be nonnegative")
        if cursor is None:
            cursor = parsed_after if parsed_after is not None else 0
        if parsed_after is not None and parsed_after != cursor:
            raise PeerProtocolRefused("cursor argument disagrees with the URL")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise PeerProtocolRefused("cursor must be a nonnegative integer")
        if limit is None:
            raw_limit = query.get("limit")
            if raw_limit is not None:
                try:
                    limit = int(raw_limit)
                except ValueError:
                    raise PeerProtocolRefused("limit in URL is not an integer")
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > MAX_PAGE_LIMIT
        ):
            raise PeerProtocolRefused("limit must be between 1 and 500")
        try:
            body, headers, status = self._request(url)
        except RemoteSourceRefusal:
            raise
        try:
            payload, resolved_chain = self._decode_body(body, chain_id, cursor, status)
        except ResetRequired:
            record = self._chain_state(chain_id) if chain_id else None
            if record is not None and record["next_seq"] > 1:
                raise LossAfterCommit(
                    "peer reset the export after a committed cursor",
                    {
                        "reason": "reset_required",
                        "chain_id": chain_id,
                        "committed_cursor": record["next_seq"] - 1,
                        "committed_next_seq": record["next_seq"],
                        "peer_cursor": cursor,
                        "peer_next_seq": cursor + 1,
                        "missing": list(range(cursor + 1, record["next_seq"])),
                        "mismatched": [],
                    },
                )
            raise
        report = self._evaluate(
            payload,
            headers,
            resolved_chain,
            cursor,
            strict=strict,
            require_fresh=require_fresh,
            allow_loss=allow_loss,
        )
        report["url"] = url
        self._touch_peer(True)
        return report

    def fetch(
        self,
        chain_id,
        cursor=0,
        limit=DEFAULT_PAGE_LIMIT,
        *,
        strict=False,
        require_fresh=False,
        allow_loss=False,
        after=None,
    ):
        if cursor is None and after is None:
            cursor = 0
        if after is not None:
            if cursor not in (0, None) and cursor != after:
                raise PeerProtocolRefused("cursor and after arguments disagree")
            cursor = after
        url = self.export_url(chain_id, cursor=cursor, limit=limit)
        return self.fetch_url(
            url,
            chain_id=chain_id,
            cursor=cursor,
            limit=limit,
            strict=strict,
            require_fresh=require_fresh,
            allow_loss=allow_loss,
        )

    def replay_after_commit(
        self, chain_id=None, *, report=None, require_fresh=False, allow_loss=False
    ):
        if isinstance(chain_id, Mapping):
            report = chain_id
            chain_id = None
        if chain_id is None and isinstance(report, Mapping):
            chain_id = report.get("chain_id")
        if chain_id is None:
            chains = sorted(self._state.get("chains", {}))
            if len(chains) != 1:
                raise RemoteSourceRefusal(
                    "replay needs exactly one known chain or an explicit chain_id",
                    code="replay",
                )
            chain_id = chains[0]
        record = self._chain_state(chain_id)
        report = self.fetch_url(
            self.export_url(chain_id, cursor=0),
            chain_id=chain_id,
            cursor=0,
            require_fresh=require_fresh,
            allow_loss=allow_loss,
        )
        report["replay_from_committed_cursor"] = record["next_seq"] - 1
        return report

    def commit(self, report):
        if not isinstance(report, Mapping):
            raise RemoteSourceRefusal("commit requires an export report", code="commit")
        if report.get("loss"):
            raise LossAfterCommit(
                "refusing to commit a report that lost committed data",
                report.get("loss"),
            )
        if report.get("gaps"):
            raise ExportGapDetected(
                "refusing to commit across an export gap",
                report.get("gaps"),
                report=dict(report),
            )
        chain_id = report.get("chain_id")
        if not isinstance(chain_id, str) or not chain_id:
            raise PeerProtocolRefused("report does not identify a chain")
        next_seq = report.get("next_seq")
        if isinstance(next_seq, bool) or not isinstance(next_seq, int):
            raise PeerProtocolRefused("report does not carry a next sequence")
        record = self._chain_state(chain_id)
        if next_seq < record["next_seq"]:
            raise LossAfterCommit(
                "commit sequence moved backwards",
                {
                    "reason": "commit_cursor_backwards",
                    "chain_id": chain_id,
                    "committed_cursor": record["next_seq"] - 1,
                    "committed_next_seq": record["next_seq"],
                    "peer_cursor": next_seq - 1,
                    "peer_next_seq": next_seq,
                    "missing": list(range(next_seq, record["next_seq"])),
                    "mismatched": [],
                },
            )
        for text, origin_hash in zip(
            report.get("events") or [], report.get("origin_hashes") or []
        ):
            envelope = json.loads(text)
            seq = str(envelope["seq"])
            known = record["hashes"].get(seq)
            if known is not None and known != origin_hash:
                raise LossAfterCommit(
                    "commit would overwrite a different committed hash",
                    {
                        "reason": "hash_mismatch",
                        "chain_id": chain_id,
                        "committed_cursor": record["next_seq"] - 1,
                        "committed_next_seq": record["next_seq"],
                        "peer_cursor": next_seq - 1,
                        "peer_next_seq": next_seq,
                        "missing": [],
                        "mismatched": [int(seq)],
                    },
                )
            record["hashes"][seq] = origin_hash
        if report.get("head_hash"):
            record["head_hash"] = report["head_hash"]
        if report.get("head") and isinstance(report["head"], Mapping):
            record["head_hash"] = report["head"].get("hash")
        elif report.get("origin_hashes"):
            record["head_hash"] = report["origin_hashes"][-1]
        record["next_seq"] = max(record["next_seq"], next_seq)
        record["committed_at"] = rails_contracts.utc_now()
        self._save_state()
        report = dict(report)
        report["committed"] = True
        return {
            "chain_id": chain_id,
            "cursor": record["next_seq"] - 1,
            "next_seq": record["next_seq"],
            "head_hash": record["head_hash"],
            "count": len(record["hashes"]),
            "committed_at": record["committed_at"],
        }

    mark_committed = commit
    commit_export = commit

    def health(self):
        peer = self._peer_state()
        last_seen = peer.get("last_seen")
        if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
            return {
                "state": "unavailable",
                "health": "unavailable",
                "last_success": None,
                "last_seen": None,
                "last_seen_age_seconds": None,
                "stale": True,
                "fresh": False,
                "code": peer.get("last_error") or "never_seen",
            }
        age = _now_epoch(self._now) - float(last_seen)
        error = peer.get("last_error")
        stale = age > self.stale_after_seconds
        state = "unavailable" if error else ("stale" if stale else "live")
        return {
            "state": state,
            "health": state,
            "last_success": peer.get("last_success"),
            "last_seen": peer.get("last_seen_stamp") or _stamp(last_seen),
            "last_seen_age_seconds": age,
            "stale": bool(error) or stale,
            "fresh": not error and not stale,
            "code": error,
        }

    def describe(self):
        return {
            "adapter_id": ADAPTER_ID,
            "version": SOURCE_VERSION,
            "source_instance": self.peer_id,
            "capabilities": [
                "export-page",
                "origin-bytes",
                "gap-report",
                "stale-report",
                "post-commit-loss",
            ],
            "supported_formats": [EXPORT_SCHEMA],
        }

    def status(self, chain_id=None):
        if chain_id is None:
            return {
                "adapter": ADAPTER_ID,
                "peer_id": self.peer_id,
                "base_url": self.base_url,
                "stale_after_seconds": self.stale_after_seconds,
                "chains": sorted(self._state.get("chains", {})),
                "health": self.health(),
            }
        record = self._chain_state(chain_id)
        return {
            "adapter": ADAPTER_ID,
            "peer_id": self.peer_id,
            "base_url": self.base_url,
            "chain_id": chain_id,
            "cursor": record["next_seq"] - 1,
            "next_seq": record["next_seq"],
            "head_hash": record["head_hash"],
            "committed_seqs": len(record["hashes"]),
            "stale_after_seconds": self.stale_after_seconds,
        }

    def import_into(self, ledger, chain_id, cursor=0, limit=DEFAULT_PAGE_LIMIT):
        importer = getattr(ledger, "import_origin_page", None)
        if not callable(importer):
            raise RemoteSourceRefusal(
                "ledger does not expose import_origin_page", code="ledger"
            )
        report = self.fetch(chain_id, cursor=cursor, limit=limit)
        if report["gaps"]:
            raise ExportGapDetected(
                "refusing to import across an export gap",
                report["gaps"],
                report=report,
            )
        if report["events"]:
            result = importer(chain_id, report["origin_bytes"])
        else:
            result = {"imported": 0, "through_seq": cursor, "duplicated": True}
        report["ledger"] = result
        self.commit(report)
        return report

    def _draft(self, kind, chain_id, record_id, revision, payload):
        return {
            "kind": kind,
            "source": {
                "adapter": ADAPTER,
                "instance": self.peer_id,
                "record_id": record_id,
                "revision": revision,
            },
            "scope": {"session_id": "rails-remote"},
            "payload": payload,
            "authority": "imported_observation",
            "lane": {
                "lane_id": None,
                "parent_lane_id": None,
                "parent_event_id": None,
            },
            "occurred_at": None,
            "duration_ms": None,
            "timing_quality": "unknown",
            "redaction_policy": {"mode": "structured_safe", "path_detail": "omitted"},
            "dedupe_key": "rails-remote:"
            + kind
            + ":"
            + chain_id
            + ":"
            + record_id,
        }

    def emit_events(self, report):
        chain_id = report["chain_id"]
        revision = report.get("head_hash") or _unavailable_revision()
        drafts = []
        if report.get("gaps"):
            for gap in report["gaps"]:
                drafts.append(
                    self._draft(
                        "source.gap",
                        chain_id,
                        "gap:"
                        + chain_id
                        + ":"
                        + str(gap["start"])
                        + "-"
                        + str(gap["end"]),
                        revision,
                        {
                            "range": [gap["start"], gap["end"]],
                            "cursor": gap["end"],
                            "code": "export_gap",
                            "count": 0,
                            "digest": _empty_digest(),
                        },
                    )
                )
        elif report.get("events"):
            first = report["range"][0]
            last = report["range"][1]
            drafts.append(
                self._draft(
                    "source.checkpoint",
                    chain_id,
                    "remote:" + chain_id + ":" + str(last),
                    revision,
                    {
                        "range": [first, last],
                        "cursor": last,
                        "code": "imported",
                        "count": report["count"],
                        "digest": _digest_of(report["origin_bytes"]),
                    },
                )
            )
        if report.get("stale"):
            if report.get("range"):
                span = list(report["range"])
            else:
                request = report.get("requested_cursor") or 0
                span = [request + 1, request + 1]
            drafts.append(
                self._draft(
                    "source.error",
                    chain_id,
                    "stale:"
                    + chain_id
                    + ":"
                    + str(report.get("next_cursor") or 0),
                    revision,
                    {
                        "range": span,
                        "cursor": report.get("next_cursor"),
                        "code": "stale_host",
                        "count": report.get("count") or 0,
                        "digest": (
                            _digest_of(report["origin_bytes"])
                            if report.get("origin_bytes")
                            else _empty_digest()
                        ),
                    },
                )
            )
        if report.get("loss"):
            loss = report["loss"]
            missing = loss.get("missing") or []
            if missing:
                span = [min(missing), max(missing)]
            else:
                peer = loss.get("peer_cursor") or 0
                committed = loss.get("committed_cursor") or peer
                span = [peer + 1, max(peer + 1, committed)]
            drafts.append(
                self._draft(
                    "source.error",
                    chain_id,
                    "loss:" + chain_id + ":" + str(loss.get("peer_cursor") or 0),
                    revision,
                    {
                        "range": span,
                        "cursor": loss.get("peer_cursor"),
                        "code": "loss_after_commit",
                        "count": 0,
                        "digest": _empty_digest(),
                    },
                )
            )
        for draft in drafts:
            if frozenset(draft) != _EVENT_DRAFT_KEYS:
                raise PeerProtocolRefused("event draft shape is not the frozen schema")
        return drafts

    checkpoint_events = emit_events
    fetch_export = fetch
    get_export = fetch


OffPolicyRefused = OffPolicyUrlRefused
ArbitraryUrlRefused = OffPolicyUrlRefused
ExportGap = ExportGapDetected
StaleHost = StaleHostDetected
CommittedLoss = LossAfterCommit
RemotePeerRailSource = RemoteRailSource
RemoteSource = RemoteRailSource
RemoteSourceAdapter = RemoteRailSource
RemoteSourceError = RemoteSourceRefusal

__all__ = [
    "ADAPTER",
    "ADAPTER_ID",
    "SOURCE_VERSION",
    "EXPORT_SCHEMA",
    "DEFAULT_STALE_AFTER_SECONDS",
    "RemoteRailSource",
    "RemotePeerRailSource",
    "RemoteSource",
    "RemoteSourceAdapter",
    "RemoteSourceError",
    "RemoteSourceRefusal",
    "EnrollmentRefused",
    "OffPolicyUrlRefused",
    "OffPolicyRefused",
    "ArbitraryUrlRefused",
    "RedirectRefused",
    "OriginBytesRefused",
    "PeerProtocolRefused",
    "PeerUnavailable",
    "PeerError",
    "ResetRequired",
    "ExportGapDetected",
    "ExportGap",
    "SourceGapRefused",
    "StaleHostDetected",
    "StaleHost",
    "LossAfterCommit",
    "CommittedLoss",
    "SourceError",
    "HttpResponse",
    "UrllibTransport",
]
