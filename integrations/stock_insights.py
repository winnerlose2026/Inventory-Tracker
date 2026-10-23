"""Count checks (theoretical vs actual) and per-DC reorder suggestions.

Pure functions over the inventory / usage / audit / ledger data -- no Flask, no
file writes -- served by blueprints/insights.py.

THEORETICAL vs ACTUAL
    Between counts, a SKU's ``quantity`` is theoretical: the last count, plus
    PO receipts since, minus the forecast burn (weekly_usage / 7 a day) and
    manual moves since. When the distributor's next count lands, on-hand
    resets to it. The gap at that moment -- counted minus what we projected
    for the count's date -- is the COUNT CHECK. A big gap means the weekly
    usage is wrong, a delivery was missed or counted early, or shrink.

    sync_inventory._apply_email_event stamps ``item["count_check"]`` when a
    NEW count lands (projected, counted, previous count). Counts that landed
    before that existed are reconstructed from inventory_audit.json: the
    first on_hand row ingested at/after the count carries the projection
    (old_quantity) and the reset (new_quantity).

REORDER
    Inventory position = theoretical on-hand + everything on order. Reorder
    point = daily usage x (lead + safety days); order-up-to = daily usage x
    (lead + review + safety days). Below the reorder point -> suggest the gap,
    each line rounded up to whole layers (8 cs) and the DC's total up to whole
    pallets (56 cs), extra layers going to the lowest-cover lines. Lead time
    per DC is the median order -> arrival of its recent arrived POs, else the
    30-day default. A day-by-day projection with the open POs' arrival dates
    gives the date the SKU runs out, if it does before a new order could land.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import median

from core.util import naive_local_dt, rollover_row_live, absorbed_row_live, absorbed_row_qty

REORDER_DEFAULTS = {
    "lead_days": 30,           # USF / Cheney auto-ETA when no history
    "review_days": 7,          # DCs order about weekly
    "safety_days_top4": 14,
    "safety_days_other": 10,
    "line_multiple_cs": 8,
    "pallet_cs": 56,
    "lead_history_days": 120,
    "lead_min_samples": 3,
    "exclude_categories": ["bagels-lto"],
    "distributors": ["US Foods", "Cheney Brothers"],
    # "counts": plan on what the counts show was actually used (last ~6 weeks,
    # >= 3 clean count windows / 14 days), else the forecast weekly usage.
    # "forecast": always the weekly usage the distributor reports / we burn.
    "usage_basis": "counts",
    "counts_min_windows": 3,
    "counts_min_days": 14,
}
COUNT_CHECK_DEFAULTS = {
    "min_cs": 10,              # below this a gap is noise
    "min_weeks": 0.5,          # ...or under half a week of usage
}


def _variety(name: str) -> str:
    return (name or "").split(" Bagel")[0].strip()


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Count checks
# ---------------------------------------------------------------------------

def _count_groups(rows: list) -> list:
    """Scan-audit on_hand rows for one SKU -> one entry per COUNT.

    Before 2026-10-08 the same report was re-applied on later scans (each
    re-read reset the burn), so rows within 36 h of the previous one are the
    same count; the first row of each group is the count landing."""
    out, last = [], None
    for r in sorted((r for r in rows if r["_dt"] is not None), key=lambda r: r["_dt"]):
        if last is None or (r["_dt"] - last).total_seconds() > 36 * 3600:
            out.append(r)
        last = r["_dt"]
    return out


def _receipts_between(usage_by_key: dict, key: str, start_at: str, end_at: str) -> float:
    """Cases received after the count at ``start_at`` and inside the count at
    ``end_at`` -- the same inside-a-count rule on-hand uses (calendar day,
    exact time for confirmed deliveries, not_in_count_at for a count that
    provably missed one). Live arrivals only."""
    from sync_inventory import _rollover_arrival
    from core.util import receipt_inside_count
    tot = 0.0
    for r in usage_by_key.get(key, ()):
        if rollover_row_live(r):
            q = abs(_f(r.get("amount")))
        elif absorbed_row_live(r):
            q = absorbed_row_qty(r)
        else:
            continue
        a = _rollover_arrival(r)
        if not a:
            continue
        if receipt_inside_count(r, end_at, a) and not receipt_inside_count(r, start_at, a):
            tot += q
    return tot


def pdt_ok(raw) -> bool:
    return naive_local_dt(raw) is not None


def count_checks(inv: dict, usage: list, audit: list, cfg: "dict | None" = None,
                 ledger: "list | None" = None) -> dict:
    """{"items": {item_key: check}, "warehouses": [rollup...], "params": cfg}

    Per SKU, for the LATEST count:
      projected / counted / variance (counted - projected), variance_weeks,
      flag (beyond max(min_cs, min_weeks x weekly usage));
      week: count-to-count usage over the last window (previous count ->
        this one) -- what was actually used, from the counts and receipts;
      usage_4w: the same over the last ~6 weeks of counts (steadier), skipping
        windows where stock rose with no delivery on record;
      unexplained_rise: cs the stock ROSE in the last window beyond recorded
        deliveries (a missing delivery, or a bad count).
    """
    cfg = dict(COUNT_CHECK_DEFAULTS, **(cfg or {}))
    audit_by_name: dict = {}
    for a in audit or []:
        if (a.get("event_type") or "") != "on_hand":
            continue
        r = dict(a)
        r["_dt"] = naive_local_dt(a.get("ts"))
        audit_by_name.setdefault((a.get("name") or "").lower(), []).append(r)
    usage_by_key: dict = {}
    for r in usage or []:
        usage_by_key.setdefault(r.get("item_key") or "", []).append(r)

    items: dict = {}
    by_wh: dict = {}
    for key, it in inv.items():
        count_at = it.get("last_count_at") or ""
        counted = it.get("last_count_qty")
        cdt = naive_local_dt(count_at)
        if not count_at or counted is None or cdt is None:
            continue
        counted = _f(counted)
        groups = _count_groups(audit_by_name.get((it.get("name") or key).lower()) or [])
        landed = [g for g in groups if g["_dt"] >= cdt - timedelta(minutes=5)]
        earlier = [g for g in groups if g["_dt"] < cdt - timedelta(hours=12)]
        cc = it.get("count_check") or {}
        if cc.get("count_at") == count_at and cc.get("projected") is not None:
            variance = counted - _f(cc["projected"])
            prev_at = cc.get("prev_count_at") or ""
            prev_q = cc.get("prev_counted")
            source = "recorded"
        elif landed:
            g = landed[0]
            variance = _f(g.get("new_quantity")) - _f(g.get("old_quantity"))
            prev_at = earlier[-1]["ts"] if earlier else ""
            prev_q = _f(earlier[-1].get("new_quantity")) if earlier else None
            source = "audit"
        else:
            continue
        wu = _f(it.get("weekly_usage"))
        threshold = max(float(cfg["min_cs"]), float(cfg["min_weeks"]) * wu)
        chk = {"count_at": count_at, "counted": counted,
               "projected": round(counted - variance, 2),
               "variance": round(variance, 2), "weekly_usage": wu,
               "variance_weeks": round(variance / wu, 2) if wu > 0 else None,
               "flag": abs(variance) >= threshold,
               "direction": "higher" if variance > 0 else ("lower" if variance < 0 else ""),
               "prev_count_at": prev_at, "source": source}

        # What was actually used, count to count: previous count + receipts
        # in between - this count. A window where stock ROSE beyond any
        # recorded delivery says a delivery is missing from our records (or a
        # count was wrong) -- it is reported, and left out of the average.
        pts = [(g["_dt"], g["ts"], _f(g.get("new_quantity")))
               for g in earlier if (cdt - g["_dt"]) <= timedelta(days=42)]
        if source == "recorded" and pdt_ok(prev_at) and prev_q is not None:
            # the stamped previous count is exact -- prefer it to the audit's
            pts = [p for p in pts if p[0] < naive_local_dt(prev_at) - timedelta(hours=12)]
            pts.append((naive_local_dt(prev_at), prev_at, _f(prev_q)))
        pts.append((cdt, count_at, counted))
        windows = []
        for (d0, t0, q0), (d1, t1, q1) in zip(pts, pts[1:]):
            days = (d1 - d0).total_seconds() / 86400.0
            if days < 3:
                continue
            used = q0 + _receipts_between(usage_by_key, key, t0, t1) - q1
            windows.append({"days": days, "used": used, "q0": q0,
                            "valid": used >= -max(2.0, 0.05 * q0)})
        # A rise right after a window is usually ONE delivery booked on the
        # wrong side of a count (an ETA rolled it in before the truck landed):
        # the first window looks heavy, the next one rises. Across both the
        # intermediate count cancels, so judge them together.
        merged = []
        for w_ in windows:
            if not w_["valid"] and merged and merged[-1]["valid"]:
                prev = merged.pop()
                both = {"days": prev["days"] + w_["days"],
                        "used": prev["used"] + w_["used"], "q0": prev["q0"],
                        "merged": True}
                both["valid"] = both["used"] >= -max(2.0, 0.05 * both["q0"])
                merged.append(both if both["valid"] else w_)
            else:
                merged.append(w_)
        last_raw = windows[-1] if windows else None
        windows = merged
        if windows:
            if last_raw["valid"]:
                chk["week"] = round(max(0.0, last_raw["used"]) / last_raw["days"] * 7.0, 1)
                chk["week_days"] = round(last_raw["days"], 1)
            else:
                chk["unexplained_rise"] = round(-last_raw["used"], 1)
            ok = [w_ for w_ in windows if w_["valid"]]
            days_ok = sum(w_["days"] for w_ in ok)
            if days_ok >= 10:
                chk["usage_4w"] = round(max(0.0, sum(w_["used"] for w_ in ok))
                                        / days_ok * 7.0, 1)
                chk["usage_4w_days"] = round(days_ok, 1)
                chk["usage_4w_windows"] = len(ok)
            chk["rise_windows"] = sum(1 for w_ in windows if not w_["valid"])
        items[key] = chk

        wh = it.get("warehouse") or ""
        w = by_wh.setdefault(wh, {"warehouse": wh, "distributor": it.get("distributor") or "",
                                  "count_at": count_at, "net_variance": 0.0,
                                  "abs_variance": 0.0, "projected": 0.0, "counted": 0.0,
                                  "weekly_usage": 0.0, "usage_4w": 0.0, "usage_4w_skus": 0,
                                  "skus": 0, "flagged": 0, "higher": 0, "lower": 0,
                                  "lines": []})
        if count_at > w["count_at"]:
            w["count_at"] = count_at
        w["net_variance"] += variance
        w["abs_variance"] += abs(variance)
        w["projected"] += chk["projected"]
        w["counted"] += counted
        w["skus"] += 1
        if chk.get("usage_4w") is not None and (it.get("category") or "") != "bagels-lto":
            w["weekly_usage"] += wu
            w["usage_4w"] += chk["usage_4w"]
            w["usage_4w_skus"] += 1
        if chk["flag"]:
            w["flagged"] += 1
            w["higher" if variance > 0 else "lower"] += 1
        w["lines"].append({"item_key": key, "variety": _variety(it.get("name") or key),
                           **{k: chk.get(k) for k in (
                               "projected", "counted", "variance", "variance_weeks",
                               "flag", "weekly_usage", "week", "usage_4w",
                               "unexplained_rise")}})

    warehouses = []
    for w in by_wh.values():
        for k in ("net_variance", "abs_variance", "projected", "counted",
                  "weekly_usage", "usage_4w"):
            w[k] = round(w[k], 1)
        w["lines"].sort(key=lambda l: -abs(l["variance"] or 0))
        w["flag"] = w["flagged"] > 0
        w["hints"] = _hints(w, ledger, cfg)
        warehouses.append(w)
    warehouses.sort(key=lambda w: (-int(w["flag"]), -w["abs_variance"]))
    return {"items": items, "warehouses": warehouses, "params": cfg}


def _po_hints(w: dict, ledger) -> list:
    """POs whose lines line up with the count gap."""
    out = []
    count_dt = naive_local_dt(w["count_at"])
    if not ledger or count_dt is None:
        return out
    var = {l["variety"]: (l["variance"] or 0) for l in w["lines"]}
    best = {}
    for r in ledger:
        if (r.get("warehouse") or "") != w["warehouse"] or \
                r.get("distributor") == "Chefs Warehouse":
            continue
        lines = [l for l in (r.get("lines") or []) if not l.get("absorbed")]
        total = sum(_f(l.get("qty")) for l in lines)
        status = r.get("status") or ""
        if total <= 0 or status == "canceled":
            continue
        arr = naive_local_dt(r.get("arrival_date") or "")
        rolled = naive_local_dt(r.get("rolled_at") or "")
        if status != "arrived":
            sign, why = 1, ("is still on order, but the count is OVER by about that "
                            "much on its lines — it may have arrived early")
        elif arr is not None and count_dt - timedelta(days=10) <= arr <= count_dt + timedelta(days=1):
            if rolled is not None and rolled > count_dt:
                sign, why = 1, (f"arrived {arr.date().isoformat()} but was booked after "
                                "the count, so the projection did not have it — that "
                                "explains the overage")
            else:
                sign, why = -1, (f"was booked as arrived {arr.date().isoformat()}, but the "
                                 "count is SHORT by about that much on its lines — check "
                                 "it actually landed before the count")
        else:
            continue
        score = sum(min(_f(l.get("qty")), max(0.0, sign * var.get(l.get("variety") or "", 0)))
                    for l in lines)
        if score >= 0.6 * total and score >= 0.4 * abs(w["net_variance"] or 1):
            if sign not in best or score > best[sign][0]:
                best[sign] = (score, f"PO {r.get('po_number')} ({total:g} cs) {why}.")
    return [v[1] for v in best.values()]


def _hints(w: dict, ledger, cfg: dict) -> list:
    """Likely causes for a warehouse's count gap, most specific first."""
    if not w["flagged"]:
        return []
    hints = _po_hints(w, ledger)
    flagged = [l for l in w["lines"] if l["flag"]]
    rises = [l for l in flagged if (l.get("unexplained_rise") or 0) >= float(cfg["min_cs"])]
    if len(rises) >= max(2, 0.5 * len(flagged)) and not hints:
        tot = sum(l["unexplained_rise"] for l in rises)
        hints.append(f"Stock ROSE between counts on {len(rises)} SKUs (+{tot:.0f} cs) with no "
                     "delivery on record — either a delivery we have no PO/arrival for, "
                     "or the previous count was short.")
    elif len(flagged) >= 3:
        hi = sum(1 for l in flagged if (l["variance"] or 0) > 0)
        lo = len(flagged) - hi
        if max(hi, lo) >= 0.75 * len(flagged):
            tail = ""
            if w.get("usage_4w_skus"):
                tail = (f" Over the last ~6 weeks the counts show about "
                        f"{w['usage_4w']:.0f} cs/wk actually used, against "
                        f"{w['weekly_usage']:.0f} cs/wk in the forecast.")
            hints.append(("Counts came in HIGHER than projected on most SKUs — the "
                          "weekly usage we burn is set too high (we took off stock that "
                          "was not sold)." if hi > lo else
                          "Counts came in LOWER than projected on most SKUs — usage is "
                          "running above the forecast, or there is shrink / damage.")
                         + tail)
    if not hints:
        # Few SKUs, but their counts have drifted the same way for weeks: the
        # usage on those SKUs is off, not the count.
        drift = [l for l in flagged if l.get("usage_4w") is not None and l["weekly_usage"]
                 and abs(l["usage_4w"] - l["weekly_usage"]) >= 0.3 * l["weekly_usage"]
                 and ((l["variance"] or 0) > 0) == (l["usage_4w"] < l["weekly_usage"])]
        if drift:
            hints.append("Usage looks off on " + ", ".join(
                f"{l['variety']} (counts show ~{l['usage_4w']:.0f} cs/wk used vs "
                f"{l['weekly_usage']:.0f} forecast)" for l in drift[:4])
                + " \u2014 the gap has built up over several counts, so it is the usage "
                  "rate, not a one-off count error.")
    if not hints:
        names = ", ".join(f"{l['variety']} {l['variance']:+.0f}" for l in flagged[:4])
        hints.append(f"Isolated gap{'s' if len(flagged) > 1 else ''} ({names}) — check "
                     "those lines on the count report (a mis-keyed or mis-read figure) "
                     "before changing anything.")
    return hints


# ---------------------------------------------------------------------------
# Reorder suggestions
# ---------------------------------------------------------------------------

def lead_times(ledger: list, now: datetime, cfg: dict) -> dict:
    """{warehouse: (days, basis)} from recent arrived POs' order -> arrival."""
    by_wh: dict = {}
    cutoff = now - timedelta(days=int(cfg["lead_history_days"]))
    for r in ledger or []:
        if r.get("status") != "arrived" or r.get("distributor") not in cfg["distributors"]:
            continue
        o = naive_local_dt(r.get("ordered_at"))
        a = naive_local_dt(r.get("arrival_date"))
        if o is None or a is None or a < cutoff:
            continue
        d = (a - o).total_seconds() / 86400.0
        if 3 <= d <= 90:
            by_wh.setdefault(r.get("warehouse") or "", []).append(d)
    out = {}
    for wh, ds in by_wh.items():
        if len(ds) >= int(cfg["lead_min_samples"]):
            m = median(ds)
            out[wh] = (int(round(min(45, max(14, m)))),
                       f"median of {len(ds)} recent POs, order → arrival")
    return out


try:
    from integrations.planning_config import transfer_group_for
except Exception:  # noqa: BLE001
    transfer_group_for = None


def reorder_suggestions(inv: dict, ledger: list, cfg: "dict | None" = None,
                        now: "datetime | None" = None, top4: "set | None" = None,
                        count_items: "dict | None" = None) -> dict:
    cfg = dict(REORDER_DEFAULTS, **(cfg or {}))
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    top4 = top4 or {"Plain", "Everything", "Sesame", "Cinnamon Raisin"}
    leads = lead_times(ledger, now, cfg)
    mult = int(cfg["line_multiple_cs"]) or 1
    pallet = int(cfg["pallet_cs"]) or 1
    by_wh: dict = {}
    excluded = set()
    for key, it in inv.items():
        if (it.get("distributor") or "") not in cfg["distributors"]:
            continue
        variety = _variety(it.get("name") or key)
        if (it.get("category") or "") in cfg["exclude_categories"]:
            excluded.add(variety)
            continue
        forecast_wu = _f(it.get("weekly_usage"))
        cnt = (count_items or {}).get(key) or {}
        counts_wu = cnt.get("usage_4w")
        wu, basis_u = forecast_wu, "forecast"
        if (cfg["usage_basis"] == "counts" and counts_wu is not None
                and (cnt.get("usage_4w_windows") or 0) >= int(cfg["counts_min_windows"])
                and (cnt.get("usage_4w_days") or 0) >= float(cfg["counts_min_days"])):
            wu, basis_u = float(counts_wu), "counts"
        if wu <= 0:
            continue
        wh = it.get("warehouse") or ""
        lead, basis = leads.get(wh, (int(cfg["lead_days"]), "30-day default"))
        safety = int(cfg["safety_days_top4"] if variety in top4 else cfg["safety_days_other"])
        review = int(cfg["review_days"])
        d = wu / 7.0
        th = max(0.0, _f(it.get("quantity")))
        arrivals = []
        for o in it.get("on_order") or []:
            q = _f(o.get("qty"))
            if q <= 0:
                continue
            when = naive_local_dt(o.get("arrival_date") or o.get("eta") or "")
            if when is None:
                when = today + timedelta(days=lead)
            arrivals.append((max(today, when.replace(hour=0, minute=0, second=0,
                                                     microsecond=0)), q,
                             o.get("po_number") or ""))
        arrivals.sort()
        on_order = sum(q for _, q, _ in arrivals)
        position = th + on_order
        rop = d * (lead + safety)
        out_level = d * (lead + review + safety)
        # Day-by-day: when does it run out, given the open POs' dates?
        horizon = lead + review + safety
        q = th
        stockout = None
        ai = 0
        for day in range(0, horizon + 1):
            date = today + timedelta(days=day)
            while ai < len(arrivals) and arrivals[ai][0] <= date:
                q += arrivals[ai][1]
                ai += 1
            if day > 0:
                q -= d
            if q < 0 and stockout is None:
                stockout = date
        suggest = 0
        if position < rop:
            suggest = int(math.ceil((out_level - position) / mult) * mult)
        new_order_lands = today + timedelta(days=lead)
        if stockout is not None and stockout < new_order_lands:
            urgency = "short"          # runs out before anything ordered today
        elif suggest > 0:
            urgency = "order"
        else:
            urgency = "ok"
        line = {
            "item_key": key, "variety": variety, "top4": variety in top4,
            "theoretical": round(th, 1), "weekly_usage": round(wu, 1),
            "usage_basis": basis_u, "forecast_weekly_usage": round(forecast_wu, 1),
            "on_order": round(on_order, 1),
            "next_arrival": arrivals[0][0].date().isoformat() if arrivals else "",
            "weeks_cover_now": round(th / d / 7.0, 1),
            "weeks_cover_with_orders": round(position / d / 7.0, 1),
            "reorder_point": round(rop, 1), "order_up_to": round(out_level, 1),
            "stockout_date": stockout.date().isoformat() if stockout else "",
            "suggest_cs": suggest, "urgency": urgency,
            "usage_from_counts": counts_wu,
        }
        w = by_wh.setdefault(wh, {"warehouse": wh, "distributor": it.get("distributor") or "",
                                  "lead_days": lead, "lead_basis": basis,
                                  "new_order_lands": new_order_lands.date().isoformat(),
                                  "lines": []})
        w["lines"].append(line)

    warehouses = []
    for w in by_wh.values():
        lines = w["lines"]
        need = [l for l in lines if l["suggest_cs"] > 0]
        raw = sum(l["suggest_cs"] for l in need)
        total = raw
        if raw:
            total = int(math.ceil(raw / pallet) * pallet)
            extra = total - raw
            # Spread the pallet top-up a layer at a time onto the lowest cover.
            while extra > 0 and need:
                lowest = min(need, key=lambda l: (l["theoretical"] + l["on_order"]
                                                   + l["suggest_cs"]) / max(l["weekly_usage"], 0.1))
                lowest["suggest_cs"] += mult
                extra -= mult
        w["suggest_total_cs"] = sum(l["suggest_cs"] for l in lines)
        w["suggest_pallets"] = round(w["suggest_total_cs"] / pallet, 2)
        w["short_count"] = sum(1 for l in lines if l["urgency"] == "short")
        w["order_count"] = sum(1 for l in lines if l["urgency"] == "order")
        fc = [(l["forecast_weekly_usage"], l["usage_from_counts"]) for l in lines
              if l["usage_from_counts"] is not None]
        w["usage_note"] = ""
        w["counts_basis_lines"] = sum(1 for l in lines if l["usage_basis"] == "counts")
        if len(fc) >= 3:
            a, b = sum(x for x, _ in fc), sum(y for _, y in fc)
            if a > 0 and abs(b - a) / a >= 0.3:
                w["usage_note"] = (f"Counts over the last ~6 weeks show about {b:.0f} cs/wk "
                                   f"actually used here vs {a:.0f} cs/wk in the forecast"
                                   + (" — suggestions use the counts." if
                                      w["counts_basis_lines"] else
                                      f" — suggestions may run {'high' if b < a else 'low'}."))
        w["pool_note"] = ""
        if transfer_group_for is not None and transfer_group_for(w["warehouse"]):
            w["pool_note"] = ("Pooled with the other Cheney FL DCs — transfers between "
                              "them show up as usage here; check the pool before ordering.")
        rank = {"short": 0, "order": 1, "ok": 2}
        lines.sort(key=lambda l: (rank[l["urgency"]], l["weeks_cover_with_orders"]))
        warehouses.append(w)
    warehouses.sort(key=lambda w: (-w["short_count"], -w["suggest_total_cs"], w["warehouse"]))
    return {"warehouses": warehouses, "params": cfg,
            "excluded_varieties": sorted(excluded),
            "as_of": now.isoformat(timespec="seconds")}


__all__ = ["count_checks", "reorder_suggestions", "lead_times",
           "REORDER_DEFAULTS", "COUNT_CHECK_DEFAULTS"]
