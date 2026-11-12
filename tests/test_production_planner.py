#!/usr/bin/env python3
"""Tests for the weekly production planner (Phase 4, PO-driven). pytest/standalone."""
import sys
from datetime import datetime
sys.path.insert(0, ".")
from integrations.production_planner import build_production_guide
from integrations.planning_config import load_planning_config

NOW = datetime(2026, 7, 1)
CFG = load_planning_config()


def _guide():
    ledger = [
        {"po_number": "OPEN1", "distributor": "US Foods", "warehouse": "Zebulon, NC",
         "status": "pending", "ship_date": "", "total_cs": 224,
         "lines": [{"variety": "Plain", "qty": 112}, {"variety": "Everything", "qty": 112}]},
        {"po_number": "SHIP1", "distributor": "US Foods", "warehouse": "La Mirada, CA",
         "status": "in_transit", "ship_date": "2026-06-25T00:00:00", "total_cs": 168,
         "ship_date_source": "freight",
         "lines": [{"variety": "Sesame", "qty": 168}]},
        {"po_number": "ARR1", "distributor": "Cheney Brothers", "warehouse": "Ocala, FL",
         "status": "arrived", "total_cs": 112, "lines": [{"variety": "Plain", "qty": 112}]},
        {"po_number": "PROD1", "distributor": "Cheney Brothers", "warehouse": "Punta Gorda, FL",
         "status": "in_production", "ship_date": "2026-07-05T00:00:00", "total_cs": 56,
         "transfer_group": "cheney-fl", "lines": [{"variety": "Cinnamon Raisin", "qty": 56}]},
    ]
    inv = {  # Chicago Plain depleting, no open PO -> buffer_watch; Zebulon Plain has OPEN1
        "plain bagel 4oz [usf - chicago]": {"name": "Plain Bagel 4oz [USF - Chicago]",
            "distributor": "US Foods", "warehouse": "Chicago, IL", "quantity": 20},
        "plain bagel 4oz [usf - zebulon]": {"name": "Plain Bagel 4oz [USF - Zebulon]",
            "distributor": "US Foods", "warehouse": "Zebulon, NC", "quantity": 300},
    }
    demand = [
        {"warehouse": "Chicago, IL", "variety": "Plain", "rate_cs_per_day": 5.0, "stale": False},
        {"warehouse": "Zebulon, NC", "variety": "Plain", "rate_cs_per_day": 10.0, "stale": False},
    ]
    return build_production_guide(inv, ledger, demand, CFG, toast_trend={"available": False}, now=NOW)


def test_open_pos_are_the_production_queue():
    g = _guide()
    q = {r["po_number"]: r for r in g["production_queue"]}
    assert set(q) == {"OPEN1", "PROD1"}          # shipped + arrived excluded
    assert "SHIP1" not in q and "ARR1" not in q
    assert q["OPEN1"]["produce_by"] is None and q["OPEN1"]["urgency"] == "ASAP"
    assert q["PROD1"]["produce_by"] == "2026-07-04"   # ship 7/5 minus a day
    assert g["summary"]["produce_now_cs"] == 280 and g["summary"]["produce_now_pos"] == 2


def test_bake_by_variety_and_capacity():
    g = _guide()
    bake = {b["variety"]: b for b in g["bake_by_variety"]}
    assert bake["Plain"]["cs"] == 112 and bake["Everything"]["cs"] == 112 and bake["Cinnamon Raisin"]["cs"] == 56
    assert "Sesame" not in bake                   # that PO already shipped
    assert g["capacity"]["committed_cs"] == 280


def test_priority_usf_before_cheney():
    g = _guide()
    order = [r["po_number"] for r in g["production_queue"]]
    assert order.index("OPEN1") < order.index("PROD1")   # both top-4; US Foods first


def test_buffer_watch_flags_depleting_without_open_po():
    g = _guide()
    bw = {(r["unit"], r["variety"]) for r in g["buffer_watch"]}
    assert ("Chicago, IL", "Plain") in bw          # depleting, no open PO
    assert ("Zebulon, NC", "Plain") not in bw       # has OPEN1 -> not double-flagged


def test_shipped_po_covers_buffer_and_is_not_produced():
    # A shipped (in_transit) PO is already baked: it must NOT be in the queue,
    # but its cases DO net as incoming supply for the buffer check.
    ledger = [
        {"po_number": "SHIP-LM", "distributor": "US Foods", "warehouse": "La Mirada, CA",
         "status": "in_transit", "ship_date": "2026-06-26T00:00:00", "total_cs": 168,
         "ship_date_source": "lineage",
         "lines": [{"variety": "Sesame", "qty": 168}]},
    ]
    inv = {"sesame bagel 4oz [usf - la mirada]": {"name": "Sesame Bagel 4oz [USF - La Mirada]",
        "distributor": "US Foods", "warehouse": "La Mirada, CA", "quantity": 10}}
    demand = [{"warehouse": "La Mirada, CA", "variety": "Sesame", "rate_cs_per_day": 8.0, "stale": False}]
    g = build_production_guide(inv, ledger, demand, CFG, toast_trend={"available": False}, now=NOW)
    assert g["production_queue"] == []                 # shipped -> nothing to bake
    assert g["summary"]["produce_now_cs"] == 0
    # 10 on-hand + 168 shipped-incoming = 178 / 8 ~= 22d cover -> NOT a buffer risk
    assert ("La Mirada, CA", "Sesame") not in {(w["unit"], w["variety"]) for w in g["buffer_watch"]}


def test_truly_empty_queue_reports_zero():
    # Legit zero case: everything arrived/canceled, healthy stock, no demand risk.
    ledger = [{"po_number": "A", "distributor": "US Foods", "warehouse": "Alcoa, TN",
               "status": "arrived", "total_cs": 112, "lines": [{"variety": "Plain", "qty": 112}]}]
    inv = {"plain bagel 4oz [usf - alcoa]": {"name": "Plain Bagel 4oz [USF - Alcoa]",
        "distributor": "US Foods", "warehouse": "Alcoa, TN", "quantity": 500}}
    demand = [{"warehouse": "Alcoa, TN", "variety": "Plain", "rate_cs_per_day": 2.0, "stale": False}]
    g = build_production_guide(inv, ledger, demand, CFG, toast_trend={"available": False}, now=NOW)
    assert g["summary"]["produce_now_pos"] == 0 and g["buffer_watch"] == []


def _po(**kw):
    base = {"po_number": "P1", "distributor": "US Foods", "warehouse": "Zebulon, NC",
            "status": "in_transit", "ship_date": "2026-06-29", "total_cs": 112,
            "lines": [{"variety": "Plain", "qty": 112}]}
    base.update(kw)
    return base


def test_ship_date_passed_but_nothing_baked_stays_queued_and_late():
    # The ledger calls a PO "in transit" at 00:00 of its ship date. With no
    # freight/Lineage proof and nothing baked, it missed its truck.
    g = build_production_guide({}, [_po()], [], CFG, now=NOW)
    q = g["production_queue"]
    assert [r["po_number"] for r in q] == ["P1"]
    assert q[0]["late"] is True and q[0]["late_days"] == 3      # 6/29 -> 7/1
    assert g["late_pos"] == ["P1"] and g["summary"]["late_pos"] == 1


def test_shipped_when_verified_baked_or_operator_says_so():
    for kw in ({"ship_date_source": "freight"}, {"ship_date_source": "lineage"},
               {"override": "in_transit"},
               {"production": {"baked_cs": 112, "by_variety": {"Plain": 112}}},
               {"status": "arrived"}):
        g = build_production_guide({}, [_po(**kw)], [], CFG, now=NOW)
        assert g["production_queue"] == [], kw


def test_partly_baked_late_po_queues_only_the_remainder():
    po = _po(production={"baked_cs": 56, "by_variety": {"Plain": 56}})
    g = build_production_guide({}, [po], [], CFG, now=NOW)
    assert g["production_queue"][0]["total_cs"] == 56 and g["production_queue"][0]["late"]


def test_late_po_shows_in_load_overdue():
    from integrations.order_plan import production_load
    g = build_production_guide({}, [_po()], [], CFG, now=NOW)
    load = production_load(g["production_queue"], {"warehouses": []}, CFG, [], now=NOW)
    assert load["overdue_pos"] == ["P1"]


def test_capacity_card_uses_the_load_tables_cap():
    from integrations.order_plan import production_load
    g = _guide()
    load = production_load([], {"warehouses": []}, CFG, [], now=NOW)
    assert g["capacity"]["capacity_cs_per_day"] == load["cap_cs_per_day"] == 336
    assert g["capacity"]["weekly_capacity_cs"] == 336 * 5
    assert g["capacity"]["weekly_max_cs"] == 336 * 5        # was 280/day fallback


def _bw(ledger, qty=20):
    inv = {"plain bagel 4oz [usf - chicago]": {"name": "Plain Bagel 4oz [USF - Chicago]",
           "distributor": "US Foods", "warehouse": "Chicago, IL", "quantity": qty}}
    demand = [{"warehouse": "Chicago, IL", "variety": "Plain", "rate_cs_per_day": 5.0, "stale": False}]
    g = build_production_guide(inv, ledger, demand, CFG, now=NOW)
    return {(r["unit"], r["variety"]): r for r in g["buffer_watch"]}


def test_open_po_too_small_or_too_late_does_not_hide_buffer_flag():
    # need 14 days x 5 cs = 70 cs; 20 on hand
    small = [{"po_number": "S", "distributor": "US Foods", "warehouse": "Chicago, IL",
              "status": "pending", "ship_date": "", "arrival_date": "2026-07-08",
              "lines": [{"variety": "Plain", "qty": 16}]}]
    assert ("Chicago, IL", "Plain") in _bw(small)
    late = [dict(small[0], arrival_date="2026-08-20",
                 lines=[{"variety": "Plain", "qty": 500}])]
    row = _bw(late)[("Chicago, IL", "Plain")]
    assert row["open_po_cs"] == 500 and row["open_po_in_time_cs"] == 0
    enough = [dict(small[0], lines=[{"variety": "Plain", "qty": 112}])]
    assert ("Chicago, IL", "Plain") not in _bw(enough)


def test_buffer_watch_skips_hidden_and_finished_lto():
    inv = {"pump": {"name": "Pumpernickel Bagel 4oz [USF - Chicago]", "category": "bagels-lto",
                    "distributor": "US Foods", "warehouse": "Chicago, IL", "quantity": 1},
           "hid": {"name": "Onion Bagel 4oz [USF - Chicago]", "hidden": True,
                   "distributor": "US Foods", "warehouse": "Chicago, IL", "quantity": 1}}
    demand = [{"warehouse": "Chicago, IL", "variety": v, "rate_cs_per_day": 5.0, "stale": False}
              for v in ("Pumpernickel", "Onion")]
    g = build_production_guide(inv, [], demand, CFG, now=NOW)
    assert g["buffer_watch"] == []


def test_planning_inventory_filter():
    from integrations.planning_config import planning_inventory
    inv = {"a": {"hidden": True}, "b": {"category": "bagels-lto"}, "c": {}}
    assert set(planning_inventory(inv)) == {"b", "c"}
    assert set(planning_inventory(inv, drop_finished_lto=True)) == {"c"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        try: fn(); print("ok:", fn.__name__)
        except Exception as e: bad += 1; print("FAIL:", fn.__name__, e)
    print(f"\n{len(fns)-bad}/{len(fns)} passed"); sys.exit(1 if bad else 0)
