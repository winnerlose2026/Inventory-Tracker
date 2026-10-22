"""Learned ship -> delivery transit times, per receiving location.

Until 2026-10-22 every estimated arrival was ``ship date + 7 days`` and the
Freight tab's "transit" chart measured that same guess back (arrival there WAS
ship + 7), so every location read ~7 days. The real numbers vary a lot: Bronx
(16 miles) took 3 days over a weekend, Zebulon took 10 in the Labor Day
capacity crunch, Houston took 3 (Fri pickup -> Mon 8:00 AM appointment).

A *sample* is one PO whose delivery Lineage (or the receiving DC) CONFIRMED --
a ``delivered`` record in data/po_delivery.json, each one tied to a quoted
email -- paired with the date it left Woodside: the Lineage freight invoice's
ship date (authoritative), else Lineage's pickup time, else the PO's own ship
date. Transit = delivered date - ship date, in calendar days, both read in the
local wall time of where they happened.

Per location (distributor + DC) the model keeps every sample and a *trend*:
the mean of the TREND_WINDOW most recent deliveries, so a lane that is
getting faster or slower shows up within a few loads. A location needs
MIN_SAMPLES deliveries before its trend replaces the configured default
(planning_config transit_days, 7) -- one odd load shouldn't move every
estimate.

Pure functions; callers pass the tracking file, PO metadata and the freight
index (blueprints/freight.py wires them up).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional

MIN_SAMPLES = 2
TREND_WINDOW = 5
MAX_TRANSIT_DAYS = 30          # longer is a data error (wrong PO / wrong year)
DEFAULT_TRANSIT_DAYS = 7

# arrival_source values that mean "an estimate made from the ship date" --
# safe to re-derive when the learned transit changes. Anything else (operator,
# lineage appointment, lineage-delivered, sibling-lines) is a real date.
DERIVED_SOURCES = ("ship+transit", "freight", "ship+7")


def _d(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s or "")[:10])
    except ValueError:
        return None


def location_key(distributor: str, warehouse: str) -> str:
    return f"{(distributor or '').strip()}|{(warehouse or '').strip()}"


def location_label(distributor: str, warehouse: str) -> str:
    """'Chicago, IL' exists for both US Foods and Chefs Warehouse."""
    wh = (warehouse or "").strip() or "Unassigned"
    if (distributor or "").strip() == "Chefs Warehouse":
        return f"{wh} (CW)"
    return wh


def collect_samples(tracking: dict, po_meta: dict, freight_idx: dict,
                    norm: Callable[[str], str], wall_time: Callable[[str], str]
                    ) -> tuple:
    """(samples, excluded) from confirmed deliveries.

    tracking    -- {norm_po: delivery record} (data/po_delivery.json)
    po_meta     -- {norm_po: {"distributor", "warehouse", "ship_date",
                   "ordered_at"}} from the PO ledger
    freight_idx -- {norm_po: "YYYY-MM-DD"} Lineage invoice ship dates
    """
    samples, excluded = [], []
    for key, rec in (tracking or {}).items():
        if (rec.get("status") or "") != "delivered" or not rec.get("delivered_at"):
            continue
        po = rec.get("po_number") or key
        meta = po_meta.get(key) or po_meta.get(norm(po)) or {}
        delivered_local = wall_time(rec.get("delivered_at") or "")
        dd = _d(delivered_local)
        ship, ship_src = "", ""
        if freight_idx.get(norm(po)):
            ship, ship_src = freight_idx[norm(po)][:10], "freight invoice"
        elif rec.get("pickup_at"):
            ship, ship_src = wall_time(rec["pickup_at"])[:10], "Lineage pickup"
        elif meta.get("ship_date"):
            ship, ship_src = str(meta["ship_date"])[:10], "PO ship date"
        sd = _d(ship)
        base = {"po_number": po, "distributor": meta.get("distributor", ""),
                "warehouse": meta.get("warehouse", ""),
                "location": location_label(meta.get("distributor", ""),
                                           meta.get("warehouse", "")),
                "ship_date": ship, "ship_source": ship_src,
                "delivered_at": delivered_local}
        if not meta.get("warehouse"):
            excluded.append({**base, "reason": "PO not in the ledger"})
            continue
        if dd is None or sd is None:
            excluded.append({**base, "reason": "no ship date"})
            continue
        days = (dd - sd).days
        if not 0 <= days <= MAX_TRANSIT_DAYS:
            excluded.append({**base, "transit_days": days,
                             "reason": f"transit {days} d is outside 0-{MAX_TRANSIT_DAYS}"})
            continue
        ev = next((e for e in reversed(rec.get("events") or [])
                   if e.get("event") == "delivered"), {})
        evid = ev.get("evidence") or {}
        od = _d(meta.get("ordered_at"))
        samples.append({**base, "transit_days": days,
                        "order_to_delivery_days": ((dd - od).days if od and dd >= od
                                                   else None),
                        "evidence": {"sender": evid.get("sender", ""),
                                     "sent": evid.get("sent", ""),
                                     "quote": (evid.get("quote") or "")[:200]}})
    samples.sort(key=lambda s: (s["delivered_at"], s["po_number"]))
    return samples, excluded


def _stats(vals: list) -> dict:
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "avg": None, "median": None, "min": None, "max": None}
    s = sorted(vals)
    n, mid = len(s), len(s) // 2
    med = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    return {"n": n, "avg": round(sum(s) / n, 1), "median": round(med, 1),
            "min": s[0], "max": s[-1]}


def build_model(samples: list, default_fn: Callable[[str], int]) -> dict:
    """{location_key: {...stats, trend_avg, used_days, basis, samples}}."""
    by: dict = {}
    for s in samples:
        by.setdefault(location_key(s["distributor"], s["warehouse"]), []).append(s)
    model = {}
    for key, ss in by.items():
        ss = sorted(ss, key=lambda s: s["delivered_at"])
        dist, wh = key.split("|", 1)
        transit = [s["transit_days"] for s in ss]
        recent = transit[-TREND_WINDOW:]
        trend = round(sum(recent) / len(recent), 1)
        default = int(default_fn(wh) or DEFAULT_TRANSIT_DAYS)
        learned = len(ss) >= MIN_SAMPLES
        used = max(1, int(trend + 0.5)) if learned else default   # half-up
        model[key] = {
            "distributor": dist, "warehouse": wh,
            "location": location_label(dist, wh),
            **{f"transit_{k}": v for k, v in _stats(transit).items()},
            "trend_avg": trend, "trend_window": len(recent),
            "order_to_delivery": _stats([s.get("order_to_delivery_days") for s in ss]),
            "default_days": default, "used_days": used, "learned": learned,
            "basis": (f"avg of last {len(recent)} deliveries" if learned else
                      f"{default}-day default ({len(ss)} delivery so far, "
                      f"need {MIN_SAMPLES})"),
            "last_delivered": ss[-1]["delivered_at"],
            "samples": [{k: s[k] for k in ("po_number", "ship_date", "ship_source",
                                           "delivered_at", "transit_days")}
                        for s in ss],
        }
    return model


def transit_for(model: dict, distributor: str, warehouse: str,
                default_fn: Callable[[str], int]) -> tuple:
    """(days, basis) to add to a ship date for this location."""
    m = (model or {}).get(location_key(distributor, warehouse))
    if m:
        return m["used_days"], m["basis"]
    d = int(default_fn(warehouse) or DEFAULT_TRANSIT_DAYS)
    return d, f"{d}-day default (no confirmed deliveries yet)"


def estimate_arrival(ship: str, distributor: str, warehouse: str, model: dict,
                     default_fn: Callable[[str], int]) -> tuple:
    """(arrival_iso, days, basis) for a ship date, or ("", 0, "") if unparseable."""
    sd = _d(ship)
    if sd is None:
        return "", 0, ""
    days, basis = transit_for(model, distributor, warehouse, default_fn)
    arr = datetime.combine(sd + timedelta(days=days), datetime.min.time())
    return arr.isoformat(), days, basis


def arrival_is_derived(entry: dict) -> bool:
    """Was this entry's arrival_date computed from its ship date (and so may be
    re-derived), rather than given by Lineage, the DC or an operator?"""
    src = (entry.get("arrival_source") or "").strip()
    if src in DERIVED_SOURCES:
        return True
    if src:
        return False
    # Legacy rows: an unlabelled arrival exactly ship + 7 was the old guess.
    sd, ad = _d(entry.get("ship_date")), _d(entry.get("arrival_date"))
    return bool(sd and ad and (ad - sd).days == DEFAULT_TRANSIT_DAYS)
