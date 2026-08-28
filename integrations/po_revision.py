"""Ordering two documents of the SAME purchase order.

Why this isn't just `int(revision)`
-----------------------------------
Per JD (2026-08-03): US Foods occasionally amends a PO **without changing the
PO number** -- they re-send it labelled "REVISED" or "REPRINT" instead of
bumping the numeric revision. So the revision token alone cannot order two
copies of a PO, and the previous rule ("any non-numeric token sorts after every
integer") silently booked stale quantities:

    07-30 15:57  305202B2 REPRINT   Plain = 176   <- pre-amendment snapshot
    07-30 19:05  305202B2 0000003   Plain = 168   <- Foley's actual correction

REPRINT won on the sentinel, so Houston booked 1128 cs against a 1120 cs PO.

Why "higher revision number wins" is ALSO wrong
----------------------------------------------
The first fix here promoted the numeric revision above the date, on the theory
that "numeric revisions are monotonic and authoritative." Two real POs proved
they are not -- US Foods re-numbers DOWNWARD when a buyer re-cuts an order:

    08-25 17:03  505876B2  rev 0000006   Plain 32 / WW-Everything 32, no PRNKL
    08-25 18:20  505876B2  rev 0000003   Foley's correction: Everything, +8 PRNKL

    08-18 14:58  4537584C  rev 0000006   original
    08-25 20:21  4537584C  rev 0000001   re-cut a week later

Under "higher number wins" the corrections were rejected as stale, and Houston
sat at 104 cs against a 112 cs PO -- missing the pumpernickel the PO existed to
order -- while still carrying a Whole Wheat Everything line the buyer removed.

The rule
--------
Ordering is by the SOURCE EMAIL'S RECEIVED TIME, with one guard:

1. SENDER guard. A copy that reaches us from our OWN domain (someone
   forwarding a PO PDF into JD@ / info@) can never supersede a copy sent by
   the distributor. This is the stale-forward protection that rule 1 used to
   provide -- Greene's 305202B2 REPRINT landing on top of Foley's correction --
   expressed against the thing that actually distinguishes the two cases. A
   corrective PO always arrives FROM the vendor; a stale replay arrives from
   us. Unknown/blank senders (legacy rows) are not treated as internal.
2. Otherwise the later received time wins. `source_received_at` is a property
   of the DOCUMENT, not of when we happened to read it, so this stays correct
   under wide-lookback backfills and re-scans.
3. Same instant, or no usable dates: fall back to the numeric comparison, so
   behaviour is unchanged for historical entries that predate the
   `source_received_at` field. The revision token is a TIE-BREAK now, never an
   override.

This is a comparison PREDICATE rather than a sort key on purpose: rule 1 is
conditional on the two senders, which no single scalar key expresses.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


def rev_int(s) -> int:
    """Numeric value of a revision token; 0 for empty/non-numeric.

    NOTE: unlike the old `_po_rev_int`, a non-numeric token is NOT promoted to
    a 'latest' sentinel -- that promotion was the bug. Non-numeric tokens are
    ordered by date instead (see `is_newer`).
    """
    if s is None:
        return 0
    raw = str(s).strip()
    if not raw:
        return 0
    try:
        return int(raw.lstrip("0") or "0")
    except (ValueError, TypeError):
        return 0


def is_numeric_rev(s) -> bool:
    if s is None:
        return False
    raw = str(s).strip()
    if not raw:
        return False
    try:
        int(raw.lstrip("0") or "0")
        return True
    except (ValueError, TypeError):
        return False


def parse_dt(s) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (with or without 'Z'/offset) to aware UTC."""
    if not s:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Bare date, or something we don't understand.
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Mail domains that are US, not a distributor. A PO document arriving from one
# of these is a forward/replay of something the vendor already sent, so it must
# not outrank the vendor's own copy no matter how recently it landed.
INTERNAL_DOMAINS = ("hhbagels.com",)


def _addr(sender) -> str:
    """Bare lowercase email address out of a From: header (or '')."""
    if not sender:
        return ""
    raw = str(sender).strip().lower()
    m = re.search(r"[\w.\-+]+@[\w.\-]+", raw)
    return m.group(0) if m else ""


def is_internal_sender(sender) -> bool:
    """True if this PO copy came from our own mail domain (a forward)."""
    addr = _addr(sender)
    if not addr:
        return False              # unknown != internal; legacy rows have none
    domain = addr.rsplit("@", 1)[-1]
    return any(domain == d or domain.endswith("." + d)
               for d in INTERNAL_DOMAINS)


def is_newer(new_rev, new_received, old_rev, old_received,
             new_sender="", old_sender="") -> bool:
    """True if the incoming PO document supersedes the stored one.

    Ties resolve to False (not newer) so replays stay idempotent.

    ``new_sender`` / ``old_sender`` are From: headers (or bare addresses) and
    are optional -- callers that don't have them get pure date ordering, which
    is the pre-existing behaviour for everything except internal forwards.
    """
    # Rule 1 -- an internal forward never beats the vendor's own copy.
    if is_internal_sender(new_sender) and not is_internal_sender(old_sender) \
            and _addr(old_sender):
        return False

    # Rule 2 -- date decides.
    n_dt, o_dt = parse_dt(new_received), parse_dt(old_received)
    if n_dt is not None and o_dt is not None:
        if n_dt != o_dt:
            return n_dt > o_dt
        # Same instant: the same document delivered to both mailboxes, or a
        # re-read. Fall through to the numeric tie-break below.
    elif n_dt is not None and o_dt is None:
        # Stored row predates the source_received_at field, so it is
        # necessarily an older ingest. A dated document supersedes it.
        return True
    elif n_dt is None and o_dt is not None:
        return False              # undated can't beat a dated doc

    # Rule 3 -- tie-break / legacy: numeric revision.
    return rev_int(new_rev) > rev_int(old_rev)


__all__ = ["is_newer", "rev_int", "is_numeric_rev", "parse_dt",
           "is_internal_sender", "INTERNAL_DOMAINS"]
