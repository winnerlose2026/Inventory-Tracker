"""Small cross-cutting helpers shared across routes/blueprints (no app import)."""


def _norm_po_key(s: str) -> str:
    """Normalize a PO / reference token for cross-source matching.

    Uppercases, trims, drops a leading ``HHB-`` / ``HHB `` shipper prefix,
    and strips trailing punctuation. Leading zeros are preserved on
    purpose — Cheney PO numbers like ``014511...`` vs ``054511...`` differ
    only in those digits (the 2nd digit encodes the destination DC).
    """
    t = str(s or "").strip().upper()
    if t.startswith("HHB-") or t.startswith("HHB "):
        t = t[4:]
    return t.strip().strip(".").strip()
