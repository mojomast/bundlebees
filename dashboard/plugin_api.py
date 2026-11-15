"""Dashboard backend for the bundlebees Desktop pane.

Every route is namespace-relative (the host mounts this router under
``/api/plugins/bundlebees`` because ``dashboard/manifest.json`` declares
``"api": "plugin_api.py"``) and returns a JSON envelope. The launcher client
is built lazily on each request from ``plugins.entries.bundlebees.settings``
(``hermes_cli.config.load_config_readonly``) plus
``agent.secret_scope.get_secret``, which Hermes scopes to the requesting
profile for plugin routes. The backend never
accepts a launcher URL, token, or host from a request; it reuses the plugin's
preconfigured client and the shared scrubbers so production payloads cannot
carry secrets, bind addresses, private URLs, or local filesystem paths.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse

# Hermes imports this file by path (``spec_from_file_location``), so the plugin
# root is not guaranteed to be on sys.path in the dashboard process.
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from bundlebees_plugin.client import (  # noqa: E402
    ClientError,
    client_from_config,
    public_attempt,
    public_container,
    public_events,
    public_health,
    public_project,
    public_result,
)
from bundlebees_plugin.security import contain_path, scrub  # noqa: E402
from bundlebees_plugin.settings import settings_from_hermes_config  # noqa: E402

router = APIRouter()

# ---------------------------------------------------------------------------
# Backend / data-directory injection (non-secret configuration only)
# ---------------------------------------------------------------------------

_backend: Any | None = None
_backend_factory: Callable[[], Any] | None = None
_data_dir: Path | None = None

_CONCURRENCY_NOTE = (
    "Runtime concurrency applies until the launcher restarts; the disk config "
    "remains the source of truth."
)
_DEPLOY_COMMAND = "hermes bundlebees deploy"
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_UNIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]+")


def _scrub_text(value: Any) -> str:
    text = str(scrub(str(value)))
    text = _UNIX_PATH_RE.sub("[redacted-path]", text)
    return _WIN_PATH_RE.sub("[redacted-path]", text)


class DashboardError(Exception):
    """A structured, scrubbed pane error."""

    def __init__(
        self,
        error: str,
        code: str = "dashboard_error",
        fix: str | None = None,
        status: int = 400,
    ) -> None:
        self.error = _scrub_text(error)
        self.code = code
        self.fix = _scrub_text(
            fix
            or "Check the bundlebees launcher configuration, then retry. "
            "Never paste a token into chat or the pane."
        )
        self.status = status
        super().__init__(self.error)

    def as_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "code": self.code,
            "fix": self.fix,
        }


class MissingBackend(DashboardError):
    def __init__(self) -> None:
        super().__init__(
            "the bundlebees dashboard backend is not configured",
            "not_configured",
            "Enable the bundlebees plugin backend and configure the launcher "
            "token in Hermes secrets; the pane only reports configured or "
            "not configured and never receives the value.",
            503,
        )


def set_backend(client: Any) -> None:
    global _backend
    _backend = client


def set_client(client: Any) -> None:
    """Alias used by older dashboard harnesses."""
    set_backend(client)


def set_backend_factory(factory: Callable[[], Any]) -> None:
    global _backend_factory
    _backend_factory = factory


def configure_backend(config: Mapping[str, Any] | None = None) -> None:
    """Pin a client built from explicit settings (tests and harnesses only).

    Production does not call this: ``_get_backend`` builds a client lazily on
    each request from the Hermes config, so a settings change applies at once.
    """
    set_backend(client_from_config(config or {}))


def _default_backend() -> Any:
    """The production client: Hermes settings plus the secret scope."""
    return client_from_config(settings_from_hermes_config())


def set_data_dir(path: str | os.PathLike[str]) -> None:
    global _data_dir
    _data_dir = Path(path).expanduser().resolve()


def reset_backend() -> None:
    global _backend, _backend_factory
    _backend = None
    _backend_factory = None


def reset_state() -> None:
    global _data_dir
    reset_backend()
    _data_dir = None


def _get_backend() -> Any:
    global _backend
    if _backend is not None:
        return _backend
    if _backend_factory is None:
        try:
            return _default_backend()
        except DashboardError:
            raise
        except ClientError as exc:
            raise _client_error(exc) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise DashboardError(
                f"the bundlebees backend could not start: {exc}",
                "not_configured",
                "Enable the bundlebees plugin and configure the launcher in "
                "Hermes settings, then reload the pane.",
                503,
            ) from exc
    if _backend_factory is not None:
        try:
            _backend = _backend_factory()
        except DashboardError:
            raise
        except ClientError as exc:
            raise _client_error(exc) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise DashboardError(
                f"the bundlebees backend could not start: {exc}",
                "not_configured",
                "Enable the plugin backend and configure the launcher in "
                "Hermes settings, then reload the pane.",
                503,
            ) from exc
        if _backend is not None:
            return _backend
    raise MissingBackend()


def _client_error(exc: ClientError) -> DashboardError:
    status = exc.status
    if not isinstance(status, int) or not 400 <= status <= 599:
        status = {
            "not_configured": 503,
            "unauthorized": 401,
            "forbidden": 403,
            "not_found": 404,
            "no_result_yet": 409,
            "unsafe_url": 400,
            "launcher_unavailable": 503,
            "response_too_large": 502,
        }.get(exc.code, 502)
    return DashboardError(exc.error, exc.code, exc.fix, status)


def _run(builder: Callable[[], Mapping[str, Any]]) -> JSONResponse:
    try:
        payload = builder()
    except DashboardError as exc:
        return JSONResponse(exc.as_envelope(), status_code=exc.status)
    except ClientError as exc:
        mapped = _client_error(exc)
        return JSONResponse(mapped.as_envelope(), status_code=mapped.status)
    except Exception as exc:  # noqa: BLE001 - route boundary must always answer
        fallback = DashboardError(
            str(exc) or "unexpected dashboard error",
            "internal_error",
            "Retry once, then reload the bundlebees pane and check the backend log.",
            500,
        )
        return JSONResponse(fallback.as_envelope(), status_code=500)
    if not isinstance(payload, Mapping):
        payload = {"ok": False, "error": "invalid dashboard response"}
    return JSONResponse(dict(payload))


# ---------------------------------------------------------------------------
# Validation and sanitizing helpers
# ---------------------------------------------------------------------------


def _require_attempt_id(value: Any) -> str:
    attempt_id = str(value or "").strip()
    if not attempt_id or ".." in attempt_id or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise DashboardError(
            "attempt id is invalid",
            "invalid_argument",
            "Use the attempt id returned by the launcher; path segments and '..' "
            "are not accepted.",
        )
    return attempt_id


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _unwrap_attempt(data: Any) -> Mapping[str, Any]:
    if isinstance(data, Mapping):
        for key in ("attempt", "data", "worker"):
            nested = data.get(key)
            if isinstance(nested, Mapping):
                return nested
        return data
    return {}


def _safe_rel_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\\", "/").strip()
    if not text or text.startswith("/") or text.startswith("//") or _DRIVE_RE.match(text):
        return None
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) if parts else None


def _safe_branch(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or ".." in text or text.startswith("-"):
        return None
    return str(scrub(text))


def _safe_sha(value: Any) -> str | None:
    if isinstance(value, str) and _SHA_RE.fullmatch(value):
        return value
    return None


def _safe_check(value: Any) -> dict[str, Any] | None:
    data = _as_mapping(value)
    if not data:
        return None
    out: dict[str, Any] = {}
    name = data.get("name") or data.get("id")
    if isinstance(name, str) and name.strip():
        out["name"] = str(scrub(name.strip()))
    for key in ("ok", "passed"):
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if isinstance(data.get("status"), str):
        out["status"] = str(scrub(data["status"]))
    if isinstance(data.get("duration_s"), (int, float)) and not isinstance(
        data.get("duration_s"), bool
    ):
        out["duration_s"] = data["duration_s"]
    detail = data.get("detail") or data.get("message")
    if detail is not None:
        out["detail"] = str(scrub(str(detail)))
    return out or None


def _sanitize_attempt(data: Any) -> dict[str, Any]:
    return public_attempt(_unwrap_attempt(data))


def _sanitize_events(data: Any) -> list[dict[str, Any]]:
    return public_events(data)


def _sanitize_result(data: Any) -> dict[str, Any]:
    return public_result(data)


# ---------------------------------------------------------------------------
# Routes: health, containers, models
# ---------------------------------------------------------------------------


@router.get("/health")
def health() -> JSONResponse:
    def build() -> dict[str, Any]:
        client = _get_backend()
        return {
            "ok": True,
            "launcher_configured": True,
            "health": public_health(client.health()),
        }

    return _run(build)


@router.get("/containers")
def containers() -> JSONResponse:
    def build() -> dict[str, Any]:
        client = _get_backend()
        rows = client.containers()
        if not isinstance(rows, (list, tuple)):
            rows = []
        return {
            "ok": True,
            "containers": [public_container(item) for item in rows],
        }

    return _run(build)


@router.get("/models")
def models() -> JSONResponse:
    def build() -> dict[str, Any]:
        client = _get_backend()
        health_data = public_health(client.health())
        ids = list(health_data.get("models") or [])
        if not ids and health_data.get("model"):
            ids = [health_data["model"]]
        return {"ok": True, "models": ids}

    return _run(build)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _apply_concurrency(client: Any, value: int) -> dict[str, Any]:
    setter = getattr(client, "set_concurrency", None)
    if callable(setter):
        result = setter(value)
        return _as_mapping(result)
    # The Orbit loopback adapter does not expose a documented runtime config op.
    if callable(getattr(client, "_op", None)):
        raise DashboardError(
            "the Orbit loopback adapter does not support runtime concurrency changes",
            "unsupported",
            "Change max_concurrent in the launcher disk config and restart it "
            "from the worker host; the pane does not restart the launcher.",
            400,
        )
    requester = getattr(client, "request_json", None)
    if callable(requester):
        result = requester("POST", "/v1/config", {"max_concurrent": value})
        return _as_mapping(result)
    raise DashboardError(
        "this launcher adapter does not support runtime concurrency changes",
        "unsupported",
        "Edit the launcher config and restart it from the worker host; the "
        "pane does not restart the launcher.",
        400,
    )


@router.get("/concurrency")
def get_concurrency() -> JSONResponse:
    def build() -> dict[str, Any]:
        client = _get_backend()
        health_data = public_health(client.health())
        out: dict[str, Any] = {
            "ok": True,
            "max_concurrent": health_data.get("max_concurrent"),
            "persisted": False,
            "note": _CONCURRENCY_NOTE,
        }
        for key in ("active", "queued"):
            if health_data.get(key) is not None:
                out[key] = health_data[key]
        return out

    return _run(build)


@router.put("/concurrency")
def put_concurrency(payload: Any = Body(default=None)) -> JSONResponse:
    def build() -> dict[str, Any]:
        data = _as_mapping(payload)
        value = data.get("max_concurrent")
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 10
        ):
            raise DashboardError(
                "max_concurrent must be an integer from 1 to 10",
                "invalid_argument",
                "Choose a small concurrency limit and retry.",
            )
        client = _get_backend()
        result = _apply_concurrency(client, value)
        applied = result.get("max_concurrent") if isinstance(result, Mapping) else None
        if not isinstance(applied, int) or isinstance(applied, bool):
            applied = value
        return {
            "ok": True,
            "max_concurrent": applied,
            "persisted": False,
            "note": _CONCURRENCY_NOTE,
        }

    return _run(build)


# ---------------------------------------------------------------------------
# Projects and attempts
# ---------------------------------------------------------------------------


@router.get("/projects")
def projects() -> JSONResponse:
    def build() -> dict[str, Any]:
        client = _get_backend()
        rows = client.projects()
        if not isinstance(rows, (list, tuple)):
            rows = []
        return {"ok": True, "projects": [public_project(item) for item in rows]}

    return _run(build)


@router.get("/attempts")
def attempts() -> JSONResponse:
    def build() -> dict[str, Any]:
        client = _get_backend()
        rows = client.attempts()
        if not isinstance(rows, (list, tuple)):
            rows = []
        return {
            "ok": True,
            "attempts": [public_attempt(item) for item in rows],
            "untrusted": True,
        }

    return _run(build)


@router.get("/attempts/{attempt_id}")
def attempt_detail(attempt_id: str) -> JSONResponse:
    def build() -> dict[str, Any]:
        clean_id = _require_attempt_id(attempt_id)
        client = _get_backend()
        out: dict[str, Any] = {
            "ok": True,
            "attempt": _sanitize_attempt(client.get(clean_id)),
        }
        result_fn = getattr(client, "result", None)
        if callable(result_fn):
            try:
                result = result_fn(clean_id)
            except ClientError as exc:
                if exc.code != "no_result_yet":
                    raise
            else:
                out["result"] = _sanitize_result(result)
        return out

    return _run(build)


@router.get("/attempts/{attempt_id}/events")
def attempt_events(
    attempt_id: str, cursor: int | None = Query(default=None)
) -> JSONResponse:
    def build() -> dict[str, Any]:
        clean_id = _require_attempt_id(attempt_id)
        client = _get_backend()
        return {
            "ok": True,
            "attempt_id": clean_id,
            "cursor": cursor,
            "events": _sanitize_events(client.events(clean_id, cursor=cursor)),
            "untrusted": True,
        }

    return _run(build)


@router.post("/attempts/{attempt_id}/cancel")
def attempt_cancel(attempt_id: str, payload: Any = Body(default=None)) -> JSONResponse:
    def build() -> dict[str, Any]:
        clean_id = _require_attempt_id(attempt_id)
        data = _as_mapping(payload)
        if data.get("confirm") is not True:
            raise DashboardError(
                "cancel requires confirm=true",
                "confirm_required",
                "Confirm in the pane that you want to cancel this attempt, then retry.",
            )
        client = _get_backend()
        attempt = _sanitize_attempt(client.cancel(clean_id))
        out: dict[str, Any] = {
            "ok": True,
            "attempt_id": clean_id,
            "attempt": attempt,
        }
        if attempt.get("state") is not None:
            out["state"] = attempt["state"]
        return out

    return _run(build)


@router.post("/attempts/{attempt_id}/review")
def attempt_review(attempt_id: str, payload: Any = Body(default=None)) -> JSONResponse:
    def build() -> dict[str, Any]:
        clean_id = _require_attempt_id(attempt_id)
        data = _as_mapping(payload)
        if data.get("confirm") is not True:
            raise DashboardError(
                "review requires confirm=true",
                "confirm_required",
                "Confirm in the pane that you want to review this attempt, then retry.",
            )
        accepted = data.get("accepted")
        if not isinstance(accepted, bool):
            raise DashboardError(
                "review requires accepted to be true or false",
                "review_invalid",
                "Pass accepted=true to approve or accepted=false to reject.",
            )
        note = data.get("note")
        if note is not None and not isinstance(note, str):
            raise DashboardError(
                "note must be a string",
                "invalid_argument",
                "Pass a short text note or omit it.",
            )
        clean_note = str(scrub(note.strip())) if note and note.strip() else None
        client = _get_backend()
        attempt = _sanitize_attempt(
            client.review(clean_id, accepted=accepted, note=clean_note)
        )
        out: dict[str, Any] = {
            "ok": True,
            "attempt_id": clean_id,
            "accepted": accepted,
            "attempt": attempt,
        }
        if attempt.get("state") is not None:
            out["state"] = attempt["state"]
        return out

    return _run(build)


# ---------------------------------------------------------------------------
# Verify results (written by the user-run CLI)
# ---------------------------------------------------------------------------


def _default_data_dir() -> Path | None:
    """``plugins.plugin_storage.plugin_data_dir("bundlebees")`` (profile-aware)."""
    from bundlebees_plugin.cli import plugin_state_dir

    return plugin_state_dir()


def _data_root() -> Path | None:
    root = _data_dir or _default_data_dir()
    return root.resolve() if root is not None else None


def _sanitize_verify_record(data: Any, fallback_id: str) -> dict[str, Any] | None:
    record = _as_mapping(data)
    if not record:
        return None
    attempt_id = record.get("attempt_id") or fallback_id
    try:
        attempt_id = _require_attempt_id(attempt_id)
    except DashboardError:
        return None
    out: dict[str, Any] = {"attempt_id": attempt_id}
    for key in ("ok", "passed"):
        if isinstance(record.get(key), bool):
            out["ok" if key == "ok" else "passed"] = record[key]
    sha = _safe_sha(record.get("sha"))
    if sha:
        out["sha"] = sha
    branch = _safe_branch(record.get("branch"))
    if branch:
        out["branch"] = branch
    files = record.get("files")
    if isinstance(files, (list, tuple)):
        out["files"] = [
            clean for clean in (_safe_rel_path(item) for item in files) if clean
        ]
    tests = record.get("tests")
    if isinstance(tests, (list, tuple)):
        checks = [check for check in (_safe_check(item) for item in tests) if check]
        out["tests"] = checks
    at = record.get("at") or record.get("timestamp")
    if at is not None:
        out["at"] = str(scrub(str(at)))
    return out


def _load_verify_results(attempt_id: str | None = None) -> list[dict[str, Any]]:
    wanted = _require_attempt_id(attempt_id) if attempt_id else None
    root = _data_root()
    if root is None:
        return []
    directory = root / "verify-results"
    if not directory.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for candidate in sorted(directory.iterdir()):
        if not candidate.is_file() or candidate.suffix != ".json":
            continue
        try:
            safe_path = Path(contain_path(str(candidate), str(root)))
        except ValueError:
            continue
        if safe_path.parent != directory.resolve():
            continue
        if wanted and safe_path.stem != wanted:
            continue
        try:
            data = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        record = _sanitize_verify_record(data, safe_path.stem)
        if record is not None:
            results.append(record)
    return results


@router.get("/verify-results")
def verify_results(attempt_id: str | None = Query(default=None)) -> JSONResponse:
    def build() -> dict[str, Any]:
        return {"ok": True, "results": _load_verify_results(attempt_id)}

    return _run(build)


# ---------------------------------------------------------------------------
# Remote host prompt (same rules as bundlebees_host_prompt)
# ---------------------------------------------------------------------------


@router.get("/host-prompt")
def host_prompt_options() -> JSONResponse:
    def build() -> dict[str, Any]:
        return {
            "ok": True,
            "os_options": ["linux", "macos", "windows"],
            "reach_options": ["same-machine", "lan", "vpn", "ssh"],
            "max_concurrent": {"min": 1, "max": 10},
            "deploy_command": _DEPLOY_COMMAND,
            "note": (
                "Deploy is a user-run CLI command. The pane never asks for or "
                "displays a launcher token or SSH host."
            ),
        }

    return _run(build)


@router.post("/host-prompt")
def host_prompt_generate(payload: Any = Body(default=None)) -> JSONResponse:
    def build() -> dict[str, Any]:
        data = _as_mapping(payload)
        if not data:
            raise DashboardError(
                "host prompt fields are required",
                "invalid_argument",
                "Provide os, reach, provider, repos, and max_concurrent.",
            )
        try:
            from bundlebees_plugin.tools import _host_prompt
        except ImportError as exc:  # pragma: no cover - defensive
            raise DashboardError(
                "the host prompt generator is unavailable",
                "not_configured",
                "Reload the bundlebees plugin and retry.",
                503,
            ) from exc
        try:
            raw = _host_prompt(data)
            envelope = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise DashboardError(
                "the host prompt could not be built",
                "internal_error",
                "Check the fields and retry; do not include addresses or secrets.",
                500,
            ) from None
        if not isinstance(envelope, Mapping) or envelope.get("ok") is not True:
            error = "the host prompt fields are invalid"
            code = "invalid_argument"
            fix = "Provide non-secret fields only: OS, reach method, provider name, repos, and concurrency."
            if isinstance(envelope, Mapping):
                error = envelope.get("error") or error
                code = envelope.get("code") or code
                fix = envelope.get("fix") or fix
            raise DashboardError(error, code, fix, 400)
        prompt = envelope.get("prompt")
        if not isinstance(prompt, str):
            raise DashboardError(
                "the host prompt is empty",
                "internal_error",
                "Reload the bundlebees plugin and retry.",
                500,
            )
        return {"ok": True, "prompt": prompt, "deploy_command": _DEPLOY_COMMAND}

    return _run(build)


def _approval_state_dir() -> Path:
    """The Bundlebees plugin data directory, never a request-controlled path."""
    root = _data_dir or _default_data_dir()
    if root is None:
        raise DashboardError(
            "the bundlebees approval store is not configured",
            "not_configured",
            "Enable the bundlebees plugin in the active Hermes profile and reload the pane.",
            503,
        )
    return Path(root).expanduser().resolve()


def _approval_api():
    try:
        from bundlebees_plugin import approval
    except Exception as exc:  # pragma: no cover - import failure is a deployment fault
        raise DashboardError(
            "the bundlebees approval module is unavailable",
            "not_configured",
            "Install the bundlebees plugin version that includes owner approvals and retry.",
            503,
        ) from exc
    return approval


def _approval_error(exc: Exception) -> DashboardError:
    code = str(getattr(exc, "code", "") or "approval_error")
    message = str(getattr(exc, "message", "") or str(exc) or "approval failed")
    fix = str(getattr(exc, "fix", "") or "")
    if code in {"approval_conflict", "approval_consumed", "approval_scope_changed"}:
        status = 409
    elif code in {"approval_expired", "approval_missing", "approval_request_missing", "approval_receipt_missing"}:
        status = 410
    elif code in {"approval_owner_required", "approval_owner_capability_invalid"}:
        status = 403
    elif code in {"approval_key_unavailable"}:
        status = 503
    else:
        status = 400
    return DashboardError(message, code, fix, status)


def _approval_public(row: Mapping[str, Any]) -> dict[str, Any]:
    """Only the owner-facing card projection; no internal paths or credentials."""
    binding = row.get("binding") if isinstance(row.get("binding"), Mapping) else {}
    tasks = binding.get("tasks") if isinstance(binding.get("tasks"), list) else []
    task_rows = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        task_rows.append({
            "task_id": str(task.get("task_id") or ""),
            "objective": str(task.get("objective") or ""),
            "lease": [str(item) for item in (task.get("lease") or [])],
        })
    worker = binding.get("worker") if isinstance(binding.get("worker"), Mapping) else {}
    coordinator = binding.get("coordinator") if isinstance(binding.get("coordinator"), Mapping) else {}
    budgets = binding.get("budgets") if isinstance(binding.get("budgets"), Mapping) else {}
    stop = coordinator.get("stop") if isinstance(coordinator.get("stop"), Mapping) else {}
    restart = coordinator.get("restart") if isinstance(coordinator.get("restart"), Mapping) else {}
    return {
        "request_id": str(row.get("request_id") or ""),
        "request_digest": str(row.get("request_digest") or row.get("digest") or ""),
        "job_id": str(binding.get("job_id") or ""),
        "statement": str(row.get("statement") or binding.get("statement") or ""),
        "repo": str(binding.get("repo") or ""),
        "base_rev": str(binding.get("base_rev") or ""),
        "scope": [str(item) for item in (binding.get("scope") or [])],
        "tasks": task_rows,
        "worker_project": str(worker.get("project") or ""),
        "worker_profile": str(worker.get("profile") or ""),
        "code_rev": str(binding.get("code_rev") or ""),
        "bees": int(budgets.get("bees") or 0),
        "wall_clock_s": int(budgets.get("wall_clock_s") or 0),
        "attempts": int(budgets.get("attempts") or 0),
        "accepted_waves": int(budgets.get("accepted_waves") or 0),
        "coordinator_stop": dict(stop) if isinstance(stop, Mapping) else {"planned": stop},
        "restart": dict(restart) if isinstance(restart, Mapping) else {"planned": restart},
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "status": str(row.get("status") or "pending"),
    }


def _format_time(value: Any) -> str:
    try:
        import datetime

        if isinstance(value, str) and value.strip():
            parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        number = float(value)
        return datetime.datetime.fromtimestamp(number, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return str(value or "unknown")


def _approval_page(document: Mapping[str, Any], *, status: int = 200, notice: str = "") -> HTMLResponse:
    card = _approval_public(document)
    request_id = card["request_id"]
    digest = card["request_digest"]
    tasks = "".join(
        f"<li><strong>{html.escape(task['task_id'] or 'task')}</strong> — "
        f"{html.escape(task['objective'] or 'no objective')} "
        f"<span class='muted'>({html.escape(', '.join(task['lease']) or 'no lease')})</span></li>"
        for task in card["tasks"]
    ) or "<li>No task scope was recorded.</li>"
    stop_doc = card["coordinator_stop"]
    stop_text = html.escape(str(stop_doc.get("after") or stop_doc.get("planned") or "after the first dispatch"))
    if stop_doc.get("driver_exit_confirmed") is True:
        stop_text += "; the coordinator process exit is confirmed"
    restart_doc = card["restart"]
    if restart_doc.get("planned") is not None:
        restart_text = html.escape(str(restart_doc.get("planned")))
    else:
        restart_text = html.escape(
            f"{restart_doc.get('coordinator_restarts', 1)} restart(s), "
            f"one overall deadline of {restart_doc.get('deadline_s', 'unknown')} seconds"
        )
    expiry = html.escape(_format_time(card["expires_at"]))
    settled = card["status"] not in {"pending", "open"} or status != 200 or not digest
    if card["status"] == "expired" and not notice:
        notice = "This approval request has expired. Ask for a fresh preview; no decision can be recorded."
    buttons = (
        "<div class='actions'><button class='approve' data-decision='approve'>Approve once</button>"
        "<button class='decline' data-decision='decline'>Decline</button></div>"
        if not settled
        else ""
    )
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bundlebees job approval</title>
<style>
:root{{color-scheme:dark;font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}}
body{{margin:0;background:#0b0f14;color:#e6edf5}}
main{{max-width:760px;margin:0 auto;padding:28px 20px 42px}}
.card{{background:#111821;border:1px solid #243242;border-radius:12px;padding:20px 22px}}
h1{{font-size:22px;margin:0 0 8px}} h2{{font-size:15px;margin:22px 0 8px;color:#93a4b8;text-transform:uppercase;letter-spacing:.06em}}
p{{margin:8px 0}} .muted{{color:#93a4b8}} .row{{display:grid;grid-template-columns:180px minmax(0,1fr);gap:10px;padding:6px 0;border-bottom:1px solid #1c2835}}
.key{{color:#93a4b8}} .value{{overflow-wrap:anywhere}} code{{color:#54d3a1;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
.actions{{display:flex;gap:12px;flex-wrap:wrap;margin-top:24px}}
button{{border:1px solid #243242;border-radius:8px;padding:10px 18px;font:inherit;cursor:pointer;color:#e6edf5;background:#17212c}}
button.approve{{border-color:#54d3a1;color:#54d3a1}} button.decline{{border-color:#f27f8b;color:#f27f8b}}
button:disabled{{opacity:.45;cursor:not-allowed}} .notice{{margin-top:16px;padding:10px 12px;border:1px solid #243242;border-radius:8px;color:#f0b86e}}
.ok{{color:#54d3a1}} .error{{color:#f27f8b}} ul{{padding-left:20px}} li{{margin:6px 0}}
summary{{cursor:pointer;color:#93a4b8}} .digest{{font-size:12px;color:#93a4b8;overflow-wrap:anywhere}}
</style></head><body><main><div class="card">
<h1>Bundlebees asks for one-time approval</h1>
<p class="muted">Review this exact run. Approve starts only this job; it does not approve a merge, release, deployment, publication or final product acceptance.</p>
<div class="row"><span class="key">Job</span><span class="value"><code>{html.escape(card['job_id'] or 'unknown')}</code> — {html.escape(card['statement'] or 'no statement')}</span></div>
<div class="row"><span class="key">Repository</span><span class="value"><code>{html.escape(card['repo'] or 'unknown')}</code></span></div>
<div class="row"><span class="key">Base revision</span><span class="value"><code>{html.escape(card['base_rev'] or 'unknown')}</code></span></div>
<div class="row"><span class="key">Authorized paths</span><span class="value">{html.escape(', '.join(card['scope']) or 'none')}</span></div>
<div class="row"><span class="key">Worker</span><span class="value">project <code>{html.escape(card['worker_project'] or 'unknown')}</code>; profile <code>{html.escape(card['worker_profile'] or 'unknown')}</code></span></div>
<div class="row"><span class="key">Code revision</span><span class="value"><code>{html.escape(card['code_rev'] or 'unknown')}</code></span></div>
<div class="row"><span class="key">Budget</span><span class="value">{card['bees']} bee(s); {card['wall_clock_s']} seconds; {card['attempts']} attempt(s); {card['accepted_waves']} accepted wave(s)</span></div>
<div class="row"><span class="key">Coordinator stop</span><span class="value">{stop_text}</span></div>
<div class="row"><span class="key">Restart</span><span class="value">{restart_text}</span></div>
<div class="row"><span class="key">Expires</span><span class="value">{expiry}</span></div>
<h2>Task scope</h2><ul>{tasks}</ul>
<details><summary>Exact preview identity</summary><p class="digest">request {html.escape(request_id)}<br>digest {html.escape(digest)}</p></details>
<div class="actions">{buttons}</div>
<div id="status" class="notice" role="status" aria-live="polite">{html.escape(notice)}</div>
</div></main>
<script>
(() => {{
  const requestId = {json.dumps(request_id)};
  const digest = {json.dumps(digest)};
  const buttons = [...document.querySelectorAll('button[data-decision]')];
  const status = document.getElementById('status');
  buttons.forEach(button => button.addEventListener('click', async () => {{
    const decision = button.dataset.decision;
    if (!['approve','decline'].includes(decision)) return;
    if (decision === 'approve' && !confirm('Approve exactly this job, revision, scope, worker and budget?')) return;
    if (decision === 'decline' && !confirm('Decline this job? No work will be dispatched.')) return;
    buttons.forEach(item => item.disabled = true);
    status.textContent = decision === 'approve' ? 'Recording approval…' : 'Recording decline…';
    try {{
      const response = await fetch('../approvals/' + encodeURIComponent(requestId) + '/decision', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{decision, expected_digest: digest}})
      }});
      const data = await response.json();
      if (!response.ok || data.ok !== true) throw Error(data.error || data.code || 'decision was refused');
      status.className = 'notice ok';
      status.textContent = decision === 'approve'
        ? 'Approved once. The coordinator may now proceed; this page cannot be used as a second approval.'
        : 'Declined. The coordinator must not dispatch this job.';
    }} catch (error) {{
      buttons.forEach(item => item.disabled = false);
      status.className = 'notice error';
      status.textContent = String(error.message || error);
    }}
  }}));
}})();
</script></body></html>"""
    return HTMLResponse(body, status_code=status, headers={
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    })


@router.get("/approvals")
def approvals() -> JSONResponse:
    def build() -> dict[str, Any]:
        module = _approval_api()
        try:
            rows = module.list_pending(_approval_state_dir())
        except Exception as exc:
            raise _approval_error(exc) from exc
        return {"ok": True, "approvals": [_approval_public(row) for row in rows]}

    return _run(build)


@router.post("/approvals/{request_id}/decision")
def approval_decision(request_id: str, payload: Any = Body(default=None)) -> JSONResponse:
    def build() -> dict[str, Any]:
        data = _as_mapping(payload)
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in {"approve", "decline"}:
            raise DashboardError(
                "decision must be approve or decline",
                "invalid_argument",
                "Reload the approval card and choose Approve once or Decline.",
            )
        expected = str(data.get("expected_digest") or "").strip()
        if not expected:
            raise DashboardError(
                "the exact preview digest is required",
                "stale_resource",
                "Reload the card and review the current preview before deciding.",
                409,
            )
        module = _approval_api()
        try:
            capability = module.owner_capability(request_id)
            receipt = module.record_decision(
                _approval_state_dir(),
                request_id,
                decision="approved" if decision == "approve" else "declined",
                expected_digest=expected,
                owner="owner",
                auth_method="hermes_dashboard_session",
                owner_capability=capability,
            )
        except Exception as exc:
            raise _approval_error(exc) from exc
        return {"ok": True, "receipt": receipt}

    return _run(build)


@router.get("/approval/{request_id}", response_class=HTMLResponse)
def approval_card(request_id: str) -> HTMLResponse:
    try:
        module = _approval_api()
        row = module.load_request(_approval_state_dir(), request_id)
        try:
            receipt = module.load_receipt(_approval_state_dir(), request_id)
        except Exception as exc:
            if str(getattr(exc, "code", "")) not in {
                "approval_receipt_missing",
                "approval_receipt_invalid",
            }:
                raise
        else:
            row = dict(row)
            row["status"] = str(receipt.get("decision") or "settled")
        if str(row.get("status") or "pending") in {"pending", "open"}:
            from datetime import datetime, timezone
            try:
                expiry = datetime.fromisoformat(str(row.get("expires_at") or "").replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    row = dict(row)
                    row["status"] = "expired"
            except ValueError:
                pass
    except Exception as exc:
        mapped = _approval_error(exc)
        return _approval_page(
            {"request_id": request_id, "request_digest": "", "binding": {}},
            status=mapped.status,
            notice=mapped.error,
        )
    return _approval_page(row)


@router.api_route(
    "/{unmatched_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
def unmatched_route(unmatched_path: str) -> JSONResponse:
    error = DashboardError(
        "no such bundlebees route",
        "not_found",
        "Reload the pane; only documented bundlebees routes are available.",
        404,
    )
    return JSONResponse(error.as_envelope(), status_code=404)


__all__ = [
    "router",
    "DashboardError",
    "MissingBackend",
    "configure_backend",
    "reset_backend",
    "reset_state",
    "set_backend",
    "set_client",
    "set_backend_factory",
    "set_data_dir",
]
