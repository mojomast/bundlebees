#!/usr/bin/env python3
"""Bump plugin.yaml version without rewriting the rest of the manifest.

Usage: python3 scripts/bump_plugin_version.py 0.1.1

Requires ruamel.yaml. Refuses duplicate keys (strict YAML) and refuses to run
when more than one version: line would change. Never rewrites name: or other keys.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin.yaml"


def load_strict(path: Path) -> dict:
    from ruamel.yaml import YAML
    from ruamel.yaml.constructor import DuplicateKeyError

    yaml = YAML(typ="safe")
    yaml.allow_duplicate_keys = False
    try:
        data = yaml.load(path.read_text(encoding="utf-8"))
    except DuplicateKeyError as exc:
        raise SystemExit(f"plugin.yaml has duplicate keys: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("plugin.yaml did not parse to a mapping")
    return data


def bump(new_version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", new_version):
        raise SystemExit(f"version must look like X.Y.Z, got {new_version!r}")
    text = PLUGIN.read_text(encoding="utf-8")
    before = load_strict(PLUGIN)
    expected_name = before.get("name")
    if not expected_name:
        raise SystemExit("plugin.yaml missing name")
    pattern = re.compile(r'(?m)^version:\s*"[^"]+"\s*$')
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one version: line, found {len(matches)}")
    updated = pattern.sub(f'version: "{new_version}"', text, count=1)
    if updated == text and before.get("version") == new_version:
        print(f"already at {new_version}")
        return
    PLUGIN.write_text(updated, encoding="utf-8")
    after = load_strict(PLUGIN)
    if after.get("version") != new_version:
        raise SystemExit("bump failed: version not updated")
    if after.get("name") != expected_name:
        raise SystemExit("bump corrupted name")
    if set(after) != set(before):
        raise SystemExit(f"bump changed key set: {set(after) ^ set(before)}")
    print(f"bumped plugin.yaml {before.get('version')} -> {new_version}")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit(__doc__)
    bump(argv[1])


if __name__ == "__main__":
    main(sys.argv)
