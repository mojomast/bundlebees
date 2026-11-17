"""bundlebees Python package."""

# Hermes installs the plugin tree; wheels install the same runtime as a package.
import sys
from pathlib import Path

_vendor = Path(__file__).resolve().parent.parent / 'contrib'
if (_vendor / 'rizzstack_rails').is_dir() and str(_vendor) not in sys.path:
    sys.path.insert(0, str(_vendor))
