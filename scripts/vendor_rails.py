"""Deterministic offline vendor script for the rizzstack_rails runtime.

Copies the hash-pinned runtime files listed in
contrib/rizzstack_rails/vendor-manifest.json from this repository into one or
more destination trees, restoring byte-identical copies on every run. No
network access, standard library only.

Usage:
  python scripts/vendor_rails.py --check
  python scripts/vendor_rails.py --write-manifest [--pin-label LABEL]
  python scripts/vendor_rails.py --dest DIR [--dest DIR2 ...]
"""

import argparse
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import tempfile

MANIFEST_SCHEMA = "rizzstack.vendor-manifest/1"
PACKAGE_REL = "contrib/rizzstack_rails"  # manifest names use POSIX spelling on every host
MANIFEST_NAME = "vendor-manifest.json"


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def manifest_path(root):
    return os.path.join(root, PACKAGE_REL, MANIFEST_NAME)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def load_manifest(root):
    path = manifest_path(root)
    if not os.path.exists(path):
        print("vendor: manifest missing at %s" % path, file=sys.stderr)
        return None
    try:
        with open(path, "rb") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict) or manifest.get('source_package') != PACKAGE_REL:
            raise ValueError('unexpected source package')
    except (ValueError, OSError):
        print('vendor: invalid or unreadable package manifest', file=sys.stderr)
        return None
    if manifest.get("schema") != MANIFEST_SCHEMA:
        print("vendor: unsupported manifest schema %r" % (manifest.get("schema"),), file=sys.stderr)
        return None
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        print("vendor: manifest carries no files", file=sys.stderr)
        return None
    if not all(isinstance(entry, dict) for entry in entries):
        print("vendor: invalid file entry", file=sys.stderr)
        return None
    names = [entry.get("path") for entry in entries]
    if any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
        print("vendor: manifest file paths must be unique strings", file=sys.stderr)
        return None
    for entry in entries:
        if not re.fullmatch(r"contrib/rizzstack_rails/[A-Za-z_][A-Za-z_0-9]*\.py", entry['path']):
            print("vendor: expected a Python module directly inside the package", file=sys.stderr)
            return None
        if (set(entry) != {"path", "sha256", "bytes"} or not isinstance(entry['sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or type(entry['bytes']) is not int or entry['bytes'] < 0):
            print("vendor: invalid manifest entry: %s" % entry["path"], file=sys.stderr)
            return None
    return manifest


def verify_against_manifest(root, manifest):
    problems = []
    covered = {PurePosixPath(entry['path']).name for entry in manifest['files']}
    if covered != set(package_files(root)):
        problems.append('package modules differ from the manifest; regenerate and review the pins')
    for entry in manifest["files"]:
        path = os.path.join(root, entry["path"])
        if os.path.islink(path) or not os.path.isfile(path):
            problems.append("missing: " + entry["path"])
            continue
        with open(path, "rb") as handle:
            data = handle.read()
        digest = sha256_bytes(data)
        if digest != entry["sha256"]:
            problems.append(
                "hash mismatch: %s (have %s, pinned %s)" % (entry["path"], digest, entry["sha256"])
            )
        elif "bytes" in entry and entry["bytes"] != len(data):
            problems.append("size mismatch: %s" % entry["path"])
    return problems


def package_files(root):
    package_dir = os.path.join(root, PACKAGE_REL)
    return sorted(
        name
        for name in os.listdir(package_dir)
        if name.endswith(".py") and os.path.isfile(os.path.join(package_dir, name))
    )


def write_manifest(root, pin_label):
    entries = []
    for name in package_files(root):
        rel = PACKAGE_REL + '/' + name
        with open(os.path.join(root, rel), "rb") as handle:
            data = handle.read()
        entries.append({"path": rel, "sha256": sha256_bytes(data), "bytes": len(data)})
    entries.sort(key=lambda entry: entry["path"])
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "source_package": PACKAGE_REL,
        "pin_label": pin_label,
        "files": entries,
    }
    atomic_write(manifest_path(root), (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"))
    return manifest


def atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.rails-vendor-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def vendor_to_dest(root, manifest, dest):
    problems = verify_against_manifest(root, manifest)
    if problems:
        return problems, 0
    package = os.path.join(dest, PACKAGE_REL)
    if any(os.path.islink(p) for p in (dest, os.path.join(dest, 'contrib'), package)):
        return ['refusing symlink in destination package path'], 0
    if any(os.path.islink(os.path.join(dest, entry['path'])) for entry in manifest['files']):
        return ['refusing linked destination module'], 0
    written = 0
    for entry in manifest["files"]:
        target = os.path.join(dest, entry["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(os.path.join(root, entry["path"]), "rb") as handle:
            data = handle.read()
        needs_write = True
        if os.path.exists(target):
            with open(target, "rb") as handle:
                needs_write = handle.read() != data
        if needs_write:
            atomic_write(target, data)
            written += 1
        with open(target, "rb") as handle:
            copied = handle.read()
        if sha256_bytes(copied) != entry["sha256"]:
            return ["post-write hash mismatch: %s" % entry["path"]], written
    return [], written


def git_pin_label(root):
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode == 0:
            status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=all', '--', PACKAGE_REL],
                                    cwd=root, capture_output=True, timeout=30)
            if status.returncode == 0:
                return 'unreviewed-working-tree' if status.stdout.strip() else out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unpinned"


def main(argv):
    parser = argparse.ArgumentParser(description="Vendor the pinned rizzstack_rails runtime.")
    parser.add_argument("--check", action="store_true", help="verify package files against the manifest")
    parser.add_argument("--dest", action="append", default=[], help="destination root; repeatable")
    parser.add_argument(
        "--write-manifest", action="store_true", help="recompute pins from the package files"
    )
    parser.add_argument("--pin-label", default=None, help="label recorded by --write-manifest")
    args = parser.parse_args(argv)
    root = repo_root()
    if args.write_manifest:
        manifest = write_manifest(root, args.pin_label or git_pin_label(root))
        print(
            json.dumps(
                {"wrote": manifest_path(root), "files": [e["path"] for e in manifest["files"]]}
            )
        )
        return 0
    manifest = load_manifest(root)
    if manifest is None:
        return 1
    if args.check:
        problems = verify_against_manifest(root, manifest)
        for problem in problems:
            print("vendor: " + problem, file=sys.stderr)
        print(json.dumps({"check": "ok" if not problems else "failed"}))
        return 0 if not problems else 1
    if not args.dest:
        parser.error("nothing to do: pass --check, --write-manifest or --dest")
        return 2
    failures = []
    for dest in args.dest:
        problems, written = vendor_to_dest(root, manifest, dest)
        for problem in problems:
            failures.append(dest + ": " + problem)
        if not problems:
            print(
                json.dumps(
                    {
                        "dest": dest,
                        "files": len(manifest["files"]),
                        "rewritten": written,
                        "status": "ok",
                    }
                )
            )
    for failure in failures:
        print("vendor: " + failure, file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
