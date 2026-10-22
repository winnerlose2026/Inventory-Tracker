"""Destructive admin paths: /api/seed reset, remove-po / uncancel-po, and how
the ledger shows a PO canceled after part of it arrived.

  * POST /api/seed {"reset": true} wiped every SKU with no confirmation, and the
    117 per-SKU saves that followed rotated inventory.json.bak away -- no local
    way back. It now needs {"confirm": "RESET"}, snapshots inventory + usage
    to data/backups/ first and seeds with ONE save (.bak = the pre-reset file).
  * remove-po matched the PO exactly while the scanner's canceled filter
    normalises: "hhb-7500000o" removed nothing yet blocked every later
    revision. It deleted pending rows with no trace; uncancel could not bring
    them back. Now: normalised match, audit rows, and the removed rows are kept
    in canceled_pos.json so uncancel restores them.
  * A PO canceled after part of it arrived showed as "canceled" although its
    arrived lines are real cases in on-hand. The ledger now shows it as
    ARRIVED with only its arrived lines, plus `canceled_remainder`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

TOKEN = "test-token-123"
H = {"X-Inventory-Token": TOKEN}
KEY = "plain bagel 4oz [usf - manassas]"
KEY2 = "everything bagel 4oz [usf - manassas]"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
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
    import sync_inventory as sync
    from seed_bagels import BAGELS
    it.save_inventory({b["name"].lower(): dict(b, on_order=[], unit="cs",
                                               low_stock_threshold=0)
                       for b in BAGELS})
    it.save_usage([{"item_key": KEY, "amount": 1.0, "timestamp": "2026-10-01T00:00:00"}])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, sync, app_module.app.test_client()


def _day(n):
    return (datetime.now() + timedelta(days=n)).date().isoformat()


def _book(sync, po, lines=(("Plain", 56),), received_day=-1):
    """Book ONE PO document (all its lines in one apply)."""
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    od = _day(-1)
    rd = _day(received_day)
    sync._apply_events([EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=qty, distributor="US Foods", variety=variety,
                      warehouse="Manassas, VA", unit="cases"),
        source_message_id=po, source_subject=po, po_number=po,
        po_revision="0000001", po_order_date=od, source_received_at=f"{rd}T12:00:00Z")
        for variety, qty in lines])


def _ledger(po):
    from app import app
    from blueprints.pos import build_po_ledger
    with app.test_request_context():
        return next(r for r in build_po_ledger() if r["po_number"] == po)


# --- /api/seed reset ---------------------------------------------------------

def test_seed_reset_needs_confirmation(env):
    it, _sync, c = env
    inv = it.load_inventory()
    inv[KEY]["quantity"] = 123.0
    it.save_inventory(inv)
    r = c.post("/api/seed", json={"reset": True}, headers=H)
    assert r.status_code == 400 and "RESET" in r.get_json()["error"]
    assert it.load_inventory()[KEY]["quantity"] == 123.0


def test_seed_reset_snapshots_and_keeps_the_backup(env):
    it, _sync, c = env
    inv = it.load_inventory()
    inv[KEY]["quantity"] = 123.0
    it.save_inventory(inv)
    r = c.post("/api/seed", json={"reset": True, "confirm": "RESET"}, headers=H).get_json()
    assert r["ok"] and r["added"] == 117
    snap = json.load(open(r["snapshot"]["inventory.json"]))
    assert snap[KEY]["quantity"] == 123.0
    assert json.load(open(r["snapshot"]["usage.json"]))[0]["item_key"] == KEY
    bak = json.load(open(it.INVENTORY_FILE.with_name("inventory.json.bak")))
    assert bak[KEY]["quantity"] == 123.0                 # .bak = the pre-reset file
    assert it.load_inventory()[KEY]["quantity"] != 123.0


def test_seed_without_reset_adds_missing_skus_only(env):
    it, _sync, c = env
    inv = it.load_inventory()
    inv[KEY]["quantity"] = 77.0
    del inv[KEY2]
    it.save_inventory(inv)
    r = c.post("/api/seed", json={}, headers=H).get_json()
    assert r["ok"] and r["added"] == 1 and r["skipped"] == 116
    inv = it.load_inventory()
    assert inv[KEY]["quantity"] == 77.0 and KEY2 in inv


# --- remove-po / uncancel-po ------------------------------------------------------

def test_remove_po_matches_normalised_and_uncancel_restores(env):
    it, sync, c = env
    _book(sync, "7500000O", lines=(("Plain", 56), ("Everything", 24)))
    before = {k: list(it.load_inventory()[k]["on_order"]) for k in (KEY, KEY2)}
    r = c.post("/api/admin/remove-po", json={"po_number": "hhb-7500000o", "dry_run": True},
               headers=H).get_json()
    assert r["dry_run"] and r["removed_entries"] == 2 and r["removed_cs"] == 80
    assert it.load_inventory()[KEY]["on_order"]           # dry run wrote nothing
    r = c.post("/api/admin/remove-po", json={"po_number": "hhb-7500000o"},
               headers=H).get_json()
    assert r["ok"] and r["removed_entries"] == 2 and r["po_number"] == "7500000O"
    inv = it.load_inventory()
    assert not inv[KEY]["on_order"] and not inv[KEY2]["on_order"]
    canceled = it.load_canceled_pos()
    assert len(canceled["7500000O"]["removed_rows"]) == 2
    audit = it.load_inventory_audit(10)
    assert sum(1 for a in audit if a.get("event_type") == "po_removed") == 2
    # A later copy of the PO is ignored by the scanner while it is canceled.
    _book(sync, "7500000O", lines=(("Plain", 64),), received_day=0)
    assert not it.load_inventory()[KEY]["on_order"]
    r = c.post("/api/admin/uncancel-po", json={"po_number": "7500000o"}, headers=H).get_json()
    assert r["was_canceled"] and r["restored_entries"] == 2
    inv = it.load_inventory()
    assert inv[KEY]["on_order"] == before[KEY] and inv[KEY2]["on_order"] == before[KEY2]
    assert "7500000O" not in it.load_canceled_pos()


def test_canceled_after_partial_arrival_shows_arrived_lines(env):
    it, sync, c = env
    PO = "7600000O"
    _book(sync, PO)                                   # Plain stays pending
    usage = it.load_usage()
    usage.append({"item_key": KEY2, "amount": -24.0, "source": "on_order_rollover",
                  "po_number": PO, "arrival_date": f"{_day(-1)}T00:00:00",
                  "timestamp": f"{_day(-1)}T04:00:00", "unit": "cs"})
    it.save_usage(usage)
    assert _ledger(PO)["partial"] is True
    c.post("/api/admin/remove-po", json={"po_number": PO}, headers=H)
    rec = _ledger(PO)
    assert rec["status"] == "arrived"
    assert rec["total_cs"] == 24.0 and [l["item_key"] for l in rec["lines"]] == [KEY2]
    assert rec["canceled_remainder"]["removed_cs"] == 56.0


def test_fully_pending_cancel_is_still_canceled(env):
    it, sync, c = env
    _book(sync, "7700000O")
    c.post("/api/admin/remove-po", json={"po_number": "7700000O"}, headers=H)
    from app import app
    from blueprints.pos import build_po_ledger
    with app.test_request_context():
        assert not any(r["po_number"] == "7700000O" for r in build_po_ledger())


def _rolled_twice(it, counted=20.0, receipt=16.0, weekly=9.0):
    """A PO that rolled in AFTER the count that already contained it."""
    inv = it.load_inventory()
    inv[KEY].update(last_count_at="2026-10-19T13:16:00", last_count_qty=counted,
                    quantity=counted + receipt, weekly_usage=weekly)
    it.save_inventory(inv)
    it.save_usage([{"item_key": KEY, "item_name": inv[KEY]["name"], "amount": -receipt,
                    "unit": "cs", "source": "on_order_rollover", "po_number": "7199415O",
                    "arrival_date": "2026-10-20T00:00:00",
                    "timestamp": "2026-10-20T00:01:51",
                    "note": "PO 7199415O arrived"}])


def test_absorb_po_into_count_removes_the_double_count(env):
    it, _sync, c = env
    _rolled_twice(it)
    body = {"po_number": "7199415O", "count_at": "2026-10-19T13:16:00",
            "arrived_on": "2026-10-16"}
    dry = c.post("/api/admin/absorb-po-into-count", json=body, headers=H).get_json()
    assert dry["ok"] and dry["dry_run"] and dry["absorbed_cs"] == 16
    assert it.load_inventory()[KEY]["quantity"] == 36          # dry run wrote nothing
    live = c.post("/api/admin/absorb-po-into-count",
                  json=dict(body, dry_run=False, reason="Bozel ETA 10/16"), headers=H).get_json()
    assert live["ok"] and not live["dry_run"]
    assert it.load_inventory()[KEY]["quantity"] == 20
    row = it.load_usage()[0]
    assert row["source"] == "on_order_absorbed" and row["amount"] == 0
    assert row["absorbed_qty"] == 16 and row["arrival_date"].startswith("2026-10-16")
    again = c.post("/api/admin/absorb-po-into-count",
                   json=dict(body, dry_run=False, reason="x"), headers=H)
    assert again.status_code == 404                           # idempotent


def test_absorb_refuses_when_the_count_cannot_contain_it(env):
    it, _sync, c = env
    _rolled_twice(it, counted=2.0, receipt=56.0, weekly=1.0)
    r = c.post("/api/admin/absorb-po-into-count",
               json={"po_number": "7199415O", "count_at": "2026-10-19T13:16:00",
                     "arrived_on": "2026-10-16"}, headers=H)
    assert r.status_code == 409
    late = c.post("/api/admin/absorb-po-into-count",
                  json={"po_number": "7199415O", "count_at": "2026-10-19T13:16:00",
                        "arrived_on": "2026-10-20"}, headers=H)
    assert late.status_code == 400
