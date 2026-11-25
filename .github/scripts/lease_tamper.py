#!/usr/bin/env python3
"""Lease/tamper gate: a slice may not change files outside its lease.

The gate answers one question for a pull request: **after the verify boundary,
did any commit touch a path the slice does not own, or a frozen test file?**

A slice contract is an ``acdd/1`` contract (``scope.assigned_files``,
``scope.test_files``, ``scope.prohibited``, ``scope.read_only`` and
``source_rev``) or a lease record with the same ``scope`` shape plus a
``source_rev``/``verify_sha``.  The contract path is resolved, in order, from:

1. ``--contract`` (used by tests and local runs);
2. a pull-request label ``lease-contract:<path>`` (``lease:<path>`` also works);
3. a pull-request body line ``Lease-Contract: <path>`` (``Contract-Path:`` also
   works);
4. the repository convention ``.rizzstack/lease.json`` (base revision first,
   then the head revision).

A ``.rizzstack/lease.json`` file may be a pointer::

    {"contract_path": ".rizzstack/contracts/slice.json", "verify_sha": "<40-hex>"}

or the lease/contract object itself.  The verify boundary is, in order:
``--verify-sha``, the ``lease-verify:<sha>`` label, a ``Lease-Verify: <sha>``
body line, the lease file's ``verify_sha``/``verified_sha``/``verification.sha``,
then the contract's ``source_rev``.

Every non-merge commit in ``<boundary>..<head>`` that is not reachable from the
PR base is checked.  Merge commits are not authored changes: a merge whose
parent is on the base is ignored, and only conflict resolutions that differ
from both parents are checked.  The contract file itself (and
``.rizzstack/lease.json``) is metadata and is exempt.

Exit status: 0 = clean or no pull request in the event; 1 = tamper, an invalid
lease, or an unresolvable contract (fail closed).  Standard library only.

Usage:
    python3 .github/scripts/lease_tamper.py [EVENT_JSON_PATH]

The event path defaults to ``$GITHUB_EVENT_PATH``.  For local/test runs:
``--base``, ``--head``, ``--labels``, ``--body``, ``--verify-sha`` and
``--contract`` bypass the event payload.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.parse
import urllib.request

LEASE_FILE = ".rizzstack/lease.json"
LABEL_CONTRACT_PREFIXES = ("lease-contract:", "lease:")
LABEL_VERIFY_PREFIX = "lease-verify:"
BODY_CONTRACT = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:lease-contract|contract-path)[ \t]*:[ \t]*`?([^`\s]+)`?"
)
BODY_VERIFY = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:lease-verify|verify-sha)[ \t]*:[ \t]*`?([0-9a-fA-F]{40,64})`?"
)
HEX_REV = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
HTTP_TIMEOUT_SECONDS = 20
MAX_PAGES = 10


class GateError(RuntimeError):
    """The lease/contract could not be resolved or applied (fail closed)."""


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _run(repo, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise GateError(
            "git " + " ".join(args) + " failed: " + (detail[-1] if detail else "exit %d" % proc.returncode)
        )
    return proc


def _out(repo, *args):
    return _run(repo, *args).stdout.strip()


def _exists(repo, revision):
    return _run(repo, "cat-file", "-e", revision + "^{commit}", check=False).returncode == 0


def _is_ancestor(repo, older, newer):
    return _run(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _file_at(repo, revision, path):
    proc = _run(repo, "show", revision + ":" + path, check=False)
    return proc.stdout if proc.returncode == 0 else None


def _resolve_rev(repo, value):
    """Return a full commit sha for a local ref/short sha; leave hex alone."""
    value = (value or "").strip()
    if HEX_REV.match(value.lower()):
        return value.lower()
    proc = _run(repo, "rev-parse", "--verify", value + "^{commit}", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else value


def _commit_paths(repo, sha):
    """Paths changed by one commit, against its first parent (renames split)."""
    proc = _run(
        repo,
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-only",
        "-r",
        "--no-renames",
        sha,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _diff_paths(repo, older, newer):
    proc = _run(repo, "diff", "--name-only", "--no-renames", older, newer)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _parents(repo, sha):
    fields = _out(repo, "rev-list", "--parents", "-n", "1", sha).split()
    return fields[1:]


def _subject(repo, sha):
    return _out(repo, "log", "-1", "--format=%s", sha)


# ---------------------------------------------------------------------------
# event and contract resolution
# ---------------------------------------------------------------------------


class Target:
    def __init__(self, base, head, labels, body, number=None):
        self.base = base
        self.head = head
        self.labels = list(labels or [])
        self.body = body or ""
        self.number = number


def _labels_of(pull_request):
    return [
        str((label or {}).get("name", "")).strip()
        for label in (pull_request.get("labels") or [])
        if (label or {}).get("name")
    ]


def _http_get_json(url, token):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lease-tamper-gate",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8")), response.headers.get("Link") or ""


def _next_page(link_header):
    for part in (link_header or "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return ""


def _pull_requests_for_commit(repo, sha, token, api_url="https://api.github.com"):
    url = "%s/repos/%s/commits/%s/pulls?per_page=100" % (
        api_url,
        repo,
        urllib.parse.quote(sha),
    )
    pulls = []
    for _ in range(MAX_PAGES):
        payload, link = _http_get_json(url, token)
        if not isinstance(payload, list):
            raise GateError("the merge-queue pull-request lookup did not return a list")
        pulls.extend(item for item in payload if isinstance(item, dict))
        url = _next_page(link)
        if not url:
            return pulls
    raise GateError("the merge-queue pull-request lookup did not terminate")


def target_from_event(payload, event_name, environ):
    """Return a Target, or None for an event that carries no pull request."""
    name = (event_name or "").strip()
    if not name:
        if "pull_request" in payload:
            name = "pull_request"
        elif "merge_group" in payload:
            name = "merge_group"
        else:
            name = "other"

    if name == "pull_request":
        pull = payload.get("pull_request") or {}
        base = ((pull.get("base") or {}).get("sha") or "").strip()
        head = ((pull.get("head") or {}).get("sha") or "").strip()
        if not base or not head:
            raise GateError("the pull_request payload has no base/head sha")
        return Target(base, head, _labels_of(pull), pull.get("body") or "", pull.get("number"))

    if name == "merge_group":
        group = payload.get("merge_group") or {}
        head = (group.get("head_sha") or "").strip()
        if not head:
            raise GateError("the merge_group payload has no head_sha")
        repo = (environ.get("GITHUB_REPOSITORY") or "").strip()
        token = (environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN") or "").strip()
        if not repo or not token:
            raise GateError(
                "merge_group needs GITHUB_REPOSITORY and GITHUB_TOKEN to resolve the queued pull request"
            )
        pulls = _pull_requests_for_commit(repo, head, token)
        pull = next(
            (item for item in pulls if ((item.get("head") or {}).get("sha") == head)),
            pulls[0] if pulls else None,
        )
        if pull is None:
            raise GateError(
                "merge_group head %s is not associated with a pull request; the lease cannot be resolved"
                % head
            )
        base = ((pull.get("base") or {}).get("sha") or "").strip()
        if not base:
            raise GateError("the queued pull request has no base sha")
        return Target(base, head, _labels_of(pull), pull.get("body") or "", pull.get("number"))

    return None


def _normalise(path):
    return posixpath.normpath(str(path).replace("\\", "/").strip())


def _valid_lease_path(path):
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    normal = _normalise(path)
    return normal != "." and normal != ".." and not normal.startswith("../")


def _contract_path(target, explicit):
    if explicit:
        return _normalise(explicit), "--contract"

    for label in target.labels:
        lowered = label.lower()
        for prefix in LABEL_CONTRACT_PREFIXES:
            if lowered.startswith(prefix) and len(label.strip()) > len(prefix):
                return _normalise(label.strip()[len(prefix):]), "label %r" % label

    match = BODY_CONTRACT.search(target.body)
    if match:
        return _normalise(match.group(1)), "PR body"

    return None, None


def _verify_sha(explicit, target, lease_doc, contract):
    if explicit:
        return explicit.strip().lower(), "verify sha"

    for label in target.labels:
        if label.lower().startswith(LABEL_VERIFY_PREFIX):
            value = label.strip()[len(LABEL_VERIFY_PREFIX):].strip().lower()
            if value:
                return value, "verify sha"

    match = BODY_VERIFY.search(target.body)
    if match:
        return match.group(1).lower(), "verify sha"

    if isinstance(lease_doc, dict):
        for key in ("verify_sha", "verified_sha"):
            value = lease_doc.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower(), "verify sha"
        verify = lease_doc.get("verify")
        if isinstance(verify, dict) and isinstance(verify.get("sha"), str):
            return verify["sha"].strip().lower(), "verify sha"

    for key in ("verify_sha", "verified_sha"):
        value = contract.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower(), "verify sha"
    verify = contract.get("verify")
    if isinstance(verify, dict) and isinstance(verify.get("sha"), str):
        return verify["sha"].strip().lower(), "verify sha"

    source = contract.get("source_rev")
    if isinstance(source, str) and source.strip():
        return source.strip().lower(), "source sha"

    return None, None


def _read_contract_object(repo, revision, path):
    raw = _file_at(repo, revision, path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise GateError("contract %r at %s is not valid JSON: %s" % (path, revision[:12], exc))


def resolve_contract(repo, target, explicit_path=None):
    """Return (contract, contract_path, lease_doc, source_revision).

    ``source_revision`` is the revision the contract document was read from
    (head or base), so later checks know whether it is branch-controlled.
    """
    path, origin = _contract_path(target, explicit_path)
    lease_doc = None
    contract_revision = target.head

    if path:
        for revision in (target.head, target.base):
            doc = _read_contract_object(repo, revision, path)
            if doc is not None:
                lease_doc = doc
                contract_revision = revision
                break
        else:
            raise GateError(
                "contract %r (from %s) does not exist at PR head %s or base %s"
                % (path, origin, target.head[:12], target.base[:12])
            )
    else:
        path = LEASE_FILE
        # The convention file may live on the base branch (a coordinator-owned
        # lease) or be introduced by the slice itself (a first-slice lease).
        for revision in (target.base, target.head):
            doc = _read_contract_object(repo, revision, path)
            if doc is not None:
                lease_doc = doc
                contract_revision = revision
                origin = "%s at %s" % (LEASE_FILE, "base" if revision == target.base else "head")
                break
        if lease_doc is None:
            raise GateError(
                "no slice contract for %s (head %s): no 'lease-contract:<path>' label, "
                "no 'Lease-Contract: <path>' body line, and no %s at head or base. "
                "Add the lease before opening the pull request."
                % (
                    ("PR #%s" % target.number) if target.number else "this pull request",
                    target.head[:12],
                    LEASE_FILE,
                )
            )

    if not isinstance(lease_doc, dict):
        raise GateError("contract %r is not a JSON object" % path)

    pointer = lease_doc.get("contract_path") or lease_doc.get("contract")
    if isinstance(pointer, str) and pointer.strip():
        contract_path = _normalise(pointer)
        contract = _read_contract_object(repo, contract_revision, contract_path)
        if contract is None:
            raise GateError(
                "contract %r points at %r, which does not exist at %s"
                % (path, contract_path, contract_revision[:12])
            )
        if not isinstance(contract, dict):
            raise GateError("contract %r is not a JSON object" % contract_path)
        return contract, contract_path, lease_doc, contract_revision

    if isinstance(pointer, dict):
        return pointer, path, lease_doc, contract_revision

    return lease_doc, path, lease_doc, contract_revision


def _scope(contract):
    scope = contract.get("scope")
    if not isinstance(scope, dict):
        raise GateError("contract has no scope object")
    assigned = scope.get("assigned_files")
    if not isinstance(assigned, list) or not assigned:
        raise GateError("contract scope.assigned_files must be a non-empty list")
    if not all(isinstance(item, str) and _valid_lease_path(item) for item in assigned):
        raise GateError(
            "contract scope.assigned_files contains an invalid repository-relative path"
        )
    out = {"assigned_files": [_normalise(item) for item in assigned]}
    for key in ("test_files", "prohibited", "read_only"):
        value = scope.get(key)
        if value is None:
            out[key] = []
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            out[key] = [_normalise(item) for item in value]
        else:
            raise GateError("contract scope.%s must be a list of paths/patterns" % key)
    return out


def _matches(path, patterns):
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern) or path == pattern:
            return True
    return False


def _frozen_reason(path, scope):
    if _matches(path, scope["test_files"]):
        return "scope.test_files"
    if _matches(path, scope["prohibited"]):
        return "scope.prohibited"
    if _matches(path, scope["read_only"]):
        return "scope.read_only"
    return None


def _in_lease(path, scope):
    return _matches(path, scope["assigned_files"])


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


class Finding:
    def __init__(self, commit, path, reason, subject, merge=False):
        self.commit = commit
        self.path = path
        self.reason = reason
        self.subject = subject
        self.merge = merge


def check(repo, target, contract, contract_path, lease_doc, verify_sha):
    """Return (findings, boundary, boundary_kind, commits_checked, scope)."""
    scope = _scope(contract)

    boundary, boundary_kind = _verify_sha(verify_sha, target, lease_doc, contract)
    if not boundary or not HEX_REV.match(boundary):
        raise GateError(
            "contract %r has no usable verify_sha/source_rev (40 or 64 lowercase hex)" % contract_path
        )
    if not HEX_REV.match(target.head) or not HEX_REV.match(target.base):
        raise GateError("the pull request base/head are not full lowercase commit shas")

    for label, revision in (("base", target.base), ("head", target.head), ("boundary", boundary)):
        if not _exists(repo, revision):
            raise GateError(
                "%s %s is not a commit in this repository; fetch the pull request history (fetch-depth: 0)"
                % (label, revision[:12])
            )

    if not _is_ancestor(repo, boundary, target.head):
        raise GateError(
            "%s %s is not an ancestor of PR head %s; the lease cannot be applied to this history. "
            "Rebase onto the contract's source_rev or correct the lease."
            % (boundary_kind, boundary[:12], target.head[:12])
        )

    exempt = {_normalise(contract_path), LEASE_FILE}
    if target.base == target.head:
        return [], boundary, boundary_kind, 0, scope

    commits = [
        line
        for line in _out(
            repo, "rev-list", "--no-merges", "%s..%s" % (boundary, target.head), "--not", target.base
        ).splitlines()
        if line
    ]
    merges = [
        line
        for line in _out(
            repo, "rev-list", "--merges", "%s..%s" % (boundary, target.head), "--not", target.base
        ).splitlines()
        if line
    ]

    findings = []
    for sha in commits:
        subject = _subject(repo, sha)
        for path in _commit_paths(repo, sha):
            path = _normalise(path)
            if path in exempt:
                continue
            frozen = _frozen_reason(path, scope)
            if frozen:
                findings.append(Finding(sha, path, frozen, subject))
            elif not _in_lease(path, scope):
                findings.append(Finding(sha, path, "scope.assigned_files", subject))

    for sha in merges:
        parents = _parents(repo, sha)
        if not parents:
            continue
        subject = _subject(repo, sha)
        # A merge of the base (or any commit already on the base) is not an
        # authored change: its content is main's history and is excluded from
        # the commit range. An evil conflict resolution still differs from both
        # parents, so inspect the intersection of the two first-parent diffs.
        if any(_is_ancestor(repo, parent, target.base) for parent in parents):
            if len(parents) < 2:
                continue
            left = {_normalise(path) for path in _diff_paths(repo, sha, parents[0])}
            right = {_normalise(path) for path in _diff_paths(repo, sha, parents[1])}
            suspect = left & right
        else:
            suspect = {_normalise(path) for path in _diff_paths(repo, parents[0], sha)}

        for path in sorted(suspect):
            if path in exempt:
                continue
            frozen = _frozen_reason(path, scope)
            if frozen:
                findings.append(Finding(sha, path, frozen, subject, merge=True))
            elif not _in_lease(path, scope):
                findings.append(Finding(sha, path, "scope.assigned_files", subject, merge=True))

    # De-duplicate (commit, path) pairs; keep the first reason (frozen wins
    # because it is checked first above).
    seen, unique = set(), []
    for finding in findings:
        key = (finding.commit, finding.path)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    unique.sort(key=lambda item: (item.path, item.commit))
    return unique, boundary, boundary_kind, len(commits) + len(merges), scope


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_failure(findings, boundary, boundary_kind, contract, contract_path, head):
    label = contract.get("id") or contract_path
    lines = []
    for finding in findings:
        where = "merge commit" if finding.merge else "commit"
        if finding.reason == "scope.assigned_files":
            lines.append(
                "lease-tamper: FAILED: '%s' is outside the slice lease (scope.assigned_files); "
                "touched after %s %s by %s %s (\"%s\")."
                % (
                    finding.path,
                    boundary_kind,
                    boundary,
                    where,
                    finding.commit[:12],
                    finding.subject,
                )
            )
        else:
            lines.append(
                "lease-tamper: FAILED: frozen test file '%s' (%s) modified after %s %s by %s %s "
                "(\"%s\")."
                % (
                    finding.path,
                    finding.reason,
                    boundary_kind,
                    boundary,
                    where,
                    finding.commit[:12],
                    finding.subject,
                )
            )
    lines.append(
        "lease-tamper: FAILED: %d violation(s) after %s %s; contract %s from %s; PR head %s. "
        "Only scope.assigned_files may change after verify, and frozen tests may never change."
        % (len(findings), boundary_kind, boundary, label, contract_path, head)
    )
    return "\n".join(lines)


def render_success(contract, contract_path, boundary, boundary_kind, commits, scope, head):
    label = contract.get("id") or contract_path
    return (
        "lease-tamper: OK: contract %s from %s; %d commit(s) after %s %s (head %s) stayed inside "
        "%d leased path(s); no frozen test file touched."
        % (
            label,
            contract_path,
            commits,
            boundary_kind,
            boundary,
            head,
            len(scope["assigned_files"]),
        )
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv=None, environ=None):
    environ = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(description="Fail a PR that tampers with its slice lease.")
    parser.add_argument("event", nargs="?", default=environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--contract", default=None)
    parser.add_argument("--verify-sha", default=None)
    parser.add_argument("--base", default=None)
    parser.add_argument("--head", default=None)
    parser.add_argument("--labels", default=None, help="comma-separated label names (local runs)")
    parser.add_argument("--body", default=None, help="pull-request body text (local runs)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        labels = [item for item in (args.labels or "").split(",") if item.strip()]
        body = args.body or ""
        if args.base and args.head:
            target = Target(args.base.strip(), args.head.strip(), labels, body)
        else:
            if not args.event:
                raise GateError("no event JSON path (pass one or set GITHUB_EVENT_PATH)")
            try:
                with open(args.event, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError) as exc:
                raise GateError("cannot read the event payload %r: %s" % (args.event, exc))
            target = target_from_event(payload, environ.get("GITHUB_EVENT_NAME"), environ)
            if target is None:
                if not args.quiet:
                    print("lease-tamper: no pull request in this event; nothing to check.")
                return 0
            if labels:
                target.labels = labels
            if body:
                target.body = body

        contract, contract_path, lease_doc, _revision = resolve_contract(
            args.repo, target, args.contract
        )
        target.base = _resolve_rev(args.repo, target.base)
        target.head = _resolve_rev(args.repo, target.head)
        if args.verify_sha:
            args.verify_sha = _resolve_rev(args.repo, args.verify_sha)
        findings, boundary, boundary_kind, commits, scope = check(
            args.repo, target, contract, contract_path, lease_doc, args.verify_sha
        )
        if findings:
            print(render_failure(findings, boundary, boundary_kind, contract, contract_path, target.head))
            print("::error::lease-tamper: %d lease/frozen-test violation(s); see the log above." % len(findings))
            return 1
        if not args.quiet:
            print(render_success(contract, contract_path, boundary, boundary_kind, commits, scope, target.head))
        return 0
    except GateError as exc:
        print("lease-tamper: FAILED: %s" % exc)
        print("::error::lease-tamper: %s" % str(exc).replace("\n", " "))
        return 1
    except subprocess.TimeoutExpired:
        print("lease-tamper: FAILED: git did not finish within the timeout; failing closed.")
        print("::error::lease-tamper: git timeout; failing closed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
