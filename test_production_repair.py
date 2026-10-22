#!/usr/bin/env python3
"""POST /api/admin/production/repair -- re-judges stored records with the
fixed rules. Cases are the real ones from production.json on 2026-10-22."""
import json
import sys
sys.path.insert(0, ".")

import pytest

import inventory_tracker as it
import integrations.production_store as ps

LEDGER = [
    {"po_number": "1135526", "warehouse": "Opa Locka, FL", "distributor": "Chefs Warehouse"},
    {"po_number": "1135525", "warehouse": "Chicago, IL", "distributor": "Chefs Warehouse"},
    {"po_number": "054511751617", "warehouse": "Ocala, FL", "distributor": "Cheney Brothers"},
    {"po_number": "393072B2", "warehouse": "Houston, TX", "distributor": "US Foods"},
]


def _lines(pairs):
    return [{"variety": v, "cs_count": cs, "lot_number": lot, "raw_variety": v.upper(),
             "derived_from": ""} for v, cs, lot in pairs]


RECORDS = [
    # Ocala 054511751617 sent twice; the second adds the Cinnamon Raisin lot
    {"source_message_id": "<oc1>", "source_subject": "Daily Production OCALA.PO.054511751617",
     "warehouse": "Ocala, FL", "warehouse_raw": "OCALA", "distributor": "Cheney Brothers",
     "po_number": "054511751617", "production_date": "2026-09-15",
     "received_at": "2026-09-16T18:34:00Z", "total_cases": 112,
     "lines": _lines([("Poppy Seed", 8, "1152091526"), ("Onion", 24, "1151091526"),
                      ("Asiago", 40, "1159091526"), ("Jalapeno Cheddar", 32, "1189091526"),
                      ("Cinnamon Raisin", 8, "")])},
    {"source_message_id": "<oc2>", "source_subject": "Daily Production Sheet Template.xlsx",
     "warehouse": "Ocala, FL", "warehouse_raw": "OCALA", "distributor": "Cheney Brothers",
     "po_number": "054511751617", "production_date": "2026-09-15",
     "received_at": "2026-09-16T18:40:52Z", "total_cases": 112,
     "lines": _lines([("Poppy Seed", 8, "1152091526"), ("Onion", 24, "1151091526"),
                      ("Asiago", 40, "1159091526"), ("Jalapeno Cheddar", 32, "1189091526"),
                      ("Cinnamon Raisin", 8, "1155091526")])},
    # CWC sheet whose header printed CWFL's PO
    {"source_message_id": "<cwc>", "source_subject": "Daily Production CWC.PO.1135525",
     "warehouse": "Chefs Warehouse, Chicago", "warehouse_raw": "CWC",
     "distributor": "Chefs Warehouse", "po_number": "1135526",
     "production_date": "2026-07-01", "received_at": "2026-07-01T19:42:05Z",
     "total_cases": 112, "lines": _lines([("Plain", 56, ""), ("Everything", 56, "")])},
    # Houston stored under the raw label
    {"source_message_id": "<hou>", "source_subject": "Daily Production US FOODS HOUSTON PO.393072B2",
     "warehouse": "US FOODS HOUSTON", "warehouse_raw": "US FOODS HOUSTON", "distributor": "",
     "po_number": "393072B2", "production_date": "2026-10-13",
     "received_at": "2026-10-16T19:53:02Z", "total_cases": 224,
     "lines": _lines([("Plain", 224, "1150101326")])},
    # back-dated by a typo lot
    {"source_message_id": "<riv>", "source_subject": "Daily Production RIVIERA BEACH PO.014511751616",
     "warehouse": "Riviera Beach, FL", "warehouse_raw": "", "distributor": "Cheney Brothers",
     "po_number": "014511751616", "production_date": "2026-03-10",
     "received_at": "2026-09-11T14:00:00Z", "total_cases": 240,
     "lines": _lines([("Plain", 168, "1150091026"), ("Jalapeno Cheddar", 72, "1189031026")])},
    # "PO. daily production" -> no PO
    {"source_message_id": "<deli>", "source_subject": "DELIBAG PO. daily production",
     "warehouse": "", "warehouse_raw": "", "distributor": "", "po_number": "daily",
     "production_date": "2025-04-24", "received_at": "2025-04-24T17:01:15Z", "total_cases": 56,
     "lines": _lines([("Blueberry", 56, "1171042425")])},
    {"source_message_id": "<stub>", "source_subject": "Daily production US FOOODS CHICAGO PO.5903413Y",
     "warehouse": "", "po_number": "", "lines": [], "total_cases": 0,
     "parse_error": "PDF contained no extractable text (image-only scan?)",
     "received_at": "2026-10-01T19:04:00Z", "production_date": ""},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    monkeypatch.setattr(it, "PRODUCTION_FILE", tmp_path / "production.json")
    monkeypatch.setattr(it, "INVENTORY_AUDIT_FILE", tmp_path / "inventory_audit.json")
    idx = ps.ledger_po_index(LEDGER)
    monkeypatch.setattr(ps, "ledger_po_index", lambda ledger=None: idx)
    it._FILE_CACHE.clear()
    it.save_production(json.loads(json.dumps(RECORDS)))
    yield tmp_path
    it._FILE_CACHE.clear()


def _call(body):
    import blueprints.production as bp
    from app import app
    with app.test_request_context(json=body):
        return bp.api_admin_production_repair().get_json()


def test_dry_run_is_the_default_and_writes_nothing(env):
    out = _call({})
    assert out["dry_run"] is True
    s = out["summary"]
    assert s["duplicates_removed"] == 1 and s["cases_removed"] == 112
    assert s["reclassified"] == 2       # Houston, DeliBag
    assert s["relinked"] == 2           # 1135526 -> 1135525, "daily" -> ""
    assert s["redated"] == 1            # Riviera 2026-03-10 -> 2026-09-10
    assert json.loads((env / "production.json").read_text()) == RECORDS
    assert not (env / "inventory_audit.json").exists()


def test_real_run_repairs_and_is_idempotent(env):
    out = _call({"dry_run": False})
    recs = {r["source_message_id"]: r for r in it.load_production()}
    assert "<oc1>" not in recs and recs["<oc2>"]["replaced"] == ["<oc1>"]
    assert sum(r["total_cases"] for r in recs.values()
               if r["po_number"] == "054511751617") == 112
    assert recs["<cwc>"]["po_number"] == "1135525"
    assert recs["<hou>"]["warehouse"] == "Houston, TX"
    assert recs["<hou>"]["distributor"] == "US Foods"
    assert recs["<riv>"]["production_date"] == "2026-09-10"
    assert recs["<deli>"]["po_number"] == ""
    assert recs["<deli>"]["warehouse"] == "DeliBag, South Korea"
    assert recs["<stub>"]["parse_error"]                       # untouched
    audit = json.loads((env / "inventory_audit.json").read_text())
    assert audit[0]["event_type"] == "production_repair"
    by_id = {c["source_message_id"]: c for c in out["changes"]}
    assert by_id["<riv>"]["before"]["production_date"] == "2026-03-10"
    assert by_id["<riv>"]["after"]["production_date"] == "2026-09-10"
    again = _call({"dry_run": False})
    assert again["summary"]["records_changed"] == 0
    assert again["summary"]["duplicates_removed"] == 0
