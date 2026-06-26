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


def test_toast_trend_direction():
    rows = []
    # prior window (14-28d before anchor 2026-06-25): lower
    for day in ["2026-06-02", "2026-06-05"]:
        rows.append({"business_date": day, "menu_group": "Bagels", "item": "Dozen Bagels", "qty": 100})
    # recent window (0-14d): higher -> rising
    for day in ["2026-06-20", "2026-06-25"]:
        rows.append({"business_date": day, "item": "Bagel w/ Plain Cream Cheese", "qty": 300})
    rows.append({"business_date": "2026-06-24", "item": "Nova Salmon", "qty": 999})  # non-bagel, ignored
    t = toast_demand_trend(rows, window_days=14)
    assert t["available"] is True and t["direction"] == "rising" and t["pct_change"] > 10
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
