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


def resolved_labels(payload, event_name, repo, token, api_url, http):
    """Return ``[(where, labels)]`` for the event, or ``[]`` when not applicable."""
    if event_name == "pull_request":
        pull_request = payload.get("pull_request") or {}
        number = pull_request.get("number") or payload.get("number")
        where = f"pull_request #{number}" if number else "pull_request"
        return [(where, label_names(pull_request))]

    if event_name == "merge_group":
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
            number = queue_ref_number(str(merge_group.get("head_ref") or ""))
            if not number:
                raise LookupError(f"no pull request is associated with merge group head {sha}")
            queued = [pull_request_by_number(repo, number, token, api_url, http)]
        resolved = []
        for pull_request in queued:
            number = pull_request.get("number") or "?"
            resolved.append((f"merge_group #{number}", label_names(pull_request)))
        return resolved

    return []


def infer_event_name(payload):
    """Best-effort event name when GITHUB_EVENT_NAME is not set."""
    if "merge_group" in payload:
        return "merge_group"
    if "pull_request" in payload:
        return "pull_request"
    return ""


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
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except OSError as error:
        print(f"owner-held: cannot read event payload {event_path}: {error}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as error:
        print(f"owner-held: event payload {event_path} is not JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print(f"owner-held: event payload {event_path} is not a JSON object", file=sys.stderr)
        return 1

    event_name = env.get("GITHUB_EVENT_NAME") or infer_event_name(payload)
    repo = env.get("GITHUB_REPOSITORY", "").strip()
    token = (env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or "").strip()
    api_url = (env.get("GITHUB_API_URL") or DEFAULT_API_URL).rstrip("/")

    print(f"owner-held: event={event_name or '<unknown>'} repository={repo or '<unset>'}")

    try:
        resolved = resolved_labels(payload, event_name, repo, token, api_url, http)
    except (LookupError, OSError, ValueError) as error:
        # OSError covers urllib.error.URLError/HTTPError; ValueError covers bad JSON.
        print(f"::error::owner-held: fail closed: {error}", file=sys.stderr)
        return 1

    if not resolved:
        print(f"owner-held: no pull request to hold for event={event_name or '<unknown>'}; pass")
        return 0

    held = []
    for where, labels in resolved:
        held.extend(hold_findings(labels))
        print(f"owner-held: {where} labels: {', '.join(sorted(labels)) or '<none>'}")

    if held:
        print(
            f"::error::owner-held: {HOLD_LABEL} is present; this pull request must not be merged "
            "or modified without a typed owner yes."
        )
        return 1

    print(f"owner-held: pass; no {HOLD_LABEL} label")
    return 0


if __name__ == "__main__":
    sys.exit(main())
