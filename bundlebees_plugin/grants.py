"""Standing authorization grants (M3.5 phase 2).

A grant is enrolled by a human in an interactive terminal, once. The coordinator
may then derive a narrow, job-specific authorization that is provably inside the
grant. Nothing in this module can create, widen or renew a grant without an
interactive human — and possession of the coordinator's HMAC key is explicitly
not treated as consent: the seal protects against silent edits, the TTY gate and
the audit trail record who approved what.

Derivation is budget-reserved atomically: per-grant reservation files are created
with O_EXCL under an exclusive lock, so two concurrent jobs cannot spend the same
remaining units.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SCHEMA = "rizzstack.standing-grant/1"


class GrantError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def _canonical(body: Mapping[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in body.items() if k != "seal"}, sort_keys=True, separators=(",", ":")).encode()


def seal(body: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    sealed = dict(body)
    sealed["seal"] = hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()
    return sealed


def unseal(body: Mapping[str, Any], key: bytes, what: str = "grant") -> dict[str, Any]:
    provided = body.get("seal")
    if not isinstance(provided, str) or not hmac.compare_digest(provided, hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()):
        raise GrantError("seal_invalid", f"the {what} seal does not verify", "Do not use it; re-enroll or restore from a trusted copy.")
    return {k: v for k, v in body.items() if k != "seal"}


def _atomic(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class Limits:
    max_bees: int
    max_jobs: int
    max_concurrent_jobs: int
    wall_clock_s: int
    budget_units: int


@dataclass(frozen=True)
class Grant:
    grant_id: str
    approved_by: str
    approved_at: str
    expires_at: float
    repos: tuple[str, ...]
    hosts: tuple[str, ...]
    profiles: tuple[str, ...]
    task_scopes: tuple[str, ...]
    limits: Limits
    revoked_at: float | None = None

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> "Grant":
        missing = [k for k in ("grant_id", "approved_by", "approved_at", "expires_at", "repos", "hosts", "profiles") if not body.get(k)]
        if missing:
            raise GrantError("grant_incomplete", f"grant is missing {', '.join(missing)}", "Re-enroll a complete grant.")
        limits = body.get("limits") or {}
        try:
            parsed_limits = Limits(
                max_bees=int(limits["max_bees"]), max_jobs=int(limits["max_jobs"]),
                max_concurrent_jobs=int(limits["max_concurrent_jobs"]),
                wall_clock_s=int(limits["wall_clock_s"]), budget_units=int(limits["budget_units"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GrantError("grant_limits", f"grant limits are incomplete or invalid: {exc}", "Re-enroll with explicit limits.") from exc
        return cls(
            grant_id=str(body["grant_id"]), approved_by=str(body["approved_by"]), approved_at=str(body["approved_at"]),
            expires_at=float(body["expires_at"]), repos=tuple(body["repos"]), hosts=tuple(body["hosts"]),
            profiles=tuple(body["profiles"]), task_scopes=tuple(body.get("task_scopes") or ()),
            limits=parsed_limits, revoked_at=float(body["revoked_at"]) if body.get("revoked_at") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        body = {"schema": SCHEMA, "grant_id": self.grant_id, "approved_by": self.approved_by,
                "approved_at": self.approved_at, "expires_at": self.expires_at, "repos": list(self.repos),
                "hosts": list(self.hosts), "profiles": list(self.profiles),
                "task_scopes": list(self.task_scopes),
                "limits": {"max_bees": self.limits.max_bees, "max_jobs": self.limits.max_jobs,
                           "max_concurrent_jobs": self.limits.max_concurrent_jobs,
                           "wall_clock_s": self.limits.wall_clock_s, "budget_units": self.limits.budget_units}}
        if self.revoked_at:
            body["revoked_at"] = self.revoked_at
        return body

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


class GrantStore:
    """Enrollment, revocation and derivation, all under one directory."""

    def __init__(self, root: Path, *, key: bytes, tty: Callable[[], bool] | None = None,
                 now: Callable[[], float] = time.time, actor: Callable[[], str] | None = None) -> None:
        self.root = Path(root)
        self.key = key
        self._tty = tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
        self.now = now
        self._actor = actor or (lambda: os.environ.get("USER") or os.environ.get("USERNAME") or "unknown")

    # -- paths --------------------------------------------------------------

    def grant_path(self, grant_id: str) -> Path:
        safe = "".join(ch for ch in grant_id if ch.isalnum() or ch in "-_")
        if not safe or safe != grant_id:
            raise GrantError("grant_id_invalid", "grant ids are [A-Za-z0-9_-]+", "Use a generated id.")
        return self.root / "grants" / f"{safe}.json"

    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    def _audit(self, event: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = dict(event, at=self.now(), actor=self._actor())
        with open(self.audit_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- human operations ---------------------------------------------------

    def enroll(self, grant: Grant, *, statement: str) -> Path:
        if not self._tty():
            raise GrantError("interactive_required", "enrolling a standing grant needs a real interactive terminal", "Run it yourself in a terminal; the agent must not proxy this.")
        if not statement.strip():
            raise GrantError("statement_required", "enrollment requires the human's own statement of what is authorized", "Pass --statement in your own words.")
        if grant.expires_at <= self.now():
            raise GrantError("grant_expired", "the grant expires in the past", "Set a future expiry.")
        body = seal(grant.to_dict(), self.key)
        path = self.grant_path(grant.grant_id)
        _atomic(path, json.dumps(body, indent=2, sort_keys=True).encode())
        self._audit({"event": "grant.enrolled", "grant_id": grant.grant_id, "digest": grant.digest, "statement": statement})
        return path

    def revoke(self, grant_id: str, *, statement: str) -> None:
        if not self._tty():
            raise GrantError("interactive_required", "revoking a standing grant needs a real interactive terminal", "Run it yourself in a terminal.")
        grant = self.load(grant_id)
        body = seal(grant.to_dict() | {"revoked_at": self.now()}, self.key)
        _atomic(self.grant_path(grant_id), json.dumps(body, indent=2, sort_keys=True).encode())
        self._audit({"event": "grant.revoked", "grant_id": grant_id, "statement": statement})

    def renew(self, grant_id: str, *, expires_at: float, statement: str) -> None:
        if not self._tty():
            raise GrantError("interactive_required", "renewing a standing grant needs a real interactive terminal", "Run it yourself in a terminal.")
        grant = self.load(grant_id)
        if expires_at <= self.now():
            raise GrantError("grant_expired", "the new expiry is in the past", "Set a future expiry.")
        body = seal(grant.to_dict() | {"expires_at": expires_at}, self.key)
        _atomic(self.grant_path(grant_id), json.dumps(body, indent=2, sort_keys=True).encode())
        self._audit({"event": "grant.renewed", "grant_id": grant_id, "expires_at": expires_at, "statement": statement})

    # -- reads --------------------------------------------------------------

    def load(self, grant_id: str) -> Grant:
        try:
            raw = json.loads(self.grant_path(grant_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GrantError("grant_missing", f"no grant {grant_id}", "Enroll it first.") from exc
        except (OSError, ValueError) as exc:
            raise GrantError("grant_unreadable", f"grant {grant_id} is unreadable: {exc}", "Do not proceed; re-enroll from a trusted copy.") from exc
        return Grant.parse(unseal(raw, self.key))

    def _check_usable(self, grant: Grant) -> None:
        if grant.revoked_at:
            raise GrantError("grant_revoked", f"grant {grant.grant_id} was revoked", "Enroll a new grant; do not work around this.")
        if grant.expires_at <= self.now():
            raise GrantError("grant_expired", f"grant {grant.grant_id} expired", "Renew it as a human, or fall back to per-job authorization.")

    # -- derivation ---------------------------------------------------------

    def derive(self, grant_id: str, *, job_id: str, repo: str, host: str, profile: str,
               source_rev: str, assigned_files: Iterable[str], bees: int, wall_clock_s: int,
               contract_sha256: str = "") -> dict[str, Any]:
        """Return a narrow job authorization, with the budget reservation taken."""

        grant = self.load(grant_id)
        self._check_usable(grant)

        if repo not in grant.repos:
            raise GrantError("repo_not_granted", f"{repo} is not in grant {grant_id}", "Ask the human to enroll a grant that includes it.")
        if host not in grant.hosts:
            raise GrantError("host_not_granted", f"{host} is not in grant {grant_id}", "Ask the human to enroll that worker host.")
        if profile not in grant.profiles:
            raise GrantError("profile_not_granted", f"profile {profile} is not in grant {grant_id}", "Ask the human to enroll that profile.")
        files = sorted(str(item) for item in assigned_files)
        if not files:
            raise GrantError("scope_empty", "a job authorization needs at least one assigned file", "Declare the task scope.")
        outside = [path for path in files if not any(path == scope or path.startswith(scope.rstrip("*").rstrip("/") + "/") or (scope.endswith("*") and path.startswith(scope[:-1])) for scope in grant.task_scopes)]
        if grant.task_scopes and outside:
            raise GrantError("scope_not_granted", f"files outside the grant: {', '.join(outside)}", "Narrow the task or enroll a wider grant deliberately.")
        if bees < 1 or bees > grant.limits.max_bees:
            raise GrantError("bees_not_granted", f"bees={bees} exceeds the grant maximum {grant.limits.max_bees}", "Reduce the bee count.")
        if wall_clock_s < 1 or wall_clock_s > grant.limits.wall_clock_s:
            raise GrantError("wall_clock_not_granted", f"wall_clock_s={wall_clock_s} exceeds the grant maximum {grant.limits.wall_clock_s}", "Reduce the wall clock.")
        if not source_rev or len(str(source_rev)) != 40:
            raise GrantError("revision_invalid", "a standing grant requires an exact 40-hex revision", "Pin the revision.")

        reservation = self._reserve(grant, job_id=job_id, units=bees)

        authorization = {
            "schema": "rizzstack.job-authorization/1",
            "authority": "standing-grant",
            "job_id": job_id,
            "grant_id": grant.grant_id,
            "grant_digest": grant.digest,
            "grant_seal": hashlib.sha256(json.dumps(grant.to_dict(), sort_keys=True).encode()).hexdigest(),
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now())),
            "repo": repo,
            "host": host,
            "profile": profile,
            "source_rev": source_rev,
            "assigned_files": files,
            "bees": bees,
            "wall_clock_s": wall_clock_s,
            "contract_sha256": contract_sha256,
            "budget_reservation": reservation,
        }
        authorization["digest"] = hashlib.sha256(json.dumps(authorization, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._audit({"event": "grant.derived", "grant_id": grant.grant_id, "job_id": job_id,
                     "repo": repo, "host": host, "bees": bees, "reservation": reservation, "digest": authorization["digest"]})
        return authorization

    # -- atomic budget ------------------------------------------------------

    def _reserve(self, grant: Grant, *, job_id: str, units: int) -> str:
        reservations = self.root / "reservations"
        reservations.mkdir(parents=True, exist_ok=True)
        lock = self.root / "reservations.lock"
        fd = None
        for _ in range(400):
            try:
                fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                with contextlib.suppress(OSError):
                    if self.now() - lock.stat().st_mtime > 30:
                        lock.unlink()
                        continue
                time.sleep(0.01)
        if fd is None:
            raise GrantError("budget_busy", "the grant budget is locked by another writer", "Retry shortly.")
        try:
            used_jobs = 0
            used_units = 0
            mine = reservations / f"{grant.grant_id}.{job_id}.json"
            for path in reservations.glob(f"{grant.grant_id}.*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if record.get("released_at"):
                    continue
                if path == mine:
                    continue
                used_jobs += 1
                used_units += int(record.get("units") or 0)
            if used_jobs + 1 > grant.limits.max_jobs:
                raise GrantError("jobs_exhausted", f"grant {grant.grant_id} already used {used_jobs}/{grant.limits.max_jobs} jobs", "Ask the human for a new grant.")
            if used_units + units > grant.limits.budget_units:
                raise GrantError("budget_exhausted", f"grant {grant.grant_id} has {grant.limits.budget_units - used_units} units left, needs {units}", "Reduce the job or ask the human to raise the grant.")
            record = {"grant_id": grant.grant_id, "job_id": job_id, "units": units, "at": self.now()}
            _atomic(mine, json.dumps(record, sort_keys=True).encode())
            return mine.name
        finally:
            os.close(fd)
            with contextlib.suppress(OSError):
                lock.unlink()

    def release(self, grant_id: str, job_id: str) -> None:
        path = self.root / "reservations" / f"{grant_id}.{job_id}.json"
        if not path.exists():
            return
        record = json.loads(path.read_text(encoding="utf-8"))
        record["released_at"] = self.now()
        _atomic(path, json.dumps(record, sort_keys=True).encode())
        self._audit({"event": "grant.budget_released", "grant_id": grant_id, "job_id": job_id})
