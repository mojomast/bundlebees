"""HTTP clients for the Orbit loopback adapter and a user-configured launcher.

The target URL is always configuration, never a tool argument. Tokens come only
from a caller-supplied ``get_secret`` provider. Results are mapped through the
``public_*`` helpers so upstream token files, bind addresses, hosts, and private
paths cannot reach a model-visible envelope.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from .security import (
    UnsafeURLError,
    is_public_git_url,
    scrub,
    validate_url,
)

DEFAULT_ORBIT_URL = "http://127.0.0.1:4318"
TOKEN_SECRET_NAME = "BUNDLEBEES_LAUNCHER_TOKEN"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 1_048_576
MAX_REDIRECTS = 5
USER_AGENT = "bundlebees/0.1.0"

_FIX_LAUNCHER = (
    "Confirm the worker launcher is running and that the endpoint and token are "
    "configured in the bundlebees plugin settings. Do not paste tokens into chat."
)
_FIX_PROJECT = (
    "The owner must add this project to the launcher config and restart the "
    "launcher; tools do not restart it."
)


class ClientError(Exception):
    """A mapped client failure that can be serialized as a tool envelope."""

    def __init__(
        self,
        error: str,
        code: str = "launcher_error",
        fix: str | None = None,
        *,
        status: int | None = None,
    ) -> None:
        clean = str(scrub(str(error)))
        super().__init__(clean)
        self.error = clean
        self.code = code
        self.fix = fix or _FIX_LAUNCHER
        self.status = status

    def as_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "code": self.code,
            "fix": self.fix,
        }


class NotConfigured(ClientError):
    def __init__(self, error: str = "the launcher is not configured", fix: str | None = None):
        super().__init__(
            error,
            "not_configured",
            fix
            or (
                "Set BUNDLEBEES_LAUNCHER_TOKEN in Hermes secrets and choose an "
                "adapter/endpoint in the bundlebees plugin settings, then retry."
            ),
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    # An empty ProxyHandler disables ambient HTTP(S)_PROXY configuration.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _unwrap_attempt(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in ("attempt", "data", "worker"):
            nested = data.get(key)
            if isinstance(nested, dict):
                return nested
        return data
    return {}


def _as_list(data: Any, *keys: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return list(value.values())
    return []


# ---------------------------------------------------------------------------
# Public (sanitized) mappings
# ---------------------------------------------------------------------------


def _model_ids(data: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    model = data.get("model")
    if isinstance(model, str) and model:
        found.append(model)
    elif isinstance(model, dict):
        ident = model.get("id") or model.get("name") or model.get("model")
        if isinstance(ident, str) and ident:
            found.append(ident)
    for item in data.get("models") or []:
        if isinstance(item, str) and item:
            found.append(item)
        elif isinstance(item, dict):
            ident = item.get("id") or item.get("name") or item.get("model")
            if isinstance(ident, str) and ident:
                found.append(ident)
    return sorted(set(found))


def public_health(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    if "ok" in data:
        out["ok"] = bool(data.get("ok"))
    for key in ("version", "max_concurrent", "active", "queued", "paused"):
        if key in data:
            out[key] = data[key]
    models = _model_ids(data)
    if models:
        out["model"] = models[0]
        out["models"] = models
    return out


def public_attempt(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    attempt_id = data.get("attempt_id") or data.get("id")
    if attempt_id is not None:
        out["attempt_id"] = str(attempt_id)
    for key in (
        "task_id",
        "project",
        "branch",
        "model",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "queued_at",
        "event_cursor",
        "cursor",
        "kanban_task_id",
    ):
        if key in data and data[key] is not None:
            out[key] = data[key]
    state = data.get("state") or data.get("status")
    if state is not None:
        out["state"] = state
    for key in ("detail", "error"):
        if key in data and data[key] is not None:
            out[key] = scrub(str(data[key]))
    return out


def public_event(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    for key in (
        "seq",
        "cursor",
        "event_cursor",
        "ts",
        "timestamp",
        "level",
        "kind",
        "type",
        "event_id",
    ):
        if key in data and data[key] is not None:
            out[key] = data[key]
    text = data.get("text") or data.get("message") or data.get("detail")
    if text is not None:
        out["text"] = scrub(str(text))
    out["untrusted"] = True
    return out


def public_events(payload: Any) -> list[dict[str, Any]]:
    return [public_event(item) for item in _as_list(payload, "events", "items")]


def public_project(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    key = data.get("key") or data.get("project") or data.get("name")
    if key is not None:
        out["key"] = str(key)
    repo = data.get("repo") or data.get("repository") or data.get("url")
    if isinstance(repo, str) and is_public_git_url(repo):
        out["repo"] = repo
    sync_branch = data.get("sync_branch") or data.get("branch")
    if sync_branch is not None:
        out["sync_branch"] = str(sync_branch)
    return out


def public_container(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    digest = data.get("digest") or data.get("image_digest")
    if isinstance(digest, str) and digest:
        out["digest"] = digest
    else:
        image = data.get("image")
        if isinstance(image, str) and "@" in image:
            candidate = image.rsplit("@", 1)[1]
            if candidate.startswith("sha256:"):
                out["digest"] = candidate
    for key in ("runtime", "memory_mb"):
        if key in data and data[key] is not None:
            out[key] = data[key]
    toolchain = data.get("toolchain") or data.get("toolchains")
    if isinstance(toolchain, (list, tuple)):
        names = []
        for item in toolchain:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("id")
                if isinstance(name, str):
                    names.append(name)
        if names:
            out["toolchain"] = names
    return out


def _safe_rel_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts) if parts else None


def _public_check(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    for key in ("name", "ok", "status", "passed", "duration_s"):
        if key in data and data[key] is not None:
            out[key] = data[key]
    detail = data.get("detail") or data.get("message")
    if detail is not None:
        out["detail"] = scrub(str(detail))
    return out


def public_result(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    if isinstance(data.get("result"), dict):
        data = data["result"]
    out: dict[str, Any] = {}
    attempt_id = data.get("attempt_id") or data.get("id")
    if attempt_id is not None:
        out["attempt_id"] = str(attempt_id)
    state = data.get("state") or data.get("status")
    if state is not None:
        out["state"] = state

    observed = data.get("observed") if isinstance(data.get("observed"), dict) else {}
    obs: dict[str, Any] = {}
    files = observed.get("changed_files") or data.get("changed_files") or data.get("files")
    if isinstance(files, (list, tuple)):
        safe_files = [p for p in (_safe_rel_path(item) for item in files) if p]
        obs["changed_files"] = safe_files
    usage = observed.get("usage")
    if isinstance(usage, dict):
        obs["usage"] = scrub(usage)
    stats = observed.get("stats")
    if isinstance(stats, dict):
        obs["stats"] = scrub(stats)
    sha = observed.get("sha256")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        obs["sha256"] = sha
    if obs:
        out["observed"] = obs

    checks = data.get("checks")
    if isinstance(checks, (list, tuple)):
        out["checks"] = [_public_check(item) for item in checks]
    # worker_claims is a worker self-report, not evidence; omit it entirely.
    return out


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class _BaseClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        token_provider: Callable[[], str | None] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = MAX_RESPONSE_BYTES,
        allow_loopback: bool = True,
        allow_private: bool = False,
    ) -> None:
        if token_provider is None:
            if token is not None:
                token_provider = lambda: token  # noqa: E731
            else:
                token_provider = lambda: None  # noqa: E731
        self._token_provider = token_provider
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._allow_loopback = allow_loopback
        self._allow_private = allow_private
        self._base_url = str(base_url).rstrip("/")
        self._validate(self._base_url)

    # -- policy ------------------------------------------------------------

    def _validate(self, url: str) -> None:
        try:
            validate_url(
                url,
                allow_loopback=self._allow_loopback,
                allow_private=self._allow_private,
            )
        except UnsafeURLError as exc:
            raise ClientError(
                "launcher endpoint is not allowed by the address policy",
                "unsafe_url",
                "Use the loopback Orbit adapter, or set a launcher endpoint in "
                "the plugin settings. URLs are never taken from tool arguments.",
            ) from exc

    def _token(self) -> str:
        token = self._token_provider()
        if token is None:
            raise NotConfigured()
        token = str(token).strip()
        if not token:
            raise NotConfigured()
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _absolute(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self._base_url + "/" + path.lstrip("/")

    # -- transport ---------------------------------------------------------

    def _decode(self, data: bytes) -> Any:
        if not data:
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientError(
                "launcher returned an invalid JSON response",
                "launcher_error",
                _FIX_LAUNCHER,
            ) from exc

    def _http_error(self, status: int, data: bytes) -> ClientError:
        message = "launcher request failed"
        try:
            payload = json.loads(data.decode("utf-8")) if data else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            candidate = payload.get("error") or payload.get("message") or payload.get("detail")
            if candidate:
                message = str(candidate)
        if status == 401:
            code, fix = "unauthorized", (
                "Check the launcher token in Hermes secrets. Do not paste the "
                "token into chat."
            )
        elif status == 403:
            code, fix = "forbidden", _FIX_PROJECT
        elif status == 404:
            code, fix = "not_found", "Check the attempt id and launcher configuration."
        elif status == 409:
            code, fix = "no_result_yet", "Wait for the worker to finish, then retry."
        elif status == 413:
            code, fix = "response_too_large", _FIX_LAUNCHER
        elif status >= 500:
            code, fix = "launcher_unavailable", _FIX_LAUNCHER
        else:
            code, fix = "launcher_error", _FIX_LAUNCHER
        return ClientError(message, code, fix, status=status)

    def request_json(self, method: str, path: str, payload: Any = None) -> Any:
        url = self._absolute(path)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        for _ in range(MAX_REDIRECTS + 1):
            self._validate(url)
            request = urllib.request.Request(
                url,
                data=body,
                method=method.upper(),
                headers=self._headers(),
            )
            try:
                with _build_opener().open(request, timeout=self._timeout) as response:
                    data = response.read(self._max_bytes + 1)
                    if len(data) > self._max_bytes:
                        raise ClientError(
                            "launcher response exceeded the size limit",
                            "response_too_large",
                            _FIX_LAUNCHER,
                        )
                    return self._decode(data)
            except urllib.error.HTTPError as exc:
                data = exc.read(self._max_bytes + 1)
                status = exc.code
                location = exc.headers.get("Location") if exc.headers else None
                exc.close()
                if len(data) > self._max_bytes:
                    raise ClientError(
                        "launcher response exceeded the size limit",
                        "response_too_large",
                        _FIX_LAUNCHER,
                    ) from exc
                if status in (301, 302, 303, 307, 308):
                    if not location:
                        raise ClientError(
                            "launcher returned a redirect without a location",
                            "launcher_unavailable",
                            _FIX_LAUNCHER,
                        ) from exc
                    url = urllib.parse.urljoin(url, location)
                    # Re-validate immediately; the next loop also validates.
                    self._validate(url)
                    if status == 303 or (
                        status in (301, 302) and method.upper() not in ("GET", "HEAD")
                    ):
                        method = "GET"
                        body = None
                    continue
                raise self._http_error(status, data) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ClientError(
                    "launcher is unreachable",
                    "launcher_unavailable",
                    _FIX_LAUNCHER,
                ) from exc
        raise ClientError(
            "launcher returned too many redirects",
            "launcher_unavailable",
            _FIX_LAUNCHER,
        )


def _quote_id(attempt_id: str) -> str:
    return urllib.parse.quote(str(attempt_id), safe="")


class LauncherClient(_BaseClient):
    """Direct launcher client using the documented ``/v1`` HTTP API."""

    def health(self) -> dict[str, Any]:
        return public_health(self.request_json("GET", "/v1/health"))

    def capacity(self) -> dict[str, Any]:
        return self.health()

    def attempts_raw(self) -> list[Any]:
        data = self.request_json("GET", "/v1/attempts")
        return _as_list(data, "attempts", "items")

    def attempts(self) -> list[dict[str, Any]]:
        return [public_attempt(item) for item in self.attempts_raw()]

    def get_raw(self, attempt_id: str) -> Any:
        return self.request_json("GET", f"/v1/attempts/{_quote_id(attempt_id)}")

    def get(self, attempt_id: str) -> dict[str, Any]:
        return public_attempt(_unwrap_attempt(self.get_raw(attempt_id)))

    def events(self, attempt_id: str, cursor: int | str | None = None) -> list[dict[str, Any]]:
        path = f"/v1/attempts/{_quote_id(attempt_id)}/events"
        if cursor is not None:
            path += "?cursor=" + urllib.parse.quote(str(cursor), safe="")
        return public_events(self.request_json("GET", path))

    def result(self, attempt_id: str) -> dict[str, Any]:
        try:
            data = self.request_json(
                "GET", f"/v1/attempts/{_quote_id(attempt_id)}/result"
            )
        except ClientError as exc:
            if exc.status == 409 or exc.code == "no_result_yet":
                raise ClientError(
                    "no result yet",
                    "no_result_yet",
                    "Wait for the worker to finish, then retry.",
                    status=409,
                ) from exc
            raise
        return public_result(data)

    def create(
        self,
        brief: Mapping[str, Any],
        *,
        model: str | None = None,
        kanban_task_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"brief": dict(brief)}
        if model:
            payload["model"] = model
        if kanban_task_id:
            payload["kanban_task_id"] = kanban_task_id
        try:
            data = self.request_json("POST", "/v1/attempts", payload)
        except ClientError as exc:
            if exc.status == 403:
                raise ClientError(
                    "project is not authorized on the launcher",
                    "project_unauthorized",
                    _FIX_PROJECT,
                    status=403,
                ) from exc
            raise
        return public_attempt(_unwrap_attempt(data))

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        data = self.request_json(
            "POST", f"/v1/attempts/{_quote_id(attempt_id)}/cancel", {}
        )
        return public_attempt(_unwrap_attempt(data))

    def review(
        self, attempt_id: str, *, accepted: bool, note: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"accepted": bool(accepted)}
        if note:
            payload["note"] = scrub(str(note))
        data = self.request_json(
            "POST", f"/v1/attempts/{_quote_id(attempt_id)}/review", payload
        )
        out = public_attempt(_unwrap_attempt(data))
        if isinstance(data, dict) and "accepted" in data:
            out["accepted"] = bool(data.get("accepted"))
        return out

    def projects(self) -> list[dict[str, Any]]:
        data = self.request_json("GET", "/v1/projects")
        return [public_project(item) for item in _as_list(data, "projects", "items")]

    def containers(self) -> list[dict[str, Any]]:
        data = self.request_json("GET", "/v1/containers")
        return [public_container(item) for item in _as_list(data, "containers", "items")]


class OrbitAdapterClient(_BaseClient):
    """Loopback adapter using ``POST /api/local-workers`` with an ``op`` field."""

    def _op(self, op: str, **payload: Any) -> Any:
        body = {"op": op}
        body.update(payload)
        return self.request_json("POST", "/api/local-workers", body)

    def _state(self) -> dict[str, Any]:
        data = self._op("list")
        return data if isinstance(data, dict) else {}

    def health(self) -> dict[str, Any]:
        return public_health(self._state())

    def capacity(self) -> dict[str, Any]:
        return self.health()

    def attempts_raw(self) -> list[Any]:
        return _as_list(self._state(), "attempts", "items")

    def attempts(self) -> list[dict[str, Any]]:
        return [public_attempt(item) for item in self.attempts_raw()]

    def get_raw(self, attempt_id: str) -> Any:
        return self._op("get", attempt_id=attempt_id)

    def get(self, attempt_id: str) -> dict[str, Any]:
        return public_attempt(_unwrap_attempt(self.get_raw(attempt_id)))

    def events(self, attempt_id: str, cursor: int | str | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"attempt_id": attempt_id}
        if cursor is not None:
            payload["cursor"] = cursor
        return public_events(self._op("get", **payload))

    def result(self, attempt_id: str) -> dict[str, Any]:
        data = self._op("get", attempt_id=attempt_id)
        if isinstance(data, dict):
            for key in ("result", "data"):
                if isinstance(data.get(key), dict):
                    return public_result(data[key])
        nested = _unwrap_attempt(data)
        if nested and (nested.get("observed") or nested.get("checks")):
            return public_result(nested)
        raise ClientError(
            "no result yet",
            "no_result_yet",
            "Wait for the worker to finish, then retry.",
            status=409,
        )

    def create(
        self,
        brief: Mapping[str, Any],
        *,
        model: str | None = None,
        kanban_task_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"brief": dict(brief)}
        if model:
            payload["model"] = model
        if kanban_task_id:
            payload["kanban_task_id"] = kanban_task_id
        try:
            data = self._op("create", **payload)
        except ClientError as exc:
            if exc.status == 403:
                raise ClientError(
                    "project is not authorized on the launcher",
                    "project_unauthorized",
                    _FIX_PROJECT,
                    status=403,
                ) from exc
            raise
        return public_attempt(_unwrap_attempt(data))

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        return public_attempt(_unwrap_attempt(self._op("cancel", attempt_id=attempt_id)))

    def review(
        self, attempt_id: str, *, accepted: bool, note: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"attempt_id": attempt_id, "accepted": bool(accepted)}
        if note:
            payload["note"] = scrub(str(note))
        data = self._op("review", **payload)
        out = public_attempt(_unwrap_attempt(data))
        if isinstance(data, dict) and "accepted" in data:
            out["accepted"] = bool(data.get("accepted"))
        return out

    def projects(self) -> list[dict[str, Any]]:
        return [public_project(item) for item in _as_list(self._state(), "projects", "items")]

    def containers(self) -> list[dict[str, Any]]:
        return [
            public_container(item)
            for item in _as_list(self._state(), "containers", "items")
        ]


# ---------------------------------------------------------------------------
# Configuration / secret plumbing
# ---------------------------------------------------------------------------


def token_from_agent(
    agent: Any, name: str = TOKEN_SECRET_NAME
) -> str | None:
    """Read a secret only through ``agent.secret_scope.get_secret``."""
    scope = getattr(agent, "secret_scope", None)
    if scope is None:
        return None
    getter = getattr(scope, "get_secret", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:  # fail closed; never fall back to env/files
        return None


def client_from_agent(
    agent: Any = None,
    config: Mapping[str, Any] | None = None,
) -> _BaseClient:
    """Build the preferred client from non-secret config plus ``get_secret``."""
    config = config or {}
    provider = lambda: token_from_agent(agent)  # noqa: E731
    adapter = str(config.get("adapter") or "orbit-loopback").strip().lower()
    if adapter in {"launcher", "direct", "http", "https"}:
        url = config.get("launcher_url") or config.get("url")
        if not url:
            raise NotConfigured(
                "launcher endpoint is not configured",
                "Set the launcher URL in the bundlebees plugin settings. Tool "
                "arguments never carry a URL.",
            )
        return LauncherClient(
            str(url),
            token_provider=provider,
            allow_loopback=True,
            allow_private=bool(config.get("allow_private", True)),
        )
    url = (
        config.get("orbit_url")
        or config.get("orbit_loopback_url")
        or DEFAULT_ORBIT_URL
    )
    return OrbitAdapterClient(
        str(url),
        token_provider=provider,
        allow_loopback=True,
        allow_private=False,
    )
