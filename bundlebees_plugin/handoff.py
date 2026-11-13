"""Sealed descendant policies and the production A→B handoff.

A human enrolls one bounded policy before the run: repository, root revision,
dependency job and feature, the intended dependent feature and job ids, permitted
scope, budget, gates and an approved contract template. The policy is sealed with
a separate *approval* key (``<state_dir>/approval.key``) that ordinary coordinator
job sealing never uses: the coordinator can verify an enrollment but cannot mint
one. This is file-level authority separation inside one state directory, not
OS-principal isolation; a process that can read the approval key can verify with
it, and a process that can write the whole state directory can replace files.

Selection is explicit: the dependent feature's intent packet names a ``policy_id``
and the coordinator reads exactly that policy. There is no "first policy that
mentions the dependency" path.

The handoff itself is production code: the coordinator validates the dependency's
accepted evidence independently, publishes the accepted commit, derives the exact
revision, writes an immutable source-resolution record, records a create-only
derived authorization rooted in the verified policy, and creates the dependent job
with its intent packet bound. The intent packet is never edited: only the
resolution record and the frozen contract carry the resolved revision.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import contract as contract_mod
from . import descendants, job as jobs, packets, transport

APPROVAL_KEY_NAME = "approval.key"
_KEY_BYTES = 32
POLICY_KIND = "descendant-policy"
RESOLUTIONS_DIR = "resolutions"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class HandoffError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Approval authority: a key file the coordinator may verify but never mint
# ---------------------------------------------------------------------------


def approval_key_path(state_dir: Path) -> Path:
    return Path(state_dir) / APPROVAL_KEY_NAME


def create_approval_key(state_dir: Path) -> bytes:
    """One-time human setup: create the enrollment key. Never called by the coordinator."""

    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = approval_key_path(state_dir)
    tmp = state_dir / f".{APPROVAL_KEY_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    except OSError as exc:
        raise HandoffError("approval_key_unwritable", f"the approval key could not be created: {exc}",
                           "Fix the state directory permissions.") from exc
    try:
        key = os.urandom(_KEY_BYTES)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise HandoffError("approval_key_exists",
                               "an approval key is already enrolled; refusing to replace it",
                               "Enroll policies with the existing key; never rotate it in place.") from None
        except OSError:
            try:
                direct = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            except FileExistsError:
                raise HandoffError("approval_key_exists",
                                   "an approval key is already enrolled; refusing to replace it",
                                   "Enroll policies with the existing key; never rotate it in place.") from None
            with os.fdopen(direct, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
        return key
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def load_approval_key(state_dir: Path) -> bytes:
    """Read the enrollment key for verification. Never creates it."""

    path = approval_key_path(Path(state_dir))
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError as exc:
        raise HandoffError(
            "approval_key_missing",
            "no human approval key is enrolled in this state directory",
            "The approver runs the enrollment-key setup themselves; the coordinator cannot mint it.",
        ) from exc
    except OSError as exc:
        raise HandoffError("approval_key_invalid", "the approval key could not be opened safely",
                           "Inspect the key file; do not replace it silently.") from exc
    data = b""
    keep_fd = True
    try:
        info = os.fstat(fd)
        if (info.st_mode & 0o077) or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
            raise HandoffError("approval_key_invalid",
                               "the approval key is group/world-accessible or owned by another user",
                               "Fix its mode to 0600 and owner before enrolling.")
        handle = os.fdopen(fd, "rb")
        keep_fd = False
        with handle as stream:
            data = stream.read(_KEY_BYTES + 1)
    finally:
        if keep_fd:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(data) != _KEY_BYTES:
        raise HandoffError("approval_key_invalid", "the approval key is not 32 bytes",
                           "Re-enroll from a trusted copy; do not guess.")
    return data


# ---------------------------------------------------------------------------
# Sealed policy documents
# ---------------------------------------------------------------------------


def _policy_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 64 or not (text[0].isalnum()):
        raise HandoffError("policy_invalid", f"{value!r} is not a valid policy id",
                           "Policy ids are [A-Za-z0-9][A-Za-z0-9._-]{0,63}.")
    if not all(ch.isalnum() or ch in "-_." for ch in text):
        raise HandoffError("policy_invalid", f"{value!r} is not a valid policy id",
                           "Policy ids are [A-Za-z0-9][A-Za-z0-9._-]{0,63}.")
    return text


def policy_path(state_dir: Path, policy_id: str) -> Path:
    return Path(state_dir) / "policies" / f"{_policy_id(policy_id)}.json"


def policy_digest(policy: Mapping[str, Any]) -> str:
    """The digest of the enrolled policy content (independent of the seal)."""

    return hashlib.sha256(json.dumps(dict(policy), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text.lower())


def _normalize_scope(scope: Sequence[Any]) -> list[str]:
    try:
        return jobs._normalize_scope(scope)
    except jobs.JobError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc


def _all_policies(state_dir: Path) -> list[dict[str, Any]]:
    folder = Path(state_dir) / "policies"
    if not folder.exists():
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            found.append(load_policy(state_dir, path.stem))
        except HandoffError:
            # An unreadable or unverifiable policy is not authority; it is skipped
            # for discovery but can never be selected by an intent packet.
            continue
    return found


def enroll_policy(state_dir: Path, *, policy_id: str, statement: str, repo: str, root_rev: str,
                  dependency_job: str, dependency_feature: str, dependent_feature: str,
                  dependent_job: str, scope: Sequence[str], bees: int, wall_clock_s: int,
                  contract_template: Mapping[str, Any], gates: Sequence[str] = ("merge",),
                  host: str = "", profile: str = "", project: str = "",
                  workflow_ref: str = "", approval_key: bytes | None = None) -> Path:
    """Human enrollment, sealed with the approval key. Create-only, race-safe.

    The enrollment is bound to exactly one dependency (job and feature) and to the
    intended dependent feature and job, so a policy cannot be reused for a
    different lineage or a different target.
    """

    state_dir = Path(state_dir)
    text = str(statement or "").strip()
    if len(text) < 8:
        raise HandoffError("statement_required", "enrolling a policy needs the human's own statement",
                           "Record what the approver actually asked for.")
    policy_id = _policy_id(policy_id)
    if "/" not in str(repo or ""):
        raise HandoffError("policy_incomplete", "repo must be 'owner/name'", "Name the authorized repository.")
    if not _is_sha(root_rev):
        raise HandoffError("policy_incomplete", "root_rev must be a full 40-hex commit", "Pin the authorized root.")
    dependency_job = jobs._job_id(str(dependency_job or ""))
    dependent_job = jobs._job_id(str(dependent_job or ""))
    dependency_feature = str(dependency_feature or "").strip()
    dependent_feature = str(dependent_feature or "").strip()
    if not dependency_feature or not dependent_feature:
        raise HandoffError("policy_incomplete", "a policy must name the dependency feature and the dependent feature",
                           "Bind the enrollment to both features.")
    if dependency_job == dependent_job or dependency_feature == dependent_feature:
        raise HandoffError("policy_ambiguous", "a policy cannot name one job or feature as both sides",
                           "Enroll a dependent feature that is distinct from its dependency.")
    scope_globs = _normalize_scope(scope)
    if not scope_globs:
        raise HandoffError("policy_incomplete", "a policy needs at least one scope glob",
                           "Declare the files the dependent feature may touch.")
    if int(bees) < 1 or int(wall_clock_s) < 60:
        raise HandoffError("policy_incomplete", "the budget must allow at least one bee and 60 s",
                           "Set a real budget.")
    if not isinstance(contract_template, Mapping):
        raise HandoffError("policy_incomplete", "the approved contract template is missing", "Attach the template.")
    template = copy.deepcopy(dict(contract_template))
    try:
        contract_mod.validate_contract(template)
    except contract_mod.ContractError as exc:
        raise HandoffError("template_invalid", f"the contract template is not a valid approved contract: {exc}",
                           "Freeze a real contract before enrolling.") from exc
    template_root = str(template.get("source_rev") or "")
    if template_root and template_root != str(root_rev).lower():
        raise HandoffError("template_root_mismatch", "the contract template is bound to another source revision",
                           "The template must be approved against the authorized root.")
    template_gates = {jobs._slug(g) for g in (template.get("authorization") or {}).get("human_gates") or []}
    wanted_gates = sorted({jobs._slug(gate) for gate in gates}) or ["merge"]
    if template_gates and not set(wanted_gates).issubset(template_gates):
        raise HandoffError("policy_gates_widened", f"gates {wanted_gates} are not all in the approved template",
                           "Only enroll gates the contract already requires.")
    workflow = str(workflow_ref or "")
    if workflow and not _is_sha(workflow):
        raise HandoffError("policy_incomplete", "workflow_ref must be a full commit SHA", "Pin the protocol commit.")

    key = approval_key if approval_key is not None else load_approval_key(state_dir)
    body = {
        "kind": POLICY_KIND,
        "policy_id": policy_id,
        "statement": text,
        "repo": str(repo).strip(),
        "root_rev": str(root_rev).lower(),
        "dependencies": [dependency_job],
        "dependency_features": [dependency_feature],
        "dependent_feature": dependent_feature,
        "dependent_job": dependent_job,
        "scope": scope_globs,
        "budget": {"bees": int(bees), "wall_clock_s": int(wall_clock_s)},
        "contract_template": template,
        "gates": wanted_gates,
        "host": host,
        "profile": profile,
        "project": project,
        "workflow_ref": workflow,
        "key_file": APPROVAL_KEY_NAME,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    for other in _all_policies(state_dir):
        if other.get("policy_id") == policy_id:
            continue
        if other.get("dependent_job") == dependent_job:
            raise HandoffError("policy_conflict",
                               f"policy {other.get('policy_id')} already governs dependent job {dependent_job}",
                               "One bounded policy per dependent job; revoke the old one deliberately.")

    path = policy_path(state_dir, policy_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(jobs._seal(body, key), indent=2, sort_keys=True).encode() + b"\n"
    if not jobs._create_only(path, payload, mode=0o400):
        raise HandoffError("policy_exists", f"policy {policy_id} is already enrolled",
                           "Revoke it deliberately; never overwrite or widen an enrollment.")
    return path


def load_policy(state_dir: Path, policy_id: str) -> dict[str, Any]:
    """Read and verify an enrolled policy with the approval key only."""

    key = load_approval_key(Path(state_dir))
    path = policy_path(Path(state_dir), policy_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HandoffError("policy_missing", f"no enrolled policy {policy_id}",
                           "Enroll it before unattended execution.") from exc
    except (OSError, ValueError) as exc:
        raise HandoffError("policy_unreadable", f"policy {policy_id}: {exc}", "Refuse to proceed.") from exc
    try:
        body = jobs.open_sealed(raw, key, "descendant policy")
    except jobs.JobError as exc:
        raise HandoffError("seal_invalid", f"policy {policy_id} does not verify under the approval key",
                           "Refuse it; the coordinator key cannot vouch for an enrollment.") from exc
    if body.get("kind") != POLICY_KIND:
        raise HandoffError("policy_invalid", f"{policy_id} is not a descendant policy", "Fix the enrollment.")
    if str(body.get("policy_id")) != str(policy_id):
        raise HandoffError("policy_invalid", f"policy file {policy_id} names another policy",
                           "Refuse: the file name and the sealed identity must agree.")
    return body


def find_policy_for_dependency(state_dir: Path, dependency: str) -> dict[str, Any] | None:
    """Removed selection path.

    Authority is selected by the intent packet's explicit ``policy_id``. Searching
    the policy directory for the first document that mentions a dependency let a
    caller pick the wrong (often the widest) enrollment, so this now refuses.
    """

    raise HandoffError(
        "policy_selection_forbidden",
        f"refusing to select a policy by dependency {dependency!r}; the intent packet must name policy_id",
        "Read the policy id from the dependent feature's intent packet.",
    )


# ---------------------------------------------------------------------------
# Intent-packet binding
# ---------------------------------------------------------------------------


def policy_for_intent(state_dir: Path, packet: Mapping[str, Any]) -> dict[str, Any]:
    """Load exactly the policy the intent packet names and prove it is bound to it.

    The packet is the human's approved intent: its ``policy_id`` is the only
    selector, and the loaded policy must agree on the repository, the root
    revision, the dependency (feature and job), the intended dependent feature and
    job, the scope, the budget, the gates and the contract template.
    """

    try:
        policy_ref = packets.source_policy(packet)
    except packets.PacketError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc
    policy_id = str(policy_ref["policy_id"]).strip()
    dependency = str(policy_ref["dependencies"][0])
    policy = load_policy(Path(state_dir), policy_id)

    def refuse(code: str, message: str) -> None:
        raise HandoffError(code, message, "Refuse: the policy does not cover this intent.")

    jobs_bound = [str(item) for item in policy.get("dependencies") or []]
    features_bound = [str(item) for item in policy.get("dependency_features") or []]
    if len(jobs_bound) != 1 or len(features_bound) != 1:
        refuse("policy_ambiguous", f"policy {policy_id} must name exactly one dependency job and feature")

    if str(policy.get("dependent_job") or "") != str(packet.get("job_id") or ""):
        refuse("policy_job_mismatch",
               f"policy {policy_id} is bound to dependent job {policy.get('dependent_job')!r}, "
               f"not {packet.get('job_id')!r}")
    if str(policy.get("dependent_feature") or "") != str(packet.get("feature_id") or ""):
        refuse("policy_feature_mismatch",
               f"policy {policy_id} is bound to dependent feature {policy.get('dependent_feature')!r}, "
               f"not {packet.get('feature_id')!r}")
    if features_bound[0] != dependency:
        refuse("policy_dependency_mismatch",
               f"policy {policy_id} was enrolled for dependency {features_bound[0]!r}, not {dependency!r}")
    if str(policy.get("repo") or "") != str((packet.get("repository") or {}).get("identity") or ""):
        refuse("policy_repo_mismatch", "the policy and the packet name different repositories")
    if str(policy.get("root_rev") or "") != str(policy_ref.get("root_revision") or ""):
        refuse("policy_root_mismatch", "the policy and the packet name different authorized roots")
    pinned = str(policy.get("workflow_ref") or "")
    if pinned and pinned != str(packet.get("workflow_ref") or ""):
        refuse("policy_workflow_mismatch", "the policy was enrolled for another protocol revision")

    budget = policy.get("budget") or {}
    wanted = packet.get("budget") or {}
    if int(wanted.get("bees") or 0) > int(budget.get("bees") or 0):
        refuse("policy_budget_exceeded", "the packet's bee budget exceeds the enrolled policy")
    if int(wanted.get("wall_clock_s") or 0) > int(budget.get("wall_clock_s") or 0):
        refuse("policy_budget_exceeded", "the packet's wall-clock budget exceeds the enrolled policy")

    lease = [str(p) for p in (packet.get("scope") or {}).get("lease") or []]
    ceiling = [str(p) for p in policy.get("scope") or []]
    outside = [p for p in lease if not jobs._covered(p, ceiling)]
    if outside:
        refuse("policy_scope_widened", f"the packet leases files outside the enrolled policy: {', '.join(outside)}")

    packet_gates = {jobs._slug(g) for g in packet.get("human_gates") or []}
    if not set(policy.get("gates") or []).issubset(packet_gates):
        refuse("policy_gates_mismatch", "the policy permits a gate the intent packet does not declare")
    if dict(policy.get("contract_template") or {}) != dict(packet.get("contract_template") or {}):
        refuse("policy_template_mismatch", "the packet's contract template is not the approved template")

    for other in _all_policies(state_dir):
        if other.get("policy_id") == policy_id:
            continue
        if other.get("dependent_job") == policy.get("dependent_job"):
            refuse("policy_ambiguous",
                   f"dependent job {policy.get('dependent_job')} is governed by more than one policy")
    return policy


# ---------------------------------------------------------------------------
# Source resolution + materialization
# ---------------------------------------------------------------------------


def resolution_path(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / RESOLUTIONS_DIR / f"{jobs._job_id(job_id)}.json"


def _resolution_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("resolution_version", "packet_digest", "policy_id", "policy_digest", "dependency_job",
            "dependency_feature", "accepted_commit", "derived_source_rev", "contract", "contract_sha256")
    return {key: record.get(key) for key in keys}


def write_resolution(state_dir: Path, job_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Write the immutable resolution record once; a different one is a refusal."""

    path = resolution_path(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
    if jobs._create_only(path, payload, mode=0o400):
        return dict(record)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandoffError("resolution_unreadable", f"resolution for {job_id} cannot be read: {exc}",
                           "Refuse: inspect it by hand.") from exc
    if not isinstance(existing, Mapping) or _resolution_identity(existing) != _resolution_identity(record):
        raise HandoffError("resolution_conflict", f"a different source resolution is already recorded for {job_id}",
                           "Refuse: a resolution is immutable once written.")
    return dict(existing)


def _existing_job(state_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Return the sealed job, or None only when the document is genuinely absent."""

    if not jobs.job_path(Path(state_dir), job_id).exists():
        return None
    try:
        return jobs.load_job(Path(state_dir), job_id)
    except jobs.JobError as exc:
        raise HandoffError("job_unreadable", f"job {job_id} exists but cannot be read: {exc}",
                           "Refuse: never materialize over a damaged job record.") from exc


def _replay(existing: Mapping[str, Any], state_dir: Path, job_id: str, packet_digest: str,
            policy: Mapping[str, Any], policy_digest_value: str) -> dict[str, Any]:
    """Return the already-materialized dependent job without rewriting anything."""

    sealed = str((existing.get("goal") or {}).get("packet_digest") or "")
    if sealed != packet_digest:
        raise HandoffError("job_packet_mismatch",
                           f"job {job_id} already exists under a different packet identity",
                           "Refuse: one job, one intent packet.")
    accepted = ""
    contract_sha = ""
    source_rev = str((existing.get("goal") or {}).get("base_rev") or "")
    path = resolution_path(state_dir, job_id)
    if path.exists():
        try:
            resolution = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HandoffError("resolution_unreadable", f"resolution for {job_id} cannot be read: {exc}",
                               "Refuse: inspect it by hand.") from exc
        if (str(resolution.get("policy_digest")) != policy_digest_value
                or str(resolution.get("packet_digest")) != packet_digest):
            raise HandoffError("policy_changed",
                               f"job {job_id} was resolved under different policy bytes",
                               "Refuse: the enrolled policy no longer matches the record.")
        accepted = str(resolution.get("accepted_commit") or "")
        contract_sha = str(resolution.get("contract_sha256") or "")
    return {"job_id": job_id, "created": False, "packet_digest": packet_digest,
            "policy_id": str(policy["policy_id"]), "policy_digest": policy_digest_value,
            "dependency_job": str((policy.get("dependencies") or [""])[0]),
            "dependency_feature": str((policy.get("dependency_features") or [""])[0]),
            "accepted_commit": accepted, "source_rev": source_rev,
            "mirror_ref": "", "contract_sha256": contract_sha,
            "resolution": str(path)}


def materialize(state_dir: Path, *, feature_id: str, job_id: str, packet: Mapping[str, Any],
                worktree: Path, mirror: Path, repo_identity: str = "") -> dict[str, Any]:
    """Create the dependent job from the policy its intent packet names.

    Create-only and idempotent: a replay of the same packet returns the existing
    job, resolution and authorization; anything that disagrees is refused rather
    than overwritten. ``packet`` is the immutable intent packet and is never
    mutated — the resolved revision lives only in the resolution record and in
    the source-bound frozen contract.
    """

    state_dir = Path(state_dir)
    intent = copy.deepcopy(dict(packet))
    try:
        packet_digest = packets.validate(intent)
    except packets.PacketError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc

    if str(intent.get("feature_id") or "") != str(feature_id):
        raise HandoffError("packet_feature_mismatch",
                           f"the packet names feature {intent.get('feature_id')!r}, not {feature_id!r}",
                           "Refuse: the intent packet and the queue item must agree.")
    if str(intent.get("job_id") or "") != str(job_id):
        raise HandoffError("packet_job_mismatch",
                           f"the packet names job {intent.get('job_id')!r}, not {job_id!r}",
                           "Refuse: the intent packet and the queue item must agree.")

    policy = policy_for_intent(state_dir, intent)
    policy_id = str(policy["policy_id"])
    policy_digest_value = policy_digest(policy)
    repo = str(policy.get("repo") or "")
    if repo_identity and str(repo_identity) != repo:
        raise HandoffError("policy_repo_mismatch", f"policy is for {repo}, not {repo_identity}", "Refuse.")

    existing = _existing_job(state_dir, job_id)
    if existing is not None:
        return _replay(existing, state_dir, job_id, packet_digest, policy, policy_digest_value)

    dependency_job = str((policy.get("dependencies") or [""])[0])
    dependency_feature = str((policy.get("dependency_features") or [""])[0])
    try:
        dependency = jobs.load_job(state_dir, dependency_job)
    except jobs.JobError as exc:
        raise HandoffError("dependency_unknown", f"dependency job {dependency_job} cannot be read: {exc}",
                           "Integrate the dependency first.") from exc

    try:
        accepted, attempts = transport.accepted_commit(state_dir, dependency)
    except transport.TransportError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc

    try:
        receipt = transport.publish(state_dir, dependency_job, repo=Path(worktree), mirror=Path(mirror),
                                    repo_identity=repo, root_rev=str(policy.get("root_rev") or ""))
    except transport.TransportError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc
    if receipt["source_sha"] != accepted:
        raise HandoffError("publication_mismatch", "the published commit is not the accepted commit", "Refuse.")

    try:
        rule = descendants.DescendantPolicy.parse({
            "repo": repo, "base_rev": policy["root_rev"], "scope": policy["scope"],
            "budget": policy["budget"],
            "descendant": {"policy": descendants.POLICY, "dependencies": policy["dependencies"]},
        })
        assert rule is not None
        derived = descendants.derive(rule, candidate_rev=accepted, repo=repo, worktree=Path(worktree),
                                     assigned_files=policy["scope"], state_dir=state_dir,
                                     bees=int(policy["budget"]["bees"]),
                                     wall_clock_s=int(policy["budget"]["wall_clock_s"]))
    except descendants.DescendantError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc

    body = copy.deepcopy(dict(intent["contract_template"]))
    body["source_rev"] = derived["base_rev"]
    try:
        frozen = contract_mod.freeze_contract(body, approved_by=f"descendant-policy:{policy_id}",
                                              approved_at=str(policy.get("at")))
        contract_sha = contract_mod.check_frozen(frozen)
    except contract_mod.ContractError as exc:
        raise HandoffError(exc.code, str(exc), "Refuse: the approved template must freeze unchanged.") from exc

    resolution = {
        "resolution_version": "rizzstack.source-resolution/1",
        "packet_digest": packet_digest,
        "policy_id": policy_id,
        "policy_digest": policy_digest_value,
        "dependency_job": dependency_job,
        "dependency_feature": dependency_feature,
        "accepted_commit": accepted,
        "derived_source_rev": derived["base_rev"],
        "contract": frozen,
        "contract_sha256": contract_sha,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        packets.validate_resolution(resolution, packet=intent, accepted_commit=accepted)
    except packets.PacketError as exc:
        raise HandoffError(exc.code, exc.message, "Refuse: the resolution must satisfy the pinned protocol.") from exc
    written = write_resolution(state_dir, job_id, resolution)

    evidence = {"job_id": dependency_job, "accepted_commit": accepted,
                "attempts": [str(item) for item in attempts],
                "mirror_ref": str(receipt.get("mirror_ref") or "")}
    try:
        jobs.record_derived_authorization(
            state_dir, job_id=job_id, parent_job_id=dependency_job,
            statement=f"{feature_id} under policy {policy_id}", repo=repo,
            candidate_rev=derived["base_rev"], scope=policy["scope"],
            bees=int(policy["budget"]["bees"]), wall_clock_s=int(policy["budget"]["wall_clock_s"]),
            policy_id=policy_id, dependency_feature=dependency_feature,
            dependent_feature=feature_id, dependency_evidence=evidence,
            gates=policy.get("gates") or ("merge",),
        )
    except jobs.JobError as exc:
        raise HandoffError(exc.code, exc.message, exc.fix) from exc

    task_id = f"T-{feature_id}"
    criteria = [c.get("statement") for c in written.get("contract", {}).get("acceptance_criteria") or []]
    tests = [t for c in written.get("contract", {}).get("acceptance_criteria") or [] for t in c.get("tests") or []]
    created = True
    try:
        jobs.create_job(
            state_dir, job_id=job_id, summary=str(intent.get("request", {}).get("summary") or policy.get("statement") or feature_id),
            workflow_ref=str(intent.get("workflow_ref") or ""), packet=intent,
            tasks=[{
                "task_id": task_id,
                "objective": str(frozen.get("title") or feature_id),
                "lease": list(policy["scope"]),
                "acceptance": {"criteria": criteria, "tests": tests, "commands": ["python -m pytest -q"]},
                "contract": frozen, "contract_sha256": contract_sha,
                "provision": {"repo": repo, "source_rev": derived["base_rev"],
                              "host": str(policy.get("host") or ""), "profile": str(policy.get("profile") or ""),
                              "project": str(policy.get("project") or f"smoke-{feature_id}"),
                              "contract_sha256": contract_sha},
            }])
    except jobs.JobError as exc:
        if exc.code != "job_exists":
            raise HandoffError(exc.code, exc.message, exc.fix) from exc
        concurrent = _existing_job(state_dir, job_id)
        if concurrent is None:
            raise HandoffError("job_exists", f"job {job_id} exists but cannot be read", "Refuse.") from exc
        return _replay(concurrent, state_dir, job_id, packet_digest, policy, policy_digest_value)
    return {"job_id": job_id, "created": created, "packet_digest": packet_digest,
            "policy_id": policy_id, "policy_digest": policy_digest_value,
            "dependency_job": dependency_job, "dependency_feature": dependency_feature,
            "accepted_commit": accepted, "source_rev": derived["base_rev"],
            "mirror_ref": str(receipt.get("mirror_ref") or ""), "contract_sha256": contract_sha,
            "resolution": str(resolution_path(state_dir, job_id))}
