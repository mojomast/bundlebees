"""Child processes used by the crash tests.

Two modes:

**legacy reconcile mode** (unchanged, used by ``test_reconcile_crash_process``)::

    python tests/crash_child.py <state_dir> <job_id> <phase> <journal.jsonl> <posts.jsonl>

Runs one reconciler tick against a disk-backed fake launcher and dies at a named
point with `os._exit`, so the parent test observes a *real* process termination
rather than a simulated exception.

Phases:
  before_post   die at the top of post_attempt, after recording the attempt
  after_post    record the attempt in the journal, then die before returning
  after_confirm run _dispatch_one (intent + in-memory transition) and die before
                the job is saved — the crash window between intent confirmation
                and persisted state
  multi         two tasks: dispatch the first, die during the second POST

**packet coordinator mode** (``python -m tests.crash_child packet <config.json>``)

A *real* coordinator process for the packet-native two-feature run: it builds the
production provisioning client (real hyvv client over HTTP), the production
launcher adapter over the standalone worker service, and calls
``queue.run_once`` in a loop. When ``BUNDLEBEES_CRASH_AT`` names a checkpoint, it
registers a hook that writes a durable marker JSON (checkpoint name, payload,
pid) to ``BUNDLEBEES_CRASH_MARKER`` and then ``os._exit(97)`` — no unwinding, no
flush, exactly like a power loss at that instant.

Config (JSON):

    {"state_dir", "queue_file", "worktree", "mirror", "repo_identity",
     "provision_url", "worker_url", "hyvv_src", "expect_host", "gitleaks",
     "feature_b", "suites": {"<job_id>": [argv...]}, "log", "max_ticks"}
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from bundlebees_plugin import job as jobs
from bundlebees_plugin import reconcile as rec

T0 = 1_800_000_000.0


def _append(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- #
# legacy reconcile mode
# --------------------------------------------------------------------------- #


class DiskLauncher:
    idempotent = True

    def __init__(self, phase: str, journal: Path, posts: Path) -> None:
        self.phase = phase
        self.journal = journal
        self.posts = posts
        self.calls = 0

    def _journal(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [
            json.loads(line)
            for line in self.journal.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def list_attempts(self) -> list[dict]:
        return self._journal()

    def get_attempt(self, attempt_id: str) -> dict | None:
        return next((entry for entry in self._journal() if entry["attempt_id"] == attempt_id), None)

    def post_attempt(self, brief: dict) -> dict:
        self.calls += 1
        _append(self.posts, {"phase": self.phase, "brief": dict(brief)})
        if self.phase == "before_post":
            os._exit(9)
        entry = {
            "attempt_id": f"att_{self.calls}",
            "state": "succeeded",
            "created_at": rec._iso(T0),
            "brief": dict(brief),
        }
        _append(self.journal, entry)
        if self.phase == "after_post":
            os._exit(9)
        if self.phase == "multi" and self.calls == 2:
            os._exit(9)
        return entry


def main(argv: list[str]) -> int:
    state, job_id, phase, journal, posts = argv[1:6]
    state_dir = Path(state)
    launcher = DiskLauncher(phase, Path(journal), Path(posts))

    if phase == "after_confirm":
        job = jobs.load_job(state_dir, job_id)
        task = job["tasks"][0]
        rec._dispatch_one(state_dir, job, task, rec._Adapter(launcher), T0, [])
        os._exit(9)

    rec.tick(state_dir, job_id, launcher=launcher, now=T0)
    return 0


# --------------------------------------------------------------------------- #
# packet coordinator mode
# --------------------------------------------------------------------------- #

CRASH_EXIT = 97


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_marker(path: Path, name: str, payload: dict, *, at: float | None = None) -> None:
    """Persist the crash marker before the process dies (write, fsync, rename)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"checkpoint": name, "payload": dict(payload), "pid": os.getpid(),
            "at": time.time() if at is None else at}
    data = json.dumps(body, sort_keys=True, default=str, indent=2).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _arm_crash_hook() -> str | None:
    """Register the configured crash checkpoint in *this* process, if any."""

    name = str(os.environ.get("BUNDLEBEES_CRASH_AT") or "").strip()
    if not name:
        return None
    marker_value = str(os.environ.get("BUNDLEBEES_CRASH_MARKER") or "").strip()
    if not marker_value:
        raise SystemExit("BUNDLEBEES_CRASH_AT is set but BUNDLEBEES_CRASH_MARKER is not")
    marker = Path(marker_value)

    from bundlebees_plugin import faults

    def _crash(**payload):
        _write_marker(marker, name, payload)
        os._exit(CRASH_EXIT)

    faults.register(name, _crash)
    return name


def _http_transport(base_url: str):
    """The real provisioning client's transport: plain HTTP, no retries."""

    import urllib.error
    import urllib.request

    root = base_url.rstrip("/")

    def call(method: str, path: str, body):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(root + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    return call


def packet_main(argv: list[str]) -> int:
    if len(argv) < 3:
        raise SystemExit("usage: crash_child.py packet <config.json>")
    config = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    state_dir = Path(config["state_dir"])
    log_path = Path(config["log"])

    armed = _arm_crash_hook()

    sys.path.insert(0, str(config["hyvv_src"]))
    import provisioning as hyvv  # noqa: F401  (the pinned phase-1 host)

    from bundlebees_plugin import job_cli, provisioning as P, queue
    from bundlebees_plugin.launcher_adapter import LauncherAdapter
    from tests.worker_service import WorkerServiceClient

    client = WorkerServiceClient(config["worker_url"])
    # Build the production adapter against the worker service and prove the
    # wiring here; job_cli.tick constructs the same adapter from this client.
    adapter = LauncherAdapter(client)
    adapter.list_attempts()

    provisioner = P.HyvvProvisioner(_http_transport(config["provision_url"]),
                                    expect_host=config.get("expect_host"))
    suites = {str(key): [str(part) for part in value]
              for key, value in (config.get("suites") or {}).items()}

    def tick(job_id: str):
        suite = suites.get(job_id)
        if suite is None:
            raise SystemExit(f"no suite command configured for {job_id}")
        return job_cli.tick(state_dir, job_id, client=client, repo=Path(config["worktree"]),
                            provisioner=provisioner, expect_host=config.get("expect_host"),
                            gitleaks=config.get("gitleaks"), suite_command=suite)

    backend = queue.JsonQueue(Path(config["queue_file"]))
    context = {"worktree": str(config["worktree"]), "mirror": str(config["mirror"]),
               "repo_identity": str(config["repo_identity"])}

    _append(log_path, {"event": "start", "pid": os.getpid(), "armed": armed,
                       "faults": sorted(sys.modules["bundlebees_plugin.faults"].registered())
                       if armed else []})

    idle = 0
    for index in range(int(config.get("max_ticks") or 8)):
        _append(log_path, {"event": "run_once_begin", "index": index})
        result = queue.run_once(state_dir, backend, tick=tick, handoff_context=context)
        _append(log_path, {"event": "run_once", "result": result})
        if result.get("action") == "idle":
            idle += 1
            if idle >= 2:
                break
        else:
            idle = 0
        if result.get("action") in {"blocked", "failed", "needs_approval"}:
            break
    _append(log_path, {"event": "done", "pid": os.getpid(), "armed": armed})
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "packet":
        sys.exit(packet_main(sys.argv))
    sys.exit(main(sys.argv))
