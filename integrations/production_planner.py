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
    from core.util import business_now
    now = now or business_now()
    pallet = int(config.get("pallet_cs") or 56)
    cap = config.get("capacity") or {}
    from integrations.planning_config import (daily_capacity_cs,
                                              production_weekdays)
    dep_day = float(cap.get("dependable_cs_per_day") or 224)
    # ONE daily cap for the whole page: the load table and bake schedule use
    # capacity.max_cs_per_day (6 pallets/day); this card used to judge the
    # same queue against 4 pallets/day and fall back to 280.
    max_day = daily_capacity_cs(config)
    prod_days = len(production_weekdays(config)) or 5
    wk_dependable = dep_day * prod_days
    wk_max = max_day * prod_days
    today = now.date()
    excl_cats = set(((config.get("ordering") or {}).get("exclude_categories")) or [])
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

    from integrations.po_shipping import shipped_verdict

    def _shipped(po):
        """See integrations.po_shipping -- the Pending POs tab uses the same
        rule, so a PO is never "In Transit" there and LATE here."""
        return shipped_verdict(po, today)["shipped"]

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
        late_days = 0
        if ship_dt:
            produce_by = (ship_dt - timedelta(days=1)).date().isoformat()
            urgency = ship_dt.date().isoformat()
            # Ship date gone by and still not baked/verified shipped: LATE.
            if ship_dt.date() <= today:
                late_days = (today - ship_dt.date()).days + 1
        else:
            produce_by = None
            urgency = "ASAP"
        lines = [{"variety": L.get("variety") or "", "qty": float(L.get("qty") or 0),
                  "top4": (L.get("variety") in top4)} for L in (po.get("lines") or [])]
        unit = wh_group.get(wh, wh)
        for L in lines:
            queued_unit_variety.add((unit, L["variety"]))
        # Net out what the bakery already BAKED for this PO (Daily Production
        # sheets, per variety; the ledger leaves canceled POs' bakes out).
        # A PO baked but not yet shipped sat in the queue for a median 3 days
        # -- Houston 305202B2's 1,120 cs for 11 -- inflating committed cs.
        baked = dict(((po.get("production") or {}).get("by_variety")) or {})
        ordered_cs = sum(L["qty"] for L in lines)
        baked_cs = 0.0
        for L in lines:
            take = min(L["qty"], max(0.0, float(baked.get(L["variety"]) or 0)))
            if take:
                baked[L["variety"]] = float(baked.get(L["variety"]) or 0) - take
                baked_cs += take
                L["baked"] = round(take, 1)
                L["qty"] = round(L["qty"] - take, 1)
        if lines and baked_cs and not any(L["qty"] > 0 for L in lines):
            continue   # fully baked: waiting to ship, nothing left to bake
        lines = [L for L in lines if L["qty"] > 0] if baked_cs else lines
        total = round(sum(L["qty"] for L in lines) or
                      (0.0 if baked_cs else float(po.get("total_cs") or 0)), 1)
        queue.append({
            "ordered_cs": round(ordered_cs, 1), "baked_cs": round(baked_cs, 1),
            "po_number": po.get("po_number") or "", "distributor": dist, "warehouse": wh,
            "transfer_group": po.get("transfer_group"), "status": st,
            "late": late_days > 0, "late_days": late_days,
            "ship_date_source": po.get("ship_date_source") or "",
            "arrival_date": (po.get("arrival_date") or po.get("eta") or ""),
            "ship_date": ship or None, "produce_by": produce_by, "urgency": urgency,
            "transit_days": transit, "total_cs": total,
            "has_top4": any(L["top4"] for L in lines), "lines": lines,
        })

    def _q_key(q):
        return (0 if q["has_top4"] else 1, prio.get(q["distributor"], 9),
                q["urgency"] if q["urgency"] != "ASAP" else "0000-00-00",
                -q["total_cs"])
    queue.sort(key=_q_key)
    queue.sort(key=lambda q: 0 if q["late"] else 1)     # stable: late first
    late_pos = [q["po_number"] for q in queue if q["late"]]
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
    pallets_day = round(max_day / pallet, 1)
    capacity = {
        "committed_cs": committed, "committed_pallets": round(committed / pallet, 1),
        "weekly_dependable_cs": wk_dependable, "weekly_max_cs": wk_max,
        # The figures the card shows -- same cap as the load table below.
        "capacity_cs_per_day": max_day, "capacity_pallets_per_day": pallets_day,
        "weekly_capacity_cs": wk_max, "production_days_per_week": prod_days,
        "utilization_pct": round(committed / wk_max * 100, 0) if wk_max else None,
        "utilization_pct_of_dependable": round(committed / wk_dependable * 100, 0) if wk_dependable else None,
        "feasible_on_normal_week": committed <= wk_max,
        "needs_surge": committed > wk_max,
        "spare_for_buildahead_cs_max": round(max(0.0, wk_max - committed), 0),
        "note": (f"Fits one week at {pallets_day:g} pallets/day."
                 if committed <= wk_max
                 else f"More than one week of baking at {pallets_day:g} pallets/day "
                      "-- see the load table for which weeks run over."),
    }

    # =====================================================================
    # 3) BUFFER WATCH (secondary) -- warehouses/pools depleting with NO open PO.
    #    Nets ONLY shipped/in-transit incoming (already produced), not the queue.
    # =====================================================================
    onhand, dist_of = {}, {}
    for key, item in (inv or {}).items():
        # Hidden SKUs and finished LTOs are not a build-ahead risk.
        if item.get("hidden") or (item.get("category") or "") in excl_cats:
            continue
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
        # Only POs for a warehouse we hold stock for count toward its cover.
        # Chefs Warehouse's Chicago DC is ALSO labelled "Chicago, IL", so a
        # shipped CW Chicago PO was credited to US Foods Chicago and hid its
        # buffer flag; CW is tracked PO-only (no on-hand), so it never belongs
        # here.
        if dist_of.get(wh) and (po.get("distributor") or "") \
                and (po.get("distributor") or "") != dist_of.get(wh):
            continue
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

    # Open (still to bake) PO cases per (unit, variety) with the day each is
    # expected to land: arrival/ETA, else ship date + transit, else unknown.
    open_incoming: dict = {}
    for q in queue:
        unit = wh_group.get(q["warehouse"], q["warehouse"])
        arr = _parse(q.get("arrival_date"))
        if arr is None and q.get("ship_date"):
            sd = _parse(q["ship_date"])
            arr = sd + timedelta(days=q["transit_days"]) if sd else None
        for L in q["lines"]:
            open_incoming.setdefault((unit, L["variety"]), []).append(
                (arr.date() if arr else None, float(L["qty"] or 0) + float(L.get("baked") or 0)))

    buffer_watch = []
    for (unit, var), u in units.items():
        rate = u["rate"]
        if rate <= 0:
            continue   # no demand signal -> not a buffer risk
        is_top4 = var in top4
        floor_d = top4_floor if is_top4 else other_floor
        need_days = floor_d + u["transit"]
        cover = (u["on_hand"] + u["incoming"]) / rate
        if cover >= need_days:
            continue
        # An open PO used to hide the flag outright. It only does so when the
        # cases landing before the cover-floor date actually close the gap
        # (a PO with no arrival estimate is given the benefit of the doubt).
        floor_date = today + timedelta(days=int(math.ceil(need_days)))
        opens = open_incoming.get((unit, var)) or []
        in_time = sum(q for (d, q) in opens if d is None or d <= floor_date)
        open_total = sum(q for (_d, q) in opens)
        if opens and (u["on_hand"] + u["incoming"] + in_time) / rate >= need_days:
            continue
        buffer_watch.append({
            "unit": unit, "is_pool": u["is_pool"], "distributor": u["distributor"],
            "variety": var, "top4": is_top4, "on_hand": round(u["on_hand"], 1),
            "shipped_incoming_cs": round(u["incoming"], 1),
            "rate_cs_per_day": round(rate, 3), "cover_days": round(cover, 1),
            "floor_days": floor_d, "transit_days": u["transit"],
            "confidence": "low" if u["stale"] else "high",
            "open_po_cs": round(open_total, 1),
            "open_po_in_time_cs": round(in_time, 1),
            "cover_floor_date": floor_date.isoformat(),
            "note": ("Depleting and no open PO -- order/produce proactively."
                     if not opens else
                     f"Open PO ({open_total:g} cs) will not cover it by "
                     f"{floor_date.isoformat()} -- only {in_time:g} cs land in time."),
        })
    buffer_watch.sort(key=lambda w: (0 if w["top4"] else 1, prio.get(w["distributor"], 9), w["cover_days"]))

    # =====================================================================
    # 4) Toast leading indicator
    # =====================================================================
    tt = toast_trend or {}
    toast_note = None
    if tt.get("available"):
        d = tt.get("direction")
        pct = tt.get("pct_change")
        sign = "+" if (pct or 0) > 0 else ""
        toast_note = (f"Retail bagel demand {d} ({sign}{pct}% vs prior "
                      f"{tt.get('window_days')}d; as of {tt.get('as_of') or tt.get('anchored_to')}, "
                      f"{tt.get('stores')} stores, same stores and days compared) -- "
                      + ("distributor reorders likely to rise; pull build-ahead forward."
                         if d == "rising" else "no action from Toast."))
    elif tt.get("reason") and tt.get("matched_location_days") is not None:
        toast_note = f"Toast trend not shown: {tt['reason']}."

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "produce_now_pos": len(queue),
            "produce_now_cs": queue_cs,
            "produce_now_top4_cs": queue_top4_cs,
            "produce_now_pallets": round(queue_cs / pallet, 1),
            "late_pos": len(late_pos),
            "buffer_watch": len(buffer_watch),
        },
        "capacity": capacity,
        "bake_by_variety": bake_by_variety,
        "production_queue": queue,
        "late_pos": late_pos,
        "buffer_watch": buffer_watch,
        "buildahead": {
            "freezer_pallet_cap": config.get("freezer_pallet_cap"),
            "target_days_top4": config.get("internal_buffer_target_days_top4"),
            "spare_capacity_pallets_max": round(max(0.0, wk_max - committed) / pallet, 1),
            "note": "After the queue, spend spare capacity on top-4 build-ahead within the freezer cap.",
        },
        "toast_note": toast_note,
    }
