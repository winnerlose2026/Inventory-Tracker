#!/usr/bin/env python3
"""Tests for the weekly production planner (Phase 4). pytest or standalone."""
import sys
from datetime import datetime
sys.path.insert(0, ".")
from integrations.production_planner import build_production_guide
from integrations.planning_config import load_planning_config

NOW = datetime(2026, 6, 26)
CFG = load_planning_config()


def _inputs():
    inv = {
        "plain bagel 4oz [usf - zebulon]": {"name": "Plain Bagel 4oz [USF - Zebulon]",
            "distributor": "US Foods", "warehouse": "Zebulon, NC", "quantity": 40},
        "plain bagel 4oz [cb - punta gorda]": {"name": "Plain Bagel 4oz [CB - Punta Gorda]",
            "distributor": "Cheney Brothers", "warehouse": "Punta Gorda, FL", "quantity": 8},
        "plain bagel 4oz [cb - riviera beach]": {"name": "Plain Bagel 4oz [CB - Riviera Beach]",
            "distributor": "Cheney Brothers", "warehouse": "Riviera Beach, FL", "quantity": 400},
        "onion bagel 4oz [usf - chicago]": {"name": "Onion Bagel 4oz [USF - Chicago]",
            "distributor": "US Foods", "warehouse": "Chicago, IL", "quantity": 60},
    }
    demand = [
        {"warehouse": "Zebulon, NC", "variety": "Plain", "rate_cs_per_day": 10.0, "stale": False},
        {"warehouse": "Punta Gorda, FL", "variety": "Plain", "rate_cs_per_day": 5.0, "stale": False},
        {"warehouse": "Riviera Beach, FL", "variety": "Plain", "rate_cs_per_day": 8.0, "stale": False},
        {"warehouse": "Chicago, IL", "variety": "Onion", "rate_cs_per_day": 0.0, "stale": False},
    ]
    ledger = []   # no incoming, to force the produce decision cleanly
    return inv, ledger, demand


def _guide():
    inv, ledger, demand = _inputs()
    return build_production_guide(inv, ledger, demand, CFG, toast_trend={"available": False}, now=NOW)


def test_thin_standalone_triggers_produce():
    recs = {(r["unit"], r["variety"]): r for r in _guide()["recommendations"]}
    z = recs[("Zebulon, NC", "Plain")]          # 40 on-hand / 10 per day = 4d cover < 7+7
    assert z["status"] == "produce"
    assert z["recommend_cs"] % CFG["pallet_cs"] == 0 and z["recommend_cs"] > 0
    assert z["produce_by"] is not None and z["top4"] is True


def test_transfer_pool_judged_together():
    recs = {(r["unit"], r["variety"]): r for r in _guide()["recommendations"]}
    # Punta Gorda alone is razor-thin, but the Cheney-FL POOL is healthy -> OK
    cb = recs[("cheney-fl", "Plain")]
    assert cb["is_pool"] is True
    assert sorted(cb["members"]) == ["Punta Gorda, FL", "Riviera Beach, FL"]
    assert cb["on_hand"] == 408.0 and cb["status"] == "ok"
    # and Punta Gorda does NOT appear as its own standalone unit
    assert ("Punta Gorda, FL", "Plain") not in recs


def test_no_demand_signal():
    recs = {(r["unit"], r["variety"]): r for r in _guide()["recommendations"]}
    assert recs[("Chicago, IL", "Onion")]["status"] == "no-demand-data"


def test_capacity_and_summary():
    g = _guide()
    prod = [r for r in g["recommendations"] if r["status"] == "produce"]
    assert g["summary"]["produce_now"] == len(prod) >= 1
    assert g["summary"]["produce_now_top4"] >= 1
    assert g["capacity"]["committed_cs"] == sum(r["recommend_cs"] for r in prod)
    assert isinstance(g["capacity"]["feasible_on_normal_week"], bool)
    # produce items rank ahead of ok items
    order = [r["status"] for r in g["recommendations"]]
    assert order.index("produce") < order.index("ok")


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
