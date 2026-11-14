"""Independently verifiable RETURN packets (``rizzstack.return/1``).

A RETURN packet is what a receiver can check without trusting the coordinator's
prose. Every value in ``return.json`` is derived from a durable record under the
state directory, and the *relationships between* those records are re-proved
before a byte is written:

* the intent packet is re-hashed and must equal the digest sealed into the job;
* a descendant job's source resolution must validate against that packet and its
  accepted commit, and its policy digest must equal the enrolled policy's;
* every accepted wave named by the job must have a durable, accepted wave
  record with the same head, attempts and a zero suite;
* every attempt the job claims as verified must have a verification record
  whose commit exists in the repository (when a repository is supplied);
* a publication claim must be backed by a transport receipt for the immutable
  ref of the accepted commit.

Acceptance criteria are classified strictly, from evidence rather than state:

``PASS``
    A verification record whose ``suite.exit_code`` is ``0`` and whose tests
    cover the criterion's tests, or a signed ``tdd-evidence/1`` GREEN record
    that validates and marks the criterion passed. A task or job state of
    ``accepted`` is never evidence by itself.
``FAIL``
    A recorded, executed check failed: the verification record is not ``ok``,
    its suite or gitleaks exited non-zero, or a signed GREEN record classifies
    the run as failed.
``NOT RUN``
    No record exists for the criterion.
``BLOCKED``
    A prerequisite or evidence file needed to evaluate the criterion is
    unavailable (a contract that no longer verifies, a verify record that
    cannot be read).
``UNVERIFIED``
    A claim exists without independently checkable evidence: a record that does
    not cover the criterion's tests, a record with no suite result, an invalid
    signed record, or a state/evidence string with no record behind it.

Refusal codes are stable strings so a receiver can tell *why* a RETURN was not
produced: ``packet_mismatch``, ``resolution_mismatch``, ``verification_missing``,
``verification_commit_absent``, ``wave_mismatch``, ``transport_mismatch``,
``acceptance_uncovered`` and ``report_tampered``.

Determinism: the manifest carries no timestamps and no absolute paths, so the
same durable snapshot always produces byte-identical ``return.json`` and
``RETURN.md``. ``evidence_digest`` is SHA-256 over the canonical JSON of the
manifest excluding ``evidence_digest`` and any ``generated_at`` field, so a
receiver can re-derive it from the file alone.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import contract as contracts
from . import handoff, job as jobs, leases, packets, recovery, revision, tdd, transport, verify_local

RETURN_SCHEMA = "rizzstack.return/1"
RETURN_FILE = "return.json"
RENDER_FILE = "RETURN.md"

PASS = "PASS"
FAIL = "FAIL"
NOT_RUN = "NOT RUN"
UNVERIFIED = "UNVERIFIED"
BLOCKED = "BLOCKED"
STATUSES = (PASS, FAIL, NOT_RUN, UNVERIFIED, BLOCKED)

#: The label a packet-free job gets. It is a statement of fact, not a defect.
LEGACY_LABEL = (
    "legacy: this job carries no intent packet, so packet, descendant and "
    "transport evidence are absent by design and are not fabricated here"
)

_DIGEST_EXCLUDED = ("evidence_digest", "generated_at")
_SHA40 = 40

_FIX_EVIDENCE = (
    "Do not edit or re-derive the record by hand. Restore the durable evidence "
    "or re-run the stage that produces it."
)


class ReturnError(Exception):
    """A RETURN packet was refused; ``code`` is a stable refusal name."""

    def __init__(self, code: str, message: str, fix: str = _FIX_EVIDENCE) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Canonical bytes, digest, paths
# ---------------------------------------------------------------------------


def canonical_bytes(value: Any) -> bytes:
    """The exact bytes a digest is taken over: sorted keys, no whitespace."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def document_bytes(manifest: Mapping[str, Any]) -> bytes:
    """The bytes written to ``return.json``: sorted, indented, one trailing NL."""

    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def evidence_digest(manifest: Mapping[str, Any]) -> str:
    """SHA-256 over the manifest minus the digest itself and any ``generated_at``."""

    body = {key: value for key, value in manifest.items() if key not in _DIGEST_EXCLUDED}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def return_dir(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / jobs.JOBS_DIR / jobs._job_id(job_id)


def return_path(state_dir: Path, job_id: str) -> Path:
    return return_dir(state_dir, job_id) / RETURN_FILE


def render_path(state_dir: Path, job_id: str) -> Path:
    return return_dir(state_dir, job_id) / RENDER_FILE


def read_manifest(state_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Read the on-disk RETURN manifest, or ``None`` when it was never written."""

    try:
        raw = return_path(state_dir, job_id).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReturnError("report_tampered", f"the RETURN report cannot be read: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReturnError("report_tampered", f"the RETURN report is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReturnError("report_tampered", "the RETURN report is not a JSON object")
    return data


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_document(path: Path, payload: bytes) -> None:
    """Write a report file atomically (0600 in a 0700 directory, fsynced)."""

    _atomic_write(Path(path), payload)


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------


def _is_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == _SHA40 and all(ch in "0123456789abcdef" for ch in text.lower())


def _sha(value: Any, what: str, code: str = "verification_missing") -> str:
    if not _is_sha(value):
        raise ReturnError(code, f"{what} is not a full 40-hex commit: {value!r}")
    return str(value).lower()


def _wave_number(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(1, value)
    text = str(value or "").strip().lower().lstrip("w")
    try:
        return max(1, int(text))
    except ValueError:
        return 1


def _rel(state_dir: Path, path: Path) -> str:
    try:
        return str(Path(path).relative_to(Path(state_dir)))
    except ValueError:
        return path.name


def _commit_present(repo: Path | None, sha: str) -> bool:
    if repo is None:
        return True
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError:
        return False
    return done.returncode == 0


def _read_verify(state_dir: Path, attempt_id: str) -> tuple[dict[str, Any] | None, str]:
    """Return ``(record, problem)``: problem is ``""``, ``"missing"`` or ``"corrupt"``."""

    path = verify_local.record_path(Path(state_dir), attempt_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "corrupt"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    return data, ""


def _covers(ran: Sequence[Any], wanted: Sequence[Any]) -> bool:
    """True when every wanted test id was executed by the recorded test list.

    A recorded whole-file run covers a node id inside that file, and pytest
    parameters match their base id; a node-id run never covers a whole file.
    """

    if not wanted:
        return False
    for target in wanted:
        if not any(_one_covers(str(entry), str(target)) for entry in ran):
            return False
    return True


def _one_covers(ran: str, wanted: str) -> bool:
    if ran == wanted:
        return True
    if wanted.startswith(ran.rstrip("/") + "::"):
        return True
    return tdd.matches(ran, wanted)


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------


def _packet(state_dir: Path, job: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Recompute the packet digest and prove the packet is this job's intent."""

    goal = job.get("goal") or {}
    sealed = str(goal.get("packet_digest") or "")
    job_id = str(job.get("job_id") or "")
    if not sealed:
        return None, ""
    try:
        raw = packets.read_packet(Path(state_dir), job_id)
    except packets.PacketError as exc:
        raise ReturnError("packet_mismatch", f"the stored intent packet cannot be read: {exc.message}") from exc
    if raw is None:
        raise ReturnError(
            "packet_mismatch",
            f"job {job_id} seals packet digest {sealed[:12]} but no intent packet is stored",
            "Restore packets/<job_id>.json; never rebuild a job without its packet.",
        )
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, sealed):
        raise ReturnError(
            "packet_mismatch",
            f"the stored intent packet hashes to {actual[:12]}, the job seals {sealed[:12]}",
            "Refuse: the intent packet changed after admission.",
        )
    try:
        packet = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReturnError("packet_mismatch", f"the stored intent packet is not valid JSON: {exc}") from exc
    if not isinstance(packet, dict):
        raise ReturnError("packet_mismatch", "the stored intent packet is not a JSON object")
    if not hmac.compare_digest(packets.digest(packet), sealed):
        raise ReturnError(
            "packet_mismatch",
            "the stored packet bytes are not the canonical bytes its digest was taken over",
            "Refuse: re-serialise the approved intent, never the stored bytes.",
        )
    if str(packet.get("job_id") or "") != job_id:
        raise ReturnError("packet_mismatch", f"the packet admits {packet.get('job_id')!r}, not {job_id!r}")
    identity = str((packet.get("repository") or {}).get("identity") or "")
    if identity != str(goal.get("repo") or ""):
        raise ReturnError("packet_mismatch", f"the packet names repository {identity!r}, the job {goal.get('repo')!r}")
    if str(packet.get("workflow_ref") or "") != str(goal.get("workflow_ref") or ""):
        raise ReturnError(
            "packet_mismatch",
            "the packet's pinned workflow_ref is not the job's pinned workflow_ref",
            "Refuse: a job runs under exactly the protocol it was admitted with.",
        )
    feature = str(packet.get("feature_id") or "")
    if not feature:
        raise ReturnError("packet_mismatch", "the intent packet names no feature_id")
    return packet, feature


def _resolution(state_dir: Path, job: Mapping[str, Any], packet: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate a descendant job's source resolution against its intent packet."""

    if packet is None:
        return None
    policy = packet.get("source_policy") or {}
    if str(policy.get("kind") or "") != "descendant":
        return None
    state = Path(state_dir)
    job_id = str(job.get("job_id") or "")
    path = handoff.resolution_path(state, job_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ReturnError(
            "resolution_mismatch",
            f"the intent packet names a descendant policy but {_rel(state, path)} is not recorded",
            "Refuse: a dependent job has no baseline without its source resolution.",
        ) from exc
    except OSError as exc:
        raise ReturnError("resolution_mismatch", f"the source resolution cannot be read: {exc}") from exc
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise ReturnError("resolution_mismatch", f"the source resolution is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ReturnError("resolution_mismatch", "the source resolution is not a JSON object")

    accepted = str(record.get("accepted_commit") or "")
    try:
        packets.validate_resolution(record, packet=packet, accepted_commit=accepted)
    except packets.PacketError as exc:
        raise ReturnError(
            "resolution_mismatch",
            f"the recorded resolution does not validate against the intent packet: {exc.code}: {exc}",
            "Refuse: re-derive the resolution from the enrolled policy; never edit it.",
        ) from exc

    goal = job.get("goal") or {}
    problems: list[str] = []
    if not hmac.compare_digest(str(record.get("packet_digest") or ""), str(goal.get("packet_digest") or "")):
        problems.append("its packet digest is not this job's sealed packet digest")
    if str(record.get("policy_id") or "") != str(policy.get("policy_id") or ""):
        problems.append("it names a different policy than the intent packet")
    dependencies = [str(item) for item in policy.get("dependencies") or []]
    if not dependencies or str(record.get("dependency_feature") or "") != dependencies[0]:
        problems.append("it names a different dependency feature")
    if str(record.get("derived_source_rev") or "") != accepted:
        problems.append("its derived source revision is not its accepted commit")
    if accepted != str(goal.get("base_rev") or ""):
        problems.append("its accepted commit is not the job's pinned base revision")
    if problems:
        raise ReturnError("resolution_mismatch", "the source resolution disagrees with the job: " + "; ".join(problems))

    policy_id = str(record.get("policy_id") or "")
    try:
        enrolled = handoff.load_policy(state, policy_id)
    except handoff.HandoffError as exc:
        raise ReturnError(
            "resolution_mismatch",
            f"the enrolled policy {policy_id} named by the resolution cannot be read: {exc.code}: {exc}",
        ) from exc
    policy_digest = handoff.policy_digest(enrolled)
    if not hmac.compare_digest(str(record.get("policy_digest") or ""), policy_digest):
        raise ReturnError(
            "resolution_mismatch",
            f"the resolution records policy digest {str(record.get('policy_digest'))[:12]}, "
            f"the enrolled policy hashes to {policy_digest[:12]}",
        )

    limitation = ""
    dependency_job = str(record.get("dependency_job") or "")
    if dependency_job:
        try:
            dependency = jobs.load_job(state, dependency_job, recover=False)
        except jobs.JobError as exc:
            limitation = f"dependency job {dependency_job} cannot be read here ({exc.code}); its accepted commit was not re-checked"
        else:
            try:
                dependency_commit, _attempts = transport.accepted_commit(state, dependency)
            except transport.TransportError as exc:
                limitation = f"dependency job {dependency_job} has no accepted wave to compare ({exc.code})"
            else:
                if dependency_commit != accepted:
                    raise ReturnError(
                        "resolution_mismatch",
                        f"dependency {dependency_job} accepts {dependency_commit[:12]}, "
                        f"the resolution derives {accepted[:12]}",
                    )
    return {
        "accepted_commit": accepted,
        "digest": hashlib.sha256(canonical_bytes(record)).hexdigest(),
        "policy_digest": policy_digest,
        "policy_id": policy_id,
        "dependency_job": dependency_job,
        "dependency_feature": str(record.get("dependency_feature") or ""),
        "contract_sha256": str(record.get("contract_sha256") or ""),
        "record": record,
        "limitation": limitation,
    }


def _waves(state_dir: Path, job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Match every accepted wave the job names against its durable wave record."""

    state = Path(state_dir)
    job_id = str(job.get("job_id") or "")
    integration = job.get("integration") or {}
    entries = [entry for entry in integration.get("accepted_waves") or [] if isinstance(entry, Mapping)]
    accepted: list[dict[str, Any]] = []
    for entry in entries:
        wave = _wave_number(entry.get("wave"))
        wave_id = revision.wave_id_for(job_id, wave)
        try:
            record = revision.read_wave(state, wave_id)
        except revision.RevisionError as exc:
            raise ReturnError(
                "wave_mismatch",
                f"the job names accepted wave {wave}, but {_rel(state, revision.wave_path(state, wave_id))} "
                f"is not usable evidence: {exc.code}: {exc.error}",
            ) from exc
        if record.get("accepted") is not True:
            raise ReturnError("wave_mismatch", f"wave record {wave_id} is not accepted, but the job counts it as accepted")
        if str(record.get("wave_id") or "") != wave_id:
            raise ReturnError("wave_mismatch", f"wave record {wave_id} names itself {record.get('wave_id')!r}")
        head = str(record.get("head_sha") or "")
        if not _is_sha(head):
            raise ReturnError("wave_mismatch", f"wave record {wave_id} does not name a full commit")
        if str(entry.get("commit") or "") != head:
            raise ReturnError(
                "wave_mismatch",
                f"the job records wave {wave} at {str(entry.get('commit'))[:12]}, "
                f"the durable record is at {head[:12]}",
            )
        attempts = [str(item) for item in record.get("attempts") or []]
        if not attempts:
            raise ReturnError("wave_mismatch", f"wave record {wave_id} lists no attempts")
        if entry.get("attempts") and sorted(str(a) for a in entry["attempts"]) != sorted(attempts):
            raise ReturnError(
                "wave_mismatch",
                f"the job records wave {wave}'s attempts as {sorted(str(a) for a in entry['attempts'])}, "
                f"the durable record lists {sorted(attempts)}",
            )
        problems = record.get("problems") or {}
        if isinstance(problems, Mapping) and problems:
            raise ReturnError("wave_mismatch", f"wave record {wave_id} is accepted but records problems: {dict(problems)}")
        suite = record.get("suite")
        if isinstance(suite, Mapping) and suite.get("exit_code") not in (0, None):
            raise ReturnError(
                "wave_mismatch",
                f"wave record {wave_id} is accepted but its suite exited {suite.get('exit_code')}",
            )
        accepted.append(
            {
                "wave": wave,
                "wave_id": wave_id,
                "head": head,
                "attempts": attempts,
                "record": record,
                "digest": hashlib.sha256(canonical_bytes(record)).hexdigest(),
                "entry": dict(entry),
            }
        )

    last = str(integration.get("last_commit") or "")
    if accepted:
        if last and last != accepted[-1]["head"]:
            raise ReturnError(
                "wave_mismatch",
                f"integration.last_commit is {last[:12]}, the last accepted wave is at {accepted[-1]['head'][:12]}",
            )
    elif last:
        raise ReturnError("wave_mismatch", "the job records an integration commit but no accepted wave")

    job_attempts = {str(a) for task in job.get("tasks") or [] for a in task.get("attempts") or []}
    for wave in accepted:
        unknown = [a for a in wave["attempts"] if a not in job_attempts]
        if unknown:
            raise ReturnError(
                "wave_mismatch",
                f"wave record {wave['wave_id']} integrates attempt(s) this job never dispatched: {unknown}",
            )
    covered = {a for wave in accepted for a in wave["attempts"]}
    for task in job.get("tasks") or []:
        if str(task.get("state") or "") != "accepted":
            continue
        latest = (task.get("attempts") or [None])[-1]
        if latest and str(latest) not in covered:
            raise ReturnError(
                "wave_mismatch",
                f"task {task.get('task_id')} is accepted but its attempt {latest} appears in no accepted wave",
            )
    return accepted


def _tasks(
    state_dir: Path,
    job: Mapping[str, Any],
    packet: Mapping[str, Any] | None = None,
    resolution: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Task rows plus the frozen-contract check for each task.

    A task that does not inline its contract falls back to the sealed evidence
    that *is* the approved contract for this job: the exact packet's frozen
    contract, or the source-bound contract in the validated resolution. Nothing
    is invented — a job with neither has no contract, and its criteria are the
    task's own acceptance statements.
    """

    rows: list[dict[str, Any]] = []
    for task in job.get("tasks") or []:
        if not isinstance(task, Mapping):
            continue
        contract = task.get("contract") if isinstance(task.get("contract"), Mapping) else None
        declared = str(task.get("contract_sha256") or "")
        source = "task" if contract else ""
        if contract is None and packet is not None and isinstance(packet.get("contract"), Mapping):
            contract = packet["contract"]
            declared = str(packet.get("contract_sha256") or declared)
            source = "packet"
        if contract is None and resolution is not None:
            record = resolution.get("record") or {}
            if isinstance(record.get("contract"), Mapping):
                contract = record["contract"]
                declared = str(record.get("contract_sha256") or declared)
                source = "resolution"
        contract_ok, problem = True, ""
        contract_sha = ""
        if contract:
            try:
                contract_sha = contracts.check_frozen(dict(contract))
            except contracts.ContractError as exc:
                contract_ok, problem = False, f"{exc.code}: {exc}"
            else:
                if declared and declared != contract_sha:
                    contract_ok = False
                    problem = f"contract_changed: the frozen contract hashes to {contract_sha[:12]}, the job records {declared[:12]}"
        rows.append(
            {
                "task": task,
                "task_id": str(task.get("task_id") or ""),
                "state": str(task.get("state") or "pending"),
                "attempts": [str(a) for a in task.get("attempts") or []],
                "contract": contract,
                "contract_source": source,
                "contract_sha256": contract_sha,
                "contract_ok": contract_ok,
                "problem": problem,
                "binding": task.get("binding") if isinstance(task.get("binding"), Mapping) else {},
                "provision": task.get("provision") if isinstance(task.get("provision"), Mapping) else {},
            }
        )
    return rows


def _signed_evidence(state_dir: Path, contract: Mapping[str, Any] | None, contract_sha: str) -> dict[str, Any]:
    """Load and validate the signed ``tdd-evidence/1`` records for one contract."""

    out: dict[str, Any] = {
        "available": False,
        "records": {},
        "paths": {},
        "test_first": True,
        "green": None,
        "green_error": "",
        "red": None,
        "red_error": "",
        "criteria": {},
        "tampered": "",
        "problem": "",
    }
    if not contract or not contract_sha:
        return out
    contract_id = str(contract.get("id") or "")
    if not contract_id:
        return out
    try:
        key = leases.evidence_key(Path(state_dir))
    except leases.LeaseError as exc:
        out["problem"] = f"signed evidence cannot be verified here: {exc.code}"
        return out
    try:
        records = tdd.load_records(Path(state_dir), contract_id, key)
    except tdd.TddError as exc:
        out["tampered"] = f"{exc.code}: {exc.error}"
        return out
    if not records:
        return out
    out["available"] = True
    latest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        phase = str(record.get("phase") or "")
        latest[phase] = record
        out["paths"][phase] = str(record.get("_path") or "")
    out["records"] = dict(latest)
    green = latest.get("green")
    red = latest.get("red")
    if green is not None:
        try:
            tdd.check_green(contract, contract_sha, red, green)
        except tdd.TddError as exc:
            out["green_error"] = f"{exc.code}: {exc.error}"
        else:
            out["green"] = green
            out["criteria"] = {str(k): str(v) for k, v in (green.get("criteria") or {}).items()}
    if red is not None:
        try:
            tdd.check_red(contract, contract_sha, red)
        except tdd.TddError as exc:
            out["red_error"] = f"{exc.code}: {exc.error}"
        else:
            out["red"] = red
    test_first = (contract.get("tdd") or {}).get("test_first") is not False
    if green is not None and green.get("test_first") is False:
        test_first = False
    if red is not None and red.get("test_first") is False:
        test_first = False
    out["test_first"] = test_first
    return out


def _verifications(
    state_dir: Path,
    waves: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    repo: Path | None,
) -> dict[str, dict[str, Any]]:
    """Load every verification record the job claims, and re-prove its commit."""

    state = Path(state_dir)
    claimed: list[str] = []
    for task in tasks:
        if task["state"] in {"verified", "integrating", "accepted"}:
            if not task["attempts"]:
                raise ReturnError(
                    "verification_missing",
                    f"task {task['task_id']} is {task['state']} but records no attempt",
                )
            claimed.append(task["attempts"][-1])
    for wave in waves:
        claimed.extend(wave["attempts"])

    records: dict[str, dict[str, Any]] = {}
    for attempt_id in dict.fromkeys(claimed):
        record, problem = _read_verify(state, attempt_id)
        if record is None:
            detail = "is absent" if problem == "missing" else "exists but cannot be read"
            raise ReturnError(
                "verification_missing",
                f"the job claims verification of attempt {attempt_id}, but verify-results/{attempt_id}.json {detail}",
            )
        if record.get("ok") is not True:
            raise ReturnError(
                "verification_missing",
                f"verify-results/{attempt_id}.json does not record an ok verification",
            )
        if str(record.get("attempt_id") or "") != attempt_id:
            raise ReturnError(
                "verification_missing",
                f"verify-results/{attempt_id}.json names attempt {record.get('attempt_id')!r}",
            )
        sha = _sha(record.get("sha"), f"the verification commit of {attempt_id}")
        base = str(record.get("base_rev") or "")
        if base and not _is_sha(base):
            raise ReturnError("verification_missing", f"the verification base of {attempt_id} is not a full commit")
        if repo is not None and not _commit_present(repo, sha):
            raise ReturnError(
                "verification_commit_absent",
                f"attempt {attempt_id} records commit {sha[:12]}, which is not present in {repo}",
                "Refuse: a verification record for a commit this repository does not contain is not evidence.",
            )
        if repo is not None and base and not _commit_present(repo, base):
            raise ReturnError(
                "verification_commit_absent",
                f"attempt {attempt_id} verifies against base {base[:12]}, which is not present in {repo}",
            )
        records[attempt_id] = record
    return records


def _transport(
    state_dir: Path,
    job: Mapping[str, Any],
    waves: Sequence[Mapping[str, Any]],
    authorization: Mapping[str, Any],
    resolution: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Match a publication claim to its receipt, or refuse the claim."""

    state = Path(state_dir)
    job_id = str(job.get("job_id") or "")
    goal = job.get("goal") or {}
    evidence = authorization.get("dependency_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    claim_ref = str(evidence.get("mirror_ref") or "")
    accepted = waves[-1]["head"] if waves else ""

    def check(receipt: Mapping[str, Any], path: Path, *, expect_job: str, expect_sha: str) -> dict[str, Any]:
        problems: list[str] = []
        if str(receipt.get("schema") or "") != "rizzstack.transport/1":
            problems.append(f"its schema is {receipt.get('schema')!r}")
        if str(receipt.get("job_id") or "") != expect_job:
            problems.append(f"it names job {receipt.get('job_id')!r}, not {expect_job!r}")
        if str(receipt.get("repo") or "") != str(goal.get("repo") or ""):
            problems.append(f"it names repository {receipt.get('repo')!r}")
        if str(receipt.get("source_sha") or "") != expect_sha:
            problems.append(
                f"it publishes {str(receipt.get('source_sha'))[:12]}, not {expect_sha[:12]}"
            )
        expected_ref = transport.ref_for(expect_job, expect_sha)
        if str(receipt.get("mirror_ref") or "") != expected_ref:
            problems.append(f"its mirror_ref is {receipt.get('mirror_ref')!r}, not the immutable {expected_ref!r}")
        attempts = receipt.get("attempts")
        if not isinstance(attempts, list) or not attempts or not all(isinstance(a, str) for a in attempts):
            problems.append("it records no attempt list")
        if not isinstance(receipt.get("recovered"), bool):
            problems.append("it does not say whether publication was recovered")
        if problems:
            raise ReturnError(
                "transport_mismatch",
                f"{_rel(state, path)} does not prove publication: " + "; ".join(problems),
            )
        return {
            "mirror_ref": str(receipt.get("mirror_ref") or ""),
            "source_sha": str(receipt.get("source_sha") or ""),
            "receipt_path": _rel(state, path),
        }

    if accepted and _is_sha(accepted):
        path = transport.receipt_path(state, job_id, accepted)
        if path.exists():
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReturnError("transport_mismatch", f"{_rel(state, path)} cannot be read: {exc}") from exc
            if not isinstance(receipt, dict):
                raise ReturnError("transport_mismatch", f"{_rel(state, path)} is not a JSON object")
            summary = check(receipt, path, expect_job=job_id, expect_sha=accepted)
            if claim_ref and claim_ref != summary["mirror_ref"]:
                raise ReturnError(
                    "transport_mismatch",
                    f"the job's dependency evidence claims {claim_ref!r}, the receipt proves {summary['mirror_ref']!r}",
                )
            return summary

    if claim_ref:
        dependency_job = str((resolution or {}).get("dependency_job") or evidence.get("job_id") or "")
        dependency_sha = str((resolution or {}).get("accepted_commit") or evidence.get("accepted_commit") or "")
        if dependency_job and dependency_sha and _is_sha(dependency_sha):
            path = transport.receipt_path(state, dependency_job, dependency_sha)
            if path.exists():
                try:
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ReturnError("transport_mismatch", f"{_rel(state, path)} cannot be read: {exc}") from exc
                if not isinstance(receipt, dict):
                    raise ReturnError("transport_mismatch", f"{_rel(state, path)} is not a JSON object")
                summary = check(receipt, path, expect_job=dependency_job, expect_sha=dependency_sha)
                if summary["mirror_ref"] != claim_ref:
                    raise ReturnError(
                        "transport_mismatch",
                        f"the job claims mirror publication {claim_ref!r}, the receipt proves {summary['mirror_ref']!r}",
                    )
                return summary
        raise ReturnError(
            "transport_mismatch",
            f"the job claims mirror publication {claim_ref!r} but no transport receipt backs it",
            "Refuse: a mirror ref is a claim until a receipt proves the exact commit was published.",
        )
    return None


# ---------------------------------------------------------------------------
# Acceptance classification
# ---------------------------------------------------------------------------


def _criteria_rows(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    contract = task["contract"]
    rows: list[dict[str, Any]] = []
    if contract and isinstance(contract.get("acceptance_criteria"), (list, tuple)):
        for criterion in contract["acceptance_criteria"]:
            if not isinstance(criterion, Mapping):
                continue
            rows.append(
                {
                    "id": str(criterion.get("id") or ""),
                    "statement": str(criterion.get("statement") or ""),
                    "tests": [str(t) for t in criterion.get("tests") or []],
                    "verification": str(criterion.get("verification") or "test"),
                    "task_id": task["task_id"],
                }
            )
        return rows
    acceptance = task["task"].get("acceptance")
    if not isinstance(acceptance, Mapping):
        return rows
    statements = [str(item) for item in acceptance.get("criteria") or [] if str(item).strip()]
    tests = [str(item) for item in acceptance.get("tests") or []]
    for statement in statements:
        rows.append(
            {
                "id": "",
                "statement": statement,
                "tests": tests,
                "verification": "test",
                "task_id": task["task_id"],
            }
        )
    return rows


def _classify(
    criterion: Mapping[str, Any],
    *,
    task: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    record_problem: str,
    signed: Mapping[str, Any],
    waves: Sequence[Mapping[str, Any]],
    state_dir: Path,
) -> tuple[str, list[str], list[str], str]:
    """Return ``(status, evidence, unrun, limitation)`` for one criterion."""

    state = Path(state_dir)
    evidence: list[str] = []
    unrun: list[str] = []
    limitation = ""
    verification = str(criterion.get("verification") or "test")

    if verification == "integration":
        if not waves:
            unrun.append(f"integration wave for {criterion['id'] or criterion['statement']}")
            return NOT_RUN, evidence, unrun, ""
        wave = waves[-1]
        evidence.append(_rel(state, revision.wave_path(state, wave["wave_id"])))
        return PASS, evidence, unrun, ""

    if verification == "review":
        green = signed.get("green")
        if signed.get("tampered"):
            return BLOCKED, evidence, unrun, f"signed review evidence is not authentic: {signed['tampered']}"
        if not signed.get("available"):
            return NOT_RUN, evidence, unrun, ""
        reviews = [path for phase, path in signed.get("paths", {}).items() if phase == "review"]
        if not reviews:
            unrun.append(f"signed review for {criterion['id'] or criterion['statement']}")
            return NOT_RUN, evidence, unrun, ""
        records = signed.get("records") or {}
        review = records.get("review") if isinstance(records, Mapping) else None
        role = str(((review or {}).get("produced_by") or {}).get("role") or "")
        if review is not None and review.get("classification") == "passed" and role in {"reviewer", "human"}:
            evidence.append(reviews[0])
            return PASS, evidence, unrun, ""
        return UNVERIFIED, evidence, unrun, "a review record exists but does not validate as an independent review"

    if verification != "test":
        return BLOCKED, evidence, unrun, f"unknown verification method {verification!r}; no independent check defines this criterion"

    if not task["contract_ok"]:
        return BLOCKED, evidence, unrun, f"the task's frozen contract cannot be used: {task['problem']}"

    tests = criterion.get("tests") or []
    ac_id = str(criterion.get("id") or "")

    # 1. Failures recorded by an executed check come first: they cannot be
    #    outvoted by any other record.
    if record is not None:
        suite = record.get("suite")
        scan = record.get("gitleaks")
        failed: list[str] = []
        if record.get("ok") is not True:
            failed.append("the verification record is not ok")
        if isinstance(suite, Mapping) and suite.get("exit_code") not in (None, 0):
            failed.append(f"the regression suite exited {suite.get('exit_code')}")
        if isinstance(scan, Mapping) and scan.get("exit_code") not in (None, 0):
            failed.append(f"gitleaks exited {scan.get('exit_code')}")
        if failed:
            evidence.append(_rel(state, verify_local.record_path(state, str(record.get("attempt_id")))))
            return FAIL, evidence, unrun, "; ".join(failed)
    green_record = signed.get("green")
    if signed.get("available") and green_record is None and signed.get("green_error"):
        records = signed.get("records") or {}
        candidate = records.get("green") if isinstance(records, Mapping) else None
        if isinstance(candidate, Mapping) and candidate.get("classification") != "passed":
            evidence.append(str(signed.get("paths", {}).get("green") or ""))
            return FAIL, [item for item in evidence if item], unrun, f"the signed GREEN record failed: {signed['green_error']}"

    # 2. PASS needs coverage plus a zero suite, or a validated signed GREEN.
    if record is not None:
        suite = record.get("suite")
        suite_ok = isinstance(suite, Mapping) and suite.get("exit_code") == 0
        covered = _covers(record.get("tests") or [], tests)
        if suite_ok and covered:
            evidence.append(_rel(state, verify_local.record_path(state, str(record.get("attempt_id")))))
            if ac_id and signed.get("criteria", {}).get(ac_id) == "passed":
                path = str(signed.get("paths", {}).get("green") or "")
                if path:
                    evidence.append(path)
            return PASS, evidence, unrun, ""
    if ac_id and signed.get("criteria", {}).get(ac_id) == "passed":
        path = str(signed.get("paths", {}).get("green") or "")
        if path:
            return PASS, [path], unrun, ""

    # 3. No PASS: say precisely why.
    if signed.get("tampered"):
        limitation = f"signed tdd evidence for this contract is not authentic: {signed['tampered']}"
    if record is None:
        if record_problem == "corrupt":
            return BLOCKED, evidence, unrun, "the verification record exists but cannot be read"
        if task["state"] in {"accepted", "verified", "integrating"} or task["task"].get("evidence"):
            return UNVERIFIED, evidence, unrun, "the task claims verification but no verification record exists"
        unrun.append(f"verification of {task['task_id']}")
        return NOT_RUN, evidence, unrun, limitation
    suite = record.get("suite")
    if not isinstance(suite, Mapping):
        return UNVERIFIED, evidence, unrun, limitation
    if not _covers(record.get("tests") or [], tests):
        return UNVERIFIED, evidence, unrun, (
            f"the verification record ran {list(record.get('tests') or [])}, "
            f"which does not cover {list(tests)}"
        )
    return UNVERIFIED, evidence, unrun, limitation


def _acceptance(
    state_dir: Path,
    tasks: Sequence[Mapping[str, Any]],
    records: Mapping[str, Mapping[str, Any]],
    waves: Sequence[Mapping[str, Any]],
    signed_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    state = Path(state_dir)
    signed_cache = signed_cache if signed_cache is not None else {}
    rows: list[dict[str, Any]] = []
    for task in tasks:
        contract = task["contract"]
        contract_sha = task["contract_sha256"]
        key = (str((contract or {}).get("id") or ""), contract_sha)
        if key not in signed_cache:
            signed_cache[key] = _signed_evidence(state, contract, contract_sha)
        signed = signed_cache[key]
        latest = task["attempts"][-1] if task["attempts"] else ""
        record = records.get(latest) if latest else None
        _found, problem = _read_verify(state, latest) if latest else (None, "missing")
        if record is not None:
            problem = ""
        for criterion in _criteria_rows(task):
            status, evidence, unrun, limitation = _classify(
                criterion,
                task=task,
                record=record,
                record_problem=problem,
                signed=signed,
                waves=waves,
                state_dir=state,
            )
            rows.append(
                {
                    "id": criterion["id"],
                    "statement": criterion["statement"],
                    "tests": list(criterion["tests"]),
                    "status": status,
                    "evidence": sorted(dict.fromkeys(item for item in evidence if item)),
                    "_unrun": unrun,
                    "_limitation": limitation,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Recovery evidence (M4)
# ---------------------------------------------------------------------------


def _recovery_rows(state_dir: Path, job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per durable recovery request, with its validated decision.

    Only durable files are read. A request or decision that no longer hashes to
    its recorded digest refuses the RETURN instead of being summarised away: a
    recovery decision is evidence, and evidence that cannot be re-proved is not
    evidence.
    """

    state = Path(state_dir)
    job_id = str(job.get("job_id") or "")
    try:
        requests = recovery.list_requests(state, job_id)
    except recovery.RecoveryError as exc:
        raise ReturnError(exc.code, exc.message, exc.fix) from exc
    if not requests:
        return []

    by_task: dict[str, Mapping[str, Any]] = {}
    for task in job.get("tasks") or []:
        if isinstance(task, Mapping):
            by_task[str(task.get("task_id") or "")] = task

    rows: list[dict[str, Any]] = []
    for request in requests:
        request_id = str(request.get("request_id") or "")
        task_id = str(request.get("task_id") or "")
        task = by_task.get(task_id) or {}
        # The ordinal lives in the durable filename, not in the document: a
        # canonical request has no volatile field.
        number = recovery.request_ordinal(state, request_id)
        try:
            decision = recovery.read_decision(state, request)
        except recovery.RecoveryError as exc:
            raise ReturnError(exc.code, exc.message, exc.fix) from exc
        recorded = task.get("recovery") if isinstance(task.get("recovery"), Mapping) else {}
        applied_entry = next(
            (
                entry
                for entry in task.get("recovery_applied") or []
                if isinstance(entry, Mapping) and str(entry.get("request_id") or "") == request_id
            ),
            None,
        )
        applied = applied_entry is not None or str(recorded.get("request_id") or "") == request_id
        applied_at = (
            str(applied_entry.get("at") or "")
            if isinstance(applied_entry, Mapping)
            else str(recorded.get("applied_at") or "")
        )
        escalated = str(recorded.get("request_id") or "") == request_id and bool(recorded.get("escalated"))
        supersession = task.get("contract_supersession") if isinstance(task.get("contract_supersession"), Mapping) else None
        if supersession is not None and str(supersession.get("request_id") or "") != request_id:
            supersession = None
        relative = f"{recovery.RECOVERY_DIR}/{job_id}/{task_id}.{number}.request.json"
        failure = request.get("failure") if isinstance(request.get("failure"), Mapping) else {}
        decision_block = None
        if decision is not None:
            supersedes = decision.get("supersedes")
            decision_block = {
                "decision": str(decision.get("decision") or ""),
                "adapter": str(decision.get("adapter") or ""),
                "decided_at": str(decision.get("decided_at") or ""),
                "rationale": str(decision.get("rationale") or ""),
                "corrected_tests": [str(item) for item in decision.get("corrected_tests") or []],
                "supersedes": dict(supersedes) if isinstance(supersedes, Mapping) else None,
                # A journal value recomputed from the canonical decision; the
                # document itself has no digest field.
                "digest": recovery.decision_digest(decision),
                "path": f"{recovery.RECOVERY_DIR}/{job_id}/{task_id}.{number}.decision.json",
            }
        rows.append(
            {
                "request_id": request_id,
                "task_id": task_id,
                "attempt_id": str(request.get("attempt_id") or ""),
                "failure": {
                    "class": str(failure.get("class") or ""),
                    "code": str(failure.get("code") or ""),
                    "evidence": [
                        dict(entry) for entry in failure.get("evidence") or [] if isinstance(entry, Mapping)
                    ],
                    "rationale": str(failure.get("rationale") or ""),
                },
                "contract": dict(request.get("contract") or {}),
                "supersession_permitted": bool(request.get("supersession_permitted")),
                "envelope": dict(request.get("envelope") or {}),
                "outstanding_question": str(request.get("outstanding_question") or ""),
                "request_path": relative,
                "request_digest": str(request.get("request_digest") or ""),
                "decision": decision_block,
                "applied": bool(applied),
                "applied_at": applied_at if applied else "",
                "escalated": bool(escalated),
                "supersedes": _supersession_view(decision_block, supersession),
            }
        )
    return rows


def _supersession_view(
    decision: Mapping[str, Any] | None, supersession: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """The old -> new contract pair, exactly as the receiver re-checks it.

    ``contract_id``/``contract_sha256`` name the frozen contract that was
    replaced; ``new_contract_id``/``new_contract_sha256`` and
    ``supersedes_sha256`` name the contract now in force on the task.
    """

    if not isinstance(supersession, Mapping):
        return None
    declared = decision.get("supersedes") if isinstance(decision, Mapping) else None
    declared = dict(declared) if isinstance(declared, Mapping) else {}
    new_sha = str(supersession.get("contract_sha256") or "")
    view = {
        "contract_id": str(supersession.get("supersedes") or declared.get("contract_id") or ""),
        "contract_sha256": str(supersession.get("supersedes_sha256") or declared.get("contract_sha256") or ""),
        "new_contract_id": str(supersession.get("contract_id") or ""),
        "new_contract_sha256": new_sha,
        "supersedes_sha256": new_sha,
        "path": str(supersession.get("path") or ""),
        "reprovisioned": bool(supersession.get("reprovisioned")),
    }
    return view


# ---------------------------------------------------------------------------
# Manifest assembly
# ---------------------------------------------------------------------------


def _manifest(state_dir: Path, job_id: str, *, repo: Path | None = None) -> dict[str, Any]:
    state = Path(state_dir)
    job = jobs.load_job(state, job_id)
    jid = str(job.get("job_id") or "")
    goal = job.get("goal") or {}
    authorization = jobs.load_authorization(state, jid)
    packet, feature_id = _packet(state, job)
    resolution = _resolution(state, job, packet)
    task_rows = _tasks(state, job, packet, resolution)
    waves = _waves(state, job)
    records = _verifications(state, waves, task_rows, repo)
    receipt = _transport(state, job, waves, authorization, resolution)
    signed_cache: dict[tuple[str, str], dict[str, Any]] = {}
    criteria = _acceptance(state, task_rows, records, waves, signed_cache)

    limitations: list[str] = []
    unrun: list[str] = []
    for criterion in criteria:
        unrun.extend(criterion.pop("_unrun", []))
        if criterion.get("_limitation"):
            limitations.append(str(criterion.pop("_limitation")))
        else:
            criterion.pop("_limitation", None)
    for task in task_rows:
        if task["contract_source"] in {"packet", "resolution"}:
            limitations.append(
                f"task {task['task_id']} does not inline its frozen contract; its criteria come from the "
                f"sealed {'intent packet' if task['contract_source'] == 'packet' else 'source resolution'}"
            )
        if task["contract_source"] == "" and _criteria_rows(task):
            limitations.append(
                f"task {task['task_id']} declares no frozen contract; its criteria are the task's own "
                "acceptance statements and carry no acceptance-criterion ids"
            )

    limitations.append("worker claims are self-reported; only verification, wave and transport records count as evidence")
    limitations.append("receiver_decision stays pending: bundlebees records the producer's evidence, never the receiver's decision")
    limitations.append("authority 'human' means the interactive authorization document; bundlebees cannot authenticate the person")
    if packet is None:
        limitations.insert(0, LEGACY_LABEL)
    if resolution is not None and resolution.get("limitation"):
        limitations.append(str(resolution["limitation"]))

    contracts_seen: dict[tuple[str, str], int] = {}
    for task in task_rows:
        if task["contract"] and task["contract_ok"]:
            key = (str(task["contract"].get("id") or ""), task["contract_sha256"])
            contracts_seen[key] = contracts_seen.get(key, 0) + len(_criteria_rows(task))
    contract_block = {"id": "", "sha256": ""}
    if contracts_seen:
        chosen = sorted(contracts_seen.items(), key=lambda item: (-item[1], item[0]))[0][0]
        contract_block = {"id": chosen[0], "sha256": chosen[1]}
        if len(contracts_seen) > 1:
            named = sorted(f"{cid or '<no id>'}@{sha[:12]}" for cid, sha in contracts_seen)
            limitations.append(
                "this job carries more than one frozen contract "
                f"({', '.join(named)}); the manifest names {chosen[0] or '<no id>'} and lists every task's criteria"
            )

    attempts: list[dict[str, Any]] = []
    for task in task_rows:
        project = str(task["binding"].get("project") or task["provision"].get("project") or "")
        for attempt_id in task["attempts"]:
            source_rev = ""
            try:
                lease = leases.load_lease(state, attempt_id)
            except leases.LeaseError:
                lease = {}
            source_rev = str(lease.get("source_rev") or "")
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "state": task["state"],
                    "project": project,
                    "source_rev": source_rev,
                }
            )

    verification_attempt = ""
    if waves:
        for candidate in reversed(waves[-1]["attempts"]):
            if candidate in records:
                verification_attempt = candidate
                break
    if not verification_attempt:
        for task in reversed(list(task_rows)):
            if task["state"] in {"accepted", "integrating", "verified"}:
                for candidate in reversed(task["attempts"]):
                    if candidate in records:
                        verification_attempt = candidate
                        break
            if verification_attempt:
                break
    if not verification_attempt:
        for task in reversed(list(task_rows)):
            for candidate in reversed(task["attempts"]):
                if candidate in records:
                    verification_attempt = candidate
                    break
            if verification_attempt:
                break
    record = records.get(verification_attempt) or {}
    verification_block = {
        "commit": str(record.get("sha") or ""),
        "files": [str(item) for item in record.get("files") or []],
        "base_rev": str(record.get("base_rev") or ""),
        "suite": record.get("suite"),
        "gitleaks": record.get("gitleaks"),
        "record_path": f"{verify_local.RECORD_DIR}/{verification_attempt}.json" if verification_attempt else "",
    }
    suite_value = verification_block["suite"]
    if not isinstance(suite_value, Mapping) or suite_value.get("exit_code") is None:
        unrun.append("regression suite result")
    if not isinstance(verification_block["gitleaks"], Mapping):
        unrun.append("gitleaks scan")

    gate_value = job.get("approval_gate")
    gate: Mapping[str, Any] = gate_value if isinstance(gate_value, Mapping) else {}
    authority_doc: Mapping[str, Any] = authorization if isinstance(authorization, Mapping) else {}
    gates = [str(item) for item in (gate.get("gates") or authority_doc.get("gates") or [])]
    pending = []
    for name in gates:
        path = state / jobs.APPROVALS_DIR / f"{jid}.{jobs._slug(name)}.json"
        if not path.exists():
            pending.append(name)

    if not waves:
        unrun.append("accepted integration wave")
    if receipt is None:
        unrun.append("transport receipt")

    signed_summary: dict[str, Any] = {"test_first": True, "red": None, "green": None}
    for task in task_rows:
        contract = task["contract"]
        if not contract:
            continue
        key = (str(contract.get("id") or ""), task["contract_sha256"])
        signed = signed_cache.get(key) or _signed_evidence(state, contract, task["contract_sha256"])
        signed_cache[key] = signed
        # The contract decides whether test-first applies; a signed record that
        # honestly says false also disables it. A missing RED is never invented.
        test_first = (contract.get("tdd") or {}).get("test_first") is not False
        if signed.get("available") and not signed.get("test_first"):
            test_first = False
        signed_summary["test_first"] = test_first
        if signed.get("available"):
            if signed.get("red") is not None:
                signed_summary["red"] = str(signed.get("paths", {}).get("red") or "")
            if signed.get("green") is not None:
                signed_summary["green"] = str(signed.get("paths", {}).get("green") or "")
            if signed.get("red_error"):
                limitations.append(f"signed RED does not validate: {signed['red_error']}")
            if signed.get("green_error"):
                limitations.append(f"signed GREEN does not validate: {signed['green_error']}")
        if not test_first:
            signed_summary["red"] = None
        break
    if signed_summary["test_first"] and signed_summary["red"] is None and any(task["contract"] for task in task_rows):
        unrun.append("signed RED evidence (test_first is true and no valid RED is recorded)")

    integration_block = {
        "commit": waves[-1]["head"] if waves else "",
        "wave": waves[-1]["wave"] if waves else 0,
        "evidence": str(waves[-1]["entry"].get("evidence") or "") if waves else "",
        "accepted_wave_digest": waves[-1]["digest"] if waves else "",
    }

    manifest: dict[str, Any] = {
        "schema": RETURN_SCHEMA,
        "feature_id": feature_id,
        "job_id": jid,
        "packet_digest": str(goal.get("packet_digest") or ""),
        "workflow_ref": str(goal.get("workflow_ref") or ""),
        "repository": {
            "identity": str(goal.get("repo") or ""),
            "source_rev": str(goal.get("base_rev") or ""),
        },
        "authorization": {
            "reference": str((job.get("authorization") or {}).get("ref") or f"{jobs.AUTH_DIR}/{jid}.json"),
            "approved_by": str(authorization.get("by") or ""),
            "approved_at": str(authorization.get("at") or ""),
            "authority": str(authorization.get("authority") or "human"),
        },
        "descendant": (
            {
                "policy_id": resolution["policy_id"],
                "policy_digest": resolution["policy_digest"],
                "source_resolution_digest": resolution["digest"],
                "dependency_feature": resolution["dependency_feature"],
                "dependency_job": resolution["dependency_job"],
                "accepted_commit": resolution["accepted_commit"],
            }
            if resolution is not None
            else None
        ),
        "contract": contract_block,
        "acceptance": criteria,
        "red_green": signed_summary,
        "verification": verification_block,
        "integration": integration_block,
        "transport": receipt,
        "attempts": attempts,
        "limitations": sorted(dict.fromkeys(item for item in limitations if item)),
        "unrun": sorted(dict.fromkeys(item for item in unrun if item)),
        "pending_approvals": sorted(dict.fromkeys(pending)),
        "receiver_decision": {"status": "pending", "decided_by": None, "decided_at": None, "note": None},
    }
    recovery_rows = _recovery_rows(state, job)
    if recovery_rows:
        # Optional by design: a job with no recovery has no key at all, so the
        # legacy manifest keeps its exact shape.
        manifest["recovery"] = recovery_rows
        if not any(row["decision"] for row in recovery_rows):
            limitations.append(
                "a task is rejected and its recovery request has no validated decision yet; "
                "no corrective attempt is dispatched until one is recorded"
            )
    manifest["evidence_digest"] = evidence_digest(manifest)

    _refuse_uncovered(job, waves, criteria, task_rows)
    return manifest


def _refuse_uncovered(
    job: Mapping[str, Any],
    waves: Sequence[Mapping[str, Any]],
    criteria: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
) -> None:
    """A fully accepted job whose criteria are not all PASS is a contradiction."""

    if not tasks or not waves:
        return
    if any(task["state"] != "accepted" for task in tasks):
        return
    uncovered = [f"{c['id'] or c['statement']}: {c['status']}" for c in criteria if c["status"] != PASS]
    if not uncovered:
        return
    raise ReturnError(
        "acceptance_uncovered",
        "job "
        f"{job.get('job_id')} is integrated and every task is accepted, but these criteria have no "
        f"independent PASS evidence: {'; '.join(uncovered)}",
        "Refuse: an accepted state is not evidence. Re-run verification over the criteria's own tests "
        "or correct the acceptance record.",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render(manifest: Mapping[str, Any]) -> str:
    """Render RETURN.md from the manifest. Deterministic: no clock, no paths."""

    feature = str(manifest.get("feature_id") or manifest.get("job_id") or "")
    lines: list[str] = [f"# RETURN — {_cell(feature)}", ""]
    lines.append(f"- schema: `{manifest.get('schema')}`")
    lines.append(f"- job: `{manifest.get('job_id')}`")
    lines.append(f"- feature: `{manifest.get('feature_id') or ''}`")
    lines.append(f"- intent packet digest: `{manifest.get('packet_digest') or '(none — legacy packet-free job)'}`")
    lines.append(f"- workflow ref: `{manifest.get('workflow_ref') or '(none)'}`")
    repository = manifest.get("repository") or {}
    lines.append(f"- repository: `{repository.get('identity')}`")
    lines.append(f"- source revision: `{repository.get('source_rev')}`")
    authorization = manifest.get("authorization") or {}
    lines.append(
        f"- authorization: `{authorization.get('reference')}` by `{authorization.get('approved_by')}` "
        f"({authorization.get('authority')}) at {authorization.get('approved_at')}"
    )
    lines.append(f"- receiver decision: `{(manifest.get('receiver_decision') or {}).get('status')}`")
    lines.append("")

    descendant = manifest.get("descendant")
    if isinstance(descendant, Mapping):
        lines += [
            "## Descendant source",
            "",
            f"- policy: `{descendant.get('policy_id')}` (digest `{descendant.get('policy_digest')}`)",
            f"- source resolution digest: `{descendant.get('source_resolution_digest')}`",
            f"- dependency: feature `{descendant.get('dependency_feature')}`, job `{descendant.get('dependency_job')}`",
            f"- accepted commit: `{descendant.get('accepted_commit')}`",
            "",
        ]

    lines += ["## Acceptance", "", "| Criterion | Statement | Status | Tests | Evidence |", "| --- | --- | --- | --- | --- |"]
    acceptance = manifest.get("acceptance") or []
    if not acceptance:
        lines.append("| — | (no declared acceptance criteria) | NOT RUN | — | — |")
    for item in acceptance:
        lines.append(
            "| `{id}` | {statement} | {status} | {tests} | {evidence} |".format(
                id=_cell(item.get("id") or "—"),
                statement=_cell(item.get("statement")),
                status=_cell(item.get("status")),
                tests=_cell(", ".join(item.get("tests") or []) or "—"),
                evidence=_cell(", ".join(item.get("evidence") or []) or "—"),
            )
        )
    lines.append("")

    red_green = manifest.get("red_green") or {}
    lines += [
        "## Red / green",
        "",
        f"- test_first: `{red_green.get('test_first')}`",
        f"- red: `{red_green.get('red') or '(none)'}`",
        f"- green: `{red_green.get('green') or '(none)'}`",
        "",
    ]

    verification = manifest.get("verification") or {}
    lines += [
        "## Verification",
        "",
        f"- commit: `{verification.get('commit') or '(none)'}`",
        f"- base revision: `{verification.get('base_rev') or '(none)'}`",
        f"- files: {', '.join(f'`{item}`' for item in verification.get('files') or []) or '(none)'}",
        f"- suite: `{json.dumps(verification.get('suite'), sort_keys=True)}`",
        f"- gitleaks: `{json.dumps(verification.get('gitleaks'), sort_keys=True)}`",
        f"- record: `{verification.get('record_path') or '(none)'}`",
        "",
    ]

    integration = manifest.get("integration") or {}
    lines += [
        "## Integration",
        "",
        f"- commit: `{integration.get('commit') or '(none)'}`",
        f"- wave: `{integration.get('wave')}`",
        f"- accepted wave digest: `{integration.get('accepted_wave_digest') or '(none)'}`",
        f"- evidence: {_cell(integration.get('evidence') or '(none)')}",
        "",
    ]

    transport_block = manifest.get("transport")
    lines += ["## Transport", ""]
    if isinstance(transport_block, Mapping):
        lines += [
            f"- mirror ref: `{transport_block.get('mirror_ref')}`",
            f"- source sha: `{transport_block.get('source_sha')}`",
            f"- receipt: `{transport_block.get('receipt_path')}`",
        ]
    else:
        lines.append("- none recorded (no publication is claimed)")
    lines.append("")

    lines += ["## Attempts", "", "| Attempt | State | Project | Source revision |", "| --- | --- | --- | --- |"]
    attempts = manifest.get("attempts") or []
    if not attempts:
        lines.append("| — | — | — | — |")
    for item in attempts:
        lines.append(
            "| `{attempt}` | {state} | {project} | `{rev}` |".format(
                attempt=_cell(item.get("attempt_id")),
                state=_cell(item.get("state")),
                project=_cell(item.get("project") or "—"),
                rev=_cell(item.get("source_rev") or "—"),
            )
        )
    lines.append("")

    recovery_rows = manifest.get("recovery") or []
    if recovery_rows:
        lines += [
            "## Recovery",
            "",
            "| Request | Task | Failure | Decision | Applied |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in recovery_rows:
            failure = row.get("failure") or {}
            decision = row.get("decision") or {}
            lines.append(
                "| `{request}` | {task} | {cls}/{code} | {decided} | {applied} |".format(
                    request=_cell(row.get("request_id")),
                    task=_cell(row.get("task_id")),
                    cls=_cell(failure.get("class")),
                    code=_cell(failure.get("code")),
                    decided=_cell(decision.get("decision") or "awaiting decision"),
                    applied="yes" if row.get("applied") else "no",
                )
            )
        lines.append("")
        for row in recovery_rows:
            decision = row.get("decision") or {}
            if decision:
                lines.append(
                    f"- `{_cell(row.get('request_id'))}`: {_cell(decision.get('decision'))} by "
                    f"`{_cell(decision.get('adapter'))}` — {_cell(decision.get('rationale') or '(no rationale given)')}"
                )
            supersession = row.get("supersedes")
            if isinstance(supersession, Mapping):
                lines.append(
                    f"  - contract `{_cell(supersession.get('new_contract_id'))}` "
                    f"(`{str(supersession.get('new_contract_sha256') or '')[:12]}`) supersedes "
                    f"`{_cell(supersession.get('contract_id'))}` "
                    f"(`{str(supersession.get('contract_sha256') or '')[:12]}`)"
                )
            if row.get("escalated"):
                lines.append("  - escalated: this task requires approval and will not dispatch again")
        lines.append("")

    for title, key in (("Unrun", "unrun"), ("Pending approvals", "pending_approvals"), ("Limitations", "limitations")):
        lines += [f"## {title}", ""]
        values = manifest.get(key) or []
        if not values:
            lines.append("- none")
        for value in values:
            lines.append(f"- {_cell(value)}")
        lines.append("")

    lines += ["## Evidence digest", "", f"`{manifest.get('evidence_digest')}`", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def build(state_dir: Path, job_id: str, *, repo: Path | None = None) -> dict[str, Any]:
    """Validate every relationship, then write ``return.json`` and ``RETURN.md``.

    Refuses with a named code instead of writing a plausible-looking report.
    Repeated calls on the same durable snapshot write byte-identical files.
    """

    manifest = _manifest(Path(state_dir), job_id, repo=repo)
    _atomic_write(return_path(Path(state_dir), job_id), document_bytes(manifest))
    _atomic_write(render_path(Path(state_dir), job_id), render(manifest).encode("utf-8"))
    return manifest


def verify(state_dir: Path, job_id: str, *, repo: Path | None = None) -> dict[str, Any]:
    """Rebuild from durable state and compare against the on-disk report.

    Three independent comparisons: the recorded digest against the digest of the
    file's own body (a hand edit), the rebuilt digest against the recorded one
    (a fabricated or stale report), and the rendered Markdown against the
    rebuilt manifest. Any disagreement is ``report_tampered``.
    """

    state = Path(state_dir)
    on_disk = read_manifest(state, job_id)
    if on_disk is None:
        raise ReturnError(
            "report_tampered",
            f"no RETURN report exists at {_rel(state, return_path(state, job_id))}; nothing to verify",
            "Generate it with `bundlebees job report` first.",
        )
    recorded = str(on_disk.get("evidence_digest") or "")
    body_digest = evidence_digest(on_disk)
    if not recorded or not hmac.compare_digest(recorded, body_digest):
        raise ReturnError(
            "report_tampered",
            f"the recorded evidence_digest {recorded[:12] or '(missing)'} does not match the file's own "
            f"body ({body_digest[:12]}); the report was edited after it was written",
        )

    fresh = _manifest(state, job_id, repo=repo)
    if not hmac.compare_digest(str(fresh["evidence_digest"]), recorded):
        raise ReturnError(
            "report_tampered",
            f"the recorded evidence_digest {recorded[:12]} does not match the durable state "
            f"({str(fresh['evidence_digest'])[:12]}); the report was edited or the state advanced",
            "Rebuild with `bundlebees job report`; never hand-edit a RETURN.",
        )
    try:
        rendered = render_path(state, job_id).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReturnError("report_tampered", f"{RENDER_FILE} cannot be read: {exc}") from exc
    if rendered != render(fresh):
        raise ReturnError(
            "report_tampered",
            f"{RENDER_FILE} does not match the manifest it claims to render",
        )
    return {
        "ok": True,
        "job_id": str(job_id),
        "schema": RETURN_SCHEMA,
        "evidence_digest": recorded,
        "return": _rel(state, return_path(state, job_id)),
        "markdown": _rel(state, render_path(state, job_id)),
    }
