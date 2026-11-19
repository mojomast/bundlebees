"""The lease payload, proven in the real firejail jail.

A worker's verify suite consumes the pinned ``WORKFLOW_SRC`` / ``HYVV_SRC``
checkouts through environment variables. The coordinator's checkouts live in
the coordinator home, which the firejail wrapper hides: ``--private=<copy>``
mounts the disposable copy over the coordinator home and ``--private-tmp``
replaces ``/tmp`` and ``/var/tmp``. A payload addressed at its absolute host
path is therefore ENOENT inside the jail; serving must *stage* the payload
into the sandbox copy -- exactly like a staged interpreter or tiktoken cache
-- and name it relative to the child's cwd, which is always the copy.

These tests prove both halves of the behaviour contract against the REAL
wrapper built by ``sandbox.build_command`` with ``backend="firejail"`` --
never a simulated root:

* the host checkout path is invisible to the child, and
* the served payload is visible and usable by the child through the
  lease-payload mechanism implemented by ``dispatch.py`` and ``sandbox.py``.

Measured jail behaviour (the corrected diagnosis, measured on the verify host
with the exact ``build_command`` firejail argv): ``/tmp`` and ``/var/tmp`` are
fresh tmpfs whose host paths answer ENOENT inside; the coordinator home is
masked by the private home mount while ``$HOME`` stays the literal home path;
the child's cwd is the copy content; the copy's own host path answers ENOENT
inside; a fake checkout under a runtime-generated path in the coordinator home
is masked.

Environment disclosure: nested firejail on some hosts (including this suite's
own sandbox) *downgrades*. Firejail looks for kernel processes under
``/proc/1..10`` to decide it is not already sandboxed; in a container-shaped
PID namespace none are visible, so the wrapper runs its exact argv but applies
no sandboxing features (its own ``an existing sandbox was detected`` path).
The rehearsal therefore MEASURES the wrapper first -- a canary run through the
same real argv -- and asserts each masking fact against that measurement: on
a wrapper that applies its mask (the verify host) the full corrected diagnosis
is asserted, and a downgraded wrapper is disclosed by the skip in
``test_real_firejail_applies_its_mask_when_it_can`` rather than silently
assumed. The served half (relative staging, env transport, byte identity) is
asserted unconditionally in the jail because it does not depend on masking.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from bundlebees_plugin import dispatch, leases, sandbox

PAYLOAD_NAME = "WORKFLOW_SRC"
PAYLOAD_DIR = ".bundlebees-payload"
MARKER_TEXT = "# pinned recovery validator (lease-payload probe)\n"
SCHEMA_TEXT = '{"probe": "lease-payload"}\n'
HYVV_NAME = "HYVV_SRC"
HYVV_MARKER_TEXT = "# governed provisioning contract (lease-payload probe)\n"


def _no_backends(name: str) -> None:
    return None


def _no_probe(name: str) -> bool:
    return False


def _make_checkout(checkout: Path, *, workflow: bool = True) -> Path:
    """A fake pinned checkout with the pinned sources' marker files.

    ``workflow`` plants the workflow checkout's markers (``tools/recovery.py``,
    ``schemas/recovery.schema.json``); otherwise the hyvv checkout's
    ``provisioning.py``.
    """
    checkout.mkdir(parents=True, exist_ok=True)
    if workflow:
        (checkout / "tools").mkdir(parents=True, exist_ok=True)
        (checkout / "schemas").mkdir(parents=True, exist_ok=True)
        (checkout / "tools" / "recovery.py").write_text(MARKER_TEXT, encoding="utf-8")
        (checkout / "schemas" / "recovery.schema.json").write_text(SCHEMA_TEXT, encoding="utf-8")
    else:
        (checkout / "provisioning.py").write_text(HYVV_MARKER_TEXT, encoding="utf-8")
    return checkout


def _tree_digest(root: Path) -> str:
    """Mirror of ``dispatch._tree_digest`` for local expectations."""
    return dispatch.payload_digest({"WORKFLOW_SRC": str(root)})[PAYLOAD_NAME]


def _git(cwd: Path, *args: str) -> str:
    """Run git in *cwd* with a hermetic identity; return stripped stdout."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Payload Probe",
            "GIT_AUTHOR_EMAIL": "payload@example.invalid",
            "GIT_COMMITTER_NAME": "Payload Probe",
            "GIT_COMMITTER_EMAIL": "payload@example.invalid",
        }
    )
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr}"
    return done.stdout.strip()


def _make_git_checkout(root: Path, *, commits: int = 2) -> Path:
    """A real non-bare checkout with the workflow markers and *commits* commits."""
    _make_checkout(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first commit")
    for index in range(1, commits):
        (root / f"change-{index}.txt").write_text(f"change {index}\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"commit {index}")
    return root


def _tree_entries(root: Path) -> dict[str, tuple[str, object]]:
    """Map every served path to its bytes or symlink target, skipping ignored names."""
    entries: dict[str, tuple[str, object]] = {}

    def visit(directory: Path, prefix: str) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            if entry.name in sandbox.PAYLOAD_IGNORED_NAMES:
                continue
            path = Path(entry.path)
            relative = f"{prefix}{entry.name}"
            if entry.is_symlink():
                entries[relative] = ("link", os.readlink(path))
            elif entry.is_dir(follow_symlinks=False):
                visit(path, f"{relative}/")
            elif entry.is_file(follow_symlinks=False):
                entries[relative] = ("file", path.read_bytes())

    visit(root, "")
    return entries


def _files_containing(root: Path, needle: str) -> list[Path]:
    """Regular files under *root* (including ``.git``) whose bytes contain *needle*."""
    matches: list[Path] = []
    for directory, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            path = Path(directory) / filename
            if path.is_symlink() or not path.is_file():
                continue
            if needle.encode("utf-8") in path.read_bytes():
                matches.append(path)
    return matches


# ---------------------------------------------------------------------------
# Wrapper and payload plumbing (offline)
# ---------------------------------------------------------------------------


def test_build_command_firejail_keeps_the_measured_wrapper(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    home = tmp_path / "home"
    checkout = _make_checkout(tmp_path / "pinned")
    argv = ["/usr/bin/python3", "-m", "pytest", "-q"]

    cmd = sandbox.build_command("firejail", copy_dir, home, argv)

    assert cmd[:2] == ["firejail", "--quiet"]
    assert cmd[2:-len(argv)] == [
        "--noprofile",
        "--net=none",
        f"--private={copy_dir}",
        "--private-tmp",
        "--caps.drop=all",
        "--nonewprivs",
        "--seccomp",
        "--nogroups",
        "--nosound",
        "--no3d",
    ]
    assert cmd[-len(argv):] == argv
    joined = " ".join(cmd)
    assert str(checkout) not in joined
    assert str(checkout / "tools" / "recovery.py") not in joined
    assert "--private=" + str(copy_dir) in joined
    assert "--private-tmp" in joined


def test_collect_payload_reads_only_configured_existing_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = _make_checkout(tmp_path / "pinned")
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    monkeypatch.delenv("HYVV_SRC", raising=False)

    assert dispatch.collect_payload({}) == {}
    assert dispatch.collect_payload({"WORKFLOW_SRC": ""}) == {}
    assert dispatch.collect_payload({"WORKFLOW_SRC": "   "}) == {}
    assert dispatch.collect_payload({"WORKFLOW_SRC": str(checkout)}) == {
        "WORKFLOW_SRC": str(checkout)
    }

    hyvv = tmp_path / "hyvv"
    hyvv.mkdir()
    assert dispatch.collect_payload(
        {"WORKFLOW_SRC": str(checkout), "HYVV_SRC": str(hyvv)}
    ) == {"WORKFLOW_SRC": str(checkout), "HYVV_SRC": str(hyvv)}

    monkeypatch.setenv("WORKFLOW_SRC", str(checkout))
    monkeypatch.setenv("HYVV_SRC", str(hyvv))
    assert dispatch.collect_payload() == {
        "WORKFLOW_SRC": str(checkout),
        "HYVV_SRC": str(hyvv),
    }


def test_collect_payload_fails_closed_on_unusable_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing = tmp_path / "not-a-checkout"
    cases = [
        (str(missing), "payload_source_missing"),
        ("relative/checkouts", "payload_source_invalid"),
        ("with\x00nul", "payload_source_invalid"),
    ]
    for value, code in cases:
        with pytest.raises(dispatch.PayloadError) as excinfo:
            dispatch.collect_payload({"WORKFLOW_SRC": value})
        assert excinfo.value.code == code
        assert excinfo.value.fix

    monkeypatch.setenv("WORKFLOW_SRC", str(missing))
    with pytest.raises(dispatch.PayloadError) as excinfo:
        dispatch.collect_payload()
    assert excinfo.value.code == "payload_source_missing"
    assert "existing" in excinfo.value.error


def test_payload_digest_binds_the_served_bytes(tmp_path: Path):
    checkout = _make_checkout(tmp_path / "pinned")
    payload = {PAYLOAD_NAME: str(checkout)}

    first = dispatch.payload_digest(payload)
    assert first == dispatch.payload_digest(payload)
    assert set(first) == {PAYLOAD_NAME}

    (checkout / "tools" / "recovery.py").write_text(MARKER_TEXT + "\n", encoding="utf-8")
    changed = dispatch.payload_digest(payload)
    assert changed != first

    link = checkout / "tools" / "linked.py"
    link.symlink_to("recovery.py")
    with_link = dispatch.payload_digest(payload)
    assert with_link != changed
    link.unlink()
    assert dispatch.payload_digest(payload) == changed

    before_git = dispatch.payload_digest(payload)
    (checkout / ".git").mkdir()
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert dispatch.payload_digest(payload) == before_git


def test_stage_payload_stages_relative_and_leaves_history_out(tmp_path: Path):
    """The two-case contract: a real checkout is served as a single-commit repo."""

    # Case 1: a real non-bare checkout becomes a checkout at the same commit,
    # carrying no history, tags or remotes.
    checkout = _make_git_checkout(tmp_path / "pinned")
    head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "tag", "probe-tag")
    _git(checkout, "remote", "add", "origin", str(tmp_path / "nowhere"))
    link = checkout / "tools" / "linked.py"
    link.symlink_to("recovery.py")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    served = sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})

    assert served == {PAYLOAD_NAME: f"./{PAYLOAD_DIR}/{PAYLOAD_NAME}"}
    staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME
    assert (staged / "tools" / "recovery.py").read_text(encoding="utf-8") == MARKER_TEXT
    assert (staged / "tools" / "linked.py").is_symlink()
    assert os.readlink(staged / "tools" / "linked.py") == "recovery.py"
    assert (staged / ".git").is_dir()
    assert _git(staged, "rev-parse", "HEAD") == head
    assert _git(staged, "rev-list", "--count", "HEAD") == "1"
    assert _git(staged, "tag", "-l") == ""
    assert _git(staged, "remote") == ""
    assert _tree_digest(staged) == _tree_digest(checkout)

    assert sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)}) == served

    # Case 2: a source that is not a git checkout is still a plain copy.
    plain = _make_checkout(tmp_path / "plain")
    plain_link = plain / "tools" / "linked.py"
    plain_link.symlink_to("recovery.py")

    plain_served = sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(plain)})

    assert plain_served == {PAYLOAD_NAME: f"./{PAYLOAD_DIR}/{PAYLOAD_NAME}"}
    plain_staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME
    assert (plain_staged / "tools" / "recovery.py").read_text(encoding="utf-8") == MARKER_TEXT
    assert (plain_staged / "tools" / "linked.py").is_symlink()
    assert os.readlink(plain_staged / "tools" / "linked.py") == "recovery.py"
    assert not (plain_staged / ".git").exists()
    assert _tree_digest(plain_staged) == _tree_digest(plain)
    assert sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(plain)}) == plain_served


def test_stage_payload_serves_a_git_source_as_a_single_commit_checkout(tmp_path: Path):
    checkout = _make_git_checkout(tmp_path / "pinned")
    source_head = _git(checkout, "rev-parse", "HEAD")
    parent = _git(checkout, "rev-parse", "HEAD^")
    _git(checkout, "tag", "v-probe")
    _git(checkout, "remote", "add", "origin", str(tmp_path / "coordinator-remote"))
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})
    staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME

    staged_head = _git(staged, "rev-parse", "HEAD")
    assert staged_head == source_head
    assert len(staged_head) == 40
    assert all(character in "0123456789abcdef" for character in staged_head)
    assert _git(staged, "rev-list", "--count", "HEAD") == "1"

    parent_check = subprocess.run(
        ["git", "-C", str(staged), "cat-file", "-e", f"{parent}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    assert parent_check.returncode != 0, "the parent commit must not exist in the staged store"

    assert _git(staged, "tag", "-l") == ""
    assert _git(staged, "remote") == ""
    for line in _git(staged, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        _refname, objectname = line.split(" ", 1)
        assert objectname == staged_head, "a ref names a commit other than the checked-out HEAD"


def test_stage_payload_preserves_the_git_working_tree_and_symlinks(tmp_path: Path):
    checkout = _make_git_checkout(tmp_path / "pinned")
    (checkout / "tools" / "recovery.py").write_text(MARKER_TEXT + "# modified\n", encoding="utf-8")
    (checkout / "tools" / "untracked.txt").write_text("untracked payload\n", encoding="utf-8")
    (checkout / "tools" / "linked.py").symlink_to("recovery.py")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})
    staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME

    assert _tree_entries(staged) == _tree_entries(checkout)
    assert (staged / "tools" / "recovery.py").read_text(encoding="utf-8") == (
        MARKER_TEXT + "# modified\n"
    )
    assert (staged / "tools" / "untracked.txt").read_text(encoding="utf-8") == (
        "untracked payload\n"
    )
    assert (staged / "tools" / "linked.py").is_symlink()
    assert os.readlink(staged / "tools" / "linked.py") == "recovery.py"


def test_stage_payload_git_checkout_leaks_no_host_path(tmp_path: Path):
    checkout = _make_git_checkout(tmp_path / "pinned")
    _git(checkout, "tag", "v-probe")
    _git(checkout, "remote", "add", "origin", str(tmp_path / "coordinator-remote"))
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    before = dispatch.payload_digest({PAYLOAD_NAME: str(checkout)})
    sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})
    after = dispatch.payload_digest({PAYLOAD_NAME: str(checkout)})
    staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME

    assert before == after, "staging must not change the dispatched payload digest"
    assert _files_containing(staged, str(checkout)) == []
    config = (staged / ".git" / "config").read_text(encoding="utf-8")
    assert "[remote" not in config
    assert "url" not in config.lower()
    assert str(checkout) not in config


def test_stage_payload_non_git_source_is_a_plain_copy(tmp_path: Path):
    source = _make_checkout(tmp_path / "plain")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    served = sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(source)})
    staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME

    assert served == {PAYLOAD_NAME: f"./{PAYLOAD_DIR}/{PAYLOAD_NAME}"}
    assert not (staged / ".git").exists()
    assert _tree_entries(staged) == _tree_entries(source)
    assert _tree_digest(staged) == _tree_digest(source)


def test_stage_payload_refuses_a_bare_git_source(tmp_path: Path):
    work = _make_git_checkout(tmp_path / "work")
    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "--bare", "-q", str(work), str(bare))
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(bare)})

    assert excinfo.value.code == "payload_unservable"
    assert PAYLOAD_NAME in excinfo.value.error
    assert bare.name in excinfo.value.error
    assert excinfo.value.fix
    assert not (copy_dir / PAYLOAD_DIR / PAYLOAD_NAME).exists()


def test_stage_payload_refuses_when_git_checkout_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = _make_git_checkout(tmp_path / "pinned")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    real_run = sandbox.subprocess.run

    def failing_fetch(command, *args, **kwargs):
        if any(str(part) == "fetch" for part in command):
            raise OSError("simulated fetch failure")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(sandbox.subprocess, "run", failing_fetch)

    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})

    assert excinfo.value.code == "payload_unservable"
    assert PAYLOAD_NAME in excinfo.value.error
    assert checkout.name in excinfo.value.error
    assert excinfo.value.fix
    assert not (copy_dir / PAYLOAD_DIR / PAYLOAD_NAME).exists()


def test_stage_payload_is_idempotent_for_git_sources(tmp_path: Path):
    checkout = _make_git_checkout(tmp_path / "pinned")
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})
    staged = copy_dir / PAYLOAD_DIR / PAYLOAD_NAME
    head = _git(staged, "rev-parse", "HEAD")
    digest = _tree_digest(staged)
    (staged / "stale-from-first-stage.txt").write_text("stale\n", encoding="utf-8")

    sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(checkout)})

    assert _git(staged, "rev-parse", "HEAD") == head
    assert _tree_digest(staged) == digest
    assert not (staged / "stale-from-first-stage.txt").exists()
    assert _tree_entries(staged) == _tree_entries(checkout)


def test_stage_payload_fails_closed(tmp_path: Path):
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()

    with pytest.raises(ValueError):
        sandbox.stage_payload(copy_dir, {})
    with pytest.raises(ValueError):
        sandbox.stage_payload(copy_dir, {"not-an-env-name": str(tmp_path)})
    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.stage_payload(copy_dir, {PAYLOAD_NAME: str(tmp_path / "missing")})
    assert excinfo.value.code == "payload_unservable"
    assert not (copy_dir / PAYLOAD_DIR).exists()


def test_run_in_sandbox_serves_the_payload_relative_and_only_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = _make_checkout(tmp_path / "pinned")
    work = tmp_path / "work"
    work.mkdir()
    script = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "served = os.environ.get('WORKFLOW_SRC')\n"
        "marker = Path(served) / 'tools' / 'recovery.py' if served else None\n"
        "json.dump(\n"
        "    {\n"
        "        'served': served,\n"
        "        'marker': marker.read_text(encoding='utf-8') if marker else None,\n"
        "        'cwd': sorted(os.listdir('.')),\n"
        "    },\n"
        "    open(sys.argv[1], 'w', encoding='utf-8'),\n"
        ")\n"
    )

    result = sandbox.run_in_sandbox(
        work,
        [sys.executable, "-c", script, "{evidence}/collected.json"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
        payload={PAYLOAD_NAME: str(checkout)},
        collect_file="collected.json",
    )
    assert result["exit_code"] == 0
    collected = result["collected"]
    assert collected["served"] == f"./{PAYLOAD_DIR}/{PAYLOAD_NAME}"
    assert str(checkout) not in collected["served"]
    assert collected["marker"] == MARKER_TEXT
    assert PAYLOAD_DIR in collected["cwd"]


def test_run_in_sandbox_never_serves_the_host_path_by_env_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = _make_checkout(tmp_path / "pinned")
    monkeypatch.setenv("WORKFLOW_SRC", str(checkout))
    work = tmp_path / "work"
    work.mkdir()

    result = sandbox.run_in_sandbox(
        work,
        [sys.executable, "-c", "import os; print(os.environ.get('WORKFLOW_SRC', 'ABSENT'))"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
    )
    assert result["exit_code"] == 0
    assert "ABSENT" in result["output_tail"]
    assert str(checkout) not in result["output_tail"]


def test_run_in_sandbox_fails_closed_before_serving(tmp_path: Path):
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(sandbox.SandboxUnavailable) as excinfo:
        sandbox.run_in_sandbox(
            work,
            [sys.executable, "-c", "print('never runs')"],
            backend="auto",
            which=_no_backends,
            probe=_no_probe,
            payload={PAYLOAD_NAME: str(tmp_path / "pinned")},
        )
    assert excinfo.value.code == "sandbox_unavailable"


def test_lease_payload_binds_the_recorded_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = _make_checkout(tmp_path / "pinned")
    environ = {PAYLOAD_NAME: str(checkout)}
    digest = dispatch.payload_digest(environ)[PAYLOAD_NAME]

    state = tmp_path / "state"
    record = leases.record_lease(
        state,
        "attempt-payload-1",
        {
            "source_rev": "a" * 40,
            "assigned_files": ["src/tool.py"],
            "acceptance": ["the tool works"],
            "payload": {PAYLOAD_NAME: digest},
        },
    )

    served = dispatch.lease_payload(record, environ=environ)
    assert served == {PAYLOAD_NAME: str(checkout)}

    (checkout / "tools" / "recovery.py").write_text(MARKER_TEXT + "\n", encoding="utf-8")
    with pytest.raises(dispatch.PayloadError) as excinfo:
        dispatch.lease_payload(record, environ=environ)
    assert excinfo.value.code == "payload_lease_mismatch"

    (checkout / "tools" / "recovery.py").write_text(MARKER_TEXT, encoding="utf-8")
    del record["brief"]["payload"]
    assert dispatch.lease_payload(record, environ=environ) == served

    record["brief"]["payload"] = {"HYVV_SRC": digest}
    with pytest.raises(dispatch.PayloadError) as excinfo:
        dispatch.lease_payload(record, environ=environ)
    assert excinfo.value.code == "payload_lease_mismatch"


def test_serve_in_sandbox_forwards_the_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    checkout = _make_checkout(tmp_path / "pinned")
    work = tmp_path / "work"
    work.mkdir()

    result = dispatch.serve_in_sandbox(
        work,
        [sys.executable, "-c", "import os; print(os.environ['WORKFLOW_SRC'])"],
        backend="auto",
        allow_unsafe_local=True,
        which=_no_backends,
        probe=_no_probe,
        environ={PAYLOAD_NAME: str(checkout)},
    )
    assert result["exit_code"] == 0
    assert f"./{PAYLOAD_DIR}/{PAYLOAD_NAME}" in result["output_tail"]
    assert str(checkout) not in result["output_tail"]


# ---------------------------------------------------------------------------
# The real firejail jail (skipped when firejail is unusable on this host)
# ---------------------------------------------------------------------------


_CANARY_KEYS = (
    "HOME_FIXTURE_VISIBLE",
    "TMP_FIXTURE_VISIBLE",
    "COPY_ROOT_VISIBLE",
    "HOME_LITERAL",
    "CWD_COPY_MARKER",
)


def _write_canary(work: Path) -> str:
    (work / "canary-marker.txt").write_text("cwd-copy", encoding="utf-8")
    return (
        "import os, sys\n"
        "print('HOME_FIXTURE_VISIBLE=%d' % os.path.exists(sys.argv[1]))\n"
        "print('TMP_FIXTURE_VISIBLE=%d' % os.path.exists(sys.argv[2]))\n"
        "print('COPY_ROOT_VISIBLE=%d' % os.path.exists(sys.argv[3]))\n"
        "print('HOME_LITERAL=%d' % (os.environ['HOME'] == sys.argv[4]))\n"
        "print('CWD_COPY_MARKER=%d' % os.path.exists('./canary-marker.txt'))\n"
    )


def _parse_canary(tail: str) -> dict[str, bool]:
    measured: dict[str, bool] = {}
    for line in tail.splitlines():
        name, _, value = line.strip().partition("=")
        if name in _CANARY_KEYS and value in ("0", "1"):
            measured[name] = value == "1"
    missing = set(_CANARY_KEYS) - set(measured)
    if missing:
        pytest.fail(f"the canary could not measure the jail: missing {sorted(missing)}")
    return measured


def _preallocated_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pre-allocate the verify root so the child can name its host path inside."""
    root = Path(tempfile.mkdtemp(prefix="bundlebees-verify-"))
    monkeypatch.setattr(sandbox.tempfile, "mkdtemp", lambda *a, **k: str(root))
    return root


def _measure_jail(checkout: Path, tmp_fixture: Path, canary_work: Path) -> dict[str, bool]:
    script = _write_canary(canary_work)
    with pytest.MonkeyPatch.context() as monkeypatch:
        root = _preallocated_root(monkeypatch)
        try:
            result = sandbox.run_in_sandbox(
                canary_work,
                [
                    sys.executable,
                    "-c",
                    script,
                    str(checkout),
                    str(tmp_fixture),
                    str(root),
                    str(Path.home()),
                ],
                backend="firejail",
                python=sys.executable,
                timeout=300,
            )
        except sandbox.SandboxUnavailable as exc:
            if exc.code == "sandbox_interpreter_hidden":
                pytest.skip(
                    "the coordinator interpreter cannot be made visible in the "
                    f"jail: {exc.error} {exc.fix}"
                )
            raise
    if result["exit_code"] != 0:
        pytest.fail(f"the canary run failed: {result['output_tail']}")
    measured = _parse_canary(result["output_tail"])
    assert measured["CWD_COPY_MARKER"] is True
    return measured


class _Jail:
    def __init__(
        self,
        workflow: Path,
        hyvv: Path,
        tmp_fixture: Path,
        measured: dict[str, bool],
    ) -> None:
        self.checkouts = {PAYLOAD_NAME: workflow, HYVV_NAME: hyvv}
        self.tmp_fixture = tmp_fixture
        self.measured = measured

    @property
    def masked(self) -> bool:
        return not self.measured["TMP_FIXTURE_VISIBLE"]


@pytest.fixture(scope="module")
def jail(tmp_path_factory: pytest.TempPathFactory) -> _Jail:
    """Fake pinned checkouts under the coordinator home, measured in the real jail.

    The checkouts are planted under runtime-generated paths in the coordinator
    home because that is where pinned checkouts live in production and where
    the corrected diagnosis was measured; they are removed again in ``finally``.
    """
    if not sandbox.detect_backend("firejail"):
        pytest.skip(
            "firejail is not usable on this host; the lease-payload rehearsal "
            "must run the real firejail wrapper and cannot be simulated"
        )

    prefix = Path.home() / f"bb-payload-probe-{uuid.uuid4().hex}"
    workflow = _make_checkout(prefix / "workflow")
    hyvv = _make_checkout(prefix / "hyvv", workflow=False)
    tmp_fixture = Path(tempfile.mkdtemp(prefix="bb-payload-probe-tmp-"))
    (tmp_fixture / "marker.txt").write_text("tmp", encoding="utf-8")
    try:
        measured = _measure_jail(workflow, tmp_fixture, tmp_path_factory.mktemp("canary-work"))
        yield _Jail(workflow=workflow, hyvv=hyvv, tmp_fixture=tmp_fixture, measured=measured)
    finally:
        shutil.rmtree(prefix, ignore_errors=True)
        shutil.rmtree(tmp_fixture, ignore_errors=True)


def test_real_firejail_applies_its_mask_when_it_can(jail: _Jail):
    measured = jail.measured
    masked = jail.masked
    assert measured["HOME_FIXTURE_VISIBLE"] == (not masked)
    assert measured["COPY_ROOT_VISIBLE"] == (not masked)
    assert measured["HOME_LITERAL"] is True
    assert measured["CWD_COPY_MARKER"] is True
    if not masked:
        pytest.skip(
            "nested firejail on this host downgraded to no sandboxing (measured: "
            "the home fixture and the /tmp fixture are visible inside); the "
            "corrected diagnosis cannot hold where the wrapper applies no mask"
        )
    assert measured["TMP_FIXTURE_VISIBLE"] is False
    assert measured["HOME_FIXTURE_VISIBLE"] is False
    assert measured["COPY_ROOT_VISIBLE"] is False


_PROBE_TEMPLATE = '''\
import hashlib
import os
from pathlib import Path

HOST_CHECKOUTS = %(host_checkouts)r
COPY_ROOT = %(copy_root)r
HOME_ON_HOST = %(home_on_host)r
SERVED_EXPECTED = %(served_expected)r
TREE_DIGESTS = %(tree_digests)r
MARKERS = %(markers)r
EXPECTED_HOST_VISIBLE = %(expected_host_visible)r
EXPECTED_COPY_ROOT_VISIBLE = %(expected_copy_root_visible)r


def _tree_digest(root):
    digest = hashlib.sha256(b"bundlebees-lease-payload:v1\\x00")
    entries = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirnames)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            entries.append((path.relative_to(root).as_posix(), path))
    for rel, path in entries:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\\x00")
        try:
            if path.is_symlink():
                digest.update(b"link:")
                digest.update(os.readlink(path).encode("utf-8"))
            else:
                digest.update(b"file:")
                digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\\x00")
    return digest.hexdigest()


def test_host_checkouts_are_hidden_as_the_jail_masks():
    for name, host_path in HOST_CHECKOUTS.items():
        assert os.path.exists(host_path) == EXPECTED_HOST_VISIBLE, name
    if not EXPECTED_HOST_VISIBLE:
        assert os.path.isdir(HOME_ON_HOST)


def test_served_payloads_are_relative_and_usable():
    for name, served in SERVED_EXPECTED.items():
        assert os.environ[name] == served, name
        assert not os.path.isabs(served)
        assert HOST_CHECKOUTS[name] not in served
    workflow = Path(SERVED_EXPECTED["WORKFLOW_SRC"])
    assert (workflow / "tools" / "recovery.py").is_file()
    assert (workflow / "schemas" / "recovery.schema.json").is_file()
    assert (workflow / "tools" / "recovery.py").read_text(encoding="utf-8") == (
        MARKERS["WORKFLOW_SRC"]
    )
    hyvv = Path(SERVED_EXPECTED["HYVV_SRC"])
    assert (hyvv / "provisioning.py").read_text(encoding="utf-8") == MARKERS["HYVV_SRC"]


def test_served_payloads_match_the_dispatched_digests():
    for name, served in SERVED_EXPECTED.items():
        assert _tree_digest(Path(served)) == TREE_DIGESTS[name], name


def test_the_child_cwd_is_the_sandbox_copy():
    assert os.environ["HOME"] == HOME_ON_HOST
    assert Path("test_lease_payload_probe.py").is_file()
    assert os.path.exists(COPY_ROOT) == EXPECTED_COPY_ROOT_VISIBLE
'''


def test_real_firejail_serves_the_lease_payload_to_the_child(
    tmp_path: Path, jail: _Jail
):
    environ = {name: str(path) for name, path in jail.checkouts.items()}
    tree_digests = dispatch.payload_digest(environ)
    markers = {
        PAYLOAD_NAME: MARKER_TEXT,
        HYVV_NAME: HYVV_MARKER_TEXT,
    }
    work = tmp_path / "work"
    work.mkdir()

    with pytest.MonkeyPatch.context() as patch:
        copy_root = _preallocated_root(patch)
        probe_source = _PROBE_TEMPLATE % {
            "host_checkouts": environ,
            "copy_root": str(copy_root),
            "home_on_host": str(Path.home()),
            "served_expected": {
                name: f"./{PAYLOAD_DIR}/{name}" for name in jail.checkouts
            },
            "tree_digests": tree_digests,
            "markers": markers,
            "expected_host_visible": not jail.masked,
            "expected_copy_root_visible": not jail.masked,
        }
        (work / "test_lease_payload_probe.py").write_text(probe_source, encoding="utf-8")
        try:
            result = dispatch.serve_in_sandbox(
                work,
                sandbox.pytest_argv(["test_lease_payload_probe.py"]),
                backend="firejail",
                environ=environ,
                python=sys.executable,
                timeout=300,
            )
        except sandbox.SandboxUnavailable as exc:
            if exc.code == "sandbox_interpreter_hidden":
                pytest.skip(
                    "the coordinator interpreter cannot be made visible in the "
                    f"jail: {exc.error} {exc.fix}"
                )
            raise

    assert result["backend"] == "firejail"
    assert result["exit_code"] == 0
    assert not result["timed_out"]
    assert result["junit"] == {"tests": 4, "failures": 0, "errors": 0, "skipped": 0}
    assert "4 passed" in result["output_tail"]
