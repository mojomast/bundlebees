#!/usr/bin/env python3
"""Owner-held gate resolver: fail when a pull request carries the owner-held label.

This is the single shared implementation. Every repo vendors a byte-identical
copy at ``.github/scripts/owner_held.py`` and runs it from the ``owner-held``
job that ``ci-gate`` needs, so the already-required ``ci-gate`` check enforces
the hold without any ruleset change.

Event handling (``GITHUB_EVENT_NAME``, else inferred from the payload):

``pull_request``
    Labels come straight from ``github.event.pull_request.labels``. No API call,
    so fork pull requests (read-only token) are covered too.

``pull_request_review``
    A submitted review re-runs the required checks, so it must re-evaluate the
    hold too: the payload carries the same ``pull_request`` object and the
    labels are read from it exactly as for ``pull_request``. Treating a review
    as "no pull request to hold" would let an ``owner-held`` pull request turn
    ``ci-gate`` green by submitting a review.

``merge_group``
    A queued merge commit has no labels in the payload, so the pull requests
    associated with ``github.event.merge_group.head_sha`` are resolved through
    ``GET /repos/{owner}/{repo}/commits/{sha}/pulls`` and their labels are read
    there. If that lookup cannot be completed the job fails closed (exit 1)
    rather than letting a held pull request through the queue. When GitHub
    returns no pull request for the synthetic merge commit, the queued pull
    request number embedded in ``github.event.merge_group.head_ref``
    (``refs/heads/gh-readonly-queue/<base>/pr-<number>-<sha>``) is used instead,
    so the queue never hangs on a missing check.

Any other event (``push`` to main, ``workflow_dispatch``) carries no pull
request to hold and exits 0, so ``ci-gate`` still passes on main.

Usage:
    python3 .github/scripts/owner_held.py [EVENT_JSON_PATH]

``EVENT_JSON_PATH`` defaults to ``$GITHUB_EVENT_PATH``. Environment:
``GITHUB_TOKEN`` (or ``GH_TOKEN``), ``GITHUB_REPOSITORY``, optional
``GITHUB_API_URL`` (defaults to https://api.github.com).

Exit status: 0 = no owner-held label, 1 = owner-held present or the merge-queue
lookup failed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOLD_LABEL = "owner-held"
DEFAULT_API_URL = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 20
MAX_PAGES = 10

# refs/heads/gh-readonly-queue/main/pr-1234-<sha>
QUEUE_REF = re.compile(r"/pr-(\d+)-")


class LookupError(RuntimeError):
    """The merge-queue pull request lookup could not be completed."""


def label_names(pull_request):
    """Return the label names on a pull_request-shaped object."""
    return [str((label or {}).get("name", "")) for label in (pull_request.get("labels") or [])]


def hold_findings(labels):
    """Return the labels that are the hold label (exact, case-insensitive)."""
    return sorted({name for name in labels if (name or "").strip().lower() == HOLD_LABEL})


def describe(kind, pull_request):
    """Return a log-friendly '<kind> #<number>' label for a pull request."""
    return f"{kind} #{pull_request.get('number') or '?'}"


def http_get_json(url, token):
    """GET ``url`` and return ``(payload, link_header)``.

    Isolated so tests can substitute a fixture-backed transport.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "owner-held-gate",
        },
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8")), response.headers.get("Link") or ""


def next_page_url(link_header):
    """Return the rel="next" URL from a Link header, or an empty string."""
    for part in (link_header or "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return ""


def pull_requests_for_commit(repo, sha, token, api_url, http):
    """Return the pull requests associated with ``sha`` (paginated)."""
    url = f"{api_url}/repos/{repo}/commits/{urllib.parse.quote(sha)}/pulls?per_page=100"
    pulls = []
    for _ in range(MAX_PAGES):
        payload, link = http(url, token)
        if not isinstance(payload, list):
            raise LookupError(f"{url} did not return a list of pull requests")
        pulls.extend(item for item in payload if isinstance(item, dict))
        url = next_page_url(link)
        if not url:
            return pulls
    raise LookupError(f"pull request pagination for {sha} did not terminate")


def pull_request_by_number(repo, number, token, api_url, http):
    """Return one pull request by number."""
    url = f"{api_url}/repos/{repo}/pulls/{int(number)}"
    payload, _ = http(url, token)
    if not isinstance(payload, dict):
        raise LookupError(f"{url} did not return a pull request")
    return payload


def queue_ref_number(head_ref):
    """Return the queued pull request number from a merge-queue head_ref."""
    match = QUEUE_REF.search(head_ref or "")
    return match.group(1) if match else ""


def pull_request_labels(payload):
    """Labels for a pull_request event, straight from the payload."""
    pull_request = payload.get("pull_request") or {}
    return [(describe("pull_request", pull_request), label_names(pull_request))]


def queued_pull_request(merge_group, repo, sha, token, api_url, http):
    """Resolve the queued pull request from the queue ref when the commit has none."""
    number = queue_ref_number(str(merge_group.get("head_ref") or ""))
    if not number:
        raise LookupError(f"no pull request is associated with merge group head {sha}")
    return pull_request_by_number(repo, number, token, api_url, http)


def merge_group_labels(payload, repo, token, api_url, http):
    """Labels for a merge_group event, resolved from the merge group head SHA."""
    merge_group = payload.get("merge_group") or {}
    sha = str(merge_group.get("head_sha") or "").strip()
    if not sha:
        raise LookupError("merge_group payload has no head_sha")
    if not repo:
        raise LookupError("GITHUB_REPOSITORY is not set; cannot resolve the queued pull request")
    if not token:
        raise LookupError("GITHUB_TOKEN is not set; cannot resolve the queued pull request")
    queued = pull_requests_for_commit(repo, sha, token, api_url, http)
    if not queued:
        queued = [queued_pull_request(merge_group, repo, sha, token, api_url, http)]
    return [(describe("merge_group", pull_request), label_names(pull_request)) for pull_request in queued]


def resolved_labels(payload, event_name, repo, token, api_url, http):
    """Return ``[(where, labels)]`` for the event, or ``[]`` when not applicable."""
    if event_name in ("pull_request", "pull_request_review"):
        return pull_request_labels(payload)
    if event_name == "merge_group":
        return merge_group_labels(payload, repo, token, api_url, http)
    return []


def infer_event_name(payload):
    """Best-effort event name when GITHUB_EVENT_NAME is not set."""
    if "merge_group" in payload:
        return "merge_group"
    # A pull_request_review payload carries both the review and the pull request.
    if "review" in payload and "pull_request" in payload:
        return "pull_request_review"
    if "pull_request" in payload:
        return "pull_request"
    return ""


def read_payload(path):
    """Load the event JSON payload, raising LookupError for the caller to report."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise LookupError(f"cannot read event payload {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise LookupError(f"event payload {path} is not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise LookupError(f"event payload {path} is not a JSON object")
    return payload


def report(resolved, event_name):
    """Print every label set and return the exit status for the gate."""
    if not resolved:
        print(f"owner-held: no pull request to hold for event={event_name or '<unknown>'}; pass")
        return 0
    held = []
    for where, labels in resolved:
        print(f"owner-held: {where} labels: {', '.join(sorted(labels)) or '<none>'}")
        held.extend(hold_findings(labels))
    if held:
        print(
            f"::error::owner-held: {HOLD_LABEL} is present; this pull request must not be merged "
            "or modified without a typed owner yes."
        )
        return 1
    print(f"owner-held: pass; no {HOLD_LABEL} label")
    return 0


def evaluate(payload, env, http):
    """Resolve the event's labels and return the exit status."""
    event_name = env.get("GITHUB_EVENT_NAME") or infer_event_name(payload)
    repo = (env.get("GITHUB_REPOSITORY") or "").strip()
    token = (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    api_url = (env.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")
    print(f"owner-held: event={event_name or '<unknown>'} repository={repo or '<unset>'}")
    try:
        resolved = resolved_labels(payload, event_name, repo, token, api_url, http)
    except (LookupError, OSError, ValueError) as error:
        # OSError covers urllib.error.URLError/HTTPError; ValueError covers bad JSON.
        print(f"::error::owner-held: fail closed: {error}", file=sys.stderr)
        return 1
    return report(resolved, event_name)


def main(argv=None, env=None, http=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    http = http or http_get_json

    if argv and argv[0] in ("-h", "--help"):
        print((__doc__ or "").strip())
        return 0

    event_path = argv[0] if argv else env.get("GITHUB_EVENT_PATH", "")
    if not event_path:
        print("owner-held: no event payload; pass a path or set GITHUB_EVENT_PATH", file=sys.stderr)
        return 1
    try:
        payload = read_payload(event_path)
    except LookupError as error:
        print(f"owner-held: {error}", file=sys.stderr)
        return 1
    return evaluate(payload, env, http)


if __name__ == "__main__":
    sys.exit(main())
