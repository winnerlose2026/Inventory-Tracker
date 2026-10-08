"""Through the Flask test client: the auth gate, and tz-aware dates on the
PO endpoints.

Nothing exercised the before_request gate end to end, and one API call with
a "...Z" ship/arrival date (what JS toISOString() produces) used to store a
tz-aware timestamp that made every later load_inventory() raise TypeError --
every endpoint 500'd until the JSON was hand-edited.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

TOKEN = "test-token-123"


def _setup(tmp: Path):
    os.environ["DATA_DIR"] = str(tmp)
    os.environ["INVENTORY_API_TOKEN"] = TOKEN
    os.environ["FLASK_ENV"] = "development"
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    inventory_tracker.CHEFS_WAREHOUSE_POS_FILE = tmp / "chefs_warehouse_pos.json"
    inventory_tracker.CANCELED_POS_FILE = tmp / "canceled_pos.json"
    inventory_tracker.STATUS_OVERRIDES_FILE = tmp / "po_status_overrides.json"
    import sync_inventory
    sync_inventory.INVENTORY_FILE = inventory_tracker.INVENTORY_FILE
    from seed_bagels import BAGELS
    inventory_tracker.save_inventory(
        {b["name"].lower(): dict(b, on_order=[], unit="cs",
                                 low_stock_threshold=b.get("threshold", 0))
         for b in BAGELS})
    inventory_tracker.save_usage([])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return inventory_tracker, sync_inventory, app_module.app.test_client()


def _book(sync, po, days_ahead=20):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    today = datetime.now(timezone.utc).date()
    od = today.isoformat()
    evt = EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=56, distributor="US Foods", variety="Plain",
                      warehouse="Manassas, VA", unit="cases"),
        source_message_id="m1", source_subject=po, po_number=po,
        po_revision="0000001", po_order_date=od,
        source_received_at=f"{od}T12:00:00Z",
        source_sender="northeastconfirmations.shared@usfoods.com")
    sync._apply_events([evt], dry_run=False)


def test_api_reads_require_a_token_and_header_only():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync, c = _setup(Path(td))
        assert c.get("/api/pos/ledger").status_code == 401
        assert c.get(f"/api/pos/ledger?token={TOKEN}").status_code == 401
        r = c.get("/api/pos/ledger", headers={"X-Inventory-Token": TOKEN})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        # non-ASCII token must be a clean 401, not a 500 from compare_digest
        assert c.get("/api/pos/ledger",
                     headers={"X-Inventory-Token": "tök"}).status_code == 401
        assert c.get("/healthz").status_code == 200


def test_tz_aware_ship_date_does_not_break_every_read():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync, c = _setup(Path(td))
        _book(sync, "9999991O")
        H = {"X-Inventory-Token": TOKEN}
        future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z")
        r = c.post("/api/on-order/ship-date", json={
            "po_number": "9999991O", "ship_date": future,
            "arrival_date": future}, headers=H)
        assert r.status_code == 200, r.get_json()
        # Stored naive, and every read still works.
        entry = next(p for i in it.load_inventory().values()
                     for p in (i.get("on_order") or []) if p.get("po_number") == "9999991O")
        assert "Z" not in entry["ship_date"] and "+" not in entry["ship_date"]
        for path in ("/api/inventory", "/api/pos/ledger", "/api/distributors",
                     "/api/arrived-pos"):
            assert c.get(path, headers=H).status_code == 200, path
        led = c.get("/api/pos/ledger", headers=H).get_json()
        po = next(p for p in led["pos"] if p["po_number"] == "9999991O")
        assert po["status"] == "in_transit"


def test_admin_po_order_date_accepts_z_and_marks_operator():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync, c = _setup(Path(td))
        _book(sync, "9999992O")
        H = {"X-Inventory-Token": TOKEN}
        r = c.post("/api/admin/po-order-date",
                   json={"po_number": "9999992O",
                         "order_date": "2026-10-01T00:00:00Z"}, headers=H)
        assert r.status_code == 200
        entry = next(p for i in it.load_inventory().values()
                     for p in (i.get("on_order") or []) if p.get("po_number") == "9999992O")
        assert entry["ordered_at_source"] == "operator"
        assert c.get("/api/inventory", headers=H).status_code == 200
