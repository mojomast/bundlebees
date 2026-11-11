#!/usr/bin/env python3
"""Measure registered tool-schema tokens (o200k_base and cl100k_base)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fakes import FakeCtx, load_plugin  # noqa: E402


def schemas() -> list[dict]:
    plugin = load_plugin()
    ctx = FakeCtx()
    plugin.register(ctx)
    return [rec["schema"] for rec in ctx.tools.values()]


def count(text: str, encoding: str) -> int:
    import tiktoken

    return len(tiktoken.get_encoding(encoding).encode(text))


def main() -> None:
    body = json.dumps(schemas(), separators=(",", ":"), ensure_ascii=False)
    try:
        o200k = count(body, "o200k_base")
        cl100k = count(body, "cl100k_base")
    except ModuleNotFoundError:
        print(f"tools={len(schemas())} chars={len(body)} (tiktoken not installed)")
        return
    print(f"tools={len(schemas())} chars={len(body)} o200k={o200k} cl100k={cl100k}")
    if o200k > 900:
        print("WARNING: over the 900 o200k budget", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
