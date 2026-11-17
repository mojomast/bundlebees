"""Offline tests for the pluggable worker-adapter interface.

No network, no real tokens, no Docker, no SSH. Clients are constructed against
literal loopback addresses and never called, so nothing here reaches a socket.
The binary-payload round-trip tests use a temporary real git repository (and
the coordinator's own local verifier), which is exactly how the leased file set
stays honest: the payload the adapter emits is proven against ``git apply``.
"""

from __future__ import annotations

import hashlib
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import adapters, leases, sandbox, verify_local
from bundlebees_plugin.adapters import (
    ADAPTERS,
    CodexAdapter,
    OpenCodeAdapter,
    UnsupportedAdapter,
    WorkerAdapter,
    adapter_from_config,
    available_adapters,
    normalize_result,
)
from bundlebees_plugin.client import (
    LauncherClient,
    OrbitAdapterClient,
    client_from_config,
)

TOKEN = "test-token-value"


def _provider() -> str:
    return TOKEN


def test_default_adapter_is_the_same_client_type_as_client_from_config():
    adapter = adapter_from_config({}, token_provider=_provider)
    expected = client_from_config({}, token_provider=_provider)
    assert type(adapter) is type(expected)
    assert isinstance(adapter, OrbitAdapterClient)
    assert adapter.name == "orbit-loopback"
    assert adapter._token() == TOKEN


def test_launcher_adapter_matches_client_from_config():
    config = {"adapter": "launcher", "launcher_url": "http://127.0.0.1:9"}
    adapter = adapter_from_config(config, token_provider=_provider)
    expected = client_from_config(config, token_provider=_provider)
    assert type(adapter) is type(expected)
    assert isinstance(adapter, LauncherClient)
    assert adapter.name == "launcher"
    assert adapter._token() == TOKEN


@pytest.mark.parametrize("alias", ["direct", "http", "https"])
def test_launcher_aliases_resolve_to_the_launcher_client(alias: str):
    config = {"adapter": alias, "launcher_url": "http://127.0.0.1:9"}
    adapter = adapter_from_config(config, token_provider=_provider)
    assert isinstance(adapter, LauncherClient)
    assert adapter.name == "launcher"


def test_unknown_adapter_is_rejected_with_known_names():
    with pytest.raises(UnsupportedAdapter) as excinfo:
        adapter_from_config({"adapter": "telepathy"}, token_provider=_provider)
    exc = excinfo.value
    assert exc.code == "unsupported_adapter"
    for name in ("orbit-loopback", "launcher", "opencode", "codex"):
        assert name in exc.fix
    assert "telepathy" in exc.error


@pytest.mark.parametrize("adapter_cls, adapter_name", [(OpenCodeAdapter, "opencode"), (CodexAdapter, "codex")])
def test_stub_methods_raise_unsupported_with_code_and_fix(adapter_cls, adapter_name):
    adapter = adapter_cls()
    assert adapter.name == adapter_name

    calls = [
        lambda: adapter.health(),
        lambda: adapter.projects(),
        lambda: adapter.attempts(),
        lambda: adapter.create({"objective": "x"}, model="deepseek-v4.1-flash"),
        lambda: adapter.get("att-1"),
        lambda: adapter.events("att-1", cursor=0),
        lambda: adapter.result("att-1"),
        lambda: adapter.cancel("att-1"),
        lambda: adapter.review("att-1", accepted=True, note="ok"),
    ]
    for call in calls:
        with pytest.raises(UnsupportedAdapter) as excinfo:
            call()
        exc = excinfo.value
        assert exc.code == "unsupported_adapter"
        assert adapter_name in exc.error
        assert "docs/ADAPTERS.md" in exc.fix


def test_stub_create_requires_model_keyword():
    """The Protocol keeps ``model`` keyword-only; stubs must not drift from it."""
    signature = inspect.signature(OpenCodeAdapter.create)
    assert signature.parameters["model"].kind is inspect.Parameter.KEYWORD_ONLY


def test_normalize_result_drops_worker_metadata_and_keeps_the_contract():
    raw = {
        "attempt_id": "att-1",
        "patch": "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n",
        "base_rev": "f" * 40,
        "usage": {"tokens": 120, "cost_usd": 0.02, "extra": "dropped"},
        "state": "succeeded",
        "untrusted": False,
        "assigned_files": ["src/a.py"],
        "lease": {"files": ["src/a.py"]},
        "brief": {"objective": "do a thing"},
        "worker_claims": "trust me",
        "scope": "admin",
        "token": "secret-token-value",
    }
    out = normalize_result(raw)
    assert set(out) == {"attempt_id", "patch", "base_rev", "usage", "evidence", "state", "untrusted"}
    assert out["attempt_id"] == "att-1"
    assert out["patch"] == raw["patch"]
    assert out["base_rev"] == "f" * 40
    assert out["usage"] == {"tokens": 120, "cost_usd": 0.02}
    assert out["state"] == "succeeded"
    assert out["untrusted"] is True
    for dropped in ("assigned_files", "lease", "brief", "worker_claims", "scope", "token"):
        assert dropped not in out


def test_normalize_result_tolerates_missing_and_bad_fields():
    out = normalize_result(
        {
            "id": "att-2",
            "diff": "diff --git a/x b/x\n",
            "status": "running",
            "usage": {"tokens": True, "cost_usd": "free", "extra": 1},
        }
    )
    assert out == {
        "attempt_id": "att-2",
        "patch": "diff --git a/x b/x\n",
        "base_rev": None,
        "usage": {"tokens": None, "cost_usd": None},
        "evidence": {"checks": []},
        "state": "running",
        "untrusted": True,
    }


def test_normalize_result_empty_patch_is_none():
    out = normalize_result({"attempt_id": "att-3", "patch": "   ", "state": "succeeded"})
    assert out["patch"] is None
    assert out["base_rev"] is None
    assert out["usage"] == {"tokens": None, "cost_usd": None}


def test_available_adapters_marks_orbit_loopback_default():
    entries = available_adapters()
    by_name = {entry["name"]: entry for entry in entries}
    assert set(by_name) == {"orbit-loopback", "launcher", "opencode", "codex"}
    assert by_name["orbit-loopback"]["default"] is True
    assert by_name["orbit-loopback"]["implemented"] is True
    assert by_name["launcher"]["default"] is False
    assert by_name["launcher"]["implemented"] is True
    assert by_name["opencode"]["implemented"] is False
    assert by_name["codex"]["implemented"] is False
    assert sum(1 for entry in entries if entry["default"]) == 1


def test_adapters_mapping_exposes_the_documented_names():
    assert set(ADAPTERS) == {"orbit-loopback", "launcher", "opencode", "codex"}
    for factory in ADAPTERS.values():
        assert callable(factory)
    assert isinstance(
        ADAPTERS["orbit-loopback"]({}, _provider),
        OrbitAdapterClient,
    )
    assert isinstance(
        ADAPTERS["launcher"]({"launcher_url": "http://127.0.0.1:9"}, _provider),
        LauncherClient,
    )
    assert isinstance(ADAPTERS["opencode"]({}, _provider), OpenCodeAdapter)
    assert isinstance(ADAPTERS["codex"]({}, _provider), CodexAdapter)


def test_client_adapters_satisfy_the_protocol_methods():
    adapter = adapter_from_config({}, token_provider=_provider)
    for method in (
        "health",
        "projects",
        "attempts",
        "create",
        "get",
        "events",
        "result",
        "cancel",
        "review",
    ):
        assert callable(getattr(adapter, method))
    for name in ("model", "kanban_task_id"):
        assert (
            inspect.signature(adapter.create).parameters[name].kind
            is inspect.signature(WorkerAdapter.create).parameters[name].kind
        )
    for name in ("accepted", "note"):
        assert (
            inspect.signature(adapter.review).parameters[name].kind
            is inspect.signature(WorkerAdapter.review).parameters[name].kind
        )


def test_normalize_result_maps_orbit_launcher_shapes_in_the_adapter_layer():
    sha = "ab" * 20
    fake = "not" + "-a-real-" + "value"  # built at runtime so scanners see no literal
    raw = {
        "attempt_id": "att_1",
        "source": {"project": "p", "repo_rev": sha.upper()},
        "patch": {"diff": "diff --git a/x b/x\n"},
        "observed": {"usage": {"total_tokens": 900}, "assigned_files": ["evil.py"]},
        "checks": [{"name": "suite", "command": "pytest token=" + fake, "passed": True, "extra": 1}],
        "worker_claims": {"lease": ["evil.py"]},
    }
    out = normalize_result(raw)
    assert out["base_rev"] == sha
    assert out["patch"] == "diff --git a/x b/x\n"
    assert out["usage"]["tokens"] == 900
    assert out["evidence"]["checks"][0]["passed"] is True
    assert fake not in out["evidence"]["checks"][0]["command"]
    assert "worker_claims" not in out and "observed" not in out


def test_normalize_result_rejects_short_or_named_bases():
    assert normalize_result({"base_rev": "abc1234"})["base_rev"] is None
    assert normalize_result({"base_rev": "main"})["base_rev"] is None


def test_core_verifier_and_contracts_do_not_name_an_agent_or_launcher():
    """Agent-agnostic core: only adapters.py and client.py know runtimes."""
    import pathlib
    import re as _re

    root = pathlib.Path(__file__).resolve().parents[1] / "bundlebees_plugin"
    banned = _re.compile(r"(?i)deepseek|opencode|codex|orbit|repo_rev|local-workers")
    for name in ("brief.py", "leases.py", "revision.py", "sandbox.py", "metrics.py", "schemas/brief.schema.json"):
        text = (root / name).read_text(encoding="utf-8")
        assert not banned.search(text), name


# ---------------------------------------------------------------------------
# Binary payload round trip (campaign record SS163)
#
# A lease diff that touches a binary file (a rebuilt artifact such as
# orbit-source.tar.gz) arrives from the worker as a plain unified diff whose
# binary section carries no content ("Binary files a/x and b/x differ"). The
# adapter layer is where the emitted payload must gain the lossless binary
# hunks: Git-binary-patch style sections under an explicit envelope marker, so
# the verifier's ``git apply`` reconstructs the file byte-identically.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True, text: bool = True, inp: str | bytes | None = None):
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(repo),
        capture_output=True,
        text=text,
        input=inp,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def _sandboxed(workdir, argv):
    """The repo's documented host escape hatch, as in tests/test_verify_local."""
    return sandbox.run_in_sandbox(workdir, list(argv), allow_unsafe_local=True, timeout=120)


def _binary_blob(seed: int, size: int = 3072) -> bytes:
    """Deterministic pseudo-random binary content (a small rebuilt artifact)."""
    out = bytearray()
    x = seed & ((1 << 64) - 1)
    while len(out) < size:
        x = (x * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        out += x.to_bytes(8, "big")
    return bytes(out[:size])


def _blob_id(content: bytes, hex_len: int) -> str:
    name = "sha1" if hex_len == 40 else "sha256"
    return hashlib.new(name, b"blob " + str(len(content)).encode() + b"\x00" + content).hexdigest()


def _make_binary_repo(tmp_path: Path, *, similar: bool = False, gone_test: bool = False) -> dict:
    """A repo whose source_rev tracks a small binary artifact; a temp branch
    rebuilds it (changed bytes). Returns the fixture the round trip needs."""
    repo = tmp_path / "repo"
    (repo / "assets").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_artifact.py").write_text(
        "from pathlib import Path\n\n"
        "def test_artifact_is_v2():\n"
        "    assert b'ARTIFACT-v2' in Path('assets/orbit-source.tar.gz').read_bytes()\n"
    )
    if gone_test:
        (repo / "tests" / "test_artifact_gone.py").write_text(
            "from pathlib import Path\n\n"
            "def test_artifact_is_gone():\n"
            "    assert not Path('assets/orbit-source.tar.gz').exists()\n"
        )
    old = b"ARTIFACT-v1\x00" + _binary_blob(1)[12:]
    (repo / "assets" / "orbit-source.tar.gz").write_bytes(old)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "bee/binary-rebuild", base)
    if similar:
        # a small in-place edit so git prefers delta binary sections
        new = b"ARTIFACT-v2\x00" + old[12 : 12 + 128] + b"\xff" * 64 + old[12 + 128 :]
    else:
        new = b"ARTIFACT-v2\x00" + _binary_blob(2)[12:]
    (repo / "assets" / "orbit-source.tar.gz").write_bytes(new)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rebuild orbit-source.tar.gz")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = "assets/orbit-source.tar.gz"
    old_id = _git(repo, "rev-parse", f"{base}:{path}").stdout.strip()
    new_id = _git(repo, "rev-parse", f"{head}:{path}").stdout.strip()
    plain_diff = _git(repo, "diff", f"{base}..{head}").stdout
    binary_diff = _git(repo, "diff", "--binary", f"{base}..{head}").stdout
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "path": path,
        "old": old,
        "new": new,
        "old_id": old_id,
        "new_id": new_id,
        "plain_diff": plain_diff,
        "binary_diff": binary_diff,
    }


def _carrier_modify(fixt: dict) -> dict:
    """A worker reply whose patch object carries the rebuilt file's bytes."""
    import base64

    return {
        "attempt_id": "att_binary_1",
        "patch": {
            "diff": fixt["plain_diff"],
            "binary": [
                {
                    "path": fixt["path"],
                    "from": fixt["old_id"],
                    "to": fixt["new_id"],
                    "data": base64.b64encode(fixt["new"]).decode("ascii"),
                    "old_data": base64.b64encode(fixt["old"]).decode("ascii"),
                }
            ],
        },
        "base_rev": fixt["base"],
        "state": "succeeded",
    }


def _lease(state: Path, fixt: dict, files: list[str]) -> None:
    leases.record_lease(
        state,
        "att_binary_1",
        {
            "objective": "rebuild the orbit source artifact",
            "source_rev": fixt["base"],
            "assigned_files": files,
            "acceptance": {"criteria": ["artifact is v2"], "tests": ["tests/test_artifact.py"]},
        },
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_text_only_payloads_are_unchanged_by_the_binary_envelope():
    """A text-only payload passes through byte-for-byte, with no envelope marker."""
    text_diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    out = normalize_result({"attempt_id": "att-t", "patch": text_diff, "state": "succeeded"})
    assert out["patch"] == text_diff
    assert adapters.BINARY_PAYLOAD_MARKER not in out["patch"]
    mapped = normalize_result(
        {"attempt_id": "att-t", "patch": {"diff": text_diff}, "state": "succeeded"}
    )
    assert mapped["patch"] == text_diff
    assert adapters.BINARY_PAYLOAD_MARKER not in mapped["patch"]


def test_binary_carrier_rebuilds_the_file_through_the_real_verify_path(tmp_path):
    """The emitted payload carries the binary change losslessly and the real
    verify path (lease + git apply + sandboxed tests + local commit) accepts
    it, reproducing the file byte-identically (sha256 both sides)."""
    fixt = _make_binary_repo(tmp_path)
    out = normalize_result(_carrier_modify(fixt))
    payload = out["patch"]

    state = tmp_path / "state"
    _lease(state, fixt, [fixt["path"]])
    record = verify_local.verify_and_commit(
        state,
        fixt["repo"],
        "att_binary_1",
        patch=payload,
        declared_files=[fixt["path"]],
        base_rev=fixt["base"],
        tests=["tests/test_artifact.py"],
        suite_command=[sys.executable, "-m", "pytest", "-q"],
        require_gitleaks=False,
        runner=_sandboxed,
    )
    assert record["ok"] is True

    assert payload.startswith(adapters.BINARY_PAYLOAD_MARKER + "\n")
    assert "GIT binary patch" in payload
    assert "Binary files a/" not in payload  # no content-free placeholder survives
    committed = _git(
        fixt["repo"], "cat-file", "blob", f"{record['sha']}:{fixt['path']}", text=False
    ).stdout
    assert _sha256(committed) == _sha256(fixt["new"]), (
        f"reconstructed { _sha256(committed) } != source { _sha256(fixt['new']) }"
    )


def test_binary_round_trip_reconstruction_is_byte_identical(tmp_path):
    """The payload's binary hunks reconstruct the rebuilt file exactly."""
    fixt = _make_binary_repo(tmp_path)
    out = normalize_result(_carrier_modify(fixt))
    assert set(out) == {"attempt_id", "patch", "base_rev", "usage", "evidence", "state", "untrusted"}
    payload = out["patch"]

    rebuilt = adapters.reconstruct_binary_files(payload)[fixt["path"]]
    assert _sha256(rebuilt) == _sha256(fixt["new"]), (
        f"reconstructed sha256 { _sha256(rebuilt) } != source sha256 { _sha256(fixt['new']) }"
    )


def test_two_binary_entries_reconstruct_both_files_byte_identically(tmp_path):
    """A lease touching two binary files carries and rebuilds both, in order."""
    repo = tmp_path / "repo"
    (repo / "assets").mkdir(parents=True)
    first_old, second_old = _binary_blob(11), _binary_blob(22)
    (repo / "assets" / "orbit-source.tar.gz").write_bytes(first_old)
    (repo / "assets" / "orbit-fonts.tar.gz").write_bytes(second_old)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "bee/two-binaries", base)
    first_new, second_new = _binary_blob(33), _binary_blob(44)
    (repo / "assets" / "orbit-source.tar.gz").write_bytes(first_new)
    (repo / "assets" / "orbit-fonts.tar.gz").write_bytes(second_new)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rebuild both artifacts")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    import base64

    def entry(name: str, old: bytes, new: bytes) -> dict:
        return {
            "path": f"assets/{name}",
            "from": _git(repo, "rev-parse", f"{base}:assets/{name}").stdout.strip(),
            "to": _git(repo, "rev-parse", f"{head}:assets/{name}").stdout.strip(),
            "data": base64.b64encode(new).decode("ascii"),
            "old_data": base64.b64encode(old).decode("ascii"),
        }

    out = normalize_result(
        {
            "attempt_id": "att_two",
            "patch": {
                "diff": _git(repo, "diff", f"{base}..{head}").stdout,
                "binary": [
                    entry("orbit-source.tar.gz", first_old, first_new),
                    entry("orbit-fonts.tar.gz", second_old, second_new),
                ],
            },
            "base_rev": base,
            "state": "succeeded",
        }
    )
    payload = out["patch"]
    assert payload.count("GIT binary patch") == 2
    rebuilt = adapters.reconstruct_binary_files(payload)
    assert _sha256(rebuilt["assets/orbit-source.tar.gz"]) == _sha256(first_new)
    assert _sha256(rebuilt["assets/orbit-fonts.tar.gz"]) == _sha256(second_new)


def test_worker_binary_diff_passthrough_verifies_end_to_end(tmp_path):
    """A worker that emitted ``git diff --binary`` keeps its hunks: the payload
    is validated, marked, verified by the real path, and reconstructs exactly
    (delta sections included, given the preimage from source_rev)."""
    fixt = _make_binary_repo(tmp_path, similar=True)
    out = normalize_result(
        {
            "attempt_id": "att_binary_2",
            "patch": fixt["binary_diff"],
            "base_rev": fixt["base"],
            "state": "succeeded",
        }
    )
    payload = out["patch"]

    state = tmp_path / "state"
    leases.record_lease(
        state,
        "att_binary_2",
        {
            "objective": "rebuild the orbit source artifact",
            "source_rev": fixt["base"],
            "assigned_files": [fixt["path"]],
            "acceptance": {"criteria": ["artifact is v2"], "tests": ["tests/test_artifact.py"]},
        },
    )
    record = verify_local.verify_and_commit(
        state,
        fixt["repo"],
        "att_binary_2",
        patch=payload,
        declared_files=[fixt["path"]],
        base_rev=fixt["base"],
        tests=["tests/test_artifact.py"],
        suite_command=[sys.executable, "-m", "pytest", "-q"],
        require_gitleaks=False,
        runner=_sandboxed,
    )
    assert record["ok"] is True
    assert payload.startswith(adapters.BINARY_PAYLOAD_MARKER + "\n")

    rebuilt = adapters.reconstruct_binary_files(payload, bases={fixt["path"]: fixt["old"]})
    assert _sha256(rebuilt[fixt["path"]]) == _sha256(fixt["new"]), (
        f"reconstructed sha256 { _sha256(rebuilt[fixt['path']]) } != source sha256 { _sha256(fixt['new']) }"
    )


def test_binary_deletion_carrier_round_trips_through_verify(tmp_path):
    """A binary deletion carried by the payload verifies and deletes exactly."""
    fixt = _make_binary_repo(tmp_path, gone_test=True)
    import base64

    _git(fixt["repo"], "checkout", "-q", "-b", "bee/binary-delete", fixt["base"])
    _git(fixt["repo"], "rm", "-q", fixt["path"])
    _git(fixt["repo"], "commit", "-q", "-m", "drop the artifact")
    head = _git(fixt["repo"], "rev-parse", "HEAD").stdout.strip()
    plain_delete = _git(fixt["repo"], "diff", f"{fixt['base']}..{head}").stdout
    zeros = "0" * len(fixt["new_id"])
    out = normalize_result(
        {
            "attempt_id": "att_binary_1",
            "patch": {
                "diff": plain_delete,
                "binary": [
                    {
                        "path": fixt["path"],
                        "from": fixt["old_id"],
                        "to": zeros,
                        "data": "",
                        "old_data": base64.b64encode(fixt["old"]).decode("ascii"),
                    }
                ],
            },
            "base_rev": fixt["base"],
            "state": "succeeded",
        }
    )
    payload = out["patch"]
    state = tmp_path / "state"
    _lease(state, fixt, [fixt["path"]])
    record = verify_local.verify_and_commit(
        state,
        fixt["repo"],
        "att_binary_1",
        patch=payload,
        declared_files=[fixt["path"]],
        base_rev=fixt["base"],
        tests=["tests/test_artifact_gone.py"],
        suite_command=[sys.executable, "-m", "pytest", "-q", "tests/test_artifact_gone.py"],
        require_gitleaks=False,
        runner=_sandboxed,
    )
    assert record["ok"] is True
    probe = _git(fixt["repo"], "cat-file", "-e", f"{record['sha']}:{fixt['path']}", check=False)
    assert probe.returncode != 0  # the file is gone in the verified commit


def test_binary_addition_carrier_round_trips_through_verify(tmp_path):
    """A brand-new binary artifact carried by the payload verifies and lands
    byte-identically (sha256 both sides)."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_added.py").write_text(
        "from pathlib import Path\n\n"
        "def test_new_artifact_is_v2():\n"
        "    assert b'ARTIFACT-v2' in Path('assets/new-artifact.tar.gz').read_bytes()\n"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "bee/new-artifact", base)
    new = b"ARTIFACT-v2\x00" + _binary_blob(9)[12:]
    (repo / "assets").mkdir()
    (repo / "assets" / "new-artifact.tar.gz").write_bytes(new)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add the artifact")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    new_id = _git(repo, "rev-parse", f"{head}:assets/new-artifact.tar.gz").stdout.strip()
    zeros = "0" * len(new_id)

    import base64

    out = normalize_result(
        {
            "attempt_id": "att_add",
            "patch": {
                "diff": _git(repo, "diff", f"{base}..{head}").stdout,
                "binary": [
                    {
                        "path": "assets/new-artifact.tar.gz",
                        "from": zeros,
                        "to": new_id,
                        "data": base64.b64encode(new).decode("ascii"),
                    }
                ],
            },
            "base_rev": base,
            "state": "succeeded",
        }
    )
    payload = out["patch"]
    state = tmp_path / "state"
    leases.record_lease(
        state,
        "att_add",
        {
            "objective": "ship the new artifact",
            "source_rev": base,
            "assigned_files": ["assets/new-artifact.tar.gz"],
            "acceptance": {"criteria": ["artifact is v2"], "tests": ["tests/test_added.py"]},
        },
    )
    record = verify_local.verify_and_commit(
        state,
        repo,
        "att_add",
        patch=payload,
        declared_files=["assets/new-artifact.tar.gz"],
        base_rev=base,
        tests=["tests/test_added.py"],
        suite_command=[sys.executable, "-m", "pytest", "-q"],
        require_gitleaks=False,
        runner=_sandboxed,
    )
    assert record["ok"] is True
    committed = _git(
        repo, "cat-file", "blob", f"{record['sha']}:assets/new-artifact.tar.gz", text=False
    ).stdout
    assert _sha256(committed) == _sha256(new), (
        f"reconstructed { _sha256(committed) } != source { _sha256(new) }"
    )


def test_content_free_binary_placeholder_is_refused_at_the_boundary():
    """The SS163 shape: a binary placeholder with no content must be refused
    loudly at the adapter boundary instead of failing later at git apply."""
    placeholder = (
        "diff --git a/assets/orbit-source.tar.gz b/assets/orbit-source.tar.gz\n"
        "index 948a286..5760890 100644\n"
        "Binary files a/assets/orbit-source.tar.gz and b/assets/orbit-source.tar.gz differ\n"
    )
    with pytest.raises(adapters.BinaryPayloadError) as excinfo:
        normalize_result({"attempt_id": "att-b", "patch": placeholder, "state": "succeeded"})
    exc = excinfo.value
    assert exc.code == "binary_payload_invalid"
    assert "content" in exc.error or "binary" in exc.error
    assert exc.fix


def test_truncated_binary_section_is_refused_loudly(tmp_path):
    """A payload whose binary section is cut short must not be emitted."""
    fixt = _make_binary_repo(tmp_path)
    lines = fixt["binary_diff"].splitlines()
    truncated = "\n".join(lines[: len(lines) - 4]) + "\n"
    with pytest.raises(adapters.BinaryPayloadError):
        normalize_result({"attempt_id": "att-b", "patch": truncated, "state": "succeeded"})


def test_unknown_binary_section_kind_is_refused_loudly(tmp_path):
    """A binary section whose encoding the contract does not know is refused."""
    fixt = _make_binary_repo(tmp_path, similar=True)
    unknown = fixt["binary_diff"].replace("delta ", "fancy ", 1)
    with pytest.raises(adapters.BinaryPayloadError):
        normalize_result({"attempt_id": "att-b", "patch": unknown, "state": "succeeded"})


def test_corrupt_base85_body_is_refused_loudly(tmp_path):
    """Flipped base85 data must fail closed, never emit a lossy payload."""
    fixt = _make_binary_repo(tmp_path)
    lines = fixt["binary_diff"].splitlines()
    for i, line in enumerate(lines):
        if line.startswith("literal"):
            data_line = i + 1
            break
    corrupted = list(lines)
    corrupted[data_line] = "!" + corrupted[data_line][1:]
    with pytest.raises(adapters.BinaryPayloadError):
        normalize_result({"attempt_id": "att-b", "patch": "\n".join(corrupted) + "\n", "state": "succeeded"})


def test_carrier_with_mismatched_blob_id_is_refused(tmp_path):
    """The carrier's bytes must hash to the blob id it declares."""
    fixt = _make_binary_repo(tmp_path)

    reply = _carrier_modify(fixt)
    reply["patch"]["binary"][0]["to"] = _blob_id(fixt["new"][:-1] + b"x", len(fixt["new_id"]))
    with pytest.raises(adapters.BinaryPayloadError):
        normalize_result(reply)


def test_marker_without_binary_sections_is_refused():
    """A payload that claims the binary envelope but carries no binary hunks
    is malformed, not merely text."""
    marked = adapters.BINARY_PAYLOAD_MARKER + "\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n"
    with pytest.raises(adapters.BinaryPayloadError):
        normalize_result({"attempt_id": "att-b", "patch": marked, "state": "succeeded"})
