"""Worker provisioning interface and the hyvv HTTP backend (M3.5 phase 2).

The coordinator never touches a worker filesystem. It submits a declarative
request to a host that has an enrolled provisioning policy, then validates the
host's receipt before any project is bound to a job.

Versioning: a backend must advertise ``rizzstack.provisioning/1``. A backend that
does not advertise capabilities, or advertises a different schema, is treated as
incompatible and dispatch stops — there is no legacy fallback.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

CAPABILITY_SCHEMA = "rizzstack.provisioning/1"
CAPABILITY_PATH = "/v1/provision/capabilities"
PROVISION_PATH = "/v1/provision/project"
CLEANUP_PATH = "/v1/provision/cleanup"


class ProvisionError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


class ProvisionUnavailable(ProvisionError):
    pass


class ProvisionIncompatible(ProvisionError):
    pass


class ProvisionUncertain(ProvisionError):
    """The request may or may not have been applied; never retry blindly."""


class ProvisionDenied(ProvisionError):
    pass


def digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Capabilities:
    schema: str
    backend: str
    profiles: tuple[str, ...]
    states: tuple[str, ...] = ()
    supports_cleanup: bool = False
    enabled: bool = True
    supports_receipt_lookup: bool = False

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> "Capabilities":
        schema = body.get("schema")
        if schema != CAPABILITY_SCHEMA:
            raise ProvisionIncompatible("capability_schema", f"host advertises {schema!r}, expected {CAPABILITY_SCHEMA}", "Upgrade the coordinator or the host; do not guess.")
        profiles = body.get("profiles")
        if not isinstance(profiles, list) or not all(isinstance(item, str) for item in profiles):
            raise ProvisionIncompatible("capability_profiles", "the capability advertisement has no usable profile list", "Fix the host advertisement.")
        return cls(schema=schema, backend=str(body.get("backend") or "unknown"), profiles=tuple(profiles),
                   states=tuple(str(s) for s in body.get("states") or ()), supports_cleanup=bool(body.get("supports_cleanup")),
                   enabled=bool(body.get("enabled", True)), supports_receipt_lookup=bool(body.get("supports_receipt_lookup", False)))


@dataclass(frozen=True)
class Receipt:
    schema: str
    status: str
    project: str
    repo: str
    resolved_revision: str
    workspace: str
    profile: str
    host: str
    task_scopes: tuple[str, ...] = ()
    adopted: bool = False
    expires_at: str = ""

    @classmethod
    def parse(cls, body: Mapping[str, Any], *, expect_repo: str, expect_rev: str, expect_profile: str,
              expect_host: str | None = None) -> "Receipt":
        required = ("schema", "status", "project", "repo", "resolved_revision", "workspace", "profile", "host")
        missing = [key for key in required if not body.get(key)]
        if missing:
            raise ProvisionIncompatible("receipt_incomplete", f"receipt is missing {', '.join(missing)}", "Treat the host as incompatible until it returns a complete receipt.")
        if body["schema"] != CAPABILITY_SCHEMA:
            raise ProvisionIncompatible("receipt_schema", f"receipt schema {body['schema']!r} is not {CAPABILITY_SCHEMA}", "Do not bind an unversioned receipt.")
        if body["status"] != "ready":
            raise ProvisionError("receipt_not_ready", f"receipt status is {body['status']!r}", "Provisioning is not ready; do not dispatch.")
        if body["repo"] != expect_repo:
            raise ProvisionDenied("receipt_repo", f"receipt repository {body['repo']!r} is not the authorized {expect_repo!r}", "Stop: the host provisioned the wrong repository.")
        if body["resolved_revision"] != expect_rev:
            raise ProvisionDenied("receipt_revision", f"receipt revision {body['resolved_revision']!r} is not the pinned {expect_rev!r}", "Stop: the checkout is not at the pinned revision.")
        if body["profile"] != expect_profile:
            raise ProvisionDenied("receipt_profile", f"receipt profile {body['profile']!r} is not the authorized {expect_profile!r}", "Stop.")
        if expect_host is not None and body["host"] != expect_host:
            raise ProvisionDenied("receipt_host", f"receipt host {body['host']!r} is not the authorized {expect_host!r}", "Stop: another host provisioned this project.")
        expires = str(body.get("expires_at") or "")
        if expires:
            import calendar
            import time as _time

            try:
                parsed = calendar.timegm(_time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError as exc:
                raise ProvisionIncompatible("receipt_expiry", f"receipt expiry {expires!r} is unparseable", "Treat the host as incompatible.") from exc
            if parsed <= _time.time():
                raise ProvisionDenied("receipt_expired", f"the receipt expired at {expires}", "Re-provision; do not dispatch an expired binding.")
        return cls(
            schema=body["schema"], status=body["status"], project=str(body["project"]), repo=str(body["repo"]),
            resolved_revision=str(body["resolved_revision"]), workspace=str(body["workspace"]),
            profile=str(body["profile"]), host=str(body["host"]),
            task_scopes=tuple(str(item) for item in body.get("task_scopes") or ()), adopted=bool(body.get("adopted")),
            expires_at=expires,
        )

    def immutable(self) -> dict[str, Any]:
        return {"schema": self.schema, "project": self.project, "repo": self.repo,
                "resolved_revision": self.resolved_revision, "profile": self.profile,
                "host": self.host, "workspace": self.workspace}

    @property
    def digest(self) -> str:
        return digest(self.immutable())


@dataclass(frozen=True)
class Binding:
    """What a job is allowed to dispatch against, once and only once."""

    repo: str
    source_rev: str
    host: str
    profile: str
    project: str
    receipt_digest: str
    contract_sha256: str
    workspace: str = ""

    def validate(self, receipt: Receipt, *, contract_sha256: str) -> None:
        if receipt.repo != self.repo or receipt.resolved_revision != self.source_rev:
            raise ProvisionDenied("binding_revision", "the receipt does not match the bound repository/revision", "Re-provision; never dispatch a different checkout.")
        if receipt.project != self.project:
            raise ProvisionDenied("binding_project", f"receipt project {receipt.project!r} is not the bound {self.project!r}", "Stop: dispatch would target another project.")
        if receipt.profile != self.profile or receipt.host != self.host:
            raise ProvisionDenied("binding_host", "the receipt was produced by a different profile or host", "Stop.")
        if receipt.digest != self.receipt_digest:
            raise ProvisionDenied("binding_receipt", "the receipt changed after it was bound", "Stop: treat as tampering or re-provisioning.")
        if contract_sha256 != self.contract_sha256:
            raise ProvisionDenied("binding_contract", "the frozen contract changed after binding", "Stop: re-freeze and re-authorize.")

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "source_rev": self.source_rev, "host": self.host, "profile": self.profile,
                "project": self.project, "receipt_digest": self.receipt_digest,
                "contract_sha256": self.contract_sha256, "workspace": self.workspace}


class WorkerProvisioner(Protocol):
    def capabilities(self) -> Capabilities: ...
    def provision(self, request: Mapping[str, Any]) -> Receipt: ...
    def cleanup(self, project: str) -> dict[str, Any]: ...


class HyvvProvisioner:
    """HTTP implementation over the pinned phase-1 contract.

    ``transport(method, path, body) -> (status, json)`` is injected so the
    coordinator can use the existing authenticated client, and tests can use a
    realistic HTTP fixture.
    """

    def __init__(self, transport: Callable[[str, str, Mapping[str, Any] | None], tuple[int, Any]],
                 *, expect_host: str | None = None) -> None:
        self.transport = transport
        self.expect_host = expect_host

    def capabilities(self) -> Capabilities:
        status, body = self.transport("GET", CAPABILITY_PATH, None)
        if status == 404:
            raise ProvisionIncompatible("capability_missing", "the host does not advertise provisioning capabilities", "Upgrade the host to a provisioning-capable launcher; do not probe with writes.")
        if status != 200 or not isinstance(body, Mapping):
            raise ProvisionUnavailable("capability_unavailable", f"the host answered {status} for its capabilities", "Treat provisioning as unavailable.")
        caps = Capabilities.parse(body)
        if not caps.enabled:
            raise ProvisionUnavailable("provisioning_disabled", "the host advertises provisioning as disabled", "Enroll and enable the host policy; do not attempt a write.")
        return caps

    def authoritative_receipt(self, key: str, *, expect_repo: str, expect_rev: str, expect_profile: str,
                              expect_project: str, expect_host: str | None = None) -> Receipt:
        """Recover a complete receipt after a lost acknowledgement.

        Uses the read-only lookup (never a re-POST) and revalidates every
        identity, including the launcher project, before returning it.
        """
        caps = self.capabilities()
        if not caps.supports_receipt_lookup:
            raise ProvisionIncompatible("receipt_lookup_unsupported", "the host cannot return a receipt by idempotency key", "Upgrade the host; do not re-POST to discover the outcome.")
        status, body = self.transport("GET", f"/v1/provision/receipts/{key}", None)
        if status == 404:
            raise ProvisionError("receipt_unknown", f"the host has no record for {key}", "The POST may not have arrived; treat as uncertain and reconcile.")
        if status == 409:
            raise ProvisionUncertain("receipt_stale", (body or {}).get("message", "the host's receipt is stale"), "Re-provision with a new key after inspecting the host.")
        if status != 200 or not isinstance(body, Mapping):
            raise ProvisionUncertain("receipt_unavailable", f"receipt lookup answered {status}", "Do not dispatch; reconcile later.")
        if str(body.get("project")) != expect_project:
            raise ProvisionDenied("receipt_project", f"receipt project {body.get('project')!r} is not the authorized {expect_project!r}", "Stop: the host registered another project.")
        receipt = Receipt.parse(body, expect_repo=expect_repo, expect_rev=expect_rev,
                                expect_profile=expect_profile, expect_host=expect_host)
        return receipt

    def provision(self, request: Mapping[str, Any]) -> Receipt:
        caps = self.capabilities()
        if request.get("profile") not in caps.profiles:
            raise ProvisionDenied("profile_not_enrolled", f"profile {request.get('profile')!r} is not in {caps.profiles}", "Enroll the profile on the host first.")
        status, body = self.transport("POST", PROVISION_PATH, request)
        if status in (500, 502, 503, 504):
            raise ProvisionUncertain("provision_uncertain", f"the host answered {status}; the request may have been applied", "Reconcile the receipt; never re-POST blindly.")
        if status == 409:
            raise ProvisionError("provision_conflict", (body or {}).get("message", "conflicting provisioning request"), "Inspect the journal; do not retry with the same key.")
        if status != 200 or not isinstance(body, Mapping):
            raise ProvisionError("provision_failed", f"the host refused provisioning with {status}: {(body or {}).get('error', '')}", "Fix the request or the host policy.")
        return Receipt.parse(body, expect_repo=str(request["repo"]), expect_rev=str(request["source_rev"]),
                             expect_profile=str(request["profile"]), expect_host=self.expect_host)

    def cleanup(self, project: str) -> dict[str, Any]:
        status, body = self.transport("POST", CLEANUP_PATH, {"project": project})
        if status != 200 or not isinstance(body, Mapping):
            raise ProvisionError("cleanup_failed", f"cleanup answered {status}: {(body or {}).get('error', '')}", "Inspect the workspace; evidence is preserved.")
        return dict(body)
