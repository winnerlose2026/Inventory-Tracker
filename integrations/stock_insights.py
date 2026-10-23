"""Count checks: theoretical vs actual at each count.

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

Suggested orders moved to integrations/order_plan.py (Production Planner).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.util import naive_local_dt, rollover_row_live, absorbed_row_live, absorbed_row_qty

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
        all_lines = list(r.get("lines") or [])
        lines = [l for l in all_lines if not l.get("absorbed")]
        status = r.get("status") or ""
        if status == "canceled":
            continue
        arr = naive_local_dt(r.get("arrival_date") or "")
        rolled = naive_local_dt(r.get("rolled_at") or "")
        bonus = 1.0
        if status != "arrived":
            sign, why = 1, ("is still on order, but the count is OVER by about that "
                            "much on its lines — it may have arrived early")
        elif arr is not None and count_dt - timedelta(days=10) <= arr <= count_dt + timedelta(days=1):
            if rolled is not None and rolled > count_dt:
                # Absorbed lines count here: they are exactly the cases the
                # count already held (Manassas 7199415O, 10/19).
                lines, bonus = all_lines, 1.05
                sign, why = 1, (f"arrived {arr.date().isoformat()} but was booked after "
                                "the count, so the projection did not have it — that "
                                "explains the overage")
            else:
                sign, why = -1, (f"was booked as arrived {arr.date().isoformat()}, but the "
                                 "count is SHORT by about that much on its lines — check "
                                 "it actually landed before the count")
        else:
            continue
        total = sum(_f(l.get("qty")) for l in lines)
        if total <= 0:
            continue
        score = bonus * sum(min(_f(l.get("qty")), max(0.0, sign * var.get(l.get("variety") or "", 0)))
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
    elif len(flagged) >= 3 and not hints:
        hi = sum(1 for l in flagged if (l["variance"] or 0) > 0)
        lo = len(flagged) - hi
        agrees = True
        if w.get("usage_4w_skus") and w.get("weekly_usage"):
            # "usage set too high" only when the counts' own usage is lower
            agrees = (w["usage_4w"] < w["weekly_usage"]) == (hi > lo)
        if max(hi, lo) >= 0.75 * len(flagged) and agrees:
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


__all__ = ["count_checks", "COUNT_CHECK_DEFAULTS"]
