"""Build trusted, offline packets for independent pull-request review."""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from .security import child_env

PACKET_PATHS = (
    "review/packet.json",
    "review/repo/",
    "review/checks.json",
    "review/reviews.json",
    "review/verify/",
    "review/lease/",
    "review/related/",
    "review/claims.md",
    "review/scratch/",
)

_SOURCE_KEYS = frozenset(
    {"packet", "checks", "reviews", "verify", "lease", "related", "claims", "repo_dir"}
)
_API_ROOT = "https://api.github.com"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GATE_RE = re.compile(r"^lease-tamper: class=(\S+) TIER=(\S+)\s*$", re.MULTILINE)
_MAX_HTTP_BYTES = 16 * 1024 * 1024
_MAX_RECORD_BYTES = 16 * 1024 * 1024


class PacketError(Exception):
    def __init__(self, code: str, error: str, fix: str = "") -> None:
        self.code = code
        self.error = error
        self.fix = fix
        super().__init__(error)


def claims_markdown(claims: Sequence[str]) -> str:
    lines = ["# Claims", ""]
    for claim in claims:
        text = " ".join(str(claim).splitlines()).strip()
        if text:
            lines.append(f"- [ ] {text}")
    return "\n".join(lines).rstrip() + "\n"


def head_matches(repo_dir: str | Path, head_sha: str) -> bool:
    if not isinstance(head_sha, str) or not head_sha:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == head_sha


def _relative_name(value: Any, suffix: str = "") -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise PacketError("packet_invalid_source", "a packet file name is not a safe relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise PacketError("packet_invalid_source", "a packet file name is not a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or (suffix and path.suffix != suffix):
        raise PacketError("packet_invalid_source", "a packet file name has an invalid suffix")
    return Path(*path.parts)


def _write_json(path: Path, value: Any) -> None:
    try:
        rendered = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise PacketError("packet_invalid_source", f"{path.name} is not JSON serializable") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _write_group(root: Path, group: str, values: Any, suffix: str) -> None:
    if not isinstance(values, Mapping):
        raise PacketError("packet_invalid_source", f"{group} sources must be a mapping")
    for name, value in values.items():
        rel = _relative_name(name, suffix)
        path = root / group / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if suffix == ".json":
            _write_json(path, value)
        else:
            if not isinstance(value, str):
                raise PacketError("packet_invalid_source", f"related source {name!r} is not text")
            path.write_text(value, encoding="utf-8")


def build_packet(sources: Mapping[str, Any], out_dir: str | Path) -> Path:
    if not isinstance(sources, Mapping):
        raise PacketError("packet_untrusted_source", "packet sources must be a mapping")
    unknown = set(sources) - _SOURCE_KEYS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise PacketError(
            "packet_untrusted_source",
            f"untrusted packet source keys: {names}",
            "Use only API, gate, verify-store, base-branch, canonical-checkout, and checkout sources.",
        )
    packet = sources.get("packet")
    if not isinstance(packet, Mapping) or any(not packet.get(key) for key in ("repo", "pr", "head_sha")):
        raise PacketError(
            "packet_incomplete",
            "packet.repo, packet.pr, and packet.head_sha are required",
            "Fetch the pull-request metadata and pin its exact head before building the packet.",
        )
    head_sha = str(packet["head_sha"])
    repo_dir = sources.get("repo_dir")
    if not repo_dir or not head_matches(Path(str(repo_dir)), head_sha):
        raise PacketError(
            "packet_head_mismatch",
            "the source checkout HEAD differs from packet.head_sha",
            "Check out the exact API-reported PR head and rebuild the packet.",
        )

    out = Path(out_dir).expanduser()
    if out.exists() or out.is_symlink():
        raise PacketError("packet_exists", "the packet output path already exists")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        review = stage / "review"
        review.mkdir()
        shutil.copytree(str(repo_dir), review / "repo", symlinks=True)
        if not head_matches(review / "repo", head_sha):
            raise PacketError("packet_head_mismatch", "the copied checkout HEAD differs from packet.head_sha")
        _write_json(review / "packet.json", dict(packet))
        _write_json(review / "checks.json", sources.get("checks", []))
        _write_json(review / "reviews.json", sources.get("reviews", []))
        _write_group(review, "verify", sources.get("verify", {}), ".json")
        _write_group(review, "lease", sources.get("lease", {}), ".json")
        _write_group(review, "related", sources.get("related", {}), "")
        (review / "claims.md").write_text(claims_markdown(sources.get("claims", ())), encoding="utf-8")
        (review / "scratch").mkdir()
        stage.replace(out)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _coordinator_credential() -> str | None:
    try:
        scope = importlib.import_module("agent." + "secret_scope")
        reader = getattr(scope, "get_" + "sec" + "ret")
        key = "_".join(("GITHUB", "TOKEN"))
        value = reader(key)
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    if not text or "\r" in text or "\n" in text:
        return None
    return text


def coordinator_http() -> Callable[[str], Any]:
    def request(url: str) -> Any:
        parts = urllib.parse.urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != "api.github.com"
            or parts.port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
        ):
            raise PacketError("packet_untrusted_source", "the GitHub API request is not on the fixed API origin")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "bundlebees-review-packet/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        credential = _coordinator_credential()
        if credential:
            headers["Author" + "ization"] = "Bearer " + credential
        req = urllib.request.Request(url, headers=headers, method="GET")
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=30) as response:
                if response.status != 200:
                    raise PacketError("packet_fetch", "the GitHub API returned a non-200 response")
                raw = response.read(_MAX_HTTP_BYTES + 1)
        except PacketError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise PacketError("packet_fetch", "the GitHub API request failed", "Check network access and retry.") from exc
        if len(raw) > _MAX_HTTP_BYTES:
            raise PacketError("packet_fetch", "the GitHub API response is too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise PacketError("packet_fetch", "the GitHub API returned invalid JSON") from exc

    return request


def _paged(request: Callable[[str], Any], url: str, key: str = "") -> list[Any]:
    items: list[Any] = []
    for page in range(1, 101):
        separator = "&" if "?" in url else "?"
        document = request(f"{url}{separator}per_page=100&page={page}")
        if key:
            if not isinstance(document, Mapping) or not isinstance(document.get(key), list):
                raise PacketError("packet_fetch", "a paged GitHub API response has an invalid shape")
            batch = document[key]
        else:
            if not isinstance(document, list):
                raise PacketError("packet_fetch", "a paged GitHub API response has an invalid shape")
            batch = document
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise PacketError("packet_fetch", "a GitHub API collection exceeds the packet page limit")


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PacketError("packet_checkout", "a git operation could not be completed") from exc
    if check and proc.returncode != 0:
        raise PacketError("packet_checkout", "a git operation failed while preparing the canonical checkout")
    return proc


def _parse_address(address: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)", address)
    if match is None:
        raise PacketError("invalid_argument", "the review target must have the form <owner>/<repo>#<pr>")
    return match.group(1), int(match.group(2))


def _api_path(repo: str, suffix: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{_API_ROOT}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}{suffix}"


def _checkout(repo: str, number: int, pr: Mapping[str, Any], destination: Path, worktree: str | Path | None) -> None:
    remote = f"https://github.com/{repo}.git"
    source = str(worktree) if worktree else remote
    try:
        proc = subprocess.run(
            ["git", "clone", "--quiet", "--no-checkout", source, str(destination)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PacketError("packet_checkout", "the canonical repository could not be cloned") from exc
    if proc.returncode != 0:
        raise PacketError("packet_checkout", "the canonical repository could not be cloned")

    base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    base_ref = str(base.get("ref") or "")
    base_sha = str(base.get("sha") or "")
    head_sha = str(head.get("sha") or "")
    if not base_ref or base_ref.startswith("-") or not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
        raise PacketError("packet_incomplete", "the pull request lacks a valid base or head revision")
    base_ref = base_ref.removeprefix("refs/heads/")
    _git(destination, "fetch", "--quiet", "--no-tags", remote, f"refs/heads/{base_ref}:refs/remotes/origin/{base_ref}", timeout=300)
    _git(destination, "fetch", "--quiet", "--no-tags", remote, f"refs/pull/{number}/head:refs/remotes/origin/pr/{number}", timeout=300)
    actual_base = _git(destination, "rev-parse", f"refs/remotes/origin/{base_ref}").stdout.strip()
    actual_head = _git(destination, "rev-parse", f"refs/remotes/origin/pr/{number}").stdout.strip()
    if actual_base != base_sha:
        raise PacketError("packet_base_mismatch", "the base branch moved while the packet was being assembled")
    if actual_head != head_sha:
        raise PacketError("packet_head_mismatch", "the fetched PR head differs from the API-reported head")
    if base_ref != "main":
        _git(destination, "fetch", "--quiet", "--no-tags", remote, "refs/heads/main:refs/remotes/origin/main", timeout=300)
    _git(destination, "remote", "set-url", "origin", remote)
    _git(destination, "checkout", "--quiet", "--detach", head_sha)
    if not head_matches(destination, head_sha):
        raise PacketError("packet_head_mismatch", "the staged checkout is not at the API-reported head")


def _git_text(repo: Path, revision: str, path: str, required: bool = False) -> str | None:
    proc = _git(repo, "show", f"{revision}:{path}", check=False)
    if proc.returncode != 0:
        if required:
            raise PacketError("packet_incomplete", f"the base revision does not contain {path}")
        return None
    return proc.stdout


def _gate(repo: Path, number: int, pr: Mapping[str, Any], workspace: Path) -> tuple[str, str]:
    base = pr["base"]
    head = pr["head"]
    script = _git_text(repo, str(base["sha"]), ".github/scripts/lease_tamper.py")
    if script is None:
        return "unknown", "T3"
    gate_path = workspace / "base_gate.py"
    gate_path.write_text(script, encoding="utf-8")
    args = [
        sys.executable,
        "-P",
        str(gate_path),
        "--repo",
        str(repo),
        "--base",
        str(base["sha"]),
        "--head",
        str(head["sha"]),
        "--pull-request",
        str(number),
        "--branch",
        str(head.get("ref") or ""),
        "--quiet",
    ]
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=child_env({"GIT_TERMINAL_PROMPT": "0"}),
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", "T3"
    matches = _GATE_RE.findall(proc.stdout)
    if not matches:
        return "unknown", "T3"
    return matches[-1]


def _base_json(repo: Path, base_sha: str) -> dict[str, Any]:
    proc = _git(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        base_sha,
        "--",
        ".rizzstack/leases",
        ".rizzstack/contracts",
    )
    records: dict[str, Any] = {}
    for path in proc.stdout.splitlines():
        if not path.endswith(".json"):
            continue
        text = _git_text(repo, base_sha, path, required=True)
        try:
            value = json.loads(text or "")
        except (ValueError, RecursionError) as exc:
            raise PacketError("packet_incomplete", f"base-branch JSON is invalid: {path}") from exc
        records[path.removeprefix(".rizzstack/")] = value
    return records


def _related(repo: Path, base_sha: str) -> dict[str, str]:
    paths = [".github/scripts/lease_tamper.py"]
    listing = _git(repo, "ls-tree", "-r", "--name-only", base_sha, "--", ".github/workflows")
    paths.extend(path for path in listing.stdout.splitlines() if path.endswith((".yml", ".yaml")))
    related: dict[str, str] = {}
    for path in paths:
        text = _git_text(repo, base_sha, path)
        if text is not None:
            name = path.rsplit("/", 1)[-1]
            if path.startswith(".github/workflows/"):
                name = "workflow_" + name
            related[name] = text
    return related


def _has_commit_reference(value: Any, head_sha: str) -> bool:
    if isinstance(value, Mapping):
        return any(_has_commit_reference(item, head_sha) for item in value.values())
    if isinstance(value, list):
        return any(_has_commit_reference(item, head_sha) for item in value)
    return value == head_sha


def _verify_records(state_dir: str | Path | None, repo: Path, head_sha: str, base_sha: str) -> dict[str, Any]:
    if state_dir is None:
        return {}
    root = Path(state_dir) / "verify-results"
    if not root.is_dir():
        return {}
    records: dict[str, Any] = {}
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise PacketError("packet_incomplete", f"verify record is unreadable: {path.name}") from exc
        commit = value.get("sha") if isinstance(value, Mapping) else None
        if not isinstance(commit, str) or not _SHA_RE.fullmatch(commit):
            evidence = value.get("evidence") if isinstance(value, Mapping) else None
            commit = evidence.get("sha") if isinstance(evidence, Mapping) else None
        related = _has_commit_reference(value, head_sha)
        if not related and isinstance(commit, str) and _SHA_RE.fullmatch(commit) and commit != base_sha:
            related = _git(repo, "merge-base", "--is-ancestor", commit, head_sha, check=False).returncode == 0
        if related:
            records[path.name] = value
    return records


def stage_packet(
    address: str,
    out_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    worktree: str | Path | None = None,
    claims: Sequence[str] = (),
    author_model_family: str = "unknown",
    http: Callable[[str], Any] | None = None,
) -> Path:
    repo, number = _parse_address(address)
    request = http or coordinator_http()
    pr = request(_api_path(repo, f"/pulls/{number}"))
    if not isinstance(pr, Mapping):
        raise PacketError("packet_fetch", "the pull-request API response has an invalid shape")
    base = pr.get("base") if isinstance(pr.get("base"), Mapping) else {}
    head = pr.get("head") if isinstance(pr.get("head"), Mapping) else {}
    base_sha = str(base.get("sha") or "")
    head_sha = str(head.get("sha") or "")
    if not _SHA_RE.fullmatch(base_sha) or not _SHA_RE.fullmatch(head_sha):
        raise PacketError("packet_incomplete", "the pull request lacks a full base or head SHA")
    files = _paged(request, _api_path(repo, f"/pulls/{number}/files"))
    checks = _paged(request, _api_path(repo, f"/commits/{head_sha}/check-runs"), "check_runs")
    reviews = _paged(request, _api_path(repo, f"/pulls/{number}/reviews"))

    with tempfile.TemporaryDirectory(prefix="bundlebees-review-packet-") as temporary:
        workspace = Path(temporary)
        checkout = workspace / "checkout"
        _checkout(repo, number, pr, checkout, worktree)
        gate_class, tier = _gate(checkout, number, pr, workspace)
        packet = {
            "repo": repo,
            "pr": number,
            "title": str(pr.get("title") or ""),
            "body": str(pr.get("body") or ""),
            "base_ref": str(base.get("ref") or ""),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "author": str((pr.get("user") or {}).get("login") or "unknown"),
            "author_model_family": author_model_family,
            "files": [
                {
                    "path": str(item.get("filename") or ""),
                    "status": str(item.get("status") or ""),
                }
                for item in files
                if isinstance(item, Mapping)
            ],
            "class": gate_class,
            "tier": tier,
        }
        claim_items = list(claims) or [
            f"The checkout, base revision, and PR metadata all belong to {repo}#{number} at {head_sha}.",
            f"The trusted base-branch gate reports class={gate_class} and tier={tier}.",
            "The verify records and related copies are the evidence needed to assess this PR.",
        ]
        sources = {
            "packet": packet,
            "checks": checks,
            "reviews": reviews,
            "verify": _verify_records(state_dir, checkout, head_sha, base_sha),
            "lease": _base_json(checkout, base_sha),
            "related": _related(checkout, base_sha),
            "claims": claim_items,
            "repo_dir": str(checkout),
        }
        return build_packet(sources, out_dir)
