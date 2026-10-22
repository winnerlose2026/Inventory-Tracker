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


def row_superseded(e: dict) -> bool:
    """Has a newer copy of this row's PO retired it?

    ``sync_inventory._reverse_po_entries`` stamps ``superseded_by_revision``
    with the NEW document's revision -- which is "" for every Cheney PO (they
    carry no revision number) -- and ``superseded_at`` with the time. Testing
    the revision tag's truthiness left a retired Cheney row LIVE: PO
    014511743693 (Riviera Beach) was re-applied 2026-08-28, its 08-28 arrival
    rows kept reading as receipts next to the 09-05 re-arrival, and the ledger
    showed 336 cs for a 168 cs PO. Either stamp retires the row.
    """
    return bool(e.get("superseded_by_revision") or e.get("superseded_at"))


def is_po_reversal_row(e: dict) -> bool:
    """Is this the audit row _reverse_po_entries appends when a newer PO copy
    takes an arrival back out of on-hand? Identified by the KEY, because the
    value is the reversed row's revision -- "" on every Cheney PO, which read
    as "not a reversal" and let the +cs audit row count as a booked receipt."""
    return "reversal_of_revision" in e


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
    return not e.get("reversed") and not row_superseded(e)


def absorbed_row_live(e: dict) -> bool:
    """Is this usage row a LIVE on_order_absorbed record?

    An absorbed row is a PO line whose arrival a physical count already
    contained, so it never moved on-hand (amount 0). It is still the record
    that the PO arrived -- every PO reader that lists arrived POs must show
    it, or the PO vanishes from the Pending POs tab altogether (Houston
    393072B2, 2026-10-20: 1,120 cs absorbed, no ledger row at all).
    Retired the same two ways as a rollover row.
    """
    if (e.get("source") or "") != "on_order_absorbed":
        return False
    return not e.get("reversed") and not row_superseded(e)


def absorbed_row_qty(e: dict) -> float:
    """Cases an absorbed row stands for (it carries amount 0)."""
    import re
    if e.get("absorbed_qty") is not None:
        try:
            return abs(float(e.get("absorbed_qty") or 0))
        except (TypeError, ValueError):
            pass
    m = re.search(r"(\d+(?:\.\d+)?) cs (?:already on hand|not re-added)",
                  e.get("note") or "")
    return float(m.group(1)) if m else 0.0


# ---------------------------------------------------------------------------
# Is a receipt inside a count?
# ---------------------------------------------------------------------------
# The base rule is by calendar day: a delivery that ARRIVED on or before a
# count's date is inside that count. Two refinements, both only for a delivery
# Lineage / the DC CONFIRMED (its arrival time is real, not an ETA guess):
#
#   1. Exact time. A count stamped before the confirmed delivery time cannot
#      contain it, even on the same day.
#   2. Count missed it. A distributor's on-hand screen lags the dock: USF
#      Houston received 393072B2 at 8:00 AM CT on 10/19, and Tom's 11:20 AM
#      screenshot still listed all 1,120 cs under "Order" with Hand at 404 cs.
#      When a count lands shortly after a confirmed delivery but its figures
#      are physically too low to contain it, the receipt row is stamped
#      ``not_in_count_at`` = that count, and re-reads of that same count keep
#      the receipt on top. The NEXT count absorbs it as usual.

CONFIRMED_DELIVERY_WINDOW_HOURS = 72.0


def naive_local_dt(raw):
    """Parse an ISO stamp to the naive server-local datetime the data files use
    (same convention as inventory_tracker._parse_sane_dt). None if unparseable."""
    from datetime import datetime
    s = str(raw or "").strip()
    if not s:
        return None
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def receipt_inside_count(row: dict, count_at: str, arrival_day: str) -> bool:
    """Is this receipt row already inside the count stamped ``count_at``?

    ``arrival_day`` is the row's arrival date (YYYY-MM-DD), resolved by the
    caller (sync_inventory._rollover_arrival handles legacy rows). An empty
    ``count_at`` means never counted -> False.
    """
    count_at = str(count_at or "")
    cd = count_at[:10]
    if not cd or not arrival_day:
        return False
    nic = row.get("not_in_count_at") or ""
    if nic:
        c, n = naive_local_dt(count_at), naive_local_dt(nic)
        if c is not None and n is not None and c <= n:
            return False
    # The exact-time rule needs a REAL delivery time; a date-only Lineage
    # confirmation (delivery_time_known False) is judged by day, with
    # not_in_count_at (the quantity check) as the only override.
    if (row.get("delivery_confirmed") and row.get("delivery_time_known") is not False
            and "T" in count_at):
        a, c = naive_local_dt(row.get("arrival_date")), naive_local_dt(count_at)
        if a is not None and c is not None:
            return a <= c
    return arrival_day <= cd


def count_missed_delivery(lines: list, hours_after: float) -> "tuple[bool, str]":
    """Do a count's figures prove it does NOT contain a confirmed delivery?

    ``lines`` -- one dict per PO line at ONE warehouse, all read from the same
    count: {"receipt": cs delivered, "counted": cs the count reported,
    "weekly_usage": cs/wk}. ``hours_after`` -- hours from delivery to count.

    If the count contained the delivery, the counted total is at least the
    delivered total less what could have been used since it landed. Allow
    double the usual burn plus 5% (min 2 cs) of slack; below that the count
    physically cannot include the receipt. Only ever decides "missed" -- it
    never removes a receipt -- and callers only consult it inside
    CONFIRMED_DELIVERY_WINDOW_HOURS of a confirmed delivery.
    """
    if not lines:
        return False, ""
    hrs = max(0.0, float(hours_after or 0))
    receipt = sum(float(l.get("receipt") or 0) for l in lines)
    counted = sum(float(l.get("counted") or 0) for l in lines)
    burn = sum(float(l.get("weekly_usage") or 0) / 7.0 * hrs / 24.0 for l in lines)
    slack = max(2.0, 0.05 * receipt)
    if receipt > 0 and counted + 2.0 * burn + slack < receipt:
        return True, (f"count shows {counted:g} cs across the PO's SKUs, "
                      f"{hrs:.1f} h after {receipt:g} cs were delivered "
                      f"-- it cannot contain the delivery")
    return False, ""


def with_data_lock(fn):
    """Run a Flask view (or any function) inside inventory_tracker.data_lock().

    Every endpoint that loads, edits and saves inventory.json / usage.json is
    a read-modify-write across gunicorn's 2 workers x 4 threads; os.replace()
    keeps the file whole but two overlapping cycles still lose one of them.
    The lock is per-thread re-entrant, so the load_inventory() inside the view
    taking it again is fine.
    """
    import functools

    @functools.wraps(fn)
    def _inner(*args, **kwargs):
        from inventory_tracker import data_lock
        with data_lock():
            return fn(*args, **kwargs)
    return _inner
