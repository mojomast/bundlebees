"""Publish an accepted integration commit to a governed development mirror.

Identities stay separate: the repository identity (`owner/name`) is what the human
authorized; the artifact identity is the exact accepted commit plus its verifier
and integration provenance; the mirror is only the transport.

Publication is create-only: a job-scoped ref `refs/bb/accepted/<job>/<sha>` is
created once and never moved. Any different commit already at that ref is a hard
refusal, and the check is enforced by Git's own ref update rather than a
check-then-write race.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from . import job as jobs
from . import verify_local


class TransportError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def _git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
    if check and done.returncode != 0:
        raise TransportError("git_error", f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}", "Inspect the mirror and the accepted commit.")
    return done


def _sha(value: Any, what: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise TransportError("revision_invalid", f"{what} is not a full 40-hex commit", "Fix the record; do not guess.")
    return text


def accepted_commit(state_dir: Path, job: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Validate the accepted-wave record and its verified attempts independently.

    ``integration.last_commit`` alone is not evidence: the wave record must name
    the same commit, and every attempt it integrates must have an ok verification
    record whose commit is an ancestor of the accepted commit (checked by the
    caller, which has the repository).
    """
    integration = job.get("integration") or {}
    waves = [w for w in integration.get("accepted_waves") or [] if isinstance(w, Mapping)]
    if not waves:
        raise TransportError("dependency_not_accepted", "the dependency has no accepted integration wave", "Integrate it first; a card status is not evidence.")
    wave = waves[-1]
    wave_commit = _sha(wave.get("commit"), "accepted wave commit")
    last = integration.get("last_commit")
    if last and _sha(last, "integration.last_commit") != wave_commit:
        raise TransportError("provenance_mismatch", "integration.last_commit and the accepted wave disagree", "Refuse: the artifact identity is ambiguous.")
    attempts = [str(a) for a in wave.get("attempts") or []]
    if not attempts:
        attempts = [str((task.get("attempts") or [""])[-1]) for task in job.get("tasks") or [] if task.get("state") == "accepted"]
    if not attempts:
        raise TransportError("no_verified_attempt", "the accepted wave names no verified attempts", "Re-integrate from verified work.")
    for attempt_id in attempts:
        record = verify_local.read_record(Path(state_dir), attempt_id)
        if not record or record.get("ok") is not True or not record.get("sha"):
            raise TransportError("attempt_unverified", f"{attempt_id} has no accepted verification record", "Refuse to publish unverified work.")
    return wave_commit, attempts


def receipt_path(state_dir: Path, job_id: str, sha: str) -> Path:
    return Path(state_dir) / "transport" / f"{job_id}.{sha[:12]}.json"


def _write_receipt(path: Path, body: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(body), indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _ref_for(job_id: str, sha: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in job_id)
    return f"refs/bb/accepted/{safe}/{sha}"


def _remote_ref(mirror: Path, ref: str) -> str | None:
    done = _git(["ls-remote", str(mirror), ref], Path(mirror).parent, check=False)
    if done.returncode != 0:
        raise TransportError("mirror_unreachable", f"the mirror cannot be read: {done.stderr.strip()[:200]}", "Check the mirror path and policy.")
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return _sha(parts[0], "mirror ref")
    return None


def publish(state_dir: Path, job_id: str, *, repo: Path, mirror: Path, repo_identity: str,
            root_rev: str) -> dict[str, Any]:
    """Create the immutable ref for an accepted commit; idempotent and recoverable."""

    job = jobs.load_job(Path(state_dir), job_id)
    sha, attempts = accepted_commit(Path(state_dir), job)
    repo_identity = str(repo_identity or (job.get("goal") or {}).get("repo") or "")
    if not repo_identity:
        raise TransportError("repo_missing", "the job has no repository identity", "Fix the job goal.")
    root = _sha(root_rev, "authorized root revision")

    exists = _git(["cat-file", "-e", f"{sha}^{{commit}}"], Path(repo), check=False)
    if exists.returncode != 0:
        raise TransportError("commit_missing", f"{sha} is not present in the coordinator repository", "Refuse to publish a commit that does not exist here.")
    ancestor = _git(["merge-base", "--is-ancestor", root, sha], Path(repo), check=False)
    if ancestor.returncode != 0:
        raise TransportError("not_a_descendant", f"{sha} does not descend from {root}", "Refuse to publish an unrelated history.")
    for attempt_id in attempts:
        record = verify_local.read_record(Path(state_dir), attempt_id)
        verified = str(record.get("sha") or "")
        if _git(["merge-base", "--is-ancestor", verified, sha], Path(repo), check=False).returncode != 0:
            raise TransportError("provenance_mismatch", f"{attempt_id}'s verified commit is not an ancestor of {sha}", "Refuse: the accepted commit does not contain the verified work.")

    ref = _ref_for(job_id, sha)
    path = receipt_path(Path(state_dir), job_id, sha)
    existing = _remote_ref(Path(mirror), ref)
    if existing is not None:
        if existing != sha:
            raise TransportError("ref_conflict", f"{ref} already points at {existing}, not {sha}", "Hard refusal: immutable refs are never moved.")
        receipt = {"schema": "rizzstack.transport/1", "job_id": job_id, "repo": repo_identity,
                   "source_sha": sha, "attempts": attempts, "mirror_ref": ref, "recovered": True,
                   "published_at": _now()}
        _write_receipt(path, receipt)
        return receipt

    push = _git(["push", "--porcelain", str(mirror), f"{sha}:{ref}"], Path(repo), check=False)
    if push.returncode != 0:
        # A concurrent publisher may have created it with the same commit.
        raced = _remote_ref(Path(mirror), ref)
        if raced == sha:
            receipt = {"schema": "rizzstack.transport/1", "job_id": job_id, "repo": repo_identity,
                       "source_sha": sha, "attempts": attempts, "mirror_ref": ref, "recovered": True,
                       "published_at": _now()}
            _write_receipt(path, receipt)
            return receipt
        raise TransportError("publish_failed", f"the mirror refused the ref: {push.stderr.strip()[:200]}", "Check mirror permissions and ref ownership.")

    advertised = _remote_ref(Path(mirror), ref)
    if advertised != sha:
        raise TransportError("publish_unverified", f"the mirror advertises {advertised}, expected {sha}", "Refuse: publication is not proven.")
    receipt = {"schema": "rizzstack.transport/1", "job_id": job_id, "repo": repo_identity,
               "source_sha": sha, "attempts": attempts, "mirror_ref": ref, "recovered": False,
               "published_at": _now()}
    _write_receipt(path, receipt)
    return receipt


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
