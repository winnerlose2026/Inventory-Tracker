"""In-process aggregation cache shared across routes/blueprints.

Keyed on source-file mtimes so the Inventory/Freight aggregations aren't
recomputed on every poll; any write to a source file invalidates the entry.
No app/blueprint import.
"""

# {key: (signature_tuple, result)}. Item-assignment only — never rebind this name.
_AGG_CACHE: dict = {}


def _data_sig(*names):
    """mtime signature for the given data files; a change invalidates the cache."""
    from inventory_tracker import DATA_DIR
    sig = []
    for n in names:
        try:
            sig.append((DATA_DIR / n).stat().st_mtime_ns)
        except OSError:
            sig.append(0)
    return tuple(sig)
