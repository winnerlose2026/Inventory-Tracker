"""One answer to "has this PO really left the bakery?" -- shared by the
Production Planner and the Pending POs tab.

Before 2026-11-13 the two tabs judged it differently: the Planner kept a PO
whose ship date had passed un-baked in the bake queue as LATE, while the
Pending POs tab called the same PO "In Transit" because its ship date had
gone by. Both now read ``shipped_verdict``; the ledger carries it on every
row as ``shipping`` so the browser does not re-derive it.

A ship date that has merely gone by is NOT proof. A PO counts as shipped
only when
  * it has arrived, or an operator set it In Transit by hand;
  * the ship date is verified (freight invoice / Lineage pickup);
  * the Daily Production sheets show it fully baked; or
  * the status says in transit and there is no ship date to doubt.
Dates are the bakery's (America/New_York) calendar.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional


def _day(iso_s) -> Optional[date]:
    s = (iso_s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def baked_vs_ordered(po: dict) -> tuple:
    ordered = sum(float(L.get("qty") or 0) for L in (po.get("lines") or [])) \
        or float(po.get("total_cs") or 0)
    baked = float(((po.get("production") or {}).get("baked_cs")) or 0)
    return baked, ordered


def shipped_verdict(po: dict, today: Optional[date] = None) -> dict:
    """{shipped, late, late_days, basis} for one ledger row.

    ``basis`` says why: arrived | operator | freight | lineage | baked |
    status | not_due | unverified | canceled. ``late`` = the ship date has
    gone by and nothing above proves it shipped."""
    if today is None:
        from core.util import business_today
        today = business_today()
    st = (po.get("status") or "")
    out = {"shipped": False, "late": False, "late_days": 0, "basis": ""}
    if st in ("canceled", "cancelled"):
        out["basis"] = "canceled"
        return out
    if st == "arrived":
        return {**out, "shipped": True, "basis": "arrived"}
    if (po.get("override") or "") == "in_transit":
        return {**out, "shipped": True, "basis": "operator"}
    ship = _day(po.get("ship_date"))
    if ship is None:
        return {**out, "shipped": st == "in_transit",
                "basis": "status" if st == "in_transit" else "not_due"}
    if ship > today:
        return {**out, "basis": "not_due"}
    src = (po.get("ship_date_source") or "")
    if src in ("freight", "lineage"):
        return {**out, "shipped": True, "basis": src}
    baked, ordered = baked_vs_ordered(po)
    if ordered > 0 and baked >= ordered - 1e-9:
        return {**out, "shipped": True, "basis": "baked"}
    return {**out, "late": True, "late_days": (today - ship).days + 1,
            "basis": "unverified"}
