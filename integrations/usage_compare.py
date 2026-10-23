"""Reps' usage vs count-based usage -- which one predicts what is really used?

Two candidate usage figures drive how fast cases are consumed between counts
(the daily burn), the run-out dates, the suggested orders and so the
production plan:

  * REPS   -- the weekly usage the warehouse reps report (US Foods report
              columns, Cheney's usage grid, Tom Foley's typed lines). This is
              what the daily burn uses today. Its value on any past day is
              recorded exactly: each "forecast-daily" usage row is that day's
              weekly_usage / 7.
  * COUNTS -- what the counts themselves show was used: previous count +
              deliveries in between - this count, over the last few count
              windows.

BACKTEST (no peeking). For every past count window of every SKU:
    actual  = q_start + receipts in the window - q_end
    reps    = the rep figure in force during the window x its length
              (mean of that window's forecast-daily rows x days)
    counts  = the count-based rate from up to 3 EARLIER clean windows x days
              (needs >= 2 of them, >= 10 days)
Windows where stock rose with no delivery on record are left out, and so is
the window right before one (that pair is usually a delivery booked on the
wrong side of a count), so neither source is judged on bad data. Pure.
"""
from __future__ import annotations

from datetime import timedelta

from core.util import naive_local_dt
from integrations.stock_insights import _count_groups, _receipts_between, _f, _variety


def _mean_burn(rows: list, start, end):
    vals = []
    for r in rows:
        if (r.get("source") or "") != "forecast-daily":
            continue
        t = naive_local_dt(r.get("timestamp"))
        if t is not None and start < t <= end:
            vals.append(_f(r.get("amount")))
    return (sum(vals) / len(vals)) if vals else None


def sku_windows(item: dict, key: str, audit_rows: list, usage_rows: list,
                usage_by_key: dict) -> list:
    """Every count-to-count window on record for one SKU, oldest first."""
    groups = _count_groups(audit_rows)
    pts = [(g["_dt"], g["ts"], _f(g.get("new_quantity"))) for g in groups]
    cdt = naive_local_dt(item.get("last_count_at") or "")
    if cdt is not None and item.get("last_count_qty") is not None:
        pts = [p for p in pts if p[0] < cdt - timedelta(hours=12)]
        pts.append((cdt, item.get("last_count_at"), _f(item.get("last_count_qty"))))
    out = []
    for (d0, t0, q0), (d1, t1, q1) in zip(pts, pts[1:]):
        days = (d1 - d0).total_seconds() / 86400.0
        if days < 3:
            continue
        rec = _receipts_between(usage_by_key, key, t0, t1)
        used = q0 + rec - q1
        burn = _mean_burn(usage_rows, d0, d1)
        out.append({"start": d0.isoformat(timespec="minutes"),
                    "end": d1.isoformat(timespec="minutes"), "days": days,
                    "q0": q0, "q1": q1, "receipts": rec, "used": used,
                    "rep_daily": burn,
                    "valid": used >= -max(2.0, 0.05 * q0)})
    # the window before a rise is suspect too (delivery on the wrong side)
    for i in range(len(out) - 1):
        if not out[i + 1]["valid"]:
            out[i]["suspect"] = True
    return out


def backtest(inv: dict, usage: list, audit: list, exclude_categories=("bagels-lto",),
             trailing: int = 3) -> dict:
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

    by_wh: dict = {}
    first_day, last_day = None, None
    for key, it in inv.items():
        if (it.get("category") or "") in exclude_categories:
            continue
        wins = sku_windows(it, key, audit_by_name.get((it.get("name") or key).lower()) or [],
                           usage_by_key.get(key, []), usage_by_key)
        wh = it.get("warehouse") or ""
        w = by_wh.setdefault(wh, {"warehouse": wh, "distributor": it.get("distributor") or "",
                                  "windows": 0, "days": 0.0, "actual": 0.0,
                                  "reps_pred": 0.0, "counts_pred": 0.0,
                                  "reps_abs": 0.0, "counts_abs": 0.0,
                                  "counts_windows": 0, "counts_actual": 0.0,
                                  "reps_on_counts_windows": 0.0, "reps_abs_same": 0.0,
                                  "counts_closer": 0, "excluded": 0, "skus": []})
        clean = [x for x in wins if x["valid"] and not x.get("suspect")]
        sku = {"variety": _variety(it.get("name") or key), "item_key": key,
               "windows": 0, "actual_wk": None, "reps_wk": None, "counts_wk": None}
        s_act = s_rep = s_days = 0.0
        s_cnt = s_cnt_act = s_cnt_days = 0.0
        for i, x in enumerate(wins):
            if not x["valid"] or x.get("suspect"):
                w["excluded"] += 1
                continue
            if x["rep_daily"] is None:
                continue
            rep = x["rep_daily"] * x["days"]
            act = max(0.0, x["used"])
            w["windows"] += 1
            w["days"] += x["days"]
            w["actual"] += act
            w["reps_pred"] += rep
            w["reps_abs"] += abs(rep - act) / x["days"] * 7.0
            s_act += act
            s_rep += rep
            s_days += x["days"]
            sku["windows"] += 1
            d0 = naive_local_dt(x["start"])
            if first_day is None or d0 < first_day:
                first_day = d0
            d1 = naive_local_dt(x["end"])
            if last_day is None or d1 > last_day:
                last_day = d1
            prior = [p for p in clean if naive_local_dt(p["end"]) <= d0][-trailing:]
            pdays = sum(p["days"] for p in prior)
            if len(prior) >= 2 and pdays >= 10:
                rate = max(0.0, sum(p["used"] for p in prior)) / pdays
                cnt = rate * x["days"]
                w["counts_pred"] += cnt
                w["counts_abs"] += abs(cnt - act) / x["days"] * 7.0
                w["counts_windows"] += 1
                w["counts_actual"] += act
                w["reps_on_counts_windows"] += rep
                w["reps_abs_same"] += abs(rep - act) / x["days"] * 7.0
                if abs(cnt - act) < abs(rep - act):
                    w["counts_closer"] += 1
                s_cnt += cnt
                s_cnt_act += act
                s_cnt_days += x["days"]
        if s_days:
            sku["actual_wk"] = round(s_act / s_days * 7.0, 1)
            sku["reps_wk"] = round(s_rep / s_days * 7.0, 1)
        if s_cnt_days:
            sku["counts_wk"] = round(s_cnt / s_cnt_days * 7.0, 1)
        sku["reps_now"] = round(_f(it.get("weekly_usage")), 1)
        w["skus"].append(sku)

    rows = []
    for w in by_wh.values():
        n = w["windows"]
        if not n:
            continue
        row = {
            "warehouse": w["warehouse"], "distributor": w["distributor"],
            "windows": n, "excluded_windows": w["excluded"],
            "actual_cs": round(w["actual"], 0),
            "reps_cs": round(w["reps_pred"], 0),
            "reps_bias_pct": round((w["reps_pred"] - w["actual"]) / w["actual"] * 100, 0)
            if w["actual"] else None,
            "reps_miss_per_sku_wk": round(w["reps_abs"] / n, 1),
            "counts_windows": w["counts_windows"],
            "counts_cs": round(w["counts_pred"], 0) if w["counts_windows"] else None,
            "counts_bias_pct": round((w["counts_pred"] - w["counts_actual"])
                                     / w["counts_actual"] * 100, 0)
            if w["counts_windows"] and w["counts_actual"] else None,
            "counts_miss_per_sku_wk": round(w["counts_abs"] / w["counts_windows"], 1)
            if w["counts_windows"] else None,
            "reps_miss_same_windows": round(w["reps_abs_same"] / w["counts_windows"], 1)
            if w["counts_windows"] else None,
            "counts_closer_pct": round(w["counts_closer"] / w["counts_windows"] * 100, 0)
            if w["counts_windows"] else None,
            "skus": sorted(w["skus"], key=lambda s: s["variety"]),
        }
        if w["counts_windows"] and w["counts_actual"]:
            row["reps_bias_same_windows_pct"] = round(
                (w["reps_on_counts_windows"] - w["counts_actual"]) / w["counts_actual"] * 100, 0)
        rows.append(row)
    rows.sort(key=lambda r: r["warehouse"])
    # Cheney FL DCs transfer cases between themselves: what one rep calls
    # usage can be another DC's stock, so judge the pool as one unit too.
    try:
        from integrations.planning_config import load_planning_config
        pools = (load_planning_config().get("transfer_groups") or {})
    except Exception:  # noqa: BLE001
        pools = {}
    pool_rows = []
    for g, members in pools.items():
        mem = [r for r in rows if r["warehouse"] in members]
        if len(mem) < 2:
            continue
        a = sum(r["actual_cs"] for r in mem)
        rp = sum(r["reps_cs"] for r in mem)
        cw = [r for r in mem if r["counts_cs"] is not None]
        ca = sum(by_wh[r["warehouse"]]["counts_actual"] for r in cw)
        cp = sum(by_wh[r["warehouse"]]["counts_pred"] for r in cw)
        pool_rows.append({"pool": g, "warehouses": [r["warehouse"] for r in mem],
                          "actual_cs": a, "reps_cs": rp,
                          "reps_bias_pct": round((rp - a) / a * 100, 0) if a else None,
                          "counts_bias_pct": round((cp - ca) / ca * 100, 0) if ca else None})
    tot_a = sum(r["actual_cs"] for r in rows)
    tot_r = sum(r["reps_cs"] for r in rows)
    return {
        "warehouses": rows,
        "from": first_day.date().isoformat() if first_day else "",
        "to": last_day.date().isoformat() if last_day else "",
        "pools": pool_rows,
        "total": {"actual_cs": tot_a, "reps_cs": tot_r,
                  "reps_bias_pct": round((tot_r - tot_a) / tot_a * 100, 0) if tot_a else None},
        "method": ("Each past count window per SKU: actual = start count + deliveries "
                   "- end count; reps = the rep figure in force x days; counts = the "
                   "rate from up to 3 earlier clean windows x days. Windows with a rise "
                   "and no delivery on record (and the one before) are left out."),
    }


__all__ = ["sku_windows", "backtest"]
