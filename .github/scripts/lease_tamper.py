#!/usr/bin/env python3
"""Lease/tamper gate: a slice may not change files outside its lease.

The gate answers one question for a pull request: **after the verify boundary,
did any commit touch a path the slice does not own, or a frozen test file?**

A slice lease is a record with ``scope.assigned_files`` / ``scope.test_files`` /
``scope.prohibited`` / ``scope.read_only``, a ``source_rev`` and a
``contract_sha256`` that names an approved frozen contract committed under
``.rizzstack/contracts/<contract_sha256>.json`` on the **base** branch.  The
lease itself must be committed to the frozen store ``.rizzstack/leases/*.json``
on the base branch; there is no other source.  A lease is accepted only when:

1. it declares ``review.status`` ``approved`` or ``frozen`` -- a lease without a
   review block is **not** frozen (the default is fail-closed);
2. it declares ``contract_sha256``;
3. ``.rizzstack/contracts/<contract_sha256>.json`` exists at the base tip, its
   own ``review.status`` is ``approved``/``frozen`` with a non-empty
   ``review.approved_by``, the file's canonical hash (compact, key-sorted JSON
   with the ``review`` block removed) equals the hash in its file name and in
   the lease, and the contract's ``source_rev`` and four scope lists equal the
   lease's.

**Slice or non-slice.**  A pull request is a *slice* pull request exactly when
such a lease on the **base** branch names it -- by number (``pull_request``),
by branch (``branch``), or by slice name (``slice``/``id`` against the branch's
last path segment).  Nothing on the pull request itself selects or supplies the
lease: there is no ``lease-contract:`` label, no ``Lease-Contract:`` body line,
no ``.rizzstack/lease.json`` on the head, and no lease file from the head.

**Any change to the lease or contract store** (``.rizzstack/leases/**``,
``.rizzstack/contracts/**`` or ``.rizzstack/lease.json``) in the pull request's
own commits makes it a **non-slice** pull request: landing a lease or a contract
always needs an independent review at the head, so a pull request can never
grant itself a scope.

A **non-slice** pull request -- a docs fix, a release chore, a gate change, a
lease or contract landing -- passes only with an independent review recorded at
its exact head commit: a review whose state is ``APPROVED``, or a ``COMMENTED``
review whose first line is the canonical attestation ``APPROVE (independent
review) at <sha>`` (the pre-canonical ``APPROVE — independent review ...`` and
``APPROVE (independent review at <sha>)`` forms are accepted by the same
parser), with ``commit_id`` equal to the head and any named sha a prefix of it.
Without one the gate fails closed with the named reason
``non_slice_requires_independent_review``.

**The newest decisive review governs.**  A rejection -- state
``CHANGES_REQUESTED``, or a ``COMMENTED`` review whose first line starts
``CHANGES REQUESTED`` -- submitted after the approval being used, at that
approval's commit or at any later commit (the head counts), refuses it: the
gate does not approve a pull request whose latest word is a rejection.  A later
re-approval supersedes the rejection in its turn, and a rejection the approval
itself supersedes (recorded at an earlier commit) does not block.  The
canonical rejection line is ``CHANGES REQUESTED (<why>) at <sha>``.

**A review at an earlier head is carried forward** when, and only when, the
branch moved by merging the base into it and nothing else changed: every commit
in ``review_sha..<head>`` that is not already on the base is a merge commit
whose non-first parents are on the base, and the pull request's diff against
the base (``base...<head>``) is byte-identical to the diff the review saw
(``base...review_sha``) under per-file blob equality.  Updating a branch with
the base therefore does not demand a fresh review just to move ``commit_id``,
while an authored commit, a merge of any other branch, or any content change
still does.  A rejected carry names its reason: ``review_not_ancestor``,
``review_carry_authored_commit``, ``review_carry_unrelated_merge``,
``review_carry_content_changed`` or ``review_carry_unreadable``.

The review line, the rejection rule, the carry-forward predicate, the
sensitive-path list and the only-green conclusion live in one shared policy
block, vendored byte-identically here and in the merge lane
(``scripts/merge_lane_v5.py``): two parsers would drift, and the lane must
accept exactly what this gate accepts.  The gate prints a machine-readable
``lease-tamper: class=slice`` / ``lease-tamper: class=non-slice`` line on a
passing run so the lane takes the class from the trusted gate output instead of
deriving it from a branch name.

The verify boundary is, in order: ``--verify-sha`` (a local/test argument, never
a pull-request input), the base-branch lease's
``verify_sha``/``verified_sha``/``verification.sha``, then the contract's
``source_rev``.  Labels and the pull-request body are never read as a boundary.

Every non-merge commit in ``<boundary>..<head>`` that is not reachable from the
PR base is checked.  Merge commits are not authored changes: a merge whose
parent is on the base is ignored, and only conflict resolutions that differ
from both parents are checked.  The lease/contract store and
``.rizzstack/lease.json`` are metadata, and a commit that changes them makes the
pull request non-slice before any scope check runs.

Exit status: 0 = clean or no pull request in the event; 1 = tamper, an invalid
lease, an unresolvable contract, or a merge_group whose pull-request context
cannot be established (always fail closed).  Standard library only.

A ``merge_group`` event is a queued candidate, not a pull request: its head is
a synthetic merge commit that may stack several pull requests.  The gate does
**not** re-check the combined group diff against one lease -- the lease belongs
to one slice, and a stacked diff belongs to several.  Instead it proves, per
pull request, that the head the queue is testing is exactly the head where the
``pull_request`` run of this gate already passed:

1. every ``pr-<N>-<sha>`` in the queue branch name
   ``refs/heads/gh-readonly-queue/<base>/pr-<N>-<sha>`` is resolved with
   ``GET /repos/{repo}/pulls/{N}``;
2. the entry heads stacked by the group are read from its first-parent merge
   chain and each is resolved to its pull request;
3. for every pull request found, its **current** head sha must be an ancestor
   of the group head (a head that moved after the queue built the group is not
   the reviewed head) and must carry a completed, successful ``lease-tamper``
   check run -- the proof that the gate passed at that exact sha.

Any failure to establish that context is a hard failure with the named reason
``merge_group_pr_unresolved`` (exit 1).  It is never a neutral or skipped
conclusion: GitHub counts a neutral required check as passing, so a neutral
result would be a way around the gate.  A resolved pull request whose reviewed
head did not pass the gate also fails.

A ``pull_request_review`` event is a **pull request re-check**, not a review
verdict: the gate reads the pull request and its head out of
``pull_request.head.sha`` exactly as it does for ``pull_request`` and applies the
same slice and non-slice rules to that head.  The review's own ``commit_id`` is
the commit the review was submitted against and may be older than the head; it
is **never** substituted for the head, and a review payload with no
``pull_request.head.sha`` fails closed instead of falling back to it.  Which
reviews decide a non-slice pull request is still read from the API under the
normal exact-head and carry-forward rules.

Usage:
    python3 .github/scripts/lease_tamper.py [EVENT_JSON_PATH]

The event path defaults to ``$GITHUB_EVENT_PATH``.  For local/test runs:
``--base``, ``--head``, ``--labels``, ``--body``, ``--verify-sha`` and
``--contract`` bypass the event payload.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

LEASE_FILE = ".rizzstack/lease.json"
LEASES_DIR = ".rizzstack/leases"
CONTRACTS_DIR = ".rizzstack/contracts"
GATE_SCRIPT = ".github/scripts/lease_tamper.py"
PR_NAME_KEYS = ("pull_request", "pull_requests", "pr", "prs")
BRANCH_NAME_KEYS = ("branch", "branches")
SLICE_NAME_KEYS = ("slice", "slice_name", "id")
HEX_REV = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GLOB_CHARS = ("*", "?", "[")
FROZEN_STATUSES = ("approved", "frozen")
# refs/heads/gh-readonly-queue/<base>/pr-<number>-<sha>
QUEUE_REF = re.compile(r"/pr-(\d+)-([0-9a-fA-F]{7,64})")
HTTP_TIMEOUT_SECONDS = 20
MAX_PAGES = 10
MAX_GROUP_ENTRIES = 100
DEFAULT_API_URL = "https://api.github.com"
LEASE_JOB_NAME = "lease-tamper"
MERGE_GROUP_REASON = "merge_group_pr_unresolved"

#: Events whose payload carries the pull request itself.  A review submission is
#: re-checked against ``pull_request.head.sha`` for the same reason a push is:
#: an approval or a rejection can change a non-slice pull request's answer, so
#: the gate must judge the head the review is carried to, not the reviewed commit.
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_review")

# --- BEGIN SHARED GATE POLICY (vendored; keep byte-identical) ---------------
# One implementation of the rules the lease/tamper gate and the merge lane both
# apply: how a review attestation is parsed, which rejection overrides an
# approval, the review carry-forward predicate, the sensitive-path list and the
# only green conclusion.  The two repositories cannot import each other (the
# gate must stay a single self-contained script), so this block is duplicated
# verbatim and the tests prove the copies are byte-identical with a sha256.
#
# Canonical attestation line (first non-empty line of the review body):
#     APPROVE (independent review) at <sha>
#     APPROVE (owner) at <sha>
#     CHANGES REQUESTED (<why>) at <sha>
# The pre-canonical forms stay accepted by the same parser:
#     APPROVE — independent review ... at <sha>
#     APPROVE (independent review at <sha>)
#     OWNER APPROVED

REVIEW_APPROVE_INDEPENDENT = "approve_independent"
REVIEW_APPROVE_OWNER = "approve_owner"
REVIEW_CHANGES_REQUESTED = "changes_requested"
REVIEW_APPROVAL_KINDS = (REVIEW_APPROVE_INDEPENDENT, REVIEW_APPROVE_OWNER)

#: How a review matches the head: at the head itself, or carried from an
#: earlier head by a base-merge-only update with an unchanged diff.
REVIEW_MATCH_EXACT = "exact"
REVIEW_MATCH_CARRIED = "carried"

#: Named outcomes of the base-merge review carry-forward.  The refusal reasons
#: are stable codes, so a lane decision and a gate log name the same condition.
CARRY_OK = "review_carry_ok"
CARRY_NOT_ANCESTOR = "review_not_ancestor"
CARRY_AUTHORED_COMMIT = "review_carry_authored_commit"
CARRY_UNRELATED_MERGE = "review_carry_unrelated_merge"
CARRY_CONTENT_CHANGED = "review_carry_content_changed"
CARRY_UNREADABLE = "review_carry_unreadable"

#: Paths whose change makes a pull request a gate/lane change: it needs an
#: explicit owner attestation in addition to the independent review, because a
#: coordinator or a bee could otherwise relax the machinery that judges it.
#: The explicit list is authoritative; the prefix and token rules below only
#: catch new files in the same places.
SENSITIVE_PATHS = frozenset((
    "bundlebees_plugin/acdd_cli.py",
    "bundlebees_plugin/approval.py",
    "bundlebees_plugin/contract.py",
    "bundlebees_plugin/return_report.py",
    "bundlebees_plugin/subversion_scan.py",
    "bundlebees_plugin/tdd.py",
    "bundlebees_plugin/test_receipts.py",
    "bundlebees_plugin/transport.py",
    "bundlebees_plugin/verify_local.py",
    "tests/test_lease_tamper.py",
    "tests/test_merge_lane_v5.py",
))
SENSITIVE_PREFIXES = (".github/", ".rizzstack/", "gate/", "gates/", "policy/")
SENSITIVE_TOKENS = frozenset(("gate", "gates", "lane", "lanes"))

#: Only SUCCESS is green.  A skipped or neutral required check proves nothing
#: about the change, so it is not evidence that a gate ran.
CHECK_GREEN_CONCLUSION = "success"
CHECK_RED_CONCLUSIONS = (
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
    "error",
)

_POLICY_APPROVE = re.compile(r"(?i)^APPROVE\b(?!\?)")
_POLICY_OWNER_WORD = re.compile(r"(?i)^OWNER\s+APPROVE[DS]?\b")
_POLICY_CHANGES = re.compile(r"(?i)^CHANGES[\s_-]*REQUESTED\b")
_POLICY_INDEPENDENT = re.compile(r"(?i)\bindependent\s+review\b")
_POLICY_NEGATED = re.compile(
    r"(?i)\b(?:not|no|never|without|isn'?t|is\s+not)\b[^.;:!?]{0,48}?\bindependent\s+review\b"
)
_POLICY_OWNER = re.compile(r"(?i)\bowner\b")
_POLICY_SHA = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{7,64})(?![0-9a-fA-F])")
_POLICY_LEAD = re.compile(r"^[\s>#•\-\u2013\u2014]+")
_POLICY_EMPHASIS = re.compile(r"^(?:\*\*|__|`)+")
_POLICY_TRAILING_SHA = re.compile(r"(?i)\s+at\s+[0-9a-f]{7,64}\s*$")
_CARRY_HEX = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _policy_text(value):
    return value if isinstance(value, str) else ""


def _policy_first_line(body):
    for line in _policy_text(body).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _policy_strip_markup(line):
    line = _POLICY_LEAD.sub("", line)
    line = _POLICY_EMPHASIS.sub("", line)
    line = re.sub(
        r"(?i)^(APPROVE|CHANGES[\s_-]*REQUESTED|OWNER\s+APPROVE[DS]?)(?:\*\*|__|`)",
        lambda match: match.group(1),
        line,
    )
    return line.strip()


def _policy_descriptor(rest):
    """The words that qualify an APPROVE: its first parenthetical or clause."""
    rest = rest.strip()
    while rest[:1] in ("*", "_", "`", " "):
        rest = rest[1:]
    if rest.startswith("("):
        end = rest.find(")")
        return rest[1:end] if end != -1 else rest[1:]
    return _POLICY_TRAILING_SHA.sub("", rest).strip(" \t-–—:;,")


def parse_review_line(body):
    """``(kind, named_sha)`` for a review body's first non-empty line.

    ``kind`` is ``approve_independent``, ``approve_owner``,
    ``changes_requested`` or ``None``.  ``named_sha`` is the commit the line
    names after ``at`` (a 7..64-hex prefix), or ``None``.  Every review line is
    parsed here and nowhere else, so the gate and the lane cannot disagree
    about what an approval is.
    """
    line = _policy_strip_markup(_policy_first_line(body))
    if not line:
        return None, None
    named = None
    found = _POLICY_SHA.findall(line)
    if found:
        named = found[-1].lower()
    if _POLICY_CHANGES.search(line):
        return REVIEW_CHANGES_REQUESTED, named
    if _POLICY_OWNER_WORD.search(line):
        return REVIEW_APPROVE_OWNER, named
    match = _POLICY_APPROVE.search(line)
    if not match:
        return None, None
    descriptor = _policy_descriptor(line[match.end():])
    if _POLICY_INDEPENDENT.search(descriptor) and not _POLICY_NEGATED.search(descriptor):
        return REVIEW_APPROVE_INDEPENDENT, named
    if _POLICY_OWNER.search(descriptor):
        return REVIEW_APPROVE_OWNER, named
    return None, None


def review_form(review):
    """``(kind, named_sha)`` for one review, or ``(None, None)``.

    GitHub's ``APPROVED`` state is an approval even with an empty body.  A body
    that names the owner is an owner attestation only: an owner approval never
    satisfies the independent-review rule, whatever state GitHub records.
    """
    if not isinstance(review, dict):
        return None, None
    state = _policy_text(review.get("state")).strip().upper()
    kind, named = parse_review_line(review.get("body"))
    if state == "CHANGES_REQUESTED":
        return REVIEW_CHANGES_REQUESTED, named
    if state not in ("APPROVED", "COMMENTED"):
        return None, None
    if kind == REVIEW_CHANGES_REQUESTED:
        return REVIEW_CHANGES_REQUESTED, named
    if kind is not None:
        return kind, named
    if state == "APPROVED":
        return REVIEW_APPROVE_INDEPENDENT, named
    return None, None


def review_names_commit(named, commit):
    """True when the line names no commit, or a prefix of ``commit``."""
    if not named:
        return True
    commit = _policy_text(commit).strip().lower()
    return bool(commit) and commit.startswith(named.lower())


def review_is_approval(review, kind=None):
    form, named = review_form(review)
    if form not in REVIEW_APPROVAL_KINDS:
        return False
    if kind is not None and form != kind:
        return False
    return review_names_commit(named, (review or {}).get("commit_id"))


def review_is_rejection(review):
    return review_form(review)[0] == REVIEW_CHANGES_REQUESTED


def review_chronology(reviews):
    """Reviews in submission order.

    GitHub returns reviews chronologically.  When every review carries a
    ``submitted_at`` timestamp the list is sorted by it, so a facts file that
    reorders reviews cannot reorder history; otherwise list order is used.
    """
    items = [item for item in (reviews or []) if isinstance(item, dict)]
    stamps = [
        _policy_text(item.get("submitted_at") or item.get("submittedAt")).strip()
        for item in items
    ]
    if items and all(stamps):
        order = sorted(range(len(items)), key=lambda index: stamps[index])
        return [items[index] for index in order]
    return items


def governing_rejection(reviews, approval, head=None, ancestor=None):
    """The rejection submitted after ``approval`` that overrides it, or None.

    The newest decisive review governs: a rejection (state ``CHANGES_REQUESTED``
    or a first line starting ``CHANGES REQUESTED``) submitted after the approval
    refuses when it is recorded at the approval's commit, at the head, or at a
    commit that descends from the approval's commit.  A rejection the approval
    supersedes does not block, and a later approval supersedes the rejection in
    its turn.  With no ``ancestor`` callable an unrelated commit is treated as
    governing: failing closed beats ignoring a rejection whose history cannot be
    read.
    """
    ordered = review_chronology(reviews)
    index = next((position for position, item in enumerate(ordered) if item is approval), None)
    if index is None:
        return None
    approval_commit = _policy_text((approval or {}).get("commit_id")).strip().lower()
    head = _policy_text(head).strip().lower()
    for candidate in ordered[index + 1:]:
        if not review_is_rejection(candidate):
            continue
        commit = _policy_text(candidate.get("commit_id")).strip().lower()
        if not commit or commit == approval_commit or (head and commit == head):
            return candidate
        if ancestor is None:
            return candidate
        if approval_commit and ancestor(approval_commit, commit):
            return candidate
    return None


def approvals_at_head(reviews, head, kind):
    """Approvals of ``kind`` recorded at exactly ``head``, oldest first."""
    wanted = _policy_text(head).strip().lower()
    found = []
    if not wanted:
        return found
    for review in review_chronology(reviews):
        form, named = review_form(review)
        if form != kind:
            continue
        if _policy_text(review.get("commit_id")).strip().lower() != wanted:
            continue
        if not review_names_commit(named, wanted):
            continue
        found.append(review)
    return found


def newest_approval_at_head(reviews, head, kind, ancestor=None):
    """The newest approval of ``kind`` at ``head`` no rejection overrides."""
    for review in reversed(approvals_at_head(reviews, head, kind)):
        if governing_rejection(reviews, review, head, ancestor) is None:
            return review
    return None


def approval_chain(reviews, head, kind, ancestor=None):
    """Approvals that may decide ``head``, newest first.

    Each entry is ``(review, "exact"|"older")``.  An approval with a governing
    rejection after it is dropped, so the newest decisive review governs: a
    re-approval supersedes an earlier rejection, and a rejection supersedes the
    approval it follows.
    """
    wanted = _policy_text(head).strip().lower()
    chain = []
    for review in reversed(approvals_at_head(reviews, wanted, kind)):
        if governing_rejection(reviews, review, wanted, ancestor) is None:
            chain.append((review, REVIEW_MATCH_EXACT))
    seen = set()
    for review in reversed(review_chronology(reviews)):
        form, named = review_form(review)
        if form != kind:
            continue
        commit = _policy_text(review.get("commit_id")).strip().lower()
        if not commit or commit == wanted or commit in seen:
            continue
        if not review_names_commit(named, commit):
            continue
        if governing_rejection(reviews, review, wanted, ancestor) is not None:
            continue
        seen.add(commit)
        chain.append((review, REVIEW_MATCH_CARRIED))
    return chain


def rejection_at_head(reviews, head):
    """The newest rejection recorded at ``head`` (or naming no commit)."""
    wanted = _policy_text(head).strip().lower()
    for review in reversed(review_chronology(reviews)):
        if not review_is_rejection(review):
            continue
        commit = _policy_text(review.get("commit_id")).strip().lower()
        if not commit or commit == wanted:
            return review
    return None


def _policy_path_tokens(path):
    name = _policy_text(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return [token for token in re.split(r"[^a-z0-9]+", name) if token]


def sensitive_paths(files):
    """The changed paths that make a pull request a gate/lane change."""
    found = []
    if not isinstance(files, (list, tuple)):
        return found
    for item in files:
        if not isinstance(item, str) or not item.strip():
            continue
        path = item.replace("\\", "/")
        normal = path.lower()
        while normal.startswith("./"):
            normal = normal[2:]
        normal = normal.lstrip("/")
        sensitive = normal in SENSITIVE_PATHS
        if not sensitive:
            sensitive = any(
                normal == prefix.rstrip("/") or normal.startswith(prefix)
                for prefix in SENSITIVE_PREFIXES
            )
        if not sensitive:
            sensitive = bool(SENSITIVE_TOKENS.intersection(_policy_path_tokens(path)))
        if sensitive and item not in found:
            found.append(item)
    return found


def check_run_is_success(run):
    """A check run proves a gate ran only when it completed with SUCCESS."""
    if not isinstance(run, dict):
        return False
    status = _policy_text(run.get("status")).strip().lower()
    conclusion = _policy_text(run.get("conclusion")).strip().lower()
    return status in ("", "completed") and conclusion == CHECK_GREEN_CONCLUSION


def check_run_is_red(run):
    if not isinstance(run, dict):
        return False
    return _policy_text(run.get("conclusion")).strip().lower() in CHECK_RED_CONCLUSIONS


def _carry_git(repo, *args, timeout=60):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
    )


def _carry_commit(repo, sha):
    return _carry_git(repo, "cat-file", "-e", "%s^{commit}" % sha).returncode == 0


def _carry_ancestor(repo, older, newer):
    return _carry_git(repo, "merge-base", "--is-ancestor", older, newer).returncode == 0


def _carry_merge_base(repo, base, head):
    proc = _carry_git(repo, "merge-base", base, head)
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else base


def _carry_parents(repo, sha):
    return _carry_git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]


def _carry_subject(repo, sha):
    return _carry_git(repo, "log", "-1", "--format=%s", sha).stdout.strip()


def carry_diff_blobs(repo, older, newer):
    """``{path: new blob sha or None}`` for one tree-to-tree diff."""
    proc = _carry_git(repo, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", older, newer)
    if proc.returncode != 0:
        raise RuntimeError("git diff failed: " + (proc.stderr or "").strip()[-200:])
    fields = proc.stdout.split("\0")
    blobs = {}
    index = 0
    while index + 1 < len(fields):
        header, path = fields[index], fields[index + 1]
        index += 2
        if not header.startswith(":"):
            continue
        parts = header[1:].split()
        if len(parts) < 5:
            continue
        new_sha = parts[3]
        blobs[path] = None if set(new_sha) == {"0"} else new_sha
    return blobs


def reviewed_diff_blobs(repo, base, revision):
    """The pull request's diff at ``revision`` as ``{path: blob sha or None}``."""
    return carry_diff_blobs(repo, _carry_merge_base(repo, base, revision), revision)


def review_carry_forward(repo, review_sha, base, head, timeout=60):
    """``(ok, reason, detail)``: may a review at ``review_sha`` judge ``head``?

    Accept only when (1) the review commit is an ancestor of the head, (2)
    every commit in ``review_sha..head`` that is not already on the base is a
    merge whose non-first parents are on the base, and (3) the pull request's
    diff against the base is byte-identical at ``head`` and at ``review_sha``.
    """
    if not isinstance(review_sha, str) or not _CARRY_HEX.fullmatch(review_sha.strip().lower()):
        return False, CARRY_NOT_ANCESTOR, "the review names no full commit sha"
    review_sha = review_sha.strip().lower()
    try:
        if not _carry_commit(repo, review_sha):
            return (
                False,
                CARRY_NOT_ANCESTOR,
                "review commit %s is not in this history" % review_sha[:12],
            )
        if review_sha != head and not _carry_ancestor(repo, review_sha, head):
            return (
                False,
                CARRY_NOT_ANCESTOR,
                "review commit %s is not an ancestor of head %s; the reviewed history was "
                "rewritten away" % (review_sha[:12], head[:12]),
            )

        listed = _carry_git(repo, "rev-list", "%s..%s" % (review_sha, head), "--not", base, timeout=timeout)
        if listed.returncode != 0:
            raise RuntimeError("git rev-list failed: " + (listed.stderr or "").strip()[-200:])
        commits = [line for line in listed.stdout.splitlines() if line]
        for sha in commits:
            parents = _carry_parents(repo, sha)
            subject = _carry_subject(repo, sha)
            if len(parents) < 2:
                return (
                    False,
                    CARRY_AUTHORED_COMMIT,
                    "commit %s (\"%s\") was authored after the review; only merges of the base "
                    "may follow a review" % (sha[:12], subject),
                )
            for parent in parents[1:]:
                if not _carry_ancestor(repo, parent, base):
                    return (
                        False,
                        CARRY_UNRELATED_MERGE,
                        "merge %s (\"%s\") brings in %s, which is not on base %s; only a merge "
                        "of the base may follow a review"
                        % (sha[:12], subject, parent[:12], base[:12]),
                    )

        reviewed = reviewed_diff_blobs(repo, base, review_sha)
        current = reviewed_diff_blobs(repo, base, head)
        if reviewed != current:
            changed = sorted(
                path for path in set(reviewed) | set(current) if reviewed.get(path) != current.get(path)
            )
            detail = ", ".join(changed[:8]) + (" ..." if len(changed) > 8 else "")
            return (
                False,
                CARRY_CONTENT_CHANGED,
                "the diff against base %s is not byte-identical to the reviewed diff: %s"
                % (base[:12], detail or "<trees differ>"),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, CARRY_UNREADABLE, "cannot read the review history: %s" % type(exc).__name__
    except RuntimeError as exc:
        return False, CARRY_UNREADABLE, str(exc)
    return (
        True,
        CARRY_OK,
        "every commit after review %s is a merge of base %s and the diff is byte-identical"
        % (review_sha[:12], base[:12]),
    )
# --- END SHARED GATE POLICY -------------------------------------------------


class GateError(RuntimeError):
    """The lease/contract could not be resolved or applied (fail closed).

    ``reason`` is a stable, named machine-readable code for the failure.  It is
    required for merge_group failures, which must never be reported as a
    neutral/skipped conclusion: GitHub counts a neutral required check as
    passing, so neutral would be a way around the gate.
    """

    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


def merge_group_error(subtype, detail):
    """A merge_group context failure: always fatal, always named."""
    return GateError("[%s] %s" % (subtype, detail), reason=MERGE_GROUP_REASON)


class NoSliceLease(GateError):
    """No frozen lease on the base branch names this pull request."""




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
    def __init__(self, base, head, labels, body, number=None, branch=None):
        self.base = base
        self.head = head
        self.labels = list(labels or [])
        self.body = body or ""
        self.number = number
        self.branch = branch


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


def _api_url(environ):
    return (environ.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")


def _next_page(link_header):
    for part in (link_header or "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return ""


def _get_checked(url, token, http=None):
    """GET a GitHub API url, turning transport/parse failures into GateError."""
    try:
        return (http or _http_get_json)(url, token)
    except GateError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise GateError("%s: %s" % (url, exc))


def _pull_requests_for_commit(repo, sha, token, api_url=DEFAULT_API_URL, http=None):
    url = "%s/repos/%s/commits/%s/pulls?per_page=100" % (
        api_url,
        repo,
        urllib.parse.quote(sha),
    )
    pulls = []
    for _ in range(MAX_PAGES):
        payload, link = _get_checked(url, token, http)
        if not isinstance(payload, list):
            raise GateError("the merge-queue pull-request lookup did not return a list")
        pulls.extend(item for item in payload if isinstance(item, dict))
        url = _next_page(link)
        if not url:
            return pulls
    raise GateError("the merge-queue pull-request lookup did not terminate")


def _pull_request_by_number(repo, number, token, api_url=DEFAULT_API_URL, http=None):
    url = "%s/repos/%s/pulls/%d" % (api_url, repo, int(number))
    payload, _link = _get_checked(url, token, http)
    if not isinstance(payload, dict):
        raise GateError("%s did not return a pull request" % url)
    return payload


def merge_group_pr_numbers(head_ref):
    """Every pull request number named by a gh-readonly-queue branch name."""
    found = []
    for match in QUEUE_REF.finditer(str(head_ref or "")):
        number = int(match.group(1))
        if number not in found:
            found.append(number)
    return found


def _lease_green_at(repo, sha, token, api_url=DEFAULT_API_URL, http=None):
    """True when a completed, successful ``lease-tamper`` check run exists on ``sha``."""
    url = "%s/repos/%s/commits/%s/check-runs?check_name=%s&filter=latest&per_page=100" % (
        api_url,
        repo,
        urllib.parse.quote(sha),
        urllib.parse.quote(LEASE_JOB_NAME),
    )
    payload, _link = _get_checked(url, token, http)
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise GateError("%s did not return check runs" % url)
    for run in runs:
        if not isinstance(run, dict):
            continue
        if str(run.get("name") or "") != LEASE_JOB_NAME:
            continue
        # Only a completed SUCCESS proves the gate ran: a neutral or skipped
        # required check is not evidence (shared policy).
        if check_run_is_success(run):
            return True
    return False


def _group_entry_heads(repo, head, base):
    """The pull-request heads a merge group stacks, from its first-parent chain.

    GitHub builds a group by merging each entry in turn, so every merge on the
    first-parent chain contributes the entry head as its second parent.  The
    chain stops at the group base (or when a commit is no longer a merge).
    """
    entries = []
    current = head
    for _ in range(MAX_GROUP_ENTRIES):
        if not current or current == base or _is_ancestor(repo, current, base):
            break
        parents = _parents(repo, current)
        if len(parents) < 2:
            break
        entries.extend(parents[1:])
        if len(parents) > 2:
            break
        current = parents[0]
    return entries


def _resolved_pull_request(repo, number, token, api_url, http=None):
    """Fetch pull request ``number`` and return it with its current head sha."""
    try:
        pull = _pull_request_by_number(repo, number, token, api_url, http)
    except GateError as exc:
        raise merge_group_error("api", "pull request #%s: %s" % (number, exc))
    head = ((pull.get("head") or {}).get("sha") or "").strip()
    if not head:
        raise merge_group_error("no-head", "pull request #%s has no head sha" % number)
    return pull, head


def check_merge_group(repo, payload, environ, http=None, quiet=False):
    """Fail unless every queued pull request's reviewed head already passed this gate.

    No lease is applied to the combined group diff: a stacked group belongs to
    several slices, so the proof is per pull request -- the head the group is
    testing must be the pull request's current head and must carry a successful
    ``lease-tamper`` check run.
    """
    group = payload.get("merge_group") or {}
    head = (group.get("head_sha") or "").strip()
    head_ref = str(group.get("head_ref") or "").strip()
    base = (group.get("base_sha") or "").strip()
    if not head or not base:
        raise merge_group_error(
            "payload-incomplete",
            "the merge_group payload has no head_sha/base_sha (ref %s)" % (head_ref or "<none>"),
        )
    name = (environ.get("GITHUB_REPOSITORY") or "").strip()
    token = (environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN") or "").strip()
    if not name or not token:
        raise merge_group_error(
            "no-credentials",
            "merge_group needs GITHUB_REPOSITORY and GITHUB_TOKEN to resolve the queued pull "
            "requests (head %s, ref %s)" % (head[:12], head_ref or "<none>"),
        )
    api_url = _api_url(environ)

    numbers = merge_group_pr_numbers(head_ref)
    if not numbers:
        raise merge_group_error(
            "no-pr-in-ref",
            "the queue branch %r names no pull request (expected "
            "gh-readonly-queue/<base>/pr-<N>-<sha>)" % (head_ref or "<none>"),
        )

    # ``tested`` maps a pull request number to the head the group stacks for it.
    # The chain is authoritative: it is read from git, not trusted from the API.
    tested = {}
    try:
        chain = _group_entry_heads(repo, head, base)
    except GateError as exc:
        raise merge_group_error("git", "cannot read the group chain under %s: %s" % (head[:12], exc))
    for entry in chain:
        if not _exists(repo, entry):
            raise merge_group_error(
                "entry-missing",
                "stacked entry %s is not in this checkout (fetch-depth: 0)" % entry[:12],
            )
        try:
            candidates = _pull_requests_for_commit(name, entry, token, api_url, http)
        except GateError as exc:
            raise merge_group_error("api", "stacked entry %s: %s" % (entry[:12], exc))
        pull = next(
            (item for item in candidates if ((item.get("head") or {}).get("sha") == entry)),
            None,
        )
        if pull is None and candidates:
            # The commit belongs to a pull request whose head has since moved:
            # the group still stacks the old head, so this is a moved head, not
            # an unknown entry.
            moved = candidates[0]
            raise merge_group_error(
                "head-moved",
                "stacked entry %s belongs to pull request #%s, whose head is now %s"
                % (
                    entry[:12],
                    moved.get("number"),
                    ((moved.get("head") or {}).get("sha") or "?")[:12],
                ),
            )
        if pull is None:
            raise merge_group_error(
                "no-pr-for-entry",
                "stacked entry %s resolves to no pull request" % entry[:12],
            )
        number = int(pull.get("number") or 0)
        if not number:
            raise merge_group_error(
                "no-pr-for-entry",
                "stacked entry %s resolves to a pull request without a number" % entry[:12],
            )
        tested[number] = entry

    for number in numbers:
        if number in tested:
            continue
        # A one-entry group can be a plain fast-forward: the named head is the
        # group head itself.  Anything else means the named PR is not queued.
        _pull, current = _resolved_pull_request(name, number, token, api_url, http)
        if current != head:
            raise merge_group_error(
                "head-not-in-group",
                "pull request #%s head %s is not part of merge group head %s"
                % (number, current[:12], head[:12]),
            )
        tested[number] = head

    if not tested:
        raise merge_group_error(
            "no-pr", "merge group %s resolved to no pull request" % head[:12]
        )

    for number, tested_head in sorted(tested.items()):
        _pull, current = _resolved_pull_request(name, number, token, api_url, http)
        if current != tested_head:
            raise merge_group_error(
                "head-moved",
                "pull request #%s head moved: the group is testing %s but the pull request is "
                "at %s, so the head where lease-tamper passed is not the head being merged"
                % (number, tested_head[:12], current[:12]),
            )
        if not _lease_green_at(name, tested_head, token, api_url, http):
            raise merge_group_error(
                "not-green-at-head",
                "pull request #%s has no successful %s check run at its head %s; the queue "
                "cannot prove the reviewed head passed this gate"
                % (number, LEASE_JOB_NAME, tested_head[:12]),
            )
        if not quiet:
            print(
                "lease-tamper: merge_group: PR #%s head %s is in group head %s and lease-tamper "
                "passed at that exact head." % (number, tested_head[:12], head[:12])
            )
    if not quiet:
        print(
            "lease-tamper: OK: %d pull request(s) in merge group %s on base %s; each reviewed "
            "head passed lease-tamper before it was queued." % (len(tested), head[:12], base[:12])
        )
    return 0


def _pull_reviews(name, number, token, api_url=DEFAULT_API_URL, http=None):
    """Every review recorded on one pull request, oldest page first."""
    url = "%s/repos/%s/pulls/%d/reviews?per_page=100" % (api_url, name, int(number))
    reviews = []
    for _ in range(MAX_PAGES):
        payload, link = _get_checked(url, token, http)
        if not isinstance(payload, list):
            raise GateError("%s did not return a review list" % url)
        reviews.extend(item for item in payload if isinstance(item, dict))
        url = _next_page(link)
        if not url:
            return reviews
    raise GateError("the pull-request review lookup did not terminate")


def _is_independent_approval(body, head):
    """A COMMENTED review in the repository's independent-review convention.

    The line is parsed by the shared policy parser, so this gate and the merge
    lane accept exactly the same attestations.
    """
    kind, named = parse_review_line(body)
    if kind != REVIEW_APPROVE_INDEPENDENT:
        return False
    return review_names_commit(named, head)


def independent_review_at_head(reviews, head, repo=None):
    """The review that proves a non-slice pull request was reviewed at ``head``.

    The review's ``commit_id`` must be the exact head and no rejection may have
    been submitted after it: the newest decisive review governs.  This is the
    strict form; ``independent_review()`` adds the owner's base-merge
    carry-forward on top of it and is what a pull-request check uses.
    """
    ancestor = (lambda older, newer: _is_ancestor(repo, older, newer)) if repo else None
    return newest_approval_at_head(reviews, head, REVIEW_APPROVE_INDEPENDENT, ancestor)


def _review_commit(review):
    """The full commit a review records, or ``None`` when it names none."""
    value = str((review or {}).get("commit_id") or "").strip().lower()
    return value if HEX_REV.match(value) else None


def independent_review(repo, reviews, base, head):
    """``(review, match, refusal)`` for the non-slice review rule.

    The exact-head approval is preferred.  Otherwise the newest approval at an
    earlier head is offered to ``review_carry_forward`` and the first that
    carries wins.  An approval a later rejection overrides is dropped before
    either test: the newest decisive review governs, so a CHANGES_REQUESTED
    review (or a COMMENTED review whose first line starts CHANGES REQUESTED)
    submitted after the approval refuses it.  ``refusal`` describes the newest
    rejected candidate -- its named reason and detail -- so the failure says
    why the review did not carry, not merely that it was not at the head.
    """
    wanted = str(head or "").strip().lower()
    if not wanted:
        return None, None, None
    ancestor = lambda older, newer: _is_ancestor(repo, older, newer)
    refusal = None
    for review, match in approval_chain(reviews, wanted, REVIEW_APPROVE_INDEPENDENT, ancestor):
        if match == REVIEW_MATCH_EXACT:
            return review, REVIEW_MATCH_EXACT, None
        commit = _review_commit(review)
        ok, reason, detail = review_carry_forward(repo, commit, base, wanted)
        if ok:
            return (
                review,
                REVIEW_MATCH_CARRIED,
                {"reason": reason, "detail": detail, "review_sha": commit},
            )
        if refusal is None:
            refusal = {"reason": reason, "detail": detail, "review_sha": commit}
    if refusal is None:
        rejection = rejection_at_head(reviews, wanted)
        if rejection is None:
            # The approval may have been recorded at an earlier head and
            # overridden there; the rejection at that commit is still why the
            # carry fails, so name it instead of saying no review exists.
            for candidate in reversed(review_chronology(reviews)):
                if not review_is_approval(candidate, REVIEW_APPROVE_INDEPENDENT):
                    continue
                rejection = governing_rejection(reviews, candidate, wanted, ancestor)
                if rejection is not None:
                    break
        if rejection is not None:
            refusal = {
                "reason": "review_changes_requested",
                "detail": "a CHANGES REQUESTED review was submitted after the approval at head "
                "%s; the newest decisive review governs, so the approval no longer counts"
                % wanted[:12],
                "review_sha": str(rejection.get("commit_id") or "")[:12],
            }
    return None, None, refusal


def check_non_slice(repo, target, environ, quiet=False, http=None, detail=None):
    """A pull request no frozen base lease names passes only on review at head."""
    head = target.head
    why = detail or "no frozen lease on base %s names it" % target.base[:12]
    if target.number is None:
        raise GateError(
            "this pull request is not a slice (%s; head %s) and needs an independent "
            "review at its head; a local run without --pull-request cannot prove that "
            "review. Pass --pull-request <N> with GITHUB_REPOSITORY and GITHUB_TOKEN."
            % (why, head[:12]),
            reason="non_slice_requires_independent_review",
        )
    name = (environ.get("GITHUB_REPOSITORY") or "").strip()
    token = (environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN") or "").strip()
    if not name or not token:
        raise GateError(
            "PR #%s is not a slice (%s). A non-slice pull request passes only with an "
            "independent review at head %s, and GITHUB_REPOSITORY/GITHUB_TOKEN are "
            "required to read the reviews; failing closed without them."
            % (target.number, why, head[:12]),
            reason="non_slice_requires_independent_review",
        )
    reviews = _pull_reviews(name, target.number, token, _api_url(environ), http)
    review, match, carry_info = independent_review(repo, reviews, target.base, head)
    if review is None:
        if carry_info is not None and carry_info.get("reason") == "review_changes_requested":
            raise GateError(
                "PR #%s is not a slice (%s). A non-slice pull request passes only with an "
                "independent review at its exact head %s. The newest decisive review is a "
                "rejection, not an approval: [%s] %s. Post a new APPROVE after the rejection "
                "at %s and re-run."
                % (
                    target.number,
                    why,
                    head[:12],
                    "review_changes_requested",
                    carry_info.get("detail") or "",
                    head[:12],
                ),
                reason="non_slice_requires_independent_review",
            )
        if carry_info is not None:
            raise GateError(
                "PR #%s is not a slice (%s). A non-slice pull request passes only with an "
                "independent review at its exact head %s, or with one at an earlier head that "
                "carries forward. The newest approval (commit %s) does not carry: [%s] %s. "
                "A review at an earlier head counts only when every commit after it is a merge "
                "of the base and the pull request's diff against the base is byte-identical to "
                "the reviewed one. If the change is intentional, post the review at %s and "
                "re-run."
                % (
                    target.number,
                    why,
                    head[:12],
                    str(carry_info.get("review_sha") or "?")[:12],
                    carry_info.get("reason") or CARRY_UNREADABLE,
                    carry_info.get("detail") or "",
                    head[:12],
                ),
                reason="non_slice_requires_independent_review",
            )
        raise GateError(
            "PR #%s is not a slice (%s). A non-slice pull request passes only with an "
            "independent review at its exact head %s: an APPROVED review, or a COMMENTED "
            "review whose first line starts 'APPROVE' and names an independent review, "
            "with commit_id equal to the head. None found. Post the review at %s and "
            "re-run." % (target.number, why, head[:12], head[:12]),
            reason="non_slice_requires_independent_review",
        )
    if not quiet:
        state = str(review.get("state") or "COMMENTED").upper()
        if match == REVIEW_MATCH_CARRIED:
            print(
                "lease-tamper: OK: PR #%s is not a slice (%s) and its independent review "
                "(%s) at %s carries forward to head %s: [%s] %s"
                % (
                    target.number,
                    why,
                    state,
                    str(review.get("commit_id") or "")[:12],
                    head[:12],
                    CARRY_OK,
                    (carry_info or {}).get("detail") or "base-merge-only update, diff byte-identical",
                )
            )
        else:
            print(
                "lease-tamper: OK: PR #%s is not a slice (%s) and carries an independent "
                "review (%s) at head %s." % (target.number, why, state, head[:12])
            )
    print("lease-tamper: class=non-slice")
    return 0


def infer_event_name(payload):
    """Best-effort event name when GITHUB_EVENT_NAME is not set."""
    if "merge_group" in payload:
        return "merge_group"
    # A pull_request_review payload carries BOTH the review and the pull request
    # it was submitted on, so it must be recognised before pull_request.
    if "review" in payload and "pull_request" in payload:
        return "pull_request_review"
    if "pull_request" in payload:
        return "pull_request"
    return "other"


def target_from_pull_request(payload, event_name):
    """The Target for a pull_request-shaped object, from any pull-request event.

    Both ``pull_request`` and ``pull_request_review`` payloads carry the pull
    request itself, so the base, the head, the labels and the number come from
    the same place.  The head is ALWAYS ``pull_request.head.sha``: a review
    payload also carries ``review.commit_id`` (the commit the review was
    submitted against, which may be older than the head) and reading it here
    would let a review move the gate's head backwards.  It is never read, and a
    payload without a head fails closed rather than borrowing it.
    """
    pull = payload.get("pull_request") or {}
    base = ((pull.get("base") or {}).get("sha") or "").strip()
    head = ((pull.get("head") or {}).get("sha") or "").strip()
    if not base or not head:
        raise GateError(
            "the %s payload has no base/head sha in pull_request; the head is read only "
            "from pull_request.head.sha and a review's commit_id is never substituted "
            "for it." % event_name,
            reason="review_event_head_missing" if event_name == "pull_request_review" else None,
        )
    branch = ((pull.get("head") or {}).get("ref") or "").strip() or None
    return Target(
        base, head, _labels_of(pull), pull.get("body") or "", pull.get("number"), branch
    )


def target_from_event(payload, event_name, environ, http=None):
    """Return a Target, or None for an event that carries no pull request."""
    name = (event_name or "").strip() or infer_event_name(payload)

    if name == "merge_group":
        # Routed to check_merge_group() by main(); reaching here is a bug.
        raise merge_group_error("internal", "merge_group must be handled by check_merge_group()")

    if name in PULL_REQUEST_EVENTS:
        return target_from_pull_request(payload, name)

    return None


def _normalise(path):
    return posixpath.normpath(str(path).replace("\\", "/").strip())


def _valid_lease_path(path):
    if not path or path.startswith("/") or path.startswith("~"):
        return False
    normal = _normalise(path)
    return normal != "." and normal != ".." and not normal.startswith("../")


def _has_glob(path):
    """True when a path is a wildcard pattern rather than one concrete file."""
    return any(char in str(path) for char in GLOB_CHARS)


def _contract_sha256(document):
    """The bundlebees contract identity: canonical JSON without ``review``.

    Compact, key-sorted, ASCII JSON of the contract body with the ``review``
    block removed -- byte-identical to ``bundlebees_plugin.contract`` so a hash
    computed by the plugin, by a lease author or by this gate is the same value.
    """
    body = {key: value for key, value in document.items() if key != "review"}
    text = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope_lists(document):
    scope = document.get("scope") if isinstance(document.get("scope"), dict) else {}
    out = {}
    for key in ("assigned_files", "test_files", "prohibited", "read_only"):
        value = scope.get(key)
        if value is None:
            out[key] = []
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            out[key] = sorted(_normalise(item) for item in value)
        else:
            out[key] = None
    return out


def approved_contract(repo, revision, lease_doc):
    """The approved frozen contract a lease is derived from, or a hard failure.

    A lease is only usable when it names an approved contract that is already
    committed on the base branch: the contract's own canonical hash must equal
    the ``contract_sha256`` in both the lease and the file name, its review must
    be approved/frozen with an approver, and its ``source_rev`` and four scope
    lists must equal the lease's.  That makes a widened or self-issued lease
    unusable: the hash pins the exact approved text, and the field comparison
    refuses any drift from it.
    """
    sha = str((lease_doc or {}).get("contract_sha256") or "").strip().lower()
    if not SHA256_RE.match(sha):
        raise GateError(
            "lease %r does not declare a 64-hex contract_sha256; a lease is only usable "
            "when it names an approved frozen contract."
            % (lease_doc.get("id") if isinstance(lease_doc, dict) else "<no id>"),
            reason="lease_contract_missing",
        )
    path = "%s/%s.json" % (CONTRACTS_DIR, sha)
    document = _read_contract_object(repo, revision, path)
    if document is None:
        raise GateError(
            "lease declares contract_sha256 %s but %s does not exist on base %s; only a "
            "contract already committed to the base branch is approved. Land the contract "
            "on base in a reviewed non-slice pull request first."
            % (sha, path, revision[:12]),
            reason="lease_contract_not_approved",
        )
    if not isinstance(document, dict):
        raise GateError("contract %r is not a JSON object" % path, reason="lease_contract_not_approved")
    review = document.get("review") if isinstance(document.get("review"), dict) else {}
    status = str(review.get("status") or "").strip().lower()
    approver = str(review.get("approved_by") or "").strip()
    if status not in FROZEN_STATUSES or not approver:
        raise GateError(
            "contract %r is not an approved frozen contract (review.status=%r, "
            "review.approved_by=%r); a lease may only name an approved frozen contract."
            % (path, status or "<absent>", approver or "<absent>"),
            reason="lease_contract_not_approved",
        )
    computed = _contract_sha256(document)
    if computed != sha:
        raise GateError(
            "contract %r hashes to %s, not to the name/lease hash %s; the approved text "
            "changed after it was frozen." % (path, computed, sha),
            reason="lease_contract_hash_mismatch",
        )
    if str(document.get("source_rev") or "").strip().lower() != str(
        lease_doc.get("source_rev") or ""
    ).strip().lower():
        raise GateError(
            "lease source_rev %r does not match contract %r source_rev %r."
            % (lease_doc.get("source_rev"), path, document.get("source_rev")),
            reason="lease_contract_mismatch",
        )
    lease_scope, contract_scope = _scope_lists(lease_doc), _scope_lists(document)
    for key in ("assigned_files", "test_files", "prohibited", "read_only"):
        if lease_scope[key] != contract_scope[key]:
            raise GateError(
                "lease scope.%s differs from the approved contract %r; a lease may not "
                "widen or narrow the contract it names (lease=%r contract=%r)."
                % (key, path, lease_scope[key], contract_scope[key]),
                reason="lease_contract_mismatch",
            )
    return document, path


def _verify_sha(explicit, lease_doc, contract):
    """The verify boundary: a local flag, then the base lease, then ``source_rev``.

    Labels and the pull-request body are deliberately unreadable here: a pull
    request that could set its own boundary (for example ``Lease-Verify: <head>``)
    would move it to the head and have zero commits checked.
    """
    if explicit:
        return explicit.strip().lower(), "verify sha"

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


def _name_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _leaf_branch(branch):
    value = str(branch or "").strip()
    return value.rsplit("/", 1)[-1] if value else ""


def _names_target(doc, target):
    """True when a lease names this pull request, its branch or its slice.

    Only an explicit field counts.  The shape of the branch name never does:
    ``feat/`` does not make a pull request a slice, and a lease that does not
    name the pull request does not apply to it.
    """
    if not isinstance(doc, dict):
        return False
    number = target.number
    branch = str(target.branch or "").strip()
    leaf = _leaf_branch(branch)

    for key in PR_NAME_KEYS:
        for item in _name_list(doc.get(key)):
            if isinstance(item, bool):
                continue
            if isinstance(item, int) and number is not None and item == number:
                return True
            if isinstance(item, str):
                text = item.strip().lstrip("#")
                if text.isdigit() and number is not None and int(text) == number:
                    return True

    for key in BRANCH_NAME_KEYS:
        for item in _name_list(doc.get(key)):
            if isinstance(item, str) and branch and item.strip() == branch:
                return True

    for key in SLICE_NAME_KEYS:
        value = doc.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip().lower()
        if branch and name == branch.lower():
            return True
        if leaf and name == leaf.lower():
            return True
    return False


def _is_frozen(doc):
    """A base-store lease is frozen only when it says so, with an approver.

    Fail-closed by design: a missing or non-committal ``review`` block does not
    make a lease frozen, so a lease cannot become usable by omitting the field.
    """
    review = doc.get("review") if isinstance(doc, dict) else None
    if not isinstance(review, dict):
        return False
    status = str(review.get("status") or "").strip().lower()
    if status not in FROZEN_STATUSES:
        return False
    return bool(str(review.get("approved_by") or "").strip())


def _store_lease_paths(repo, revision):
    proc = _run(repo, "ls-tree", "-r", "--name-only", revision, "--", LEASES_DIR, check=False)
    if proc.returncode != 0:
        return []
    return [
        _normalise(line)
        for line in proc.stdout.splitlines()
        if line.strip().endswith(".json")
    ]


def _lease_json_at(repo, revision, path):
    raw = _file_at(repo, revision, path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _named_lease_at(repo, target):
    """(path, doc) for the frozen base-store lease that names ``target``."""
    for path in _store_lease_paths(repo, target.base):
        doc = _lease_json_at(repo, target.base, path)
        if not isinstance(doc, dict) or not _is_frozen(doc):
            continue
        if _names_target(doc, target):
            return path, doc
    return None, None


def metadata_changes(repo, target):
    """Store paths the pull request's own commits change.

    ``.rizzstack/leases/**``, ``.rizzstack/contracts/**`` and
    ``.rizzstack/lease.json`` are the gate's input.  A pull request that writes
    them is landing gate metadata, never a slice: it must pass the
    independent-review rule at its head, so a pull request can never grant
    itself a lease or an approved contract.  Only non-merge commits are scanned,
    so merging the base into a slice does not make the slice a metadata change.
    """
    found = []
    proc = _run(repo, "rev-list", "--no-merges", target.head, "--not", target.base, check=False)
    if proc.returncode != 0:
        raise GateError("cannot list the pull request's own commits")
    for sha in proc.stdout.split():
        for path in _commit_paths(repo, sha):
            normal = _normalise(path)
            if (
                normal == LEASE_FILE
                or normal.startswith(LEASES_DIR + "/")
                or normal.startswith(CONTRACTS_DIR + "/")
            ) and normal not in found:
                found.append(normal)
    return found


def resolve_contract(repo, target, explicit_path=None):
    """Return (contract, contract_path, lease_doc, source_revision).

    The contract is the approved frozen contract named by a base-branch lease's
    ``contract_sha256``; nothing is read from the pull request's head.  Raises
    ``NoSliceLease`` when no frozen base lease names the pull request: that is a
    non-slice pull request, and the caller applies the independent-review rule.
    """
    if explicit_path:
        path = _normalise(explicit_path)
        lease_doc = _read_contract_object(repo, target.base, path)
        if lease_doc is None:
            raise GateError(
                "--contract %r does not exist at base %s; only a lease committed to the "
                "base branch is accepted." % (path, target.base[:12]),
                reason="lease_not_on_base",
            )
    else:
        path, lease_doc = _named_lease_at(repo, target)
        if lease_doc is None:
            raise NoSliceLease(
                "no frozen lease on base %s names %s (head %s, branch %s): no "
                "'pull_request'/'branch'/'slice' match in %s/*.json. A label, a body "
                "line and a head-branch lease are never read as one."
                % (
                    target.base[:12],
                    ("PR #%s" % target.number) if target.number else "this pull request",
                    target.head[:12],
                    target.branch or "<unknown>",
                    LEASES_DIR,
                )
            )

    if not isinstance(lease_doc, dict):
        raise GateError("contract %r is not a JSON object" % path)

    contract, contract_path = approved_contract(repo, target.base, lease_doc)
    return contract, contract_path, lease_doc, target.base


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
    globbed = sorted(item for item in assigned if _has_glob(item))
    if globbed:
        # assigned_files is an ALLOW-list: a wildcard there grants an unbounded
        # scope ("*" or "**" reaches every file).  Deny patterns in
        # test_files/prohibited/read_only are still matched with fnmatch.
        raise GateError(
            "contract scope.assigned_files must enumerate concrete files; wildcard "
            "entries are rejected (%s). Lease the exact paths instead."
            % ", ".join(repr(item) for item in globbed),
            reason="lease_glob_scope",
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

    boundary, boundary_kind = _verify_sha(verify_sha, lease_doc, contract)
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

    contract_file = _normalise(contract_path)

    def exempt(path):
        # The lease/contract store is the gate's input, and the gate reads it
        # from the base branch.  A pull request that changes it never reaches
        # this function (metadata changes are classified non-slice first); the
        # exemption keeps base-merge history from raising a false violation.
        return (
            path == contract_file
            or path == LEASE_FILE
            or path.startswith(LEASES_DIR + "/")
            or path.startswith(CONTRACTS_DIR + "/")
        )
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
            if exempt(path):
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
            if exempt(path):
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
    parser.add_argument("--labels", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--body", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--pull-request", default=None, type=int, help="pull-request number")
    parser.add_argument("--branch", default=None, help="pull-request head branch (local runs)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        labels = [item for item in (args.labels or "").split(",") if item.strip()]
        body = args.body or ""
        event_name = ""
        if args.base and args.head:
            target = Target(
                args.base.strip(),
                args.head.strip(),
                labels,
                body,
                args.pull_request,
                args.branch,
            )
        else:
            if not args.event:
                raise GateError("no event JSON path (pass one or set GITHUB_EVENT_PATH)")
            try:
                with open(args.event, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError) as exc:
                raise GateError("cannot read the event payload %r: %s" % (args.event, exc))
            event_name = (
                environ.get("GITHUB_EVENT_NAME") or ""
            ).strip() or infer_event_name(payload)
            if event_name == "merge_group":
                return check_merge_group(args.repo, payload, environ, quiet=args.quiet)
            target = target_from_event(payload, event_name, environ)
            if target is None:
                if not args.quiet:
                    print("lease-tamper: no pull request in this event; nothing to check.")
                return 0
            if labels:
                target.labels = labels
            if body:
                target.body = body

        target.base = _resolve_rev(args.repo, target.base)
        target.head = _resolve_rev(args.repo, target.head)
        if event_name == "pull_request_review" and not args.quiet:
            print(
                "lease-tamper: event=pull_request_review; head read from "
                "pull_request.head.sha %s; applying the same rules as a pull_request."
                % target.head
            )

        changes = metadata_changes(args.repo, target)
        if changes and not args.contract:
            detail = "it changes gate metadata (%s), which is landed only through a " \
                     "reviewed non-slice pull request" % ", ".join(changes)
            if not args.quiet:
                print("lease-tamper: NOT A SLICE: %s." % detail)
            return check_non_slice(args.repo, target, environ, quiet=args.quiet, detail=detail)

        try:
            contract, contract_path, lease_doc, _revision = resolve_contract(
                args.repo, target, args.contract
            )
        except NoSliceLease:
            return check_non_slice(args.repo, target, environ, quiet=args.quiet)
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
        print("lease-tamper: class=slice")
        return 0
    except GateError as exc:
        if exc.reason:
            print("lease-tamper: FAILED (reason=%s): %s" % (exc.reason, exc))
            print(
                "::error title=lease-tamper %s::%s"
                % (exc.reason, str(exc).replace("\n", " "))
            )
        else:
            print("lease-tamper: FAILED: %s" % exc)
            print("::error::lease-tamper: %s" % str(exc).replace("\n", " "))
        return 1
    except subprocess.TimeoutExpired:
        print("lease-tamper: FAILED: git did not finish within the timeout; failing closed.")
        print("::error::lease-tamper: git timeout; failing closed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
