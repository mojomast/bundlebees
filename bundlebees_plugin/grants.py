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
import re
import socket
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
    unrestricted_scope: bool = False

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
            unrestricted_scope=bool(body.get("unrestricted_scope", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        body = {"schema": SCHEMA, "grant_id": self.grant_id, "approved_by": self.approved_by,
                "approved_at": self.approved_at, "expires_at": self.expires_at, "repos": list(self.repos),
                "hosts": list(self.hosts), "profiles": list(self.profiles),
                "task_scopes": list(self.task_scopes),
                "unrestricted_scope": self.unrestricted_scope,
                "limits": {"max_bees": self.limits.max_bees, "max_jobs": self.limits.max_jobs,
                           "max_concurrent_jobs": self.limits.max_concurrent_jobs,
                           "wall_clock_s": self.limits.wall_clock_s, "budget_units": self.limits.budget_units}}
        if self.revoked_at:
            body["revoked_at"] = self.revoked_at
        return body

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()



_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows host
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextlib.contextmanager
def owner_lock(path: Path, *, stale_after: float = 300.0):
    """Crash-safe lock: a dead owner's lock is stolen by atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": time.time()}).encode()
    fd = None
    for _ in range(100):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            dead = (record.get("host") == socket.gethostname() and not pid_alive(int(record.get("pid") or 0))) \
                or (time.time() - float(record.get("at") or 0)) > stale_after
            if not dead:
                time.sleep(0.01)
                continue
            stale = path.with_name(f"{path.name}.stale.{os.getpid()}")
            try:
                os.replace(path, stale)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            with contextlib.suppress(OSError):
                stale.unlink()
            continue
    if fd is None:
        raise GrantError("grant_locked", f"could not acquire {path.name}", "Retry shortly.")
    try:
        os.write(fd, payload)
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()


def _safe_job(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_RE.match(job_id):
        raise GrantError("job_id_invalid", "job ids are [A-Za-z0-9][A-Za-z0-9._-]{0,63}", "Pass the coordinator's job id.")
    return job_id


def _safe_scope(path: str) -> str:
    text = str(path)
    if not text or text.startswith(("/", "\\")) or ".." in text.replace("\\", "/").split("/") or ":" in text:
        raise GrantError("scope_invalid", f"{path!r} is not a repository-relative path", "Use relative paths inside the repository.")
    if "\\" in text:
        raise GrantError("scope_invalid", f"{path!r} uses backslashes", "Use forward slashes.")
    return text


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
        with owner_lock(path.with_suffix(".lock")):
            if path.exists():
                raise GrantError("grant_exists", f"grant {grant.grant_id} is already enrolled", "Revoke it explicitly before re-enrolling.")
            _atomic(path, json.dumps(body, indent=2, sort_keys=True).encode())
        self._audit({"event": "grant.enrolled", "grant_id": grant.grant_id, "digest": grant.digest, "statement": statement})
        return path

    def revoke(self, grant_id: str, *, statement: str) -> None:
        if not self._tty():
            raise GrantError("interactive_required", "revoking a standing grant needs a real interactive terminal", "Run it yourself in a terminal.")
        path = self.grant_path(grant_id)
        with owner_lock(path.with_suffix(".lock")):
            grant = self.load(grant_id)
            body = seal(grant.to_dict() | {"revoked_at": self.now()}, self.key)
            _atomic(path, json.dumps(body, indent=2, sort_keys=True).encode())
            self._audit({"event": "grant.revoked", "grant_id": grant_id, "statement": statement})

    def renew(self, grant_id: str, *, expires_at: float, statement: str) -> None:
        if not self._tty():
            raise GrantError("interactive_required", "renewing a standing grant needs a real interactive terminal", "Run it yourself in a terminal.")
        path = self.grant_path(grant_id)
        with owner_lock(path.with_suffix(".lock")):
            grant = self.load(grant_id)
            if expires_at <= self.now():
                raise GrantError("grant_expired", "the new expiry is in the past", "Set a future expiry.")
            body = seal(grant.to_dict() | {"expires_at": expires_at}, self.key)
            _atomic(path, json.dumps(body, indent=2, sort_keys=True).encode())
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

        job_id = _safe_job(job_id)
        files = sorted(_safe_scope(item) for item in assigned_files)
        reservation_name = None
        with owner_lock(self.grant_path(grant_id).with_suffix(".lock")):
            grant = self.load(grant_id)          # load under the lock: revoke cannot interleave
            self._check_usable(grant)

            if repo not in grant.repos:
                raise GrantError("repo_not_granted", f"{repo} is not in grant {grant_id}", "Ask the human to enroll a grant that includes it.")
            if host not in grant.hosts:
                raise GrantError("host_not_granted", f"{host} is not in grant {grant_id}", "Ask the human to enroll that worker host.")
            if profile not in grant.profiles:
                raise GrantError("profile_not_granted", f"profile {profile} is not in grant {grant_id}", "Ask the human to enroll that profile.")
            if not files:
                raise GrantError("scope_empty", "a job authorization needs at least one assigned file", "Declare the task scope; an empty scope is never unrestricted.")
            if not grant.task_scopes and not grant.unrestricted_scope:
                raise GrantError("scope_not_granted", f"grant {grant_id} authorizes no task scopes", "Enroll scopes, or an explicitly unrestricted grant, deliberately.")
            if grant.task_scopes:
                outside = [path for path in files if not any(
                    path == scope or path.startswith(scope.rstrip("*").rstrip("/") + "/") or (scope.endswith("*") and path.startswith(scope[:-1]))
                    for scope in grant.task_scopes)]
                if outside:
                    raise GrantError("scope_not_granted", f"files outside the grant: {', '.join(outside)}", "Narrow the task or enroll a wider grant deliberately.")
            if bees < 1 or bees > grant.limits.max_bees:
                raise GrantError("bees_not_granted", f"bees={bees} exceeds the grant maximum {grant.limits.max_bees}", "Reduce the bee count.")
            if wall_clock_s < 1 or wall_clock_s > grant.limits.wall_clock_s:
                raise GrantError("wall_clock_not_granted", f"wall_clock_s={wall_clock_s} exceeds the grant maximum {grant.limits.wall_clock_s}", "Reduce the wall clock.")
            if not source_rev or len(str(source_rev)) != 40 or any(ch not in "0123456789abcdef" for ch in str(source_rev)):
                raise GrantError("revision_invalid", "a standing grant requires an exact lowercase 40-hex revision", "Pin the revision.")

            request = {"repo": repo, "source_rev": source_rev, "host": host, "profile": profile,
                       "assigned_files": files, "bees": bees, "wall_clock_s": wall_clock_s,
                       "contract_sha256": contract_sha256}
            request_digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            reservation_name = self._reserve_locked(grant, job_id=job_id, units=bees, request_digest=request_digest)

        authorization = {
            "schema": "rizzstack.job-authorization/1",
            "authority": "standing-grant",
            "job_id": job_id,
            "grant_id": grant.grant_id,
            "grant_digest": grant.digest,
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now())),
            "repo": repo,
            "host": host,
            "profile": profile,
            "source_rev": source_rev,
            "assigned_files": files,
            "bees": bees,
            "wall_clock_s": wall_clock_s,
            "contract_sha256": contract_sha256,
            "budget_reservation": reservation_name,
            "request_digest": request_digest,
        }
        authorization["digest"] = hashlib.sha256(json.dumps(authorization, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._audit({"event": "grant.derived", "grant_id": grant.grant_id, "job_id": job_id,
                     "repo": repo, "host": host, "bees": bees, "reservation": reservation_name, "digest": authorization["digest"]})
        return authorization

    # -- atomic budget ------------------------------------------------------

    def _reserve_locked(self, grant: Grant, *, job_id: str, units: int, request_digest: str) -> str:
        """Reserve under the grant lock. Concurrency is active reservations;
        budget is cumulative spend, so completing a job never refunds it."""

        reservations = self.root / "reservations"
        reservations.mkdir(parents=True, exist_ok=True)
        mine = reservations / f"{grant.grant_id}.{job_id}.json"

        spent_units = active_units = lifetime_jobs = active_jobs = 0
        for path in reservations.glob(f"{grant.grant_id}.*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise GrantError("reservation_unreadable", f"{path.name} is unreadable", "Inspect the reservations directory; do not derive blindly.")
            if record.get("spent"):
                spent_units += int(record.get("units") or 0)
            elif record.get("active"):
                active_units += int(record.get("units") or 0)
                active_jobs += 1
            lifetime_jobs += 1

        if mine.exists():
            existing = json.loads(mine.read_text(encoding="utf-8"))
            if existing.get("request_digest") != request_digest:
                raise GrantError("reservation_conflict", f"job {job_id} already reserved a different request", "Use a new job id; do not mutate a reservation.")
            return mine.name

        if lifetime_jobs + 1 > grant.limits.max_jobs:
            raise GrantError("jobs_exhausted", f"grant {grant.grant_id} already used {lifetime_jobs}/{grant.limits.max_jobs} jobs", "Ask the human for a new grant.")
        if active_jobs + 1 > grant.limits.max_concurrent_jobs:
            raise GrantError("concurrency_exhausted", f"grant {grant.grant_id} has {active_jobs}/{grant.limits.max_concurrent_jobs} jobs active", "Wait for one to finish.")
        if spent_units + active_units + units > grant.limits.budget_units:
            raise GrantError("budget_exhausted", f"grant {grant.grant_id} has {grant.limits.budget_units - spent_units - active_units} units left, needs {units}", "Reduce the job or ask the human to raise the grant.")

        record = {"grant_id": grant.grant_id, "job_id": job_id, "units": units, "active": True,
                  "spent": False, "request_digest": request_digest, "at": self.now()}
        _atomic(mine, json.dumps(record, sort_keys=True).encode())
        return mine.name

    def complete(self, grant_id: str, job_id: str) -> None:
        """Mark a job finished: frees concurrency, permanently consumes budget."""
        path = self.root / "reservations" / f"{grant_id}.{job_id}.json"
        if not path.exists():
            return
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("spent"):
            return
        record.update(active=False, spent=True, completed_at=self.now())
        _atomic(path, json.dumps(record, sort_keys=True).encode())
        self._audit({"event": "grant.job_completed", "grant_id": grant_id, "job_id": job_id, "units": record.get("units")})

    def release(self, grant_id: str, job_id: str) -> None:
        """Backwards-compatible alias: releasing a completed job consumes it."""
        self.complete(grant_id, job_id)
