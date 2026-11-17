"""rizzstack_rails: shared stdlib-only Rails ledger runtime (Rails R02).

Origin: bundlebees/contrib/rizzstack_rails. This package is vendored
byte-identically into downstream contrib/rizzstack_rails copies by the
deterministic offline script scripts/vendor_rails.py with the pins in
vendor-manifest.json. Never reimplement or fetch it at runtime.
"""

__all__ = [
    "RailsLedger",
    "contracts",
    "ledger",
    "privacy",
]


def __getattr__(name):
    if name == "RailsLedger":
        from rizzstack_rails import ledger

        return ledger.RailsLedger
    if name in ("contracts", "ledger", "privacy"):
        import importlib

        return importlib.import_module("rizzstack_rails." + name)
    raise AttributeError("module 'rizzstack_rails' has no attribute %r" % (name,))
