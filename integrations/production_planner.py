"""Weekly production planner (Phase 4) -- the capstone.

Pure function over the foundation layers built in Phases 1-3:
  * inventory (on-hand)            -> depletion
  * PO ledger (incoming)           -> refill before stock-out
  * demand model (cases/day)       -> depletion rate + freshness
  * planning_config (reference)    -> transit, buffers, capacity, priority,
                                      and the Cheney-FL TRANSFER POOL

For each planning unit (a standalone warehouse, or a transfer pool judged as
ONE unit) x variety it computes cover, nets incoming POs, back-schedules a
produce-by date from the conservative transit, and recommends a pallet-rounded
quantity to restore the target buffer. Recommendations are ranked top-4 first,
US Foods + Cheney before Chefs Warehouse, soonest need first. Then a weekly
capacity rollup, a build-ahead suggestion within the freezer cap, and the
Toast leading-indicator note.

Read-only and side-effect free; the endpoint just serves the result.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta


def _ceil_pallet(cs: float, pallet: int) -> int:
    if cs <= 0:
        return 0
    return int(math.ceil(cs / pallet) * pallet)


def _variety(name: str) -> str:
    name = name or ""
    return name.split(" Bagel")[0].strip() if " Bagel" in name else name


def build_production_guide(inv: dict, ledger: list, demand_rows: list,
                           config: dict, toast_trend: dict = None,
                           now: datetime = None) -> dict:
    now = now or datetime.now()
    pallet = int(config.get("pallet_cs") or 56)
    cap = config.get("capacity") or {}
    dep_day = float(cap.get("dependable_cs_per_day") or 224)
    max_day = float(cap.get("max_cs_per_day") or 280)
    prod_days = len(cap.get("production_days") or ["Mon", "Tue", "Wed", "Thu", "Fri"]) or 5
    wk_dependable = dep_day * prod_days
    wk_max = max_day * prod_days
    bd = config.get("warehouse_buffer_days") or {}
    top4_floor = float(bd.get("top4_floor") or 7)
    top4_target = float(bd.get("top4_target") or 14)
    other_floor = float(bd.get("other_floor") or 5)
    prio = {d: i for i, d in enumerate(config.get("distributor_priority")
            or ["US Foods", "Cheney Brothers", "Chefs Warehouse"])}
    groups = config.get("transfer_groups") or {}
    wh_group = {}
    for g, members in groups.items():
        for m in members:
            wh_group[m] = g
    transit_by_wh = {}
    for w in config.get("warehouses") or []:
        transit_by_wh[w.get("warehouse")] = int(w.get("transit_days")
                                                 or config.get("transit_default_days") or 7)
    default_transit = int(config.get("transit_default_days") or 7)

    # --- on-hand + rate + distributor per (warehouse, variety)
    onhand, dist_of = {}, {}
    for key, item in (inv or {}).items():
        wh = item.get("warehouse") or ""
        var = _variety(item.get("name") or key)
        onhand[(wh, var)] = onhand.get((wh, var), 0.0) + float(item.get("quantity") or 0)
        dist_of[wh] = item.get("distributor") or dist_of.get(wh, "")
    rate_of, fresh_of = {}, {}
    for r in demand_rows or []:
        rate_of[(r["warehouse"], r["variety"])] = float(r.get("rate_cs_per_day") or 0)
        fresh_of[(r["warehouse"], r["variety"])] = r.get("stale", True)

    # --- incoming (pending / in_transit) per (warehouse, variety) from the ledger
    incoming = {}
    for po in ledger or []:
        if (po.get("status") or "") not in ("pending", "in_transit", "in_production", "overdue"):
            continue
        wh = po.get("warehouse") or ""
        for L in (po.get("lines") or []):
            var = L.get("variety") or ""
            incoming[(wh, var)] = incoming.get((wh, var), 0.0) + float(L.get("qty") or 0)

    # --- collapse into planning units (pool or standalone) x variety
    units = {}   # (unit_key, variety) -> aggregate
    for (wh, var), oh in onhand.items():
        unit = wh_group.get(wh, wh)
        u = units.setdefault((unit, var), {
            "unit": unit, "is_pool": unit in groups, "members": set(),
            "distributor": dist_of.get(wh, ""), "variety": var,
            "on_hand": 0.0, "rate": 0.0, "incoming": 0.0,
            "transit_days": default_transit, "any_stale": False,
        })
        u["members"].add(wh)
        u["on_hand"] += oh
        u["rate"] += rate_of.get((wh, var), 0.0)
        u["incoming"] += incoming.get((wh, var), 0.0)
        u["transit_days"] = max(u["transit_days"], transit_by_wh.get(wh, default_transit))
        if fresh_of.get((wh, var), True):
            u["any_stale"] = True
        if not u["distributor"]:
            u["distributor"] = dist_of.get(wh, "")

    top4 = set(config.get("top4") or [])
    recs = []
    for (unit, var), u in units.items():
        is_top4 = var in top4
        floor_d = top4_floor if is_top4 else other_floor
        target_d = top4_target if is_top4 else max(other_floor * 2, 10)
        rate, oh, inc, transit = u["rate"], u["on_hand"], u["incoming"], u["transit_days"]
        rec = {
            "unit": unit, "is_pool": u["is_pool"], "members": sorted(u["members"]),
            "distributor": u["distributor"], "variety": var, "top4": is_top4,
            "on_hand": round(oh, 1), "incoming_cs": round(inc, 1),
            "rate_cs_per_day": round(rate, 3), "transit_days": transit,
            "floor_days": floor_d, "target_days": target_d,
            "confidence": "low" if u["any_stale"] else "high",
        }
        if rate <= 0:
            rec.update(status="no-demand-data", cover_days=None,
                       cover_days_with_incoming=None, recommend_cs=0,
                       produce_by=None,
                       action="No usage signal - can't project; verify this warehouse still stocks it.")
            recs.append(rec); continue
        cover = oh / rate
        cover_inc = (oh + inc) / rate
        rec["cover_days"] = round(cover, 1)
        rec["cover_days_with_incoming"] = round(cover_inc, 1)
        # act when even WITH inbound we'd breach the floor before new product
        # could arrive (floor + transit runway).
        if cover_inc < (floor_d + transit):
            need_cs = target_d * rate - oh - inc
            rec_cs = max(_ceil_pallet(need_cs, pallet), pallet)   # at least 1 pallet
            days_to_floor = max(0.0, cover - floor_d)
            produce_by = now + timedelta(days=max(0.0, days_to_floor - transit))
            rec.update(status="produce", recommend_cs=rec_cs,
                       produce_by=produce_by.date().isoformat(),
                       action=f"Produce {rec_cs} cs (~{rec_cs // pallet} pallet(s)) by "
                              f"{produce_by.date().isoformat()} - {'POOL' if u['is_pool'] else 'wh'} "
                              f"cover {cover_inc:.0f}d < floor {floor_d:.0f}+transit {transit}d.")
        elif cover_inc < (target_d + transit):
            rec.update(status="watch", recommend_cs=0, produce_by=None,
                       action=f"Watch - cover {cover_inc:.0f}d approaching target {target_d:.0f}d+transit.")
        else:
            rec.update(status="ok", recommend_cs=0, produce_by=None,
                       action=f"OK - {cover_inc:.0f}d cover.")
        recs.append(rec)

    def _sort_key(r):
        st = {"produce": 0, "watch": 1, "no-demand-data": 2, "ok": 3}.get(r["status"], 4)
        return (st, 0 if r["top4"] else 1, prio.get(r["distributor"], 9),
                r.get("produce_by") or "9999", -(r.get("recommend_cs") or 0))
    recs.sort(key=_sort_key)

    produce = [r for r in recs if r["status"] == "produce"]
    committed_cs = sum(r["recommend_cs"] for r in produce)
    spare_dependable = max(0.0, wk_dependable - committed_cs)
    spare_max = max(0.0, wk_max - committed_cs)
    capacity = {
        "committed_cs": committed_cs,
        "committed_pallets": round(committed_cs / pallet, 1),
        "weekly_dependable_cs": wk_dependable, "weekly_max_cs": wk_max,
        "utilization_pct_of_dependable": round(committed_cs / wk_dependable * 100, 0) if wk_dependable else None,
        "feasible_on_normal_week": committed_cs <= wk_dependable,
        "needs_surge": committed_cs > wk_dependable,
        "spare_for_buildahead_cs_dependable": round(spare_dependable, 0),
        "spare_for_buildahead_cs_max": round(spare_max, 0),
        "note": ("Fits a normal Mon-Fri week." if committed_cs <= wk_dependable
                 else "Exceeds dependable capacity - use the 5th pallet / weekend, or expansion levers."),
    }
    buildahead = {
        "freezer_pallet_cap": config.get("freezer_pallet_cap"),
        "target_days_top4": config.get("internal_buffer_target_days_top4"),
        "spare_capacity_pallets_this_week": round(spare_max / pallet, 1),
        "note": "Spend spare capacity on top-4 internal buffer (5th pallet + weekend), within the freezer cap.",
    }
    tt = toast_trend or {}
    toast_note = None
    if tt.get("available"):
        d = tt.get("direction")
        toast_note = (f"Retail bagel demand {d} ({tt.get('pct_change')}% vs prior "
                      f"{tt.get('window_days')}d) - "
                      + ("pull build-ahead forward; distributor reorders likely to rise."
                         if d == "rising" else "no action from Toast."))

    return {
        "generated_at": now.isoformat(),
        "summary": {
            "produce_now": len(produce),
            "watch": sum(1 for r in recs if r["status"] == "watch"),
            "ok": sum(1 for r in recs if r["status"] == "ok"),
            "no_demand_data": sum(1 for r in recs if r["status"] == "no-demand-data"),
            "produce_now_top4": sum(1 for r in produce if r["top4"]),
        },
        "capacity": capacity,
        "buildahead": buildahead,
        "toast_note": toast_note,
        "recommendations": recs,
    }
