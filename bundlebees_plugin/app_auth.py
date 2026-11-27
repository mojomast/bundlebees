"""GitHub App installation authentication with private-key permission checks."""

from __future__ import annotations

import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping


_TOKEN_CACHE: dict[tuple[Any, Any], dict[str, Any]] = {}


class AppAuthError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def build_claims(app_id: Any, now: float) -> dict[str, Any]:
    return {"iss": app_id, "iat": now - 60, "exp": now + 540}


def _check_key_permissions(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AppAuthError(
            "app_key_permissions",
            f"GitHub App private key {path} is accessible by group or other users",
            "Set the private key file mode to 0600.",
        )


def load_private_key(path: str | Path) -> bytes:
    key_path = Path(path)
    _check_key_permissions(key_path)
    return key_path.read_bytes()


def _expiry_epoch(value: Any, now: float) -> float:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            expires = float(value)
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expires = parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return now + 3600
    return expires if expires > now else now + 3600


def _public_token(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {"token": entry["token"], "expires_at": entry["expires_at"]}


def installation_token(
    app_id: Any,
    installation_id: Any,
    signer: Any,
    http: Any,
    cache: MutableMapping[tuple[Any, Any], dict[str, Any]],
    now: float,
) -> dict[str, Any]:
    key = (app_id, installation_id)
    cached = cache.get(key)
    if cached is not None and float(cached.get("_expires_epoch", 0)) > now + 60:
        return _public_token(cached)

    assertion = signer(build_claims(app_id, now))
    response = http(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        {
            "Authorization": f"Bearer {assertion}",
            "Accept": "application/vnd.github+json",
        },
        {},
    )
    entry = {
        "token": response["token"],
        "expires_at": response["expires_at"],
        "_expires_epoch": _expiry_epoch(response["expires_at"], now),
    }
    cache[key] = entry
    return _public_token(entry)


def _configured_app(config: Mapping[str, Any], name: str, fields: tuple[str, ...]) -> Mapping[str, Any]:
    apps = config.get("github_apps") if isinstance(config, Mapping) else None
    app = apps.get(name) if isinstance(apps, Mapping) else None
    if not isinstance(app, Mapping) or any(not app.get(field) for field in fields):
        raise AppAuthError(
            "app_not_configured",
            f"GitHub App {name} is not configured",
            f"Configure github_apps.{name} with {', '.join(fields)}.",
        )
    return app


def token_for(config: Mapping[str, Any], signer: Any, http: Any, now: float) -> dict[str, Any]:
    app = _configured_app(config, "coordinator", ("app_id", "installation_id", "key_path"))
    load_private_key(app["key_path"])
    return installation_token(
        app["app_id"], app["installation_id"], signer, http, _TOKEN_CACHE, now
    )


def reviewer_key_path(config: Mapping[str, Any]) -> Path:
    app = _configured_app(config, "reviewer", ("key_path", "poster_user"))
    key_path = Path(app["key_path"])
    _check_key_permissions(key_path)
    return key_path
