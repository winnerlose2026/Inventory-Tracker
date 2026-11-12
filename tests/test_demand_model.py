#!/usr/bin/env python3
"""Tests for the unified demand model (Phase 3). pytest or standalone."""
import sys
from datetime import datetime
sys.path.insert(0, ".")
from integrations.demand_model import warehouse_demand, pool_demand, toast_demand_trend

NOW = datetime(2026, 6, 26)


def _inv():
    return {
        "plain bagel 4oz [cb - riviera beach]": {
            "name": "Plain Bagel 4oz [CB - Riviera Beach]", "distributor": "Cheney Brothers",
            "warehouse": "Riviera Beach, FL", "weekly_usage": 56, "last_count_at": "2026-06-23T10:00:00"},
        "plain bagel 4oz [cb - ocala]": {
            "name": "Plain Bagel 4oz [CB - Ocala]", "distributor": "Cheney Brothers",
            "warehouse": "Ocala, FL", "weekly_usage": 28, "last_count_at": "2026-06-24T10:00:00"},
        "sesame bagel 4oz [usf - alcoa]": {       # stale
            "name": "Sesame Bagel 4oz [USF - Alcoa]", "distributor": "US Foods",
            "warehouse": "Alcoa, TN", "weekly_usage": 7, "last_count_at": "2026-05-20T10:00:00"},
        "onion bagel 4oz [usf - chicago]": {      # zero usage
            "name": "Onion Bagel 4oz [USF - Chicago]", "distributor": "US Foods",
            "warehouse": "Chicago, IL", "weekly_usage": 0, "last_count_at": "2026-06-25T10:00:00"},
    }


def test_warehouse_demand_rate_freshness_confidence():
    rows = {r["warehouse"] + "|" + r["variety"]: r for r in warehouse_demand(_inv(), now=NOW)}
    riv = rows["Riviera Beach, FL|Plain"]
    assert riv["rate_cs_per_day"] == 8.0          # 56/7
    assert riv["stale"] is False and riv["confidence"] == "high"
    assert riv["transfer_group"] == "cheney-fl" and riv["top4"] is True
    assert rows["Alcoa, TN|Sesame"]["stale"] is True and rows["Alcoa, TN|Sesame"]["confidence"] == "low"
    assert rows["Chicago, IL|Onion"]["confidence"] == "none"   # zero usage


def test_pool_demand_aggregates_cheney_fl():
    pools = {(p["pool"], p["variety"]): p for p in pool_demand(warehouse_demand(_inv(), now=NOW))}
    cb = pools[("cheney-fl", "Plain")]
    assert cb["is_pool"] is True
    assert sorted(cb["members"]) == ["Ocala, FL", "Riviera Beach, FL"]
    assert cb["rate_cs_per_day"] == 12.0          # 8 + 4
    # standalone USF warehouse is its own "pool" key
    assert ("Alcoa, TN", "Sesame") in pools and pools[("Alcoa, TN", "Sesame")]["is_pool"] is False


def _toast_days(store, start_day, n, qty):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start_day)
    return [{"restaurant_guid": store, "business_date": (d0 + timedelta(days=i)).isoformat(),
             "menu_group": "Bagels", "item": "Dozen Bagels", "qty": qty} for i in range(n)]


def test_toast_trend_direction():
    # two stores, 28 full days: prior 14 at 100/day, recent 14 at 130/day
    rows = []
    for st in ("A", "B"):
        rows += _toast_days(st, "2026-05-29", 14, 100) + _toast_days(st, "2026-06-12", 14, 130)
    rows.append({"restaurant_guid": "A", "business_date": "2026-06-24", "item": "Nova Salmon", "qty": 999})
    t = toast_demand_trend(rows, window_days=14)
    assert t["available"] is True and t["direction"] == "rising" and t["pct_change"] == 30.0
    assert t["as_of"] == "2026-06-25" and t["stores"] == 2 and t["matched_location_days"] == 28


def test_toast_trend_compares_only_store_days_present_in_both_windows():
    # Store B has no data in the prior window (its pulls were missing). The
    # old sum-everything trend read that as demand doubling.
    rows = _toast_days("A", "2026-05-29", 28, 100) + _toast_days("B", "2026-06-12", 14, 100)
    t = toast_demand_trend(rows, window_days=14)
    assert t["available"] is False and "not enough comparable" in t["reason"]   # 14 of 28
    # with three full stores the one gappy store is simply left out
    rows += _toast_days("C", "2026-05-29", 28, 100) + _toast_days("D", "2026-05-29", 28, 100)
    t = toast_demand_trend(rows, window_days=14)
    assert t["available"] and t["direction"] == "flat" and t["pct_change"] == 0.0
    assert t["stores"] == 3


def test_toast_trend_needs_coverage_and_skips_the_open_day():
    sparse = [{"business_date": d, "menu_group": "Bagels", "item": "Bagel", "qty": q}
              for d, q in (("2026-06-02", 100), ("2026-06-05", 100),
                           ("2026-06-20", 300), ("2026-06-25", 300))]
    assert toast_demand_trend(sparse)["available"] is False
    rows = _toast_days("A", "2026-05-29", 28, 100) + _toast_days("A", "2026-06-26", 1, 5)
    t = toast_demand_trend(rows, window_days=14, today="2026-06-26")
    assert t["as_of"] == "2026-06-25" and t["pct_change"] == 0.0


def test_toast_trend_no_bagel_rows():
    assert toast_demand_trend([{"business_date": "2026-06-20", "item": "Coffee", "qty": 5}])["available"] is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try:
            fn(); print("ok:", fn.__name__)
        except Exception as e:  # noqa: BLE001
            bad += 1; print("FAIL:", fn.__name__, e)
    print(f"\n{len(fns)-bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)


def test_usage_freshness_is_the_usage_reports_date_not_the_counts():
    inv = {"plain": {"name": "Plain Bagel 4oz [USF - Alcoa]", "distributor": "US Foods",
                     "warehouse": "Alcoa, TN", "weekly_usage": 7,
                     "last_count_at": "2026-06-25T10:00:00",
                     "last_usage_report_at": "2026-04-01T10:00:00"}}
    [r] = warehouse_demand(inv, now=NOW)
    assert r["stale"] is True and r["confidence"] == "low"       # a fresh count no longer masks it
    assert r["usage_as_of_basis"] == "usage_report" and r["count_age_days"] < 1
    inv["plain"].pop("last_usage_report_at")
    [r] = warehouse_demand(inv, now=NOW)
    assert r["stale"] is False and r["usage_as_of_basis"] == "count"   # legacy record
