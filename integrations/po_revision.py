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

The rule
--------
Ordering is by the SOURCE EMAIL'S RECEIVED TIME, with one guard:

1. If both tokens are numeric and differ, the higher number wins regardless of
   date. Numeric revisions are monotonic and authoritative, and this keeps a
   stale forward (JD re-sending an old PO copy, which lands with a brand-new
   received time) from clobbering a newer revision.
2. Otherwise -- either side non-numeric ("REVISED"/"REPRINT"), or equal
   numbers -- the later received time wins. That is what makes a genuine
   REVISED/REPRINT amendment supersede an earlier numbered revision.
3. No usable dates on either side: fall back to the old numeric comparison so
   behaviour is unchanged for historical entries that predate the
   `source_received_at` field.

This is a comparison PREDICATE rather than a sort key on purpose: rule 1 is
conditional on both sides being numeric, which no single scalar key expresses.
"""
from __future__ import annotations

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


def is_newer(new_rev, new_received, old_rev, old_received) -> bool:
    """True if the incoming PO document supersedes the stored one.

    Ties resolve to False (not newer) so replays stay idempotent.
    """
    n_num, o_num = is_numeric_rev(new_rev), is_numeric_rev(old_rev)
    n_int, o_int = rev_int(new_rev), rev_int(old_rev)

    # Rule 1 -- both numbered and different: trust the numbers.
    if n_num and o_num and n_int != o_int:
        return n_int > o_int

    # Rule 2 -- date decides.
    n_dt, o_dt = parse_dt(new_received), parse_dt(old_received)
    if n_dt is not None and o_dt is not None:
        if n_dt != o_dt:
            return n_dt > o_dt
        return False                      # same instant -> duplicate
    if n_dt is not None and o_dt is None:
        # Stored row predates the source_received_at field, so it is
        # necessarily an older ingest. A dated document supersedes it. If BOTH
        # are numbered we only reach here with equal numbers (rule 1 handled
        # unequal), which is a duplicate, not an amendment.
        return not (n_num and o_num)
    if n_dt is None and o_dt is not None:
        return False                      # undated can't beat a dated doc

    # Rule 3 -- no dates at all: legacy numeric behaviour.
    return n_int > o_int


__all__ = ["is_newer", "rev_int", "is_numeric_rev", "parse_dt"]
