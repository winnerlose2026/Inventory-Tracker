"""Freight invoices: locked merge that keeps what it knew, a ship-date index
keyed by PO number only, and an audit trail for re-derived arrivals."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

TOKEN = "test-token-123"
H = {"X-Inventory-Token": TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    import inventory_tracker as it
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json"),
                     ("CANCELED_POS_FILE", "canceled_pos.json"),
                     ("CHEFS_WAREHOUSE_POS_FILE", "chefs_warehouse_pos.json"),
                     ("STATUS_OVERRIDES_FILE", "po_status_overrides.json"),
                     ("PO_DELIVERY_FILE", "po_delivery.json"),
                     ("FREIGHT_INVOICES_FILE", "freight_invoices.json"),
                     ("INVENTORY_AUDIT_FILE", "inventory_audit.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)
    it.save_inventory({})
    it.save_usage([])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, app_module.app.test_client()


def _inv(no, **kw):
    return {"invoice_number": no, "total_due": 1000, "pallets": 10, "cases": 560, **kw}


def test_reingest_with_blank_fields_keeps_the_stored_ship_date_and_po(env):
    it, c = env
    r = c.post("/api/freight/ingest", headers=H, json={"invoices": [
        _inv("F1", ship_date="2026-11-02", po_number="393072B2", carrier="Lineage")]})
    assert r.get_json()["report"]["added"] == 1
    r = c.post("/api/freight/ingest", headers=H, json={"invoices": [
        _inv("F1", ship_date="", po_number="", carrier="Lineage Logistics")]})
    assert r.get_json()["report"]["updated"] == 1
    [rec] = it.load_freight_invoices()
    assert rec["ship_date"] == "2026-11-02" and rec["po_number"] == "393072B2"
    assert rec["carrier"] == "Lineage Logistics"            # other fields replaced
    assert sorted(rec["kept_from_prior"]) == ["po_number", "ship_date"]
    # a non-blank new value still replaces
    c.post("/api/freight/ingest", headers=H, json={"invoices": [
        _inv("F1", ship_date="2026-11-03", po_number="393072B2")]})
    assert it.load_freight_invoices()[0]["ship_date"] == "2026-11-03"


def test_merge_runs_under_the_data_lock_and_dry_run_saves_nothing(env, monkeypatch):
    it, c = env
    import blueprints.freight as F
    depths = []
    real = it.save_freight_invoices
    monkeypatch.setattr(it, "save_freight_invoices",
                        lambda d: (depths.append(getattr(it._LOCK_STATE, "depth", 0)),
                                   real(d))[1])
    assert F._merge_freight_invoices([_inv("F9", ship_date="2026-11-02")], False) == (1, 0, 1)
    assert depths and depths[0] >= 1
    assert F._merge_freight_invoices([_inv("F10")], True) == (1, 0, 2)
    assert len(it.load_freight_invoices()) == 1


def test_index_is_keyed_by_po_number_only(env):
    it, c = env
    import blueprints.freight as F
    it.save_inventory({"plain bagel 4oz [usf - houston]": {
        "name": "Plain Bagel 4oz [USF - Houston]", "distributor": "US Foods",
        "warehouse": "Houston, TX",
        "on_order": [{"po_number": "1025931", "qty": 56}, {"po_number": "7700001B2", "qty": 56}]}})
    it.save_freight_invoices([
        # has a PO: its order number / shipper ref are NOT keys any more
        _inv("F1", ship_date="2026-11-02", po_number="393072B2",
             order_number="1025931", shipper_ref="HHB-7700001B2", distributor="US Foods"),
        # no PO, ref names a US Foods PO, invoice is for US Foods -> used
        _inv("F2", ship_date="2026-11-04", po_number="", shipper_ref="HHB-7700001B2",
             distributor="US Foods"),
        # no PO, ref equals a US Foods PO number but the invoice is Cheney -> ignored
        _inv("F3", ship_date="2026-10-01", po_number="", order_number="1025931",
             distributor="Cheney Brothers"),
        # no PO, ref unknown to the tracker -> ignored
        _inv("F4", ship_date="2026-10-01", po_number="", order_number="555",
             distributor="US Foods"),
    ])
    assert F._freight_ship_date_index() == {"393072B2": "2026-11-02",
                                            "7700001B2": "2026-11-04"}


def test_rederive_writes_an_audit_row(env, monkeypatch):
    it, c = env
    import blueprints.freight as F
    ship = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    it.save_inventory({"plain bagel 4oz [usf - houston]": {
        "name": "Plain Bagel 4oz [USF - Houston]", "distributor": "US Foods",
        "warehouse": "Houston, TX", "quantity": 10,
        "on_order": [{"po_number": "393072B2", "qty": 56, "ship_date": ship,
                      "arrival_date": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%dT00:00:00"),
                      "arrival_source": "ship+transit", "transit_days": 29}]}})
    out = F.rederive_estimated_arrivals(dry_run=True, model={})
    assert out["pos_changed"] == 1
    assert it._read_json(it.INVENTORY_AUDIT_FILE, []) == []
    out = F.rederive_estimated_arrivals(dry_run=False, model={})
    assert out["pos_changed"] == 1
    [row] = [a for a in it._read_json(it.INVENTORY_AUDIT_FILE, [])
             if a.get("event_type") == "arrival_rederived"]
    assert row["po_number"] == "393072B2" and row["source"] == "rederive-estimated-arrivals"
    assert row["old_arrival"] != row["new_arrival"] and row["delta"] == 0
