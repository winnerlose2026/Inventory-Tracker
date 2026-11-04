#!/usr/bin/env python3
"""Production records reach the PO ledger, the planner and Traceability.

* build_po_ledger attaches {baked_cs, by_variety, dates, lots} per PO (K3).
* The planner nets baked cases out of the to-bake queue and committed
  capacity: Houston 305202B2's 1,120 cs sat in the queue for 11 days after
  it was baked (median bake->ship gap: 3 days).
* Traceability took PO status from on_order + the CW store, so every
  DELIVERED Chefs Warehouse PO read "pending" (rendered "overdue"); it now
  uses the ledger by PO number.
"""
import sys
from datetime import datetime
sys.path.insert(0, ".")

import inventory_tracker as it
import integrations.production_store as ps
from integrations.planning_config import load_planning_config
from integrations.production_planner import build_production_guide

PROD = [
    {"po_number": "OPEN1", "warehouse": "Zebulon, NC", "production_date": "2026-06-29",
     "received_at": "2026-06-29T18:00:00Z",
     "lines": [{"variety": "Plain", "cs_count": 112, "lot_number": "115006292026"},
               {"variety": "Everything", "cs_count": 56, "lot_number": "115806292026"}]},
    {"po_number": "OPEN1", "warehouse": "Zebulon, NC", "production_date": "2026-06-30",
     "received_at": "2026-06-30T18:00:00Z",
     "lines": [{"variety": "Everything", "cs_count": 56, "lot_number": "115806302026"}]},
    {"po_number": "CANC", "warehouse": "Zebulon, NC", "production_date": "2026-06-30",
     "lines": [{"variety": "Plain", "cs_count": 56, "lot_number": "x"}]},
    {"po_number": "", "parse_error": "image-only", "lines": []},
]


def test_baked_by_po_skips_canceled_and_stubs():
    b = ps.baked_by_po(PROD, canceled={"CANC"})
    assert set(b) == {"OPEN1"}
    assert b["OPEN1"]["baked_cs"] == 224
    assert b["OPEN1"]["by_variety"] == {"Plain": 112, "Everything": 112}
    assert b["OPEN1"]["dates"] == ["2026-06-29", "2026-06-30"]
    assert b["OPEN1"]["last_date"] == "2026-06-30"


def test_ledger_attaches_production(monkeypatch):
    import blueprints.pos as pos
    inv = {"plain bagel 4oz [usf - zebulon]": {
        "name": "Plain Bagel 4oz [USF - Zebulon]", "distributor": "US Foods",
        "warehouse": "Zebulon, NC", "quantity": 10,
        "on_order": [{"po_number": "OPEN1", "qty": 112, "unit": "cs"}]}}
    monkeypatch.setattr(it, "load_inventory", lambda: inv)
    monkeypatch.setattr(it, "load_usage", lambda: [])
    monkeypatch.setattr(it, "load_chefs_warehouse_pos", lambda: [])
    monkeypatch.setattr(it, "load_canceled_pos", lambda: {"CANC": {}})
    monkeypatch.setattr(it, "load_status_overrides", lambda: {})
    monkeypatch.setattr(it, "load_po_delivery", lambda: {})
    monkeypatch.setattr(it, "load_production", lambda: PROD)
    monkeypatch.setattr(pos, "_freight_ship_date_index", lambda: {})
    rec = next(r for r in pos.build_po_ledger() if r["po_number"] == "OPEN1")
    assert rec["production"]["baked_cs"] == 224
    assert rec["production"]["lots"][0] == "115006292026"


CFG = load_planning_config()
NOW = datetime(2026, 7, 1)


def _po(po, lines, production=None, **kw):
    r = {"po_number": po, "distributor": "US Foods", "warehouse": "Zebulon, NC",
         "status": "pending", "ship_date": "", "total_cs": sum(q for _v, q in lines),
         "lines": [{"variety": v, "qty": q} for v, q in lines]}
    if production:
        r["production"] = {"by_variety": production, "baked_cs": sum(production.values())}
    r.update(kw)
    return r


def test_planner_removes_baked_cases_from_the_queue():
    ledger = [
        _po("PART", [("Plain", 112), ("Everything", 112)], {"Plain": 112, "Everything": 56}),
        _po("DONE", [("Sesame", 168)], {"Sesame": 168}),
        _po("NONE", [("Onion", 56)]),
    ]
    g = build_production_guide({}, ledger, [], CFG, toast_trend={"available": False}, now=NOW)
    q = {r["po_number"]: r for r in g["production_queue"]}
    assert set(q) == {"PART", "NONE"}                # fully baked PO drops off
    assert q["PART"]["total_cs"] == 56 and q["PART"]["baked_cs"] == 168
    assert [(L["variety"], L["qty"]) for L in q["PART"]["lines"]] == [("Everything", 56)]
    assert g["capacity"]["committed_cs"] == 112      # 56 left on PART + 56 on NONE
    bake = {b["variety"]: b["cs"] for b in g["bake_by_variety"]}
    assert bake == {"Everything": 56, "Onion": 56}


def test_traceability_status_comes_from_the_ledger(monkeypatch):
    import blueprints.pos as pos
    from app import app
    import blueprints.reporting as rep
    prod = [
        {"po_number": "1202942", "warehouse": "Chefs Warehouse, FL",
         "distributor": "Chefs Warehouse", "production_date": "2026-10-07",
         "lines": [{"variety": "Plain", "cs_count": 112, "lot_number": ""}]},
        {"po_number": "1226111", "warehouse": "Chefs Warehouse, FL",
         "distributor": "Chefs Warehouse", "production_date": "2026-10-20",
         "lines": [{"variety": "Plain", "cs_count": 112, "lot_number": ""}]},
        {"po_number": "4068765O", "warehouse": "Manassas, VA", "distributor": "US Foods",
         "production_date": "2026-09-01",
         "lines": [{"variety": "Plain", "cs_count": 56, "lot_number": ""}]},
    ]
    ledger = [
        {"po_number": "1202942", "status": "arrived", "arrival_date": "2026-10-21",
         "warehouse": "Opa Locka, FL", "distributor": "Chefs Warehouse"},
        {"po_number": "1226111", "status": "pending", "ship_date": "", "arrival_date": "",
         "eta": "", "warehouse": "Opa Locka, FL", "distributor": "Chefs Warehouse"},
    ]
    monkeypatch.setattr(it, "load_production", lambda: prod)
    monkeypatch.setattr(it, "load_inventory", lambda: {})
    monkeypatch.setattr(it, "load_usage", lambda: [])
    monkeypatch.setattr(pos, "build_po_ledger", lambda: ledger)
    with app.test_request_context("/api/traceability/search"):
        out = rep.api_traceability_search().get_json()
    st = {r["po_number"]: r["po_status"] for r in out["records"]}
    assert st == {"1202942": "arrived", "1226111": "pending", "4068765O": "unknown"}
    # the ledger's ship-to name filters Chefs Warehouse sheets too
    with app.test_request_context("/api/traceability/search?warehouse=Opa%20Locka,%20FL"):
        out = rep.api_traceability_search().get_json()
    assert {r["po_number"] for r in out["records"]} == {"1202942", "1226111"}
