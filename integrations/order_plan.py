"""Order plan -> production load -> daily bake schedule.

H&H bakes to the distributors' POs, so the bake plan needs the POs that are
COMING, not just the ones already in hand. This module projects them:

1. ORDER PLAN (per US Foods / Cheney DC). Day by day over the horizon, each
   SKU's theoretical on-hand is drawn down at its usage rate and topped up by
   the open POs on their arrival dates. When any SKU's inventory position
   (on-hand + on order) falls below its reorder point -- usage x (lead +
   safety days) -- the DC places a PO: every SKU below its order-up-to level
   (usage x (lead + review + safety)) is brought back to it in 8-cs layers,
   then the PO is padded to the DC's minimum (2 pallets; 6 at Riviera Beach,
   La Mirada, Ocala, US Foods Chicago, Zebulon) and to whole 56-cs pallets,
   the extra layers going to the lowest-cover SKUs (varieties mix on a
   pallet). It arrives lead days later (30, fixed), ships transit days before
   that, and must be baked the day before it ships. At most one PO per DC per
   review period (7 days).
   Usage is the reps' weekly figure by default ("reps"), or what the counts
   show ("counts") -- see integrations/usage_compare.py before switching.

2. PRODUCTION LOAD. Week by week: committed (open POs not yet baked, from the
   production guide's queue) + projected (the order plan, and Chefs
   Warehouse at its recent PO run-rate) against capacity -- 6 pallets a day,
   Mon-Fri.

3. DAILY SCHEDULE. The next two weeks of production days filled earliest-due
   first at the daily cap, so a late PO shows up before it is late.
Pure; blueprints/planning.py serves it.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from core.util import naive_local_dt

ORDER_DEFAULTS = {
    "lead_days": 30, "review_days": 7, "safety_days_top4": 14,
    "safety_days_other": 10, "line_multiple_cs": 8, "usage_basis": "reps",
    "horizon_days": 56, "exclude_categories": ["bagels-lto"],
    "distributors": ["US Foods", "Cheney Brothers"],
    "counts_min_windows": 3, "counts_min_days": 14,
}


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _variety(name: str) -> str:
    return (name or "").split(" Bagel")[0].strip()


def _day0(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _wh_cfg(cfg: dict, wh: str) -> dict:
    for w in cfg.get("warehouses") or []:
        if w.get("warehouse") == wh:
            return w
    return {}


def usage_rate(item: dict, key: str, basis: str, count_items: "dict | None",
               oc: dict) -> "tuple[float, str]":
    """(weekly usage, basis actually used) for one SKU."""
    reps = _f(item.get("weekly_usage"))
    if basis == "counts":
        c = (count_items or {}).get(key) or {}
        if (c.get("usage_4w") is not None
                and (c.get("usage_4w_windows") or 0) >= int(oc["counts_min_windows"])
                and (c.get("usage_4w_days") or 0) >= float(oc["counts_min_days"])):
            return float(c["usage_4w"]), "counts"
        return reps, "reps (too little count history)"
    return reps, "reps"


# ---------------------------------------------------------------------------
# 1) Order plan
# ---------------------------------------------------------------------------

def plan_orders(inv: dict, cfg: dict, basis: str = "reps",
                count_items: "dict | None" = None,
                now: "datetime | None" = None) -> dict:
    oc = dict(ORDER_DEFAULTS, **(cfg.get("ordering") or {}))
    now = now or datetime.now()
    today = _day0(now)
    lead, review = int(oc["lead_days"]), int(oc["review_days"])
    mult = int(oc["line_multiple_cs"]) or 1
    pallet = int(cfg.get("pallet_cs") or 56)
    horizon = int(oc["horizon_days"])
    top4 = set(cfg.get("top4") or [])
    default_min = int(cfg.get("min_po_pallets_default") or 2)
    default_transit = int(cfg.get("transit_default_days") or 7)

    by_wh: dict = {}
    excluded = set()
    for key, it in inv.items():
        if (it.get("distributor") or "") not in oc["distributors"]:
            continue
        variety = _variety(it.get("name") or key)
        if (it.get("category") or "") in oc["exclude_categories"]:
            excluded.add(variety)
            continue
        wk, used_basis = usage_rate(it, key, basis, count_items, oc)
        arrivals = []
        for o in it.get("on_order") or []:
            q = _f(o.get("qty"))
            if q <= 0:
                continue
            when = naive_local_dt(o.get("arrival_date") or o.get("eta") or "")
            when = _day0(when) if when else today + timedelta(days=lead)
            arrivals.append([max(today, when), q, o.get("po_number") or ""])
        safety = int(oc["safety_days_top4"] if variety in top4 else oc["safety_days_other"])
        by_wh.setdefault(it.get("warehouse") or "", []).append({
            "key": key, "variety": variety, "top4": variety in top4,
            "weekly": wk, "basis": used_basis, "reps_weekly": _f(it.get("weekly_usage")),
            "d": wk / 7.0, "q": max(0.0, _f(it.get("quantity"))),
            "arrivals": sorted(arrivals), "safety": safety,
            "distributor": it.get("distributor") or "",
        })

    out = []
    for wh, skus in by_wh.items():
        wcfg = _wh_cfg(cfg, wh)
        min_pallets = int(wcfg.get("min_po_pallets") or default_min)
        transit = int(wcfg.get("transit_days") or default_transit)
        out.append(_plan_dc(wh, skus, today, lead, review, mult, pallet, horizon,
                            min_pallets, transit))
    out.sort(key=lambda w: (-w["short_count"], 0 if w["order_now"] else 1,
                            w["next_order_date"] or "9999", w["warehouse"]))
    return {"basis": basis, "warehouses": out, "excluded_varieties": sorted(excluded),
            "params": {**oc, "pallet_cs": pallet, "min_po_pallets_default": default_min},
            "as_of": now.isoformat(timespec="seconds")}


def _plan_dc(wh, skus, today, lead, review, mult, pallet, horizon, min_pallets, transit):
    # Without any new order: when does each SKU run out? ("short" = before an
    # order placed today could land)
    lands = today + timedelta(days=lead)
    for s in skus:
        q, out_date = s["q"], None
        arr = list(s["arrivals"])
        for t in range(0, horizon + 1):
            date = today + timedelta(days=t)
            while arr and arr[0][0] <= date:
                q += arr.pop(0)[1]
            if t:
                q -= s["d"]
            if q < 0 and out_date is None:
                out_date = date
        s["stockout"] = out_date
        s["short"] = out_date is not None and out_date < lands
        s["on_order"] = sum(a[1] for a in s["arrivals"])
        s["next_arrival"] = s["arrivals"][0][0] if s["arrivals"] else None

    # With the projected orders.
    state = {s["key"]: {"q": s["q"], "arr": [list(a) for a in s["arrivals"]]} for s in skus}
    orders = []
    next_ok = 0
    for t in range(0, horizon + 1):
        date = today + timedelta(days=t)
        for s in skus:
            st = state[s["key"]]
            if t:
                st["q"] -= s["d"]
            while st["arr"] and st["arr"][0][0] <= date:
                st["q"] += st["arr"].pop(0)[1]
        if t < next_ok:
            continue
        pos = {s["key"]: state[s["key"]]["q"] + sum(a[1] for a in state[s["key"]]["arr"])
               for s in skus}
        trig = [s for s in skus if s["d"] > 0 and pos[s["key"]] < s["d"] * (lead + s["safety"])]
        if not trig:
            continue
        qty = {}
        for s in skus:
            if s["d"] <= 0:
                continue
            need = s["d"] * (lead + review + s["safety"]) - pos[s["key"]]
            if need > 0:
                qty[s["key"]] = int(math.ceil(need / mult) * mult)
        raw = sum(qty.values())
        base = dict(qty)
        target = max(raw, min_pallets * pallet)
        target = int(math.ceil(target / pallet) * pallet)
        pad = target - raw
        users = [s for s in skus if s["d"] > 0]
        while pad > 0 and users:
            low = min(users, key=lambda s: (pos[s["key"]] + qty.get(s["key"], 0)) / s["d"])
            qty[low["key"]] = qty.get(low["key"], 0) + mult
            pad -= mult
        arrive = date + timedelta(days=lead)
        ship = arrive - timedelta(days=transit)
        bake_by = ship - timedelta(days=1)
        lines = [{"variety": s["variety"], "top4": s["top4"], "qty": qty[s["key"]],
                  "padded": qty[s["key"]] - base.get(s["key"], 0)}
                 for s in skus if qty.get(s["key"])]
        lines.sort(key=lambda l: (0 if l["top4"] else 1, -l["qty"]))
        orders.append({
            "order_date": date.date().isoformat(), "arrival_date": arrive.date().isoformat(),
            "ship_date": ship.date().isoformat(), "bake_by": bake_by.date().isoformat(),
            "total_cs": target, "pallets": target // pallet,
            "needed_cs": raw, "padded_cs": target - raw, "min_pallets": min_pallets,
            "now": t == 0, "trigger": sorted({s["variety"] for s in trig}),
            "lines": lines,
        })
        for s in skus:
            if qty.get(s["key"]):
                state[s["key"]]["arr"].append([arrive, qty[s["key"]], "projected"])
                state[s["key"]]["arr"].sort()
        next_ok = t + review

    now_order = orders[0] if orders and orders[0]["now"] else None
    rows = []
    for s in skus:
        sug = 0
        if now_order:
            sug = next((l["qty"] for l in now_order["lines"] if l["variety"] == s["variety"]), 0)
        rows.append({
            "variety": s["variety"], "top4": s["top4"], "item_key": s["key"],
            "theoretical": round(s["q"], 1), "weekly_usage": round(s["weekly"], 1),
            "reps_weekly_usage": round(s["reps_weekly"], 1), "basis": s["basis"],
            "on_order": round(s["on_order"], 1),
            "next_arrival": s["next_arrival"].date().isoformat() if s["next_arrival"] else "",
            "weeks_cover": round((s["q"] + s["on_order"]) / s["d"] / 7.0, 1) if s["d"] > 0 else None,
            "stockout_date": s["stockout"].date().isoformat() if s["stockout"] else "",
            "short": s["short"], "suggest_cs": sug,
        })
    rows.sort(key=lambda r: (0 if r["short"] else 1, -(r["suggest_cs"] or 0),
                             r["weeks_cover"] if r["weeks_cover"] is not None else 99))
    future = [o for o in orders if not o["now"]]
    return {
        "warehouse": wh, "distributor": skus[0]["distributor"] if skus else "",
        "lead_days": lead, "transit_days": transit, "min_pallets": min_pallets,
        "order_lands": lands.date().isoformat(),
        "weekly_usage": round(sum(s["weekly"] for s in skus), 1),
        "horizon_usage_cs": round(sum(s["d"] for s in skus) * horizon, 0),
        "order_now": now_order,
        "next_order_date": (future[0]["order_date"] if future else "") if not now_order
        else now_order["order_date"],
        "projected_orders": orders,
        "short_count": sum(1 for s in skus if s["short"]),
        "lines": rows,
    }


# ---------------------------------------------------------------------------
# 2) Production load by week
# ---------------------------------------------------------------------------

def _first_bake_day(now: datetime) -> datetime:
    """Today counts as a production day until 2 pm."""
    return _day0(now) if now.hour < 14 else _day0(now) + timedelta(days=1)


def _week_start(d: datetime) -> datetime:
    return _day0(d) - timedelta(days=d.weekday())


def _prod_days(cfg: dict) -> list:
    names = [str(x)[:3].lower() for x in ((cfg.get("capacity") or {}).get("production_days")
                                         or ["Mon", "Tue", "Wed", "Thu", "Fri"])]
    order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return [order.index(n) for n in names if n in order]


def _due_for_queue_item(q: dict, ledger_by_po: dict, lead: int, now: datetime) -> datetime:
    """Bake-by for a queued PO: the day before its ship date; without one,
    the day before (ETA - transit); without that, ordered + lead - transit."""
    sd = naive_local_dt(q.get("ship_date") or "")
    if sd:
        return _day0(sd) - timedelta(days=1)
    transit = int(q.get("transit_days") or 7)
    rec = ledger_by_po.get(q.get("po_number") or "") or {}
    eta = naive_local_dt(rec.get("eta") or rec.get("arrival_date") or "")
    if eta:
        return _day0(eta) - timedelta(days=transit + 1)
    od = naive_local_dt(rec.get("ordered_at") or "")
    if od:
        return _day0(od) + timedelta(days=lead - transit - 1)
    return _day0(now)


def cw_weekly_runrate(ledger: list, now: datetime, weeks: int = 8) -> float:
    since = now - timedelta(days=7 * weeks)
    tot = 0.0
    for r in ledger or []:
        if r.get("distributor") != "Chefs Warehouse" or r.get("status") == "canceled":
            continue
        od = naive_local_dt(r.get("ordered_at") or "")
        if od and od >= since:
            tot += _f(r.get("total_cs"))
    return tot / weeks


def production_load(queue: list, plan: dict, cfg: dict, ledger: list,
                    now: "datetime | None" = None, weeks: int = 8) -> dict:
    now = now or datetime.now()
    today = _day0(now)
    oc = dict(ORDER_DEFAULTS, **(cfg.get("ordering") or {}))
    pallet = int(cfg.get("pallet_cs") or 56)
    cap_day = _f((cfg.get("capacity") or {}).get("max_cs_per_day"), 336.0)
    pdays = _prod_days(cfg)
    ledger_by_po = {r.get("po_number") or "": r for r in ledger or []}
    w0 = _week_start(today)
    buckets = []
    for k in range(weeks):
        ws = w0 + timedelta(days=7 * k)
        days = [ws + timedelta(days=i) for i in pdays]
        days_left = [d for d in days if d >= _first_bake_day(now)]
        buckets.append({"week_of": ws.date().isoformat(),
                        "capacity_cs": round(cap_day * len(days_left), 0),
                        "committed_cs": 0.0, "projected_cs": 0.0, "suggested_now_cs": 0.0,
                        "cw_runrate_cs": 0.0, "committed_pos": [], "projected": []})
    end = w0 + timedelta(days=7 * weeks)

    def _bucket(d: datetime):
        d = max(d, today)
        if d >= end:
            return None
        return buckets[(d - w0).days // 7]

    overdue = []
    for q in queue or []:
        due = _due_for_queue_item(q, ledger_by_po, int(oc["lead_days"]), now)
        b = _bucket(due)
        if due < today:
            overdue.append(q.get("po_number"))
        if b is None:
            continue
        b["committed_cs"] += _f(q.get("total_cs"))
        b["committed_pos"].append({"po_number": q.get("po_number"),
                                   "warehouse": q.get("warehouse"),
                                   "distributor": q.get("distributor"),
                                   "cs": _f(q.get("total_cs")),
                                   "bake_by": due.date().isoformat()})
    for w in (plan or {}).get("warehouses") or []:
        for o in w.get("projected_orders") or []:
            b = _bucket(naive_local_dt(o["bake_by"]))
            if b is None:
                continue
            key = "suggested_now_cs" if o["now"] else "projected_cs"
            b[key] += o["total_cs"]
            b["projected"].append({"warehouse": w["warehouse"], "cs": o["total_cs"],
                                   "order_date": o["order_date"], "bake_by": o["bake_by"],
                                   "now": o["now"]})
    # Chefs Warehouse: no on-hand feed, so plan its future POs at its recent
    # run-rate -- from ~5 weeks out, beyond the POs it has already sent.
    cw = cw_weekly_runrate(ledger, now)
    for b in buckets:
        ws = naive_local_dt(b["week_of"])
        if ws >= today + timedelta(days=35):
            b["cw_runrate_cs"] = round(cw, 0)
    spare_before = 0.0
    for b in buckets:
        b["total_cs"] = round(b["committed_cs"] + b["projected_cs"] + b["suggested_now_cs"]
                              + b["cw_runrate_cs"], 0)
        b["pallets"] = round(b["total_cs"] / pallet, 1)
        b["capacity_pallets"] = round(b["capacity_cs"] / pallet, 1)
        b["utilization_pct"] = round(b["total_cs"] / b["capacity_cs"] * 100, 0) \
            if b["capacity_cs"] else None
        over = b["total_cs"] - b["capacity_cs"]
        b["over_cs"] = round(max(0.0, over), 0)
        b["pull_forward_possible_cs"] = round(min(b["over_cs"], spare_before), 0) if over > 0 else 0
        spare_before += max(0.0, -over)
        for k in ("committed_cs", "projected_cs", "suggested_now_cs"):
            b[k] = round(b[k], 0)
    return {"weeks": buckets, "cap_cs_per_day": cap_day, "production_days": pdays,
            "cw_weekly_runrate_cs": round(cw, 0), "overdue_pos": [p for p in overdue if p],
            "freezer_pallet_cap": cfg.get("freezer_pallet_cap")}


# ---------------------------------------------------------------------------
# 3) Daily schedule
# ---------------------------------------------------------------------------

def daily_schedule(queue: list, plan: dict, cfg: dict, ledger: list,
                   now: "datetime | None" = None, days_ahead: int = 10) -> dict:
    """Fill the next ``days_ahead`` production days earliest-bake-by first."""
    now = now or datetime.now()
    today = _day0(now)
    oc = dict(ORDER_DEFAULTS, **(cfg.get("ordering") or {}))
    cap_day = _f((cfg.get("capacity") or {}).get("max_cs_per_day"), 336.0)
    pallet = int(cfg.get("pallet_cs") or 56)
    pdays = set(_prod_days(cfg))
    ledger_by_po = {r.get("po_number") or "": r for r in ledger or []}
    days, d = [], _first_bake_day(now)
    while len(days) < days_ahead:
        if d.weekday() in pdays:
            days.append({"date": d.date().isoformat(), "cap_cs": cap_day, "cs": 0.0, "jobs": []})
        d += timedelta(days=1)
    last_day = naive_local_dt(days[-1]["date"]) if days else today
    jobs = []
    for q in queue or []:
        due = _due_for_queue_item(q, ledger_by_po, int(oc["lead_days"]), now)
        jobs.append({"label": q.get("po_number") or "", "warehouse": q.get("warehouse") or "",
                     "kind": "po", "cs": _f(q.get("total_cs")), "due": due})
    for w in (plan or {}).get("warehouses") or []:
        for o in w.get("projected_orders") or []:
            due = naive_local_dt(o["bake_by"])
            if due and due <= last_day + timedelta(days=3):
                jobs.append({"label": "projected" if not o["now"] else "suggested now",
                             "warehouse": w["warehouse"], "kind": "projected",
                             "cs": float(o["total_cs"]), "due": due})
    jobs.sort(key=lambda j: (j["due"], 0 if j["kind"] == "po" else 1))
    late, unscheduled = [], []
    di = 0
    for j in jobs:
        left = j["cs"]
        finish = None
        while left > 1e-9 and di < len(days):
            room = days[di]["cap_cs"] - days[di]["cs"]
            if room <= 1e-9:
                di += 1
                continue
            take = min(room, left)
            days[di]["cs"] += take
            days[di]["jobs"].append({"label": j["label"], "warehouse": j["warehouse"],
                                     "kind": j["kind"], "cs": round(take, 0),
                                     "due": j["due"].date().isoformat()})
            left -= take
            finish = naive_local_dt(days[di]["date"])
        if left > 1e-9:
            unscheduled.append({"label": j["label"], "warehouse": j["warehouse"],
                                "cs": round(left, 0), "due": j["due"].date().isoformat()})
        elif finish is not None and finish > j["due"]:
            late.append({"label": j["label"], "warehouse": j["warehouse"],
                         "due": j["due"].date().isoformat(),
                         "done": finish.date().isoformat()})
    for x in days:
        x["cs"] = round(x["cs"], 0)
        x["pallets"] = round(x["cs"] / pallet, 1)
    return {"days": days, "late": late, "beyond_window": unscheduled,
            "cap_pallets_per_day": round(cap_day / pallet, 1)}


def scenario_summary(plan: dict) -> dict:
    """Per-DC totals for comparing usage bases side by side."""
    out = {}
    for w in plan.get("warehouses") or []:
        orders = w.get("projected_orders") or []
        out[w["warehouse"]] = {
            "weekly_usage": w["weekly_usage"],
            "horizon_usage_cs": w["horizon_usage_cs"],
            "orders": len(orders),
            "ordered_cs": sum(o["total_cs"] for o in orders),
            "first_order": orders[0]["order_date"] if orders else "",
            "order_now_cs": w["order_now"]["total_cs"] if w.get("order_now") else 0,
            "short_count": w["short_count"],
        }
    return out


__all__ = ["plan_orders", "production_load", "daily_schedule", "scenario_summary",
           "usage_rate", "cw_weekly_runrate", "ORDER_DEFAULTS"]
