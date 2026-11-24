"""Pin, check and refresh the vendored slop-lint copy in tools/slop-lint.

slop-lint lives in mojomast/workflow (tools/slop-lint). This repository
carries a byte-identical copy of its README.md and slop_lint/ package so CI can
run the ratchet without a network install. tools/slop-lint/vendor-manifest.json
records the workflow commit the copy came from and a SHA-256 per file.
Modeled on scripts/vendor_rails.py; standard library only.

Usage:
  python3 scripts/vendor_slop_lint.py --check
      Offline. Fails if any vendored file is missing, extra, symlinked or
      differs from the manifest, or if the manifest is missing or invalid.
  python3 scripts/vendor_slop_lint.py --refresh --source PATH/TO/workflow
      Re-vendor from a clean workflow checkout and rewrite the manifest with
      that checkout's HEAD commit. Review the diff like any other change.
  python3 scripts/vendor_slop_lint.py --compare PATH/TO/workflow
      Report files that differ from a workflow checkout (drift), exit 1 if any.

This file is kept byte-identical across the repos that vendor slop-lint.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

SCHEMA = "rizzstack.slop-lint-vendor/1"
SOURCE_REPO = "mojomast/workflow"
SOURCE_PATH = "tools/slop-lint"
VENDOR_REL = "tools/slop-lint"
MANIFEST_NAME = "vendor-manifest.json"
SKIP_DIRS = {"__pycache__"}


def default_root():
    return Path(__file__).resolve().parents[1]


def is_vendored(rel):
    """README.md and every *.py under slop_lint/ are vendored; tests are not."""
    parts = PurePosixPath(rel).parts
    if rel == "README.md":
        return True
    return len(parts) >= 2 and parts[0] == "slop_lint" and rel.endswith(".py")


def walk(base):
    """Relative POSIX paths of every file under base, skipping caches.

    Symlinks are returned too so the caller can refuse them.
    """
    found = []
    if not base.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in filenames:
            path = Path(dirpath) / name
            found.append(path.relative_to(base).as_posix())
    return sorted(found)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def entry_problem(entry):
    """Why one manifest file entry is invalid, or None."""
    if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
        return "entries need exactly path, sha256 and bytes"
    path, digest, size = entry["path"], entry["sha256"], entry["bytes"]
    if not isinstance(path, str) or not is_vendored(path) or ".." in PurePosixPath(path).parts:
        return "path is not a vendored slop-lint file"
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "sha256 must be 64 lowercase hex"
    if type(size) is not int or size < 0:
        return "bytes must be a non-negative integer"
    return None


def manifest_problem(manifest):
    """Why a parsed manifest is invalid, or None."""
    if not isinstance(manifest, dict):
        return "manifest must be a JSON object"
    expected = {"schema": SCHEMA, "source_repo": SOURCE_REPO, "source_path": SOURCE_PATH}
    for key, value in expected.items():
        if manifest.get(key) != value:
            return "manifest %s must be %r" % (key, value)
    commit = manifest.get("source_commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return "manifest source_commit must be a full 40-hex commit"
    return files_problem(manifest.get("files"))


def files_problem(files):
    """Why the manifest's file list is invalid, or None."""
    if not isinstance(files, list) or not files:
        return "manifest lists no files"
    for entry in files:
        why = entry_problem(entry)
        if why:
            return "invalid manifest entry %r: %s" % (entry, why)
    paths = [e["path"] for e in files]
    if len(set(paths)) != len(paths):
        return "duplicate manifest entries"
    return None


def load_manifest(vendor):
    path = vendor / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        return None, "manifest missing: %s/%s" % (VENDOR_REL, MANIFEST_NAME)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "manifest is not valid JSON"
    why = manifest_problem(manifest)
    return (None, why) if why else (manifest, None)


def check(root):
    vendor = root / VENDOR_REL
    manifest, err = load_manifest(vendor)
    if err:
        return [err], None
    problems = []
    pinned = {e["path"]: e for e in manifest["files"]}
    present = [p for p in walk(vendor) if p != MANIFEST_NAME]
    for rel in present:
        if rel not in pinned:
            problems.append("unpinned file: %s/%s" % (VENDOR_REL, rel))
    for rel, entry in sorted(pinned.items()):
        path = vendor / rel
        if path.is_symlink():
            problems.append("symlink refused: %s/%s" % (VENDOR_REL, rel))
            continue
        if not path.is_file():
            problems.append("missing: %s/%s" % (VENDOR_REL, rel))
            continue
        data = path.read_bytes()
        if len(data) != entry["bytes"] or sha256(data) != entry["sha256"]:
            problems.append("hash mismatch: %s/%s (have %s, pinned %s)"
                            % (VENDOR_REL, rel, sha256(data), entry["sha256"]))
    return problems, manifest


def git_out(cwd, *args):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), out.stderr.strip()))
    return out.stdout


def source_files(source):
    base = source / SOURCE_PATH
    if not (base / "slop_lint").is_dir():
        raise RuntimeError("%s/slop_lint not found under %s" % (SOURCE_PATH, source))
    files = [rel for rel in walk(base) if is_vendored(rel)]
    for rel in files:
        if (base / rel).is_symlink():
            raise RuntimeError("symlink in source refused: %s" % rel)
    return base, files


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".slop-vendor-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def source_commit(source):
    """HEAD of a clean workflow checkout; refuses local edits under tools/slop-lint."""
    commit = git_out(source, "rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("source HEAD is not a commit: %r" % commit)
    dirty = git_out(source, "status", "--porcelain", "--untracked-files=all", "--", SOURCE_PATH)
    if dirty.strip():
        raise RuntimeError("source %s has uncommitted changes; commit or stash them first"
                           % SOURCE_PATH)
    return commit


def remove_stale(vendor, keep):
    for rel in walk(vendor):
        if rel != MANIFEST_NAME and (is_vendored(rel) or rel.endswith(".py")) and rel not in keep:
            (vendor / rel).unlink()
            print("removed  %s/%s" % (VENDOR_REL, rel))


def copy_file(src, target, rel):
    data = src.read_bytes()
    if target.is_symlink():
        target.unlink()
    if not target.is_file() or target.read_bytes() != data:
        atomic_write(target, data)
        print("updated  %s/%s" % (VENDOR_REL, rel))
    return data


def refresh(root, source):
    commit = source_commit(source)
    base, files = source_files(source)
    vendor = root / VENDOR_REL
    remove_stale(vendor, set(files))
    entries = []
    for rel in files:
        data = copy_file(base / rel, vendor / rel, rel)
        entries.append({"path": rel, "sha256": sha256(data), "bytes": len(data)})
    manifest = {"schema": SCHEMA, "source_repo": SOURCE_REPO, "source_path": SOURCE_PATH,
                "source_commit": commit, "files": entries}
    atomic_write(vendor / MANIFEST_NAME,
                 (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return commit, len(entries)


def compare(root, source):
    base, files = source_files(source)
    vendor = root / VENDOR_REL
    local = [p for p in walk(vendor) if p != MANIFEST_NAME and is_vendored(p)]
    drift = []
    for rel in sorted(set(files) | set(local)):
        a, b = vendor / rel, base / rel
        if not a.is_file() or not b.is_file() or a.read_bytes() != b.read_bytes():
            drift.append(rel)
    return drift


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pin, check and refresh vendored slop-lint.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify the copy against the manifest")
    mode.add_argument("--refresh", action="store_true", help="re-vendor from --source")
    mode.add_argument("--compare", metavar="WORKFLOW_DIR", help="report drift from a checkout")
    parser.add_argument("--source", metavar="WORKFLOW_DIR", help="workflow checkout for --refresh")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else default_root()
    try:
        if args.check:
            problems, manifest = check(root)
            if problems:
                for p in problems:
                    print("vendor-slop-lint: " + p, file=sys.stderr)
                print("vendor-slop-lint: FAIL. Re-vendor with --refresh --source <workflow checkout>"
                      " and review the diff.", file=sys.stderr)
                return 1
            print("vendor-slop-lint: OK, %d files match %s@%s"
                  % (len(manifest["files"]), SOURCE_REPO, manifest["source_commit"][:12]))
            return 0
        if args.refresh:
            if not args.source:
                parser.error("--refresh needs --source WORKFLOW_DIR")
            commit, count = refresh(root, Path(args.source).resolve())
            print("vendor-slop-lint: vendored %d files from %s@%s" % (count, SOURCE_REPO, commit[:12]))
            return 0
        drift = compare(root, Path(args.compare).resolve())
        for rel in drift:
            print("drift: %s/%s" % (VENDOR_REL, rel))
        if drift:
            print("vendor-slop-lint: %d file(s) differ from the source checkout" % len(drift))
            return 1
        print("vendor-slop-lint: identical to the source checkout")
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print("vendor-slop-lint: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
