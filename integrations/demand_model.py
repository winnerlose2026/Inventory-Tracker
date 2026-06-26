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
        as_of = max([d for d in (_parse_iso(item.get("last_usage_report_at")),
                                 _parse_iso(item.get("last_count_at"))) if d] or [None],
                    default=None)
        fresh_days = round((now - as_of).total_seconds() / 86400.0, 1) if as_of else None
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


def toast_demand_trend(sales_rows: list, window_days: int = 14) -> dict:
    """Coarse chain-level bagel-demand TREND from Toast retail rows -- a
    leading indicator, not per-SKU demand. Compares the most recent
    ``window_days`` of bagel-related qty against the prior window. Anchored
    to the latest business_date present (Toast ingest can lag)."""
    bagel = []
    for r in sales_rows or []:
        mg = (r.get("menu_group") or "").lower()
        item = (r.get("item") or "").lower()
        if "bagel" in mg or "bagel" in item:
            d = (r.get("business_date") or "")[:10]
            try:
                qty = float(r.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if d:
                bagel.append((d, qty))
    if not bagel:
        return {"available": False, "reason": "no bagel-related Toast rows"}
    latest = max(d for d, _ in bagel)
    anchor = _parse_iso(latest)
    recent = prior = 0.0
    for d, qty in bagel:
        dt = _parse_iso(d)
        if dt is None:
            continue
        age = (anchor - dt).days
        if 0 <= age < window_days:
            recent += qty
        elif window_days <= age < 2 * window_days:
            prior += qty
    rpd = recent / window_days
    ppd = prior / window_days
    pct = ((rpd - ppd) / ppd * 100.0) if ppd > 0 else None
    direction = "flat"
    if pct is not None:
        direction = "rising" if pct > 10 else ("falling" if pct < -10 else "flat")
    return {
        "available": True, "anchored_to": latest, "window_days": window_days,
        "recent_per_day": round(rpd, 1), "prior_per_day": round(ppd, 1),
        "pct_change": (round(pct, 1) if pct is not None else None),
        "direction": direction,
        "note": "Leading indicator (H&H retail bagel demand); not per-SKU wholesale demand.",
    }
