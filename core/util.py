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


def rollover_row_live(e: dict) -> bool:
    """Is this usage row a LIVE on_order_rollover receipt?

    A rollover row can be retired two different ways and, until 2026-08-28,
    only one of them was ever checked:

      * ``reversed``  -- set by /api/pending/reopen and the usage-reversal
        path. Honoured everywhere.
      * ``superseded_by_revision`` -- set by
        ``sync_inventory._reverse_po_entries`` when a newer revision replaces
        the PO. Honoured NOWHERE, so every reader counted a superseded
        arrival as if it were still live.

    The visible symptom was PO 2753463T rendering SIX lines and 336 cs in the
    Pending POs tab for a three-line, 168 cs PO -- the 08-23 arrival and the
    08-28 replacement both counted. The dangerous one was silent: reopen would
    have un-rolled both copies, subtracting the cases twice.

    One predicate, so a row retired by either route is retired for everyone.
    """
    if (e.get("source") or "") != "on_order_rollover":
        return False
    return not e.get("reversed") and not e.get("superseded_by_revision")
