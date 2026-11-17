"""Pluggable worker backends behind one narrow, auditable interface.

Threat model
------------
A worker runtime is *untrusted execution*. It receives a brief, runs an agent,
and reports back text and a diff. Nothing a worker says is authority: it cannot
grant itself a file lease, widen its scope, choose its own base revision, or
approve its own work. The trusted coordinator keeps the brief validation, the
immutable lease, the revision check, the isolated verification, and wave
integration regardless of which adapter is selected. Adapters only translate
the coordinator's calls into a runtime's transport and translate the runtime's
replies back into the result contract below.

Credentials never travel through an adapter. A launcher token stays with the
coordinator and is read through Hermes's secret scope by ``client.py``; a stub
adapter has no credential parameter at all. Adapter errors are scrubbed before
they can reach a log or a model-visible envelope.

Agent-agnostic contract
-----------------------
The core verifier and contracts never import or assume a specific agent,
model or launcher. Any runtime (DeepSeek bees through the Orbit/Hermes
launcher, OpenCode, Codex, a shell script) plugs in by implementing two calls:

* **Input** - ``create(brief, *, model, kanban_task_id)`` receives the
  coordinator-validated brief (``objective``, full ``source_rev``,
  ``assigned_files``, ``acceptance``, optional ``branch``/``tests``). The same
  fields are frozen in the coordinator's signed lease before verify runs.
* **Output** - ``result(attempt_id)`` returns whatever the runtime produced;
  :func:`normalize_result` reduces it to the result contract below
  (a patch plus worker-claimed evidence).

``WorkerBackend`` is that required minimum. ``WorkerAdapter`` adds optional
capabilities (health, projects, attempts, get, events, cancel, review) that
the Hermes tools use when present; a backend without them gets a clear
``unsupported_operation`` error from those tool ops, never a fake success.

Result contract
---------------
Every adapter result is reduced to exactly::

    {
        "attempt_id": str | None,
        "patch": str | None,       # unified diff against source_rev, or None
        "base_rev": str | None,    # full hex base the worker *declares*, not proof
        "usage": {"tokens": int | None, "cost_usd": float | None},
        "evidence": {"checks": [{"name", "command", "passed"}]},  # worker claims
        "state": str | None,
        "untrusted": True,
    }

Anything else the worker sent (``assigned_files``, ``lease``, ``brief``,
``scope``, ``worker_claims``, tokens, hosts) is dropped. A declared
``base_rev`` is re-checked by the coordinator against the immutable lease,
and worker ``evidence`` is informational only: the coordinator regenerates
trusted evidence by running the tests in its own sandbox. ``patch`` is not
scrubbed because a diff is data to be verified, not prose to display.
Runtime-specific result shapes (for example the Orbit launcher's
``source.repo_rev`` and ``observed.usage``) are mapped here and nowhere else.

Binary changes (SS163)
----------------------
A unified diff against ``source_rev`` cannot carry binary content: git
summarizes a changed binary file as ``Binary files a/x and b/x differ`` and
the verifier's ``git apply`` then has nothing to apply. The adapter layer is
where the emitted payload gains the change losslessly, in git's own
binary-patch wire format so the verifier reconstructs it without any new
trusted code:

* A worker that ran ``git diff --binary <source_rev>`` sends the diff text
  itself; ``normalize_result`` validates every ``GIT binary patch`` section
  (both halves, git base85, zlib, delta instructions) and marks the payload
  with the :data:`BINARY_PAYLOAD_MARKER` envelope line.
* A reply whose ``patch`` object carries ``binary`` entries
  (``{"path", "from", "to", "data", "old_data"}`` with the file's endpoints as
  base64 and full git blob ids) has its content-free placeholders replaced by
  git binary-patch sections built from those bytes. Blob ids are re-derived
  and checked before anything is emitted.

Either way the payload stays a plain diff string: it flows through the same
patch pinning, secret scan and ``git apply`` as any text patch, and
:func:`reconstruct_binary_files` reproduces the file bytes exactly. A payload
that cannot carry its binary changes — a content-free placeholder, a
truncated, corrupt or unknown binary section, a marker without hunks — is
refused loudly with :class:`BinaryPayloadError` instead of being emitted to
fail later inside the verifier. Text-only payloads pass through untouched.
"""

from __future__ import annotations

import base64
import hashlib
import re
import zlib
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .client import client_from_config
from .security import scrub

_ADAPTER_DOC = "docs/ADAPTERS.md"
_FULL_HEX_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_KNOWN_ADAPTERS = ("orbit-loopback", "launcher", "opencode", "codex")
_LAUNCHER_ALIASES = frozenset({"direct", "http", "https"})
_IMPLEMENTED = {
    "orbit-loopback": True,
    "launcher": True,
    "opencode": False,
    "codex": False,
}
_PATCH_KEYS = ("patch", "unified_diff", "diff")
_BASE_KEYS = ("base_rev", "base_sha", "base", "source_rev", "repo_rev")
_BASE_CONTAINERS = ("observed", "source")
_MAX_CHECKS = 50

#: Explicit envelope marker: a payload starting with this line carries binary
#: changes as git binary-patch sections that the verifier's ``git apply``
#: reconstructs. Text-only payloads never carry the marker.
BINARY_PAYLOAD_MARKER = "bundlebees-binary-payload/1"

_BINARY_HEADER = "GIT binary patch"
_PLACEHOLDER_RE = re.compile(r"^Binary files .+ differ$")
_DIFF_HEADER_RE = re.compile(r'^diff --git a/(.+) b/(.+)$')
_INDEX_LINE_RE = re.compile(r"^index (\S+)\.\.(\S+)(?: (\d{6}))?$")
_CHUNK_KINDS = ("literal", "delta")


class UnsupportedAdapter(Exception):
    """A requested adapter or operation has no implementation.

    Carries the stable ``unsupported_adapter`` code plus a scrubbed ``error``
    and ``fix`` so a tool envelope can be built without leaking worker text.
    """

    code = "unsupported_adapter"

    def __init__(self, error: str, fix: str | None = None) -> None:
        self.error = str(scrub(str(error)))
        self.fix = str(
            scrub(
                str(
                    fix
                    or (
                        "Choose the orbit-loopback or launcher adapter, or "
                        f"implement the WorkerAdapter contract in {_ADAPTER_DOC}."
                    )
                )
            )
        )
        super().__init__(self.error)

    def as_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "code": self.code,
            "fix": self.fix,
        }


class BinaryPayloadError(Exception):
    """A worker patch cannot carry its binary changes losslessly.

    Raised at the adapter boundary — before anything is emitted to the
    verifier — for a content-free binary placeholder, a truncated, corrupt or
    unknown ``GIT binary patch`` section, or an envelope marker without binary
    hunks. Carries the stable ``binary_payload_invalid`` code plus a scrubbed
    ``error`` and ``fix`` so a tool envelope can be built without leaking
    worker text. Fail closed: no lossy or half-carried payload is emitted.
    """

    code = "binary_payload_invalid"

    def __init__(self, error: str, fix: str | None = None) -> None:
        self.error = str(scrub(str(error)))
        self.fix = str(
            scrub(
                str(
                    fix
                    or (
                        "Have the runtime re-emit its diff with git binary "
                        "hunks (git diff --binary against source_rev), or "
                        "carry the file's endpoints in the patch object's "
                        "binary field."
                    )
                )
            )
        )
        super().__init__(self.error)

    def as_envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "code": self.code,
            "fix": self.fix,
        }


class WorkerBackend(Protocol):
    """The required minimum any agent runtime implements (input -> output)."""

    name: str

    def create(
        self,
        brief: dict[str, Any],
        *,
        model: str,
        kanban_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Start work on a validated brief; return at least ``attempt_id``."""
        ...

    def result(self, attempt_id: str) -> dict[str, Any]:
        """Return the runtime's raw output; reduced by :func:`normalize_result`."""
        ...


class WorkerAdapter(WorkerBackend, Protocol):
    """A backend plus the optional capabilities the Hermes tools can use."""

    def health(self) -> dict[str, Any]:
        ...

    def projects(self) -> list[dict[str, Any]]:
        ...

    def attempts(self) -> list[dict[str, Any]]:
        ...

    def get(self, attempt_id: str) -> dict[str, Any]:
        ...

    def events(
        self, attempt_id: str, cursor: int | None = None
    ) -> list[dict[str, Any]]:
        ...

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        ...

    def review(
        self,
        attempt_id: str,
        *,
        accepted: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        ...


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flatten(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Use one nested ``result``/``data`` mapping, keeping outer fallbacks."""
    for key in ("result", "data"):
        nested = raw.get(key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            for outer_key, outer_value in raw.items():
                merged.setdefault(outer_key, outer_value)
            return merged
    return raw


def _normalize_usage(raw: Mapping[str, Any]) -> dict[str, int | float | None]:
    usage: Any = raw.get("usage")
    if not isinstance(usage, Mapping):
        observed = raw.get("observed")
        usage = observed.get("usage") if isinstance(observed, Mapping) else None
    if not isinstance(usage, Mapping):
        usage = {}
    tokens = usage.get("tokens", usage.get("total_tokens"))
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        tokens = None
    cost = usage.get("cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None
    else:
        cost = float(cost)
    return {"tokens": tokens, "cost_usd": cost}


def _patch_text(value: Any) -> str | None:
    text = _optional_text(value)
    if text is not None:
        return text
    if isinstance(value, Mapping):  # e.g. {"diff": "..."} or {"patch.diff": "..."}
        for key in ("diff", "patch.diff", "unified_diff", "patch"):
            text = _optional_text(value.get(key))
            if text is not None:
                return text
    return None


def _declared_base(data: Mapping[str, Any]) -> str | None:
    for container in (data, *(data.get(key) for key in _BASE_CONTAINERS)):
        if not isinstance(container, Mapping):
            continue
        for key in _BASE_KEYS:
            value = container.get(key)
            if isinstance(value, str) and _FULL_HEX_RE.match(value.strip()):
                return value.strip().lower()
    return None


def _worker_checks(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = data.get("checks")
    if not isinstance(checks, list):
        return []
    out: list[dict[str, Any]] = []
    for item in checks[:_MAX_CHECKS]:
        if not isinstance(item, Mapping):
            continue
        out.append(
            {
                "name": str(scrub(str(item.get("name") or "")))[:200],
                "command": str(scrub(str(item.get("command") or "")))[:400],
                "passed": item.get("passed") is True,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Binary payload (SS163): lossless binary changes in git's own wire format
# ---------------------------------------------------------------------------

# git's base85 alphabet (RFC 1924). The encoder zero-pads the final partial
# 4-byte group, so a chunk of n bytes is always ceil(n / 4) * 5 characters.
_B85_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{|}~"
_B85_INDEX = {char: index for index, char in enumerate(_B85_ALPHABET)}


def _b85_encode_chunk(data: bytes) -> str:
    out = []
    for start in range(0, len(data), 4):
        group = data[start : start + 4].ljust(4, b"\x00")
        acc = int.from_bytes(group, "big")
        out.append("".join(_B85_ALPHABET[(acc // (85**power)) % 85] for power in (4, 3, 2, 1, 0)))
    return "".join(out)


def _b85_decode_chunk(line: str, count: int) -> bytes:
    body = line[1:]
    expected = ((count + 3) // 4) * 5
    if len(body) != expected:
        raise BinaryPayloadError(
            f"a binary patch data line has {len(body)} characters where {expected} are required"
        )
    out = bytearray()
    for start in range(0, len(body), 5):
        acc = 0
        for char in body[start : start + 5]:
            value = _B85_INDEX.get(char)
            if value is None:
                raise BinaryPayloadError("a binary patch data line contains a character outside git base85")
            acc = acc * 85 + value
        if acc > 0xFFFFFFFF:
            raise BinaryPayloadError("a binary patch data line encodes a value outside a 32-bit group")
        out += acc.to_bytes(4, "big")
    return bytes(out[:count])


def _inflate_capped(compressed: bytes, expected: int, what: str) -> bytes:
    """Inflate exactly ``expected`` bytes; refuse bombs, truncation, trailing data."""
    decomp = zlib.decompressobj()
    out = bytearray()
    data = compressed
    while data and not decomp.eof:
        budget = expected + 1 - len(out)
        if budget <= 0:
            raise BinaryPayloadError(f"{what} inflates beyond its declared size")
        out += decomp.decompress(data, budget)
        if len(out) > expected:
            raise BinaryPayloadError(f"{what} inflates beyond its declared size")
        data = decomp.unconsumed_tail
    if not decomp.eof:
        raise BinaryPayloadError(f"{what} ends inside an incomplete deflate stream")
    if decomp.unused_data:
        raise BinaryPayloadError(f"{what} carries trailing bytes after its deflate stream")
    out += decomp.flush()
    if len(out) != expected:
        raise BinaryPayloadError(f"{what} inflates to {len(out)} bytes where {expected} are declared")
    return bytes(out)


def _read_varint(stream: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(stream):
            raise BinaryPayloadError("a binary delta ends inside a size field")
        byte = stream[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _validate_delta_stream(stream: bytes) -> tuple[int, int]:
    """Walk a git delta instruction stream; return (source size, target size)."""
    source_size, pos = _read_varint(stream, 0)
    target_size, pos = _read_varint(stream, pos)
    produced = 0
    while pos < len(stream):
        op = stream[pos]
        pos += 1
        if op & 0x80:
            for bit in range(4):
                if op & (1 << bit):
                    if pos >= len(stream):
                        raise BinaryPayloadError("a binary delta ends inside a copy offset")
                    pos += 1
            size = 0
            for bit in range(3):
                if op & (1 << (4 + bit)):
                    if pos >= len(stream):
                        raise BinaryPayloadError("a binary delta ends inside a copy size")
                    size |= stream[pos] << (8 * bit)
                    pos += 1
            produced += size if size else 0x10000
        elif op:
            produced += op
            pos += op
            if pos > len(stream):
                raise BinaryPayloadError("a binary delta insert runs past its stream")
        else:
            raise BinaryPayloadError("a binary delta carries the reserved zero instruction")
        if produced > target_size:
            raise BinaryPayloadError("a binary delta claims more output than its target size")
    return source_size, target_size


def _apply_delta(source: bytes, stream: bytes) -> bytes:
    """Reconstruct the target of a git delta (gitformat-pack instructions)."""
    source_size, pos = _read_varint(stream, 0)
    target_size, pos = _read_varint(stream, pos)
    if source_size != len(source):
        raise BinaryPayloadError(
            f"a binary delta expects a {source_size}-byte preimage, not {len(source)} bytes"
        )
    out = bytearray()
    while pos < len(stream):
        op = stream[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if op & (1 << bit):
                    if pos >= len(stream):
                        raise BinaryPayloadError("a binary delta ends inside a copy offset")
                    offset |= stream[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (1 << (4 + bit)):
                    if pos >= len(stream):
                        raise BinaryPayloadError("a binary delta ends inside a copy size")
                    size |= stream[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            if offset + size > len(source):
                raise BinaryPayloadError("a binary delta copies past the end of its preimage")
            out += source[offset : offset + size]
        elif op:
            if pos + op > len(stream):
                raise BinaryPayloadError("a binary delta insert runs past its stream")
            out += stream[pos : pos + op]
            pos += op
        else:
            raise BinaryPayloadError("a binary delta carries the reserved zero instruction")
        if len(out) > target_size:
            raise BinaryPayloadError("a binary delta claims more output than its target size")
    if len(out) != target_size:
        raise BinaryPayloadError(f"a binary delta produced {len(out)} bytes where {target_size} are declared")
    return bytes(out)


def _blob_id(content: bytes, hex_len: int) -> str:
    name = "sha1" if hex_len == 40 else "sha256"
    return hashlib.new(name, b"blob " + str(len(content)).encode() + b"\x00" + content).hexdigest()


def _is_zero_id(hex_id: str) -> bool:
    return bool(hex_id) and set(hex_id) == {"0"}


def _safe_payload_path(path: Any) -> str:
    if not isinstance(path, str) or not path or path != path.strip():
        raise BinaryPayloadError("a binary entry names no usable path")
    if "\x00" in path or "\\" in path or path.endswith("/"):
        raise BinaryPayloadError(f"the binary entry path {path!r} is not a plain repository-relative path")
    parts = path.split("/")
    if path.startswith("/") or any(part in ("", ".", "..") for part in parts) or path == "/dev/null":
        raise BinaryPayloadError(f"the binary entry path {path!r} is not a plain repository-relative path")
    return path


def _scan_binary_blocks(patch: str) -> list[dict[str, Any]]:
    """Locate every ``diff --git`` block and its binary content, if any.

    Each block record carries the block's line span, its target path, its
    ``index`` line, whether it announces a new or deleted file, the position of
    a content-free ``Binary files ... differ`` placeholder, and every
    ``GIT binary patch`` section (kind, declared sizes, decoded deflate bytes).
    Parsing is strict: anything that is not exactly git's own binary-patch
    shape raises :class:`BinaryPayloadError` so a half-carried payload can
    never be emitted.
    """
    lines = patch.splitlines(keepends=True)
    blocks: list[dict[str, Any]] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index][:-1] if lines[index].endswith("\n") else lines[index]
        if not line.startswith("diff --git "):
            index += 1
            continue
        block: dict[str, Any] = {
            "path": None,
            "start": index,
            "index_line": None,
            "index_from": None,
            "index_to": None,
            "index_mode": None,
            "new_file": False,
            "deleted_file": False,
            "placeholder": None,
            "sections": [],
            "lines": lines,
        }
        match = _DIFF_HEADER_RE.match(line)
        if match:
            block["path"] = match.group(2)
        index += 1
        while index < total:
            line = lines[index][:-1] if lines[index].endswith("\n") else lines[index]
            if line.startswith("diff --git "):
                break
            if line == _BINARY_HEADER:
                index = _scan_binary_sections(block, lines, index + 1, total)
                continue
            if line.startswith("index ") and block["index_line"] is None:
                index_match = _INDEX_LINE_RE.match(line)
                if index_match is None:
                    raise BinaryPayloadError(f"the diff block for {block['path']!r} has an unparseable index line")
                block["index_line"] = index
                block["index_from"] = index_match.group(1)
                block["index_to"] = index_match.group(2)
                block["index_mode"] = index_match.group(3)
            elif line.startswith("new file mode "):
                block["new_file"] = True
            elif line.startswith("deleted file mode "):
                block["deleted_file"] = True
            elif line.startswith("rename to "):
                block["path"] = line[len("rename to ") :]
            elif line.startswith("copy to "):
                block["path"] = line[len("copy to ") :]
            elif _PLACEHOLDER_RE.match(line) and block["placeholder"] is None:
                block["placeholder"] = index
            index += 1
        blocks.append(block)
    return blocks


def _scan_binary_sections(block: dict[str, Any], lines: list[str], index: int, total: int) -> int:
    """Parse the two binary halves that must follow a ``GIT binary patch`` line."""
    sections: list[dict[str, Any]] = block["sections"]
    while index < total:
        line = lines[index][:-1] if lines[index].endswith("\n") else lines[index]
        if line.startswith("diff --git "):
            break
        if line == "":
            index += 1
            if len(sections) == 2:
                break
            continue
        parts = line.split(" ")
        if parts[0] not in _CHUNK_KINDS or len(parts) != 2 or not parts[1].isdigit():
            if not sections:
                raise BinaryPayloadError(
                    f"the binary patch for {block['path']!r} carries an unknown section {line[:40]!r}"
                )
            raise BinaryPayloadError(
                f"the binary patch for {block['path']!r} ends after {len(sections)} of its two halves"
            )
        kind = parts[0]
        declared = int(parts[1])
        index += 1
        body: list[str] = []
        compressed = bytearray()
        while index < total:
            data_line = lines[index][:-1] if lines[index].endswith("\n") else lines[index]
            if data_line == "":
                break
            if data_line.startswith(("diff --git ", "literal ", "delta ")):
                raise BinaryPayloadError(
                    f"the binary patch for {block['path']!r} ends inside a data section"
                )
            if "A" <= data_line[0:1] <= "Z":
                count = ord(data_line[0]) - 64
            elif "a" <= data_line[0:1] <= "z":
                count = ord(data_line[0]) - 70
            else:
                raise BinaryPayloadError(
                    f"the binary patch for {block['path']!r} has a data line with an unknown chunk-size prefix"
                )
            if not 1 <= count <= 52:
                raise BinaryPayloadError(
                    f"the binary patch for {block['path']!r} declares an impossible chunk size"
                )
            compressed += _b85_decode_chunk(data_line, count)
            body.append(data_line)
            index += 1
        if not body:
            raise BinaryPayloadError(f"the binary patch for {block['path']!r} declares an empty data section")
        if index >= total:
            raise BinaryPayloadError(
                f"the binary patch for {block['path']!r} ends inside a data section"
            )
        what = f"the {kind} section of the binary patch for {block['path']!r}"
        data = _inflate_capped(bytes(compressed), declared, what)
        if kind == "literal":
            sections.append({"kind": "literal", "size": declared, "data": data})
        else:
            source_size, target_size = _validate_delta_stream(data)
            sections.append(
                {
                    "kind": "delta",
                    "size": declared,
                    "data": data,
                    "source_size": source_size,
                    "target_size": target_size,
                }
            )
    if len(sections) != 2:
        raise BinaryPayloadError(
            f"the binary patch for {block['path']!r} carries {len(sections)} sections where a forward and a reverse half are required"
        )
    return index


def _reconstruct_block(block: dict[str, Any], bases: Mapping[str, bytes]) -> bytes:
    """Rebuild the postimage of one binary block from its two halves."""
    forward, reverse = block["sections"]
    if forward["kind"] == "literal":
        return _check_postimage(block, forward["data"])
    if reverse["kind"] == "literal":
        preimage = reverse["data"]
    else:
        path = block["path"]
        if path is None or path not in bases:
            raise BinaryPayloadError(
                f"the binary patch for {block['path']!r} is a delta against a delta; "
                "reconstruction needs the file's preimage from source_rev"
            )
        preimage = bases[path]
    postimage = _apply_delta(preimage, forward["data"])
    if reverse["kind"] == "delta" and _apply_delta(postimage, reverse["data"]) != preimage:
        raise BinaryPayloadError(
            f"the reverse binary half for {block['path']!r} does not unwind its forward half"
        )
    return _check_postimage(block, postimage)


def _check_postimage(block: dict[str, Any], postimage: bytes) -> bytes:
    """When the index line names a full non-zero postimage, the bytes must match it."""
    index_to = block["index_to"]
    if (
        index_to is not None
        and len(index_to) in (40, 64)
        and not _is_zero_id(index_to)
        and _blob_id(postimage, len(index_to)) != index_to
    ):
        raise BinaryPayloadError(
            f"the reconstructed bytes for {block['path']!r} do not match the blob id the payload declares"
        )
    return postimage


def reconstruct_binary_files(
    patch: str,
    *,
    bases: Mapping[str, bytes] | None = None,
) -> dict[str, bytes]:
    """Rebuild every binary file a payload carries, keyed by path.

    The forward half is either a literal (the postimage itself) or a git delta
    against the preimage; the preimage comes from the reverse literal half, or
    — when both halves are deltas — from *bases* (the file's bytes at
    ``source_rev``). Malformed, truncated or unknown binary sections raise
    :class:`BinaryPayloadError`; a text-only payload reconstructs nothing.
    """
    blocks = _scan_binary_blocks(str(patch))
    out: dict[str, bytes] = {}
    for block in blocks:
        if not block["sections"]:
            continue
        if block["path"] is None:
            raise BinaryPayloadError("a binary patch block has an unparseable diff header")
        out[block["path"]] = _reconstruct_block(block, bases or {})
    return out


def _normalize_patch_text(text: Any) -> str | None:
    """Validate a worker's diff text and mark it when it carries binary hunks.

    Text-only payloads are returned byte-for-byte. A payload with
    ``GIT binary patch`` sections is validated section by section and prefixed
    with :data:`BINARY_PAYLOAD_MARKER`. A content-free ``Binary files ...
    differ`` placeholder — a binary change with no content — is refused here
    instead of failing later inside ``git apply``.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    first_line = text.split("\n", 1)[0]
    marker_present = first_line == BINARY_PAYLOAD_MARKER
    if not marker_present and BINARY_PAYLOAD_MARKER in text:
        raise BinaryPayloadError("the envelope marker must be the payload's first line")
    blocks = _scan_binary_blocks(text)
    has_sections = any(block["sections"] for block in blocks)
    has_placeholder = any(block["placeholder"] is not None for block in blocks)
    if not marker_present and not has_sections and not has_placeholder:
        return text
    if marker_present and not has_sections:
        raise BinaryPayloadError(
            "the payload claims the binary envelope but carries no binary hunks",
            "Emit the diff with git binary hunks (git diff --binary), or drop the marker for a text-only diff.",
        )
    for block in blocks:
        if block["placeholder"] is not None:
            raise BinaryPayloadError(
                f"the diff describes {block['path']!r} as a binary change but carries no content for it",
                (
                    "Have the runtime re-emit its diff with git binary hunks "
                    "(git diff --binary against source_rev), or carry the file's "
                    "endpoints in the patch object's binary field."
                ),
            )
    if marker_present:
        return text
    return BINARY_PAYLOAD_MARKER + "\n" + text


def _binary_entry(entry: Any) -> dict[str, Any]:
    """Validate one structured binary entry and decode its endpoints."""
    if not isinstance(entry, Mapping):
        raise BinaryPayloadError("a binary entry is not an object")
    path = _safe_payload_path(entry.get("path"))
    raw_from = entry.get("from")
    raw_to = entry.get("to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str) or not _FULL_HEX_RE.match(raw_from) or not _FULL_HEX_RE.match(raw_to):
        raise BinaryPayloadError(
            f"the binary entry for {path!r} must declare full git blob ids in 'from' and 'to'"
        )
    from_id = raw_from.strip().lower()
    to_id = raw_to.strip().lower()
    if _is_zero_id(from_id) and _is_zero_id(to_id):
        raise BinaryPayloadError(f"the binary entry for {path!r} declares two zero blob ids")
    data = _decode_entry_data(entry.get("data"), path, "data")
    old_data = _decode_entry_data(entry.get("old_data"), path, "old_data")
    if _is_zero_id(from_id) and old_data:
        raise BinaryPayloadError(f"the binary entry for {path!r} carries a preimage for a new file")
    if _is_zero_id(to_id) and data:
        raise BinaryPayloadError(f"the binary entry for {path!r} carries content for a deleted file")
    if not _is_zero_id(from_id) and _blob_id(old_data, len(from_id)) != from_id:
        raise BinaryPayloadError(
            f"the binary entry for {path!r} declares preimage blob id {from_id[:12]} that its old_data does not hash to"
        )
    if not _is_zero_id(to_id) and _blob_id(data, len(to_id)) != to_id:
        raise BinaryPayloadError(
            f"the binary entry for {path!r} declares blob id {to_id[:12]} that its data does not hash to"
        )
    return {"path": path, "from": from_id, "to": to_id, "data": data, "old_data": old_data}


def _decode_entry_data(value: Any, path: str, label: str) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        raise BinaryPayloadError(f"the {label} of the binary entry for {path!r} is not base64 text")
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (ValueError, TypeError):
        raise BinaryPayloadError(f"the {label} of the binary entry for {path!r} is not valid base64") from None


def _parse_binary_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, (list, tuple)):
        raise BinaryPayloadError("the patch object's binary field must be a list of binary entries")
    parsed = [_binary_entry(entry) for entry in entries]
    paths = [entry["path"] for entry in parsed]
    if len(set(paths)) != len(paths):
        raise BinaryPayloadError("the patch object's binary field names a path twice")
    return parsed


def _binary_half(content: bytes) -> list[str]:
    """One git literal section: ``literal <size>``, base85 lines, footer blank."""
    compressed = zlib.compress(content, 6)
    lines = [f"literal {len(content)}"]
    start = 0
    while start < len(compressed):
        count = min(52, len(compressed) - start)
        prefix = chr(ord("A") + count - 1) if count <= 26 else chr(ord("a") + count - 27)
        lines.append(prefix + _b85_encode_chunk(compressed[start : start + count]))
        start += count
    return lines


def _expand_index_line(line: str, entry: dict[str, Any]) -> str:
    match = _INDEX_LINE_RE.match(line)
    if match is None:
        raise BinaryPayloadError(f"the diff block for {entry['path']!r} has no index line to expand")
    abbrev_from, abbrev_to, mode = match.group(1), match.group(2), match.group(3)
    for abbrev, full in ((abbrev_from, entry["from"]), (abbrev_to, entry["to"])):
        if not full.startswith(abbrev):
            raise BinaryPayloadError(
                f"the diff's abbreviated blob id {abbrev} for {entry['path']!r} does not match the binary entry's {full}"
            )
    suffix = f" {mode}" if mode else ""
    return f"index {entry['from']}..{entry['to']}{suffix}"


def _splice_binary_block(lines: list[str], block: dict[str, Any], entry: dict[str, Any]) -> None:
    """Replace one content-free placeholder block with its binary hunks."""
    if block["sections"]:
        raise BinaryPayloadError(
            f"the diff already carries binary hunks for {entry['path']!r}; the binary entry would be ambiguous"
        )
    if block["new_file"] and not _is_zero_id(entry["from"]):
        raise BinaryPayloadError(f"the diff adds {entry['path']!r} but the binary entry names a preimage")
    if block["deleted_file"] and not _is_zero_id(entry["to"]):
        raise BinaryPayloadError(f"the diff deletes {entry['path']!r} but the binary entry names a postimage")
    if not block["new_file"] and not block["deleted_file"]:
        if _is_zero_id(entry["from"]) or _is_zero_id(entry["to"]):
            raise BinaryPayloadError(
                f"the diff modifies {entry['path']!r} but the binary entry declares an add or a delete"
            )
    expanded = _expand_index_line(lines[block["index_line"]], entry)
    forward = _binary_half(entry["data"])
    reverse = _binary_half(entry["old_data"])
    replacement = (
        [expanded + "\n", _BINARY_HEADER + "\n"]
        + [line + "\n" for line in forward]
        + ["\n"]
        + [line + "\n" for line in reverse]
        + ["\n"]
    )
    lines[block["index_line"] : block["placeholder"] + 1] = replacement


def _assemble_binary_payload(diff: str, entries: Any) -> str:
    """Emit a payload that carries every declared binary change losslessly."""
    parsed = _parse_binary_entries(entries)
    lines = diff.splitlines(keepends=True)
    blocks = _scan_binary_blocks(diff)
    placeholder_paths: dict[str, dict[str, Any]] = {}
    for block in blocks:
        if block["sections"]:
            raise BinaryPayloadError(
                f"the diff already carries binary hunks for {block['path']!r}; a binary entry cannot also carry them"
            )
        if block["placeholder"] is not None:
            if block["path"] is None:
                raise BinaryPayloadError("a binary placeholder block has an unparseable diff header")
            placeholder_paths[block["path"]] = block
    for entry in parsed:
        block = placeholder_paths.get(entry["path"])
        if block is None:
            raise BinaryPayloadError(
                f"the diff does not describe {entry['path']!r} as a binary change, so its content has no place in the payload"
            )
        _splice_binary_block(lines, block, entry)
    uncovered = sorted(set(placeholder_paths) - {entry["path"] for entry in parsed})
    if uncovered:
        raise BinaryPayloadError(
            f"the diff describes {uncovered[0]!r} as a binary change but carries no content for it"
        )
    payload = BINARY_PAYLOAD_MARKER + "\n" + "".join(lines)
    checked = _normalize_patch_text(payload)
    if checked != payload:
        raise BinaryPayloadError(
            "the assembled binary payload does not validate as emitted",
            "Report this to the coordinator maintainers: the payload builder produced an invalid binary patch.",
        )
    return payload


def _payload_from_value(value: Any) -> str | None:
    """The emitted payload from one worker patch field, binary-carrying or not."""
    if isinstance(value, str):
        return _normalize_patch_text(value)
    if isinstance(value, Mapping):
        diff = _patch_text(value)
        if diff is None:
            return None
        if "binary" in value:
            return _assemble_binary_payload(diff, value.get("binary"))
        return _normalize_patch_text(diff)
    return None


def normalize_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce arbitrary worker output to the documented result contract.

    Unknown keys are dropped rather than forwarded: worker metadata never
    carries lease or scope authority. Only the first non-empty known alias for
    each field is used, ``base_rev`` must be a full hex commit id, and
    ``untrusted`` is forced to true. A patch that cannot carry its binary
    changes losslessly raises :class:`BinaryPayloadError` instead of being
    reduced to a payload the verifier must refuse.
    """
    data = _flatten(raw if isinstance(raw, Mapping) else {})

    patch: str | None = None
    for key in _PATCH_KEYS:
        patch = _payload_from_value(data.get(key))
        if patch is not None:
            break

    state = _optional_text(data.get("state")) or _optional_text(data.get("status"))

    return {
        "attempt_id": _optional_id(data.get("attempt_id") or data.get("id")),
        "patch": patch,
        "base_rev": _declared_base(data),
        "usage": _normalize_usage(data),
        "evidence": {"checks": _worker_checks(data)},
        "state": state,
        "untrusted": True,
    }


class StubAdapter:
    """Base for documented, non-functional adapters.

    Every operation fails closed with ``UnsupportedAdapter`` so selecting a
    stub can never silently fall back to a different runtime or pretend work
    succeeded.
    """

    name = "stub"

    def _unsupported(self) -> UnsupportedAdapter:
        return UnsupportedAdapter(
            f"the {self.name} adapter is a documented stub, not an implementation",
            (
                f"Use orbit-loopback or launcher, or implement the WorkerAdapter "
                f"contract for {self.name} as described in {_ADAPTER_DOC}."
            ),
        )

    def health(self) -> dict[str, Any]:
        raise self._unsupported()

    def projects(self) -> list[dict[str, Any]]:
        raise self._unsupported()

    def attempts(self) -> list[dict[str, Any]]:
        raise self._unsupported()

    def create(
        self,
        brief: dict[str, Any],
        *,
        model: str,
        kanban_task_id: str | None = None,
    ) -> dict[str, Any]:
        raise self._unsupported()

    def get(self, attempt_id: str) -> dict[str, Any]:
        raise self._unsupported()

    def events(
        self, attempt_id: str, cursor: int | None = None
    ) -> list[dict[str, Any]]:
        raise self._unsupported()

    def result(self, attempt_id: str) -> dict[str, Any]:
        raise self._unsupported()

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        raise self._unsupported()

    def review(
        self,
        attempt_id: str,
        *,
        accepted: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        raise self._unsupported()


class OpenCodeAdapter(StubAdapter):
    """Documented stub for the OpenCode runtime.

    A real implementation must: create a disposable checkout at the coordinator
    supplied ``source_rev``; create and check out a feature branch there (never
    ``main`` or ``master``); run OpenCode against the brief with the brief's
    ``assigned_files`` as its only writable scope; return the unified diff from
    ``git diff <source_rev>`` plus ``base_rev=<source_rev>`` and the runtime's
    reported token/cost usage; and leave the coordinator's credentials outside
    the runtime. It must never push, merge, tag, force-push, or accept a token,
    host, or lease from worker output. Map runtime replies through
    ``normalize_result`` before returning them.
    """

    name = "opencode"


class CodexAdapter(StubAdapter):
    """Documented stub for the Codex runtime.

    A real implementation must: create a disposable checkout at the coordinator
    supplied ``source_rev``; create and check out a feature branch there (never
    ``main`` or ``master``); run Codex non-interactively against the brief with
    the brief's ``assigned_files`` as its only writable scope; return the
    unified diff from ``git diff <source_rev>`` plus ``base_rev=<source_rev>``
    and the runtime's reported token/cost usage; and keep coordinator
    credentials out of the runtime. It must never push, merge, tag, force-push,
    or treat worker metadata as lease or scope authority. Map runtime replies
    through ``normalize_result`` before returning them.
    """

    name = "codex"


def _client_adapter(name: str) -> Callable[..., WorkerAdapter]:
    """Build a factory that returns the existing client for ``name``."""

    def build(
        config: Mapping[str, Any] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        *,
        state_dir: Path | None = None,
    ) -> WorkerAdapter:
        settings = dict(config or {})
        settings["adapter"] = name
        client = client_from_config(
            settings, token_provider=token_provider, state_dir=state_dir
        )
        client.name = name
        return client

    return build


def _stub_adapter(adapter_cls: type[StubAdapter]) -> Callable[..., WorkerAdapter]:
    def build(
        config: Mapping[str, Any] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        *,
        state_dir: Path | None = None,
    ) -> WorkerAdapter:
        return adapter_cls()

    return build


ADAPTERS: dict[str, Callable[..., WorkerAdapter]] = {
    "orbit-loopback": _client_adapter("orbit-loopback"),
    "launcher": _client_adapter("launcher"),
    "opencode": _stub_adapter(OpenCodeAdapter),
    "codex": _stub_adapter(CodexAdapter),
}


def adapter_from_config(
    config: Mapping[str, Any] | None = None,
    *,
    token_provider: Callable[[], str | None] | None = None,
    state_dir: Path | None = None,
) -> WorkerAdapter:
    """Pick the configured adapter, defaulting to the loopback Orbit client."""
    settings = config or {}
    name = str(settings.get("adapter") or "orbit-loopback").strip().lower()
    if name in _LAUNCHER_ALIASES:
        name = "launcher"
    factory = ADAPTERS.get(name)
    if factory is None:
        known = ", ".join(sorted(ADAPTERS))
        aliases = ", ".join(sorted(_LAUNCHER_ALIASES))
        raise UnsupportedAdapter(
            f"unknown worker adapter: {name or '(empty)'}; known adapters: {known}",
            (
                f"Choose one of: {known}. The aliases {aliases} select launcher. "
                f"See {_ADAPTER_DOC}."
            ),
        )
    return factory(settings, token_provider, state_dir=state_dir)


def available_adapters() -> list[dict[str, Any]]:
    """List adapter names with default/implemented flags, in stable order."""
    return [
        {
            "name": name,
            "default": name == "orbit-loopback",
            "implemented": _IMPLEMENTED[name],
        }
        for name in _KNOWN_ADAPTERS
    ]
