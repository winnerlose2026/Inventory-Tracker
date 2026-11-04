"""Every load -> change -> save of the data files runs under data_lock.

The scan / ingest-events / Cheney-CSV routes held only _ingest_lock, which
serialises ingests against EACH OTHER. _apply_events' load -> save therefore
raced every data_lock writer: a 05:05 forecast burn landing mid-apply kept its
ledger rows but lost its on-hand decrement (and never re-ran, because its rows
existed). Same for _apply_cw_pos vs the CW ship-date / cancel endpoints, and
/api/sync, /api/seed, /api/migrate-units, which took no lock at all.

Also: the daily burn's idempotency key is the day the burn is FOR
(forecast_date). Keyed on the write timestamp, a backfill for a missed day made
before 05:05 made that day's real run skip.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TOKEN = "test-token-123"
H = {"X-Inventory-Token": TOKEN}
KEY = "plain bagel 4oz [usf - manassas]"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INVENTORY_API_TOKEN", TOKEN)
    import inventory_tracker as it
    monkeypatch.setattr(it, "DATA_DIR", tmp_path)
    for attr, fn in (("INVENTORY_FILE", "inventory.json"), ("USAGE_FILE", "usage.json"),
                     ("CANCELED_POS_FILE", "canceled_pos.json"),
                     ("CHEFS_WAREHOUSE_POS_FILE", "chefs_warehouse_pos.json"),
                     ("PO_DELIVERY_FILE", "po_delivery.json"),
                     ("INVENTORY_AUDIT_FILE", "inventory_audit.json")):
        monkeypatch.setattr(it, attr, tmp_path / fn)
    import sync_inventory as sync
    from seed_bagels import BAGELS
    inv = {b["name"].lower(): dict(b, on_order=[], unit="cs", low_stock_threshold=0)
           for b in BAGELS}
    inv[KEY].update(quantity=100.0, weekly_usage=70.0)
    it.save_inventory(inv)
    it.save_usage([])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, sync, app_module.app.test_client()


def _lock_free_in_another_thread() -> bool:
    import inventory_tracker as it
    res = {}

    def run():
        with it.data_lock(blocking=False) as got:
            res["got"] = got
    t = threading.Thread(target=run)
    t.start()
    t.join(5)
    return res["got"]


def _po_event(po="7400000O"):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    day = datetime.now().date().isoformat()
    return EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=8, distributor="US Foods", variety="Plain",
                      warehouse="Manassas, VA", unit="cases"),
        source_message_id=po, source_subject=po, po_number=po,
        po_revision="0000001", po_order_date=day,
        source_received_at=f"{day}T12:00:00Z")


def test_burn_during_a_scan_apply_waits_and_is_not_lost(env, monkeypatch):
    it, sync, c = env
    real_load_usage = sync.load_usage
    box = {}

    def racing_load_usage():
        # Another gunicorn thread fires the daily burn mid-apply.
        box["t"] = threading.Thread(target=lambda: box.update(
            r=c.post("/api/forecast/decrement-daily", json={}, headers=H).get_json()))
        box["t"].start()
        time.sleep(0.3)                 # unlocked, the burn would finish here
        box["done_mid_apply"] = "r" in box
        return real_load_usage()

    monkeypatch.setattr(sync, "load_usage", racing_load_usage)
    sync._apply_events([_po_event()])
    box["t"].join(10)
    assert box["done_mid_apply"] is False          # it waited for the lock
    inv = it.load_inventory()
    burns = [e for e in it.load_usage()
             if e.get("source") == "forecast-daily" and e["item_key"] == KEY]
    assert len(burns) == 1 and inv[KEY]["quantity"] == 90.0
    assert inv[KEY]["on_order"] and inv[KEY]["on_order"][0]["po_number"] == "7400000O"


def test_apply_cw_pos_holds_the_lock(env, monkeypatch):
    it, sync, _c = env
    seen = {}
    real = sync.load_chefs_warehouse_pos

    def probe():
        seen["free"] = _lock_free_in_another_thread()
        return real()
    monkeypatch.setattr(sync, "load_chefs_warehouse_pos", probe)
    sync._apply_cw_pos([{"po_number": "1209205", "warehouse": "Bronx, NY",
                         "total_cs": 112, "lines": []}])
    assert seen["free"] is False


def test_sync_all_fetches_outside_the_lock_and_applies_inside(env, monkeypatch):
    it, sync, _c = env
    from integrations.base import SyncItem
    seen = {}

    class FakeClient:
        name = "US Foods"

        def _has_live_credentials(self):
            return True

        def fetch_inventory(self):
            seen["fetch_free"] = _lock_free_in_another_thread()   # network I/O here
            return [SyncItem(quantity=42.0, distributor="US Foods", variety="Plain",
                             warehouse="Manassas, VA", name="Plain Bagel 4oz [USF - Manassas]")]
    real = sync.load_usage

    def probe():
        seen["apply_free"] = _lock_free_in_another_thread()
        return real()
    monkeypatch.setattr(sync, "load_usage", probe)
    sync.sync_all([FakeClient()])
    assert seen == {"fetch_free": True, "apply_free": False}
    assert it.load_inventory()[KEY]["quantity"] == 42.0


def test_migrate_units_holds_the_lock(env, monkeypatch):
    it, _sync, c = env
    seen = {}

    def probe(inv):
        seen["free"] = _lock_free_in_another_thread()
        return {"converted": 0}
    monkeypatch.setattr(it, "migrate_units_to_case", probe)
    assert c.post("/api/migrate-units", json={}, headers=H).get_json()["ok"]
    assert seen["free"] is False


def test_backfilling_a_missed_day_does_not_eat_todays_burn(env):
    it, _sync, c = env
    today = datetime.now(timezone.utc).date()
    yday = (today - timedelta(days=1)).isoformat()
    r1 = c.post("/api/forecast/decrement-daily", json={"date": yday}, headers=H).get_json()
    assert any(a["name"].lower() == KEY for a in r1["applied"])
    r2 = c.post("/api/forecast/decrement-daily", json={}, headers=H).get_json()
    assert any(a["name"].lower() == KEY for a in r2["applied"])     # not skipped
    r3 = c.post("/api/forecast/decrement-daily", json={}, headers=H).get_json()
    assert any(n.lower() == KEY for n in r3["skipped_existing"])  # still idempotent
    assert it.load_inventory()[KEY]["quantity"] == 80.0
