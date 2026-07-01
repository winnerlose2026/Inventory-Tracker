"""Weekly production planner (Phase 4) -- the capstone.

CORE FRAMING: H&H is the PRODUCER. An open purchase order is work to BAKE,
not incoming supply. So the primary output is the **production queue** -- the
active POs that have not yet been produced/shipped -- ranked top-4 and
US Foods/Cheney first and scheduled toward their pickup. A PO that already has
a ship date in the past (in transit) or has arrived is already produced and is
NOT in the queue; only in-transit/arrived cases are netted as supply for the
secondary buffer check.

Secondary: a buffer watch flags warehouses/pools projected below their cover
floor that DON'T already have an open PO in the queue -- i.e. depleting with
nothing ordered -- so proactive build-ahead can be planned.

Pure and side-effect free; the endpoint just serves the result.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta


def _ceil_pallet(cs: float, pallet: int) -> int:
    return int(math.ceil(cs / pallet) * pallet) if cs > 0 else 0


def _variety(name: str) -> str:
    name = name or ""
    return name.split(" Bagel")[0].strip() if " Bagel" in name else name


def _parse(iso_s):
    iso_s = (iso_s or "").strip()
    if not iso_s:
        return None
    try:
        return datetime.fromisoformat(iso_s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.fromisoformat(iso_s[:10])
        except ValueError:
            return None


def build_production_guide(inv: dict, ledger: list, demand_rows: list,
                           config: dict, toast_trend: dict = None,
                           now: datetime = None) -> dict:
    now = now or datetime.now()
    pallet = int(config.get("pallet_cs") or 56)
    cap = config.get("capacity") or {}
    dep_day = float(cap.get("dependable_cs_per_day") or 224)
    max_day = float(cap.get("max_cs_per_day") or 280)
    prod_days = len(cap.get("production_days") or [1, 2, 3, 4, 5]) or 5
    wk_dependable = dep_day * prod_days
    wk_max = max_day * prod_days
    prio = {d: i for i, d in enumerate(config.get("distributor_priority")
            or ["US Foods", "Cheney Brothers", "Chefs Warehouse"])}
    top4 = set(config.get("top4") or [])
    groups = config.get("transfer_groups") or {}
    wh_group = {m: g for g, members in groups.items() for m in members}
    transit_by_wh = {w.get("warehouse"): int(w.get("transit_days")
                     or config.get("transit_default_days") or 7)
                     for w in (config.get("warehouses") or [])}
    default_transit = int(config.get("transit_default_days") or 7)
    bd = config.get("warehouse_buffer_days") or {}
    top4_floor = float(bd.get("top4_floor") or 7)
    other_floor = float(bd.get("other_floor") or 5)

    def _shipped(po):
        """A PO is already produced+shipped when its ship_date is in the past
        (in transit) or it has arrived."""
        if (po.get("status") or "") == "arrived":
            return True
        sd = _parse(po.get("ship_date"))
        return bool(sd and sd <= now)

    # =====================================================================
    # 1) PRODUCTION QUEUE -- active POs not yet produced/shipped (bake-to-order)
    # =====================================================================
    queue = []
    queued_unit_variety = set()   # (unit, variety) already covered by an open PO
    for po in ledger or []:
        st = po.get("status") or ""
        if st in ("arrived", "canceled") or _shipped(po):
            continue
        dist = po.get("distributor") or ""
        wh = po.get("warehouse") or ""
        ship = (po.get("ship_date") or "").strip()
        transit = transit_by_wh.get(wh, default_transit)
        # Produce-by: ready the day before a scheduled ship; else as soon as
        # possible (no ship date set yet). Urgency key sorts dated ahead of ASAP.
        ship_dt = _parse(ship)
        if ship_dt:
            produce_by = (ship_dt - timedelta(days=1)).date().isoformat()
            urgency = ship_dt.date().isoformat()
        else:
            produce_by = None
            urgency = "ASAP"
        lines = [{"variety": L.get("variety") or "", "qty": float(L.get("qty") or 0),
                  "top4": (L.get("variety") in top4)} for L in (po.get("lines") or [])]
        total = round(sum(L["qty"] for L in lines) or float(po.get("total_cs") or 0), 1)
        unit = wh_group.get(wh, wh)
        for L in lines:
            queued_unit_variety.add((unit, L["variety"]))
        queue.append({
            "po_number": po.get("po_number") or "", "distributor": dist, "warehouse": wh,
            "transfer_group": po.get("transfer_group"), "status": st,
            "ship_date": ship or None, "produce_by": produce_by, "urgency": urgency,
            "transit_days": transit, "total_cs": total,
            "has_top4": any(L["top4"] for L in lines), "lines": lines,
        })

    def _q_key(q):
        return (0 if q["has_top4"] else 1, prio.get(q["distributor"], 9),
                q["urgency"] if q["urgency"] != "ASAP" else "0000-00-00",
                -q["total_cs"])
    queue.sort(key=_q_key)
    queue_cs = round(sum(q["total_cs"] for q in queue), 1)
    queue_top4_cs = round(sum(L["qty"] for q in queue for L in q["lines"] if L["top4"]), 1)

    # what to bake, aggregated by variety
    bake = {}
    for q in queue:
        for L in q["lines"]:
            b = bake.setdefault(L["variety"], {"variety": L["variety"], "top4": L["top4"], "cs": 0.0})
            b["cs"] += L["qty"]
    bake_by_variety = sorted(bake.values(), key=lambda b: (0 if b["top4"] else 1, -b["cs"]))
    for b in bake_by_variety:
        b["cs"] = round(b["cs"], 1)
        b["pallets"] = round(b["cs"] / pallet, 1)

    # =====================================================================
    # 2) CAPACITY -- the queue is the committed bake load this cycle
    # =====================================================================
    committed = queue_cs
    capacity = {
        "committed_cs": committed, "committed_pallets": round(committed / pallet, 1),
        "weekly_dependable_cs": wk_dependable, "weekly_max_cs": wk_max,
        "utilization_pct_of_dependable": round(committed / wk_dependable * 100, 0) if wk_dependable else None,
        "feasible_on_normal_week": committed <= wk_dependable,
        "needs_surge": committed > wk_dependable,
        "spare_for_buildahead_cs_max": round(max(0.0, wk_max - committed), 0),
        "note": ("Fits a normal Mon-Fri week." if committed <= wk_dependable
                 else "Exceeds dependable capacity -- use the 5th pallet / weekend or expansion levers."),
    }

    # =====================================================================
    # 3) BUFFER WATCH (secondary) -- warehouses/pools depleting with NO open PO.
    #    Nets ONLY shipped/in-transit incoming (already produced), not the queue.
    # =====================================================================
    onhand, dist_of = {}, {}
    for key, item in (inv or {}).items():
        wh = item.get("warehouse") or ""
        var = _variety(item.get("name") or key)
        onhand[(wh, var)] = onhand.get((wh, var), 0.0) + float(item.get("quantity") or 0)
        dist_of[wh] = item.get("distributor") or dist_of.get(wh, "")
    rate_of = {(r["warehouse"], r["variety"]): float(r.get("rate_cs_per_day") or 0) for r in demand_rows or []}
    stale_of = {(r["warehouse"], r["variety"]): r.get("stale", True) for r in demand_rows or []}
    shipped_incoming = {}
    for po in ledger or []:
        if not _shipped(po) or (po.get("status") or "") == "canceled":
            continue
        if (po.get("status") or "") == "arrived":
            continue   # already in on-hand
        wh = po.get("warehouse") or ""
        for L in (po.get("lines") or []):
            shipped_incoming[(wh, L.get("variety") or "")] = \
                shipped_incoming.get((wh, L.get("variety") or ""), 0.0) + float(L.get("qty") or 0)

    units = {}
    for (wh, var), oh in onhand.items():
        unit = wh_group.get(wh, wh)
        u = units.setdefault((unit, var), {"unit": unit, "is_pool": unit in groups,
            "distributor": dist_of.get(wh, ""), "variety": var, "on_hand": 0.0,
            "rate": 0.0, "incoming": 0.0, "transit": default_transit, "stale": False})
        u["on_hand"] += oh
        u["rate"] += rate_of.get((wh, var), 0.0)
        u["incoming"] += shipped_incoming.get((wh, var), 0.0)
        u["transit"] = max(u["transit"], transit_by_wh.get(wh, default_transit))
        if stale_of.get((wh, var), True):
            u["stale"] = True
        if not u["distributor"]:
            u["distributor"] = dist_of.get(wh, "")

    buffer_watch = []
    for (unit, var), u in units.items():
        if (unit, var) in queued_unit_variety:
            continue   # already has an open PO in the queue -- don't double-flag
        rate = u["rate"]
        if rate <= 0:
            continue   # no demand signal -> not a buffer risk
        is_top4 = var in top4
        floor_d = top4_floor if is_top4 else other_floor
        cover = (u["on_hand"] + u["incoming"]) / rate
        if cover < (floor_d + u["transit"]):
            buffer_watch.append({
                "unit": unit, "is_pool": u["is_pool"], "distributor": u["distributor"],
                "variety": var, "top4": is_top4, "on_hand": round(u["on_hand"], 1),
                "shipped_incoming_cs": round(u["incoming"], 1),
                "rate_cs_per_day": round(rate, 3), "cover_days": round(cover, 1),
                "floor_days": floor_d, "transit_days": u["transit"],
                "confidence": "low" if u["stale"] else "high",
                "note": "Depleting and no open PO -- order/produce proactively.",
            })
    buffer_watch.sort(key=lambda w: (0 if w["top4"] else 1, prio.get(w["distributor"], 9), w["cover_days"]))

    # =====================================================================
    # 4) Toast leading indicator
    # =====================================================================
    tt = toast_trend or {}
    toast_note = None
    if tt.get("available"):
        d = tt.get("direction")
        toast_note = (f"Retail bagel demand {d} ({tt.get('pct_change')}% vs prior "
                      f"{tt.get('window_days')}d) -- "
                      + ("distributor reorders likely to rise; pull build-ahead forward."
                         if d == "rising" else "no action from Toast."))

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "produce_now_pos": len(queue),
            "produce_now_cs": queue_cs,
            "produce_now_top4_cs": queue_top4_cs,
            "produce_now_pallets": round(queue_cs / pallet, 1),
            "buffer_watch": len(buffer_watch),
        },
        "capacity": capacity,
        "bake_by_variety": bake_by_variety,
        "production_queue": queue,
        "buffer_watch": buffer_watch,
        "buildahead": {
            "freezer_pallet_cap": config.get("freezer_pallet_cap"),
            "target_days_top4": config.get("internal_buffer_target_days_top4"),
            "spare_capacity_pallets_max": round(max(0.0, wk_max - committed) / pallet, 1),
            "note": "After the queue, spend spare capacity on top-4 build-ahead within the freezer cap.",
        },
        "toast_note": toast_note,
    }
