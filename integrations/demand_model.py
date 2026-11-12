"""Unified demand model (Phase 3 of the production planner).

Two signals, deliberately kept separate (per the operating decision):

  * WAREHOUSE USAGE = direct demand. Each warehouse-SKU\'s reported
    weekly_usage -> a cases/day run-rate, freshness-weighted by when the
    usage was last reported. This is what drives depletion / stock-out
    projection. Transfer-group warehouses (Cheney FL) can be pooled.

  * TOAST = leading indicator only. H&H retail product-mix is a coarse,
    chain-level *trend* of bagel demand -- it does NOT map cleanly to
    wholesale parbaked SKUs (Toast sells finished menu items), so it is
    surfaced as a directional early-warning, never as a per-SKU number.
"""
from __future__ import annotations

from datetime import datetime


def _parse_iso(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except ValueError:
        # bare date or unknown -> try first 10 chars
        try:
            return datetime.fromisoformat(s[:10])
        except ValueError:
            return None


def _variety(name: str) -> str:
    name = name or ""
    return name.split(" Bagel")[0].strip() if " Bagel" in name else name


def warehouse_demand(inv: dict, now: datetime = None, stale_days: int = 14) -> list:
    """Per warehouse-SKU direct demand (cases/day) with freshness. Pure.

    ``inv`` is the load_inventory() dict. Each row:
      distributor, warehouse, variety, weekly_usage, rate_cs_per_day,
      usage_as_of, freshness_days, stale, confidence, transfer_group, top4.
    """
    from integrations.planning_config import transfer_group_for, is_top4
    now = now or datetime.now()
    rows = []
    for key, item in (inv or {}).items():
        wu = float(item.get("weekly_usage") or 0)
        # Usage freshness is the USAGE REPORT's date only. max(usage, count)
        # let a weekly count keep a months-old usage figure looking fresh.
        count_as_of = _parse_iso(item.get("last_count_at"))
        as_of = _parse_iso(item.get("last_usage_report_at"))
        # Records that have never had a usage date stamped (older than the
        # stamp) fall back to the count date, and say so.
        usage_basis = "usage_report" if as_of else ("count" if count_as_of else "none")
        as_of = as_of or count_as_of
        fresh_days = round((now - as_of).total_seconds() / 86400.0, 1) if as_of else None
        count_age = round((now - count_as_of).total_seconds() / 86400.0, 1) \
            if count_as_of else None
        stale = (fresh_days is None) or (fresh_days > stale_days)
        rate = wu / 7.0
        if wu <= 0:
            confidence = "none"            # no usage signal -> can\'t forecast
        elif stale:
            confidence = "low"
        else:
            confidence = "high"
        wh = item.get("warehouse") or ""
        rows.append({
            "distributor": item.get("distributor") or "",
            "warehouse": wh,
            "variety": _variety(item.get("name") or key),
            "weekly_usage": round(wu, 2),
            "rate_cs_per_day": round(rate, 3),
            "usage_as_of": as_of.isoformat() if as_of else None,
            "freshness_days": fresh_days,
            "usage_age_days": fresh_days,
            "usage_as_of_basis": usage_basis,
            "count_as_of": count_as_of.isoformat() if count_as_of else None,
            "count_age_days": count_age,
            "stale": stale,
            "confidence": confidence,
            "transfer_group": transfer_group_for(wh),
            "top4": is_top4(_variety(item.get("name") or key)),
        })
    rows.sort(key=lambda r: (r["distributor"], r["warehouse"], r["variety"]))
    return rows


def pool_demand(rows: list) -> list:
    """Aggregate warehouse_demand by transfer pool (or standalone warehouse)
    and variety, so a transfer group (Cheney FL) is planned as one unit."""
    agg = {}
    for r in rows:
        group = r.get("transfer_group") or r.get("warehouse")
        k = (group, r["variety"])
        a = agg.setdefault(k, {
            "pool": group, "variety": r["variety"], "is_pool": bool(r.get("transfer_group")),
            "members": set(), "weekly_usage": 0.0, "rate_cs_per_day": 0.0,
            "worst_freshness_days": None, "any_stale": False, "top4": r.get("top4", False),
        })
        a["members"].add(r["warehouse"])
        a["weekly_usage"] += r["weekly_usage"]
        a["rate_cs_per_day"] += r["rate_cs_per_day"]
        if r.get("stale"):
            a["any_stale"] = True
        fd = r.get("freshness_days")
        if fd is not None:
            a["worst_freshness_days"] = fd if a["worst_freshness_days"] is None else max(a["worst_freshness_days"], fd)
    out = []
    for a in agg.values():
        a["members"] = sorted(a["members"])
        a["weekly_usage"] = round(a["weekly_usage"], 2)
        a["rate_cs_per_day"] = round(a["rate_cs_per_day"], 3)
        out.append(a)
    out.sort(key=lambda x: (x["pool"], x["variety"]))
    return out


#: The trend is only stated when at least this share of the (store, day)
#: slots in the recent window can be matched to the same store a window
#: earlier, over at least TREND_MIN_DAYS different days.
TREND_MIN_COVERAGE = 0.6
TREND_MIN_DAYS = 7


def toast_demand_trend(sales_rows: list, window_days: int = 14,
                       today: str = "") -> dict:
    """Coarse chain-level bagel-demand TREND from Toast retail rows -- a
    leading indicator, not per-SKU demand.

    Compares the most recent ``window_days`` with the window before it, LIKE
    FOR LIKE: a (store, day) counts only when the same store also has data
    exactly ``window_days`` earlier. Summing whatever happened to be stored
    in each window made a store whose pulls were missing from one window
    read as a 30% swing in demand. ``today`` (YYYY-MM-DD), when given,
    leaves out the still-open day and anything after it. Below the minimum
    coverage no trend is stated at all.
    """
    from datetime import timedelta
    qty_by: dict = {}          # (store, date) -> bagel qty
    has_data: set = set()      # (store, date) with ANY Toast row
    for r in sales_rows or []:
        d = (r.get("business_date") or "")[:10]
        if not d or (today and d >= today):
            continue
        store = r.get("restaurant_guid") or r.get("location") or ""
        has_data.add((store, d))
        mg = (r.get("menu_group") or "").lower()
        item = (r.get("item") or "").lower()
        if "bagel" in mg or "bagel" in item:
            try:
                qty = float(r.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            qty_by[(store, d)] = qty_by.get((store, d), 0.0) + qty
    if not qty_by:
        return {"available": False, "reason": "no bagel-related Toast rows"}
    latest = max(d for _s, d in qty_by)
    anchor = _parse_iso(latest)
    if anchor is None:
        return {"available": False, "reason": "Toast dates unreadable"}
    stores_all = {s for s, _d in has_data}
    recent = prior = 0.0
    matched, days, stores = 0, set(), set()
    for age in range(window_days):
        d = (anchor - timedelta(days=age)).date().isoformat()
        d_prior = (anchor - timedelta(days=age + window_days)).date().isoformat()
        for store in stores_all:
            if (store, d) in has_data and (store, d_prior) in has_data:
                recent += qty_by.get((store, d), 0.0)
                prior += qty_by.get((store, d_prior), 0.0)
                matched += 1
                days.add(d)
                stores.add(store)
    recent_slots = sum(1 for (s, d) in has_data
                       if 0 <= (anchor - _parse_iso(d)).days < window_days)
    possible = max(recent_slots, 1)
    coverage = matched / possible
    base = {"anchored_to": latest, "as_of": latest, "window_days": window_days,
            "stores": len(stores), "matched_location_days": matched,
            "coverage_pct": round(coverage * 100, 0)}
    if coverage < TREND_MIN_COVERAGE or len(days) < TREND_MIN_DAYS or prior <= 0:
        return {**base, "available": False,
                "reason": (f"not enough comparable Toast days to state a trend "
                           f"({matched} of {possible} store-days match the prior "
                           f"{window_days} days)")}
    rpd = recent / window_days
    ppd = prior / window_days
    pct = (recent - prior) / prior * 100.0
    direction = "rising" if pct > 10 else ("falling" if pct < -10 else "flat")
    return {
        **base, "available": True,
        "recent_per_day": round(rpd, 1), "prior_per_day": round(ppd, 1),
        "pct_change": round(pct, 1),
        "direction": direction,
        "note": "Leading indicator (H&H retail bagel demand); not per-SKU wholesale demand.",
    }
