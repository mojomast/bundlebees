#!/usr/bin/env python3
"""Lease/tamper gate: a slice may not change files outside its lease.

The gate answers one question for a pull request: **after the verify boundary,
did any commit touch a path the slice does not own, or a frozen test file?**

A slice contract is an ``acdd/1`` contract (``scope.assigned_files``,
``scope.test_files``, ``scope.prohibited``, ``scope.read_only`` and
``source_rev``) or a lease record with the same ``scope`` shape plus a
``source_rev``/``verify_sha``.

**The lease is never read from the pull-request branch.**  A pull request could
edit its own lease, so the gate resolves it, by contract hash or by path, only
from trusted sources:

1. ``--contract`` / ``--contract-sha256`` (local runs and tests);
2. a pull-request label ``lease-contract:<path>``, ``lease:<path>``,
   ``lease-contract-sha256:<64-hex>`` or ``contract-sha256:<64-hex>``;
3. a pull-request body line ``Lease-Contract: <path>``,
   ``Lease-Contract-Sha256: <64-hex>`` (``Contract-Path:``/``Contract-SHA256:``
   also work);
4. a contract hash found in the base branch's ``.rizzstack/leases/*.json`` at
   the base tip or merge-base, or in a frozen contract store
   (``BUNDLEBEES_CONTRACT_STORE``, ``--contract-store``,
   ``~/.hermes/plugin-data/bundlebees/contracts`` and
   ``rizzstack-t1634/contracts``) keyed by hash;
5. the repository convention ``.rizzstack/lease.json`` on the base branch.

A lease that exists only on the pull-request branch fails closed with the named
reason ``lease_not_on_base``.  The single bootstrap exception is a repository
whose base branch does not yet contain ``.github/scripts/lease_tamper.py``:
the pull request that introduces the gate has no gate to weaken, so its own
lease is accepted (``bootstrap`` mode) and the run says so.

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
LEASE_DIR = ".rizzstack/leases"
GATE_SCRIPT = ".github/scripts/lease_tamper.py"
CONTRACT_STORE_ENV = "BUNDLEBEES_CONTRACT_STORE"
DEFAULT_CONTRACT_STORES = (
    "~/.hermes/plugin-data/bundlebees/contracts",
    "rizzstack-t1634/contracts",
)
STORE_MAX_FILES = 2000
STORE_MAX_DEPTH = 3
LABEL_CONTRACT_PREFIXES = ("lease-contract:", "lease:")
LABEL_CONTRACT_HASH_PREFIXES = ("lease-contract-sha256:", "contract-sha256:")
LABEL_VERIFY_PREFIX = "lease-verify:"
BODY_CONTRACT = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:lease-contract|contract-path)[ \t]*:[ \t]*`?([^`\s]+)`?"
)
BODY_CONTRACT_HASH = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:lease-contract-sha256|contract-sha256)[ \t]*:"
    r"[ \t]*`?([0-9a-fA-F]{64})`?"
)
BODY_VERIFY = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:lease-verify|verify-sha)[ \t]*:[ \t]*`?([0-9a-fA-F]{40,64})`?"
)
HEX_REV = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# refs/heads/gh-readonly-queue/<base>/pr-<number>-<sha>
QUEUE_REF = re.compile(r"/pr-(\d+)-([0-9a-fA-F]{7,64})")
HTTP_TIMEOUT_SECONDS = 20
MAX_PAGES = 10
MAX_GROUP_ENTRIES = 100
DEFAULT_API_URL = "https://api.github.com"
LEASE_JOB_NAME = "lease-tamper"
MERGE_GROUP_REASON = "merge_group_pr_unresolved"


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


def _merge_base(repo, base, head):
    proc = _run(repo, "merge-base", base, head, check=False)
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else base


def contract_sha256(document):
    """SHA-256 of compact, key-sorted JSON with the ``review`` block removed.

    This is the frozen-contract identity used by bundlebees
    (``bundlebees_plugin.contract.contract_sha256``): a lease carried in the
    frozen store is found by this hash, not by its filename.
    """
    if not isinstance(document, dict):
        raise GateError("a contract hash can only be computed for a JSON object")
    body = {key: value for key, value in document.items() if key != "review"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _contract_body(document):
    """The contract inside a packet, or the document itself."""
    if isinstance(document, dict) and isinstance(document.get("contract"), dict):
        if "contract_sha256" in document or "packet_version" in document:
            return document["contract"]
    return document


def declared_contract_hash(document):
    """The hash a lease/packet declares for itself, if any (lowercase)."""
    if not isinstance(document, dict):
        return None
    review = document.get("review")
    if isinstance(review, dict) and isinstance(review.get("contract_sha256"), str):
        value = review["contract_sha256"].strip().lower()
        if HASH_RE.match(value):
            return value
    value = document.get("contract_sha256")
    if isinstance(value, str) and HASH_RE.match(value.strip().lower()):
        return value.strip().lower()
    return None


def lease_hashes(document):
    """Every hash a lease document can be matched by: computed and declared."""
    body = _contract_body(document)
    found = []
    try:
        found.append(contract_sha256(body))
    except GateError:
        return []
    declared = declared_contract_hash(document)
    if declared and declared not in found:
        found.append(declared)
    declared = declared_contract_hash(body)
    if declared and declared not in found:
        found.append(declared)
    return found


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
        if str(run.get("status") or "") not in ("", "completed"):
            continue
        if str(run.get("conclusion") or "") == "success":
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


def infer_event_name(payload):
    """Best-effort event name when GITHUB_EVENT_NAME is not set."""
    if "merge_group" in payload:
        return "merge_group"
    if "pull_request" in payload:
        return "pull_request"
    return "other"


def target_from_event(payload, event_name, environ, http=None):
    """Return a Target, or None for an event that carries no pull request."""
    name = (event_name or "").strip() or infer_event_name(payload)

    if name == "merge_group":
        # Routed to check_merge_group() by main(); reaching here is a bug.
        raise merge_group_error("internal", "merge_group must be handled by check_merge_group()")

    if name == "pull_request":
        pull = payload.get("pull_request") or {}
        base = ((pull.get("base") or {}).get("sha") or "").strip()
        head = ((pull.get("head") or {}).get("sha") or "").strip()
        if not base or not head:
            raise GateError("the pull_request payload has no base/head sha")
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


def _contract_hash(target, explicit):
    """Return (64-hex contract hash, where it came from) or (None, None)."""
    if explicit:
        value = explicit.strip().lower()
        if not HASH_RE.match(value):
            raise GateError("--contract-sha256 must be a 64-character lowercase hex digest")
        return value, "--contract-sha256"

    for label in target.labels:
        lowered = label.lower()
        for prefix in LABEL_CONTRACT_HASH_PREFIXES:
            if lowered.startswith(prefix):
                value = label.strip()[len(prefix):].strip().lower()
                if not HASH_RE.match(value):
                    raise GateError(
                        "label %r does not name a 64-hex contract hash" % label
                    )
                return value, "label %r" % label

    match = BODY_CONTRACT_HASH.search(target.body)
    if match:
        return match.group(1).lower(), "PR body"

    return None, None


def _gate_on_base(repo, target):
    """True when the base branch already carries this gate (no bootstrap)."""
    return _file_at(repo, target.base, GATE_SCRIPT) is not None


def _base_revisions(repo, target):
    """Trusted revisions, base tip first, then the merge-base."""
    out = [(target.base, "base")]
    base = _merge_base(repo, target.base, target.head)
    if base and base != target.base:
        out.append((base, "merge-base"))
    return out


def _lease_paths_at(repo, revision):
    proc = _run(repo, "ls-tree", "-r", "--name-only", revision, "--", LEASE_DIR, check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.endswith(".json")]


def _find_by_hash_in_tree(repo, revision, wanted):
    """A lease under .rizzstack/leases at *revision* whose hash is *wanted*."""
    for path in _lease_paths_at(repo, revision):
        doc = _read_contract_object(repo, revision, path)
        if isinstance(doc, dict) and wanted in lease_hashes(doc):
            return doc, path
    return None, None


def _find_by_hash_on_head(repo, target, wanted):
    return _find_by_hash_in_tree(repo, target.head, wanted)


def _store_roots(repo, explicit, environ):
    raw = list(explicit or [])
    env = (environ.get(CONTRACT_STORE_ENV) or "").strip()
    if env:
        raw.extend(part for part in env.split(os.pathsep) if part.strip())
    raw.extend(DEFAULT_CONTRACT_STORES)
    raw.append(
        os.path.join(os.path.dirname(os.path.abspath(str(repo))), "rizzstack-t1634", "contracts")
    )
    roots, seen = [], set()
    for item in raw:
        path = os.path.abspath(os.path.expanduser(str(item).strip()))
        if path in seen:
            continue
        seen.add(path)
        if os.path.isdir(path):
            roots.append(path)
    return roots


def _store_json_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= STORE_MAX_DEPTH:
            dirnames[:] = []
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".json"):
                yield os.path.join(dirpath, name)


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _find_by_hash_in_store(roots, wanted):
    """A frozen contract keyed by *wanted*: ``<root>/<hash>.json`` or by content."""
    for root in roots:
        direct = os.path.join(root, wanted + ".json")
        if os.path.isfile(direct):
            doc = _read_json_file(direct)
            if not isinstance(doc, dict):
                raise GateError("frozen contract %r is not a JSON object" % direct)
            if wanted not in lease_hashes(doc):
                raise GateError(
                    "frozen contract %r is not the contract with hash %s" % (direct, wanted)
                )
            return doc, direct
        count = 0
        for path in _store_json_files(root):
            count += 1
            if count > STORE_MAX_FILES:
                break
            doc = _read_json_file(path)
            if isinstance(doc, dict) and wanted in lease_hashes(doc):
                return doc, path
    return None, None


def resolve_contract(
    repo,
    target,
    explicit_path=None,
    explicit_hash=None,
    explicit_stores=None,
    environ=None,
):
    """Return (contract, contract_path, lease_doc, source_revision, notice).

    The lease is read only from trusted sources: an explicit flag, the base
    branch (tip, then merge-base) or the frozen contract store.  The PR head is
    consulted only to report that a branch-only lease is not acceptable
    (``lease_not_on_base``) -- or, in a repository that does not carry the gate
    on its base branch yet, to bootstrap the pull request that introduces it.
    """
    environ = environ if environ is not None else os.environ
    wanted, wanted_origin = _contract_hash(target, explicit_hash)
    path, path_origin = _contract_path(target, explicit_path)
    if not path and not wanted:
        path, path_origin = LEASE_FILE, "the repository convention"

    bootstrap = not _gate_on_base(repo, target)
    base_revisions = _base_revisions(repo, target)

    lease_doc = None
    contract_revision = None
    contract_path = None
    origin = None
    notice = None

    # 1. A named path is read at the base tip, then at the merge-base.  A path
    #    the branch adds or edits is never read unless this PR bootstraps the gate.
    if path and not wanted:
        for revision, label in base_revisions:
            doc = _read_contract_object(repo, revision, path)
            if doc is not None:
                lease_doc, contract_revision, contract_path = doc, revision, path
                origin = "%s at %s" % (path, label)
                break

    # 2. A named contract hash is looked up in the base lease directory, then
    #    in the frozen contract store.  The hash, not the filename, is the key.
    if lease_doc is None and wanted:
        for revision, label in base_revisions:
            doc, found = _find_by_hash_in_tree(repo, revision, wanted)
            if doc is not None:
                lease_doc, contract_revision, contract_path = doc, revision, found
                origin = "%s at %s" % (found, label)
                break
        if lease_doc is None:
            doc, found = _find_by_hash_in_store(
                _store_roots(repo, explicit_stores, environ), wanted
            )
            if doc is not None:
                lease_doc, contract_revision, contract_path = doc, "frozen-store", found
                origin = "frozen contract store %s" % found
                notice = (
                    "lease-tamper: NOTE: contract sha256:%s resolved from the frozen contract "
                    "store %s." % (wanted, found)
                )

    # 3. Bootstrap: the base branch has no gate script, so the pull request
    #    that introduces it is the only source for its own lease.  It cannot
    #    weaken an existing gate because there is none on the base.
    if lease_doc is None and bootstrap:
        if path:
            doc = _read_contract_object(repo, target.head, path)
            if doc is not None:
                lease_doc, contract_revision, contract_path = doc, target.head, path
                origin = "%s at PR head (bootstrap)" % path
                notice = (
                    "lease-tamper: NOTE: %s does not carry %s yet; reading the lease from the "
                    "PR head to bootstrap the gate (bootstrap mode)." % (target.base[:12], GATE_SCRIPT)
                )
        if lease_doc is None and wanted:
            doc, found = _find_by_hash_on_head(repo, target, wanted)
            if doc is not None:
                lease_doc, contract_revision, contract_path = doc, target.head, found
                origin = "%s at PR head (bootstrap)" % found
                notice = (
                    "lease-tamper: NOTE: %s does not carry %s yet; reading the lease from the "
                    "PR head to bootstrap the gate (bootstrap mode)." % (target.base[:12], GATE_SCRIPT)
                )

    if lease_doc is None:
        branch_copy = False
        if path and _file_at(repo, target.head, path) is not None:
            branch_copy = True
        if wanted and _find_by_hash_on_head(repo, target, wanted)[0] is not None:
            branch_copy = True
        if branch_copy:
            raise GateError(
                "contract %s (from %s) exists only on PR head %s, not on base %s or its "
                "merge-base. A pull request may not supply or edit the lease it is judged by; "
                "land the lease on the base branch (or in the frozen contract store) first."
                % (
                    path or ("sha256:%s" % (wanted or "?")),
                    wanted_origin or path_origin or "the pull request",
                    target.head[:12],
                    target.base[:12],
                ),
                reason="lease_not_on_base",
            )
        if path and not wanted and path_origin != "the repository convention":
            raise GateError(
                "contract %r (from %s) does not exist at base %s or its merge-base; "
                "only trusted, pre-existing leases are accepted. Land the lease on the base "
                "branch (or in the frozen contract store) before opening the pull request."
                % (path, path_origin, target.base[:12])
            )
        raise GateError(
            "no slice contract for %s (head %s): no contract hash/path label or body line, "
            "no matching lease in .rizzstack/leases on base %s, in the merge-base or in the "
            "frozen contract store, and no %s on the base branch. Add the lease before "
            "opening the pull request."
            % (
                ("PR #%s" % target.number) if target.number else "this pull request",
                target.head[:12],
                target.base[:12],
                LEASE_FILE,
            )
        )

    if not isinstance(lease_doc, dict):
        raise GateError("contract %r is not a JSON object" % contract_path)

    if wanted and wanted not in lease_hashes(lease_doc):
        raise GateError(
            "contract %r (from %s) does not match the named contract hash %s (%s); "
            "refusing to apply a lease whose content is not the one the pull request names."
            % (contract_path, origin, wanted, wanted_origin)
        )

    pointer = lease_doc.get("contract_path") or lease_doc.get("contract")
    if isinstance(pointer, str) and pointer.strip():
        target_path = _normalise(pointer)
        if contract_revision == "frozen-store":
            raise GateError(
                "frozen contract store %r is a pointer to %r; a store entry must be the "
                "contract itself" % (contract_path, target_path)
            )
        contract = _read_contract_object(repo, contract_revision, target_path)
        if contract is None:
            branch_copy = contract_revision != target.head and (
                _file_at(repo, target.head, target_path) is not None
            )
            if branch_copy:
                raise GateError(
                    "contract %r points at %r, which exists only on PR head %s, not at %s. "
                    "A pull request may not supply the contract it is judged by."
                    % (contract_path, target_path, target.head[:12], (contract_revision or "?")[:12]),
                    reason="lease_not_on_base",
                )
            raise GateError(
                "contract %r points at %r, which does not exist at %s"
                % (contract_path, target_path, (contract_revision or "?")[:12])
            )
        if not isinstance(contract, dict):
            raise GateError("contract %r is not a JSON object" % target_path)
        return contract, target_path, lease_doc, contract_revision, notice

    if isinstance(pointer, dict):
        return pointer, contract_path, lease_doc, contract_revision, notice

    return lease_doc, contract_path, lease_doc, contract_revision, notice



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


def _exempt_path(path, exempt):
    """Lease metadata is never enforced: the gate reads leases from base only."""
    return path in exempt or path == LEASE_DIR or path.startswith(LEASE_DIR + "/")


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
            if _exempt_path(path, exempt):
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
        # A merge of the base (or of a commit already on the base) is not an
        # authored change: the content it brings is the base branch's history.
        # It is clean when that content touches nothing leased or frozen. A
        # frozen test it brings in still fails, because a frozen test may never
        # change in the pull request's history. An evil conflict resolution
        # differs from both parents, so the intersection of the two diffs is
        # inspected as well.
        if any(_is_ancestor(repo, parent, target.base) for parent in parents):
            if len(parents) < 2:
                continue
            brought = {_normalise(path) for path in _diff_paths(repo, sha, parents[0])}
            right = {_normalise(path) for path in _diff_paths(repo, sha, parents[1])}
            suspect = (brought & right) | {
                path for path in brought if _frozen_reason(path, scope)
            }
        else:
            suspect = {_normalise(path) for path in _diff_paths(repo, parents[0], sha)}

        for path in sorted(suspect):
            if _exempt_path(path, exempt):
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
    parser.add_argument("--contract-sha256", default=None)
    parser.add_argument(
        "--contract-store",
        action="append",
        default=None,
        help="frozen contract store directory (repeatable; also BUNDLEBEES_CONTRACT_STORE)",
    )
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

        contract, contract_path, lease_doc, _revision, notice = resolve_contract(
            args.repo,
            target,
            args.contract,
            args.contract_sha256,
            args.contract_store,
            environ,
        )
        if notice and not args.quiet:
            print(notice)
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
