"""Production launcher adapter for the reconciler, plus lease binding.

The reconciler speaks a four-member contract (``list_attempts``, ``get_attempt``,
``post_attempt``, ``idempotent``). This module is the only place that knows how
that maps onto the launcher's HTTP API, and it refuses to guess:

* ``idempotent`` is **False**. The launcher has no server-enforced idempotency
  key, so the reconciler must never claim exactly-once dispatch.
* Transport failures are uncertain, not "not sent": the request may have landed.
  Only a refusal to construct a client, or a 4xx validation refusal, is treated
  as provably not dispatched / rejected.
* Journal entries are mapped field by field. A missing brief is passed through as
  missing -- the reconciler's strict identity check then refuses to adopt it.

Lease binding is a separate callable so the reconciler stays orchestrator-agnostic:
after an attempt id exists, the coordinator writes the HMAC lease from the brief it
authorised, never from anything the worker reported.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import leases
from .client import ClientError, NotConfigured
from .reconcile import DispatchNotSent, DispatchRejected, DispatchUncertain, ReconcileError

_FIX = "Check the launcher and retry when it is reachable."


def _entry(raw: Any) -> dict[str, Any] | None:
    """Map one launcher attempt record onto the reconciler's shape."""

    if not isinstance(raw, Mapping):
        return None
    attempt_id = raw.get("attempt_id") or raw.get("id")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        return None
    entry: dict[str, Any] = {"attempt_id": attempt_id.strip()}

    state = raw.get("state") or raw.get("status")
    if state is not None:
        entry["state"] = str(state)
    for key in ("created_at", "queued_at", "started_at", "finished_at"):
        value = raw.get(key)
        if value is not None:
            entry[key] = value

    brief = raw.get("brief")
    if isinstance(brief, Mapping):
        entry["brief"] = dict(brief)
    return entry


class LauncherAdapter:
    """The reconciler's launcher contract, backed by ``LauncherClient``."""

    #: No server-enforced idempotency exists yet; never claim exactly-once.
    idempotent = False

    def __init__(self, client: Any) -> None:
        if client is None:
            raise ReconcileError("launcher_incompatible", "no launcher client was supplied", _FIX)
        for name in ("attempts_raw", "get_raw", "create"):
            if not callable(getattr(client, name, None)):
                raise ReconcileError(
                    "launcher_incompatible",
                    f"the client has no {name}()",
                    "Pass a LauncherClient (or OrbitAdapterClient).",
                )
        self._client = client

    def list_attempts(self) -> list[dict[str, Any]]:
        try:
            raw = self._client.attempts_raw()
        except ClientError as exc:
            raise ReconcileError("launcher_unavailable", str(exc), _FIX) from exc
        if not isinstance(raw, (list, tuple)):
            raise ReconcileError(
                "launcher_incompatible",
                "attempts_raw() did not return a list",
                "Check the launcher version.",
            )
        return [entry for entry in (_entry(item) for item in raw) if entry is not None]

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        try:
            raw = self._client.get_raw(attempt_id)
        except ClientError as exc:
            if exc.code in {"not_found", "unknown_attempt"} or getattr(exc, "status", None) == 404:
                return None
            raise ReconcileError("launcher_unavailable", str(exc), _FIX) from exc
        return _entry(raw)

    def post_attempt(self, brief: Mapping[str, Any]) -> dict[str, Any]:
        try:
            created = self._client.create(dict(brief))
        except NotConfigured as exc:
            raise DispatchNotSent(f"no launcher is configured: {exc}") from exc
        except ClientError as exc:
            status = getattr(exc, "status", None)
            # A request the launcher validated and refused never started a worker.
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
                raise DispatchRejected(f"{exc.code}: {exc}") from exc
            # Timeouts, 5xx and connection failures may have landed. Uncertain.
            raise DispatchUncertain(f"{exc.code}: {exc}") from exc

        attempt_id = None
        if isinstance(created, Mapping):
            attempt_id = created.get("attempt_id") or created.get("id")
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise DispatchUncertain("the launcher returned no attempt id")
        return {"attempt_id": attempt_id.strip()}


class LeaseBinder:
    """Persist (or verify) the coordinator-signed lease for an attempt.

    Called by the reconciler immediately after an attempt id exists and again on
    adoption, so a crash between the POST and the lease write is reconciled
    rather than leaving an attempt that could never be verified.
    """

    def __init__(self, state_dir: Any) -> None:
        self._state_dir = state_dir

    def __call__(self, attempt_id: str, brief: Mapping[str, Any]) -> None:
        try:
            existing = leases.load_lease(self._state_dir, attempt_id)
        except leases.LeaseError as exc:
            if exc.code != "lease_missing":
                raise
            try:
                leases.record_lease(self._state_dir, attempt_id, dict(brief))
            except leases.LeaseError as write_exc:
                raise ReconcileError(
                    "lease_binding_failed",
                    f"lease for {attempt_id} could not be written: {write_exc}",
                    "Fix the lease store; an attempt without a lease must never be verified.",
                ) from write_exc
            return

        # An existing lease is authoritative. If it disagrees with what we are
        # about to claim, something is wrong and the tick must stop.
        recorded = existing.get("brief") if isinstance(existing, Mapping) else None
        if not isinstance(recorded, Mapping):
            raise ReconcileError("lease_invalid", f"lease for {attempt_id} has no brief", _FIX)
        for field in ("source_rev", "assigned_files"):
            if recorded.get(field) != brief.get(field):
                raise ReconcileError(
                    "lease_mismatch",
                    f"lease for {attempt_id} disagrees on {field}",
                    "Do not verify this attempt; investigate the lease record.",
                )
