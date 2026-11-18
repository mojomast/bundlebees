"""Minimal coordinator consumer, independent of Hermes and BundleBees settings.

Pass an already authorized, read-only RailsLedger to query(request, reader),
or run this file with ``query --kind attempt --id att_example --format json``.
"""
from pathlib import Path
import sys

_contrib = Path(__file__).resolve().parents[1]
if str(_contrib) not in sys.path:
    sys.path.insert(0,str(_contrib))

from rizzstack_rails.query import query_events


def query(request, reader, *, allowed_scope=None):
    return query_events(request,reader,allowed_scope=allowed_scope)


if __name__ == '__main__':
    from rizzstack_rails.__main__ import main
    sys.exit(main())
