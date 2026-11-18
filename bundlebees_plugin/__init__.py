"""bundlebees Python package."""

import sys
from pathlib import Path


def _configure_rails():
    root = Path(__file__).resolve().parents[1]
    canonical = root / 'rizzstack_rails'
    vendor = root / 'contrib' / 'rizzstack_rails'
    if not (canonical / '__main__.py').is_file():
        # Hermes plugin checkout: use the coordinator copy next to the plugin.
        if (vendor / '__main__.py').is_file() and str(vendor.parent) not in sys.path:
            sys.path.insert(0, str(vendor.parent))
        return
    if not vendor.is_dir():
        return  # Ordinary standalone wheel.

    # Both wheels use absolute rizzstack_rails imports. Never silently let the
    # first importer pick the worker or coordinator's version of a shared file.
    shared = {'__init__.py', 'contracts.py', 'ledger.py', 'privacy.py'}
    if ({p.name for p in vendor.glob('*.py')} != shared or
            any((vendor / f).read_bytes() != (canonical / f).read_bytes() for f in shared)):
        raise ImportError('Incompatible Rails runtimes: install matching BundleBees and Hyvv wheels or use separate environments')

    import rizzstack_rails

    origin = Path(rizzstack_rails.__file__).resolve().parent
    if origin == vendor:
        # Worker-first: preserve the already imported (identical) class and
        # module objects. Resolve all further imports only from the full wheel.
        rizzstack_rails.__path__[:] = [str(canonical)]
    elif origin != canonical:
        raise ImportError('Rails runtime did not resolve to either co-installed wheel')


_configure_rails()
