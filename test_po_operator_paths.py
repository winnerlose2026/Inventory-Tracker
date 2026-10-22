"""Pending POs operator paths: Reopen, clearing a ship date, re-sent-after-count
holds, Adjust on split-line SKUs, and the ledger's in_transit status.

  * Reopen on a Lineage-DELIVERED PO used to be undone by the next read: the
    restored lines met _delivery_gate -> "delivered" and rolled straight back.
    Reopen now reopens the delivery record too (po_delivery.reopen_delivered).
  * Clearing a ship date left `eta` at the cleared estimate and kept
    arrival_source="operator", so the PO still rolled on the cleared date and
    the Lineage hold was bypassed for good.
  * rebooked_after_count was write-only: a PO re-sent after a count absorbed it
    rolled into on-hand a second time. It now holds until an operator sets the
    ship date or Lineage confirms delivery, and the ledger exposes it.
  * Adjust wrote the same received qty onto EVERY live row of a SKU; a
    split-line SKU (Riviera Beach 014511649940 Poppy 48 + 32) recorded 2N,
    and the tab's own "Save" (one input per line, last-wins) wrote 32 onto both.
  * Ledger "in_transit" required only a ship date; a ship date in the future is
    not in transit.
"""
from __future__ import annotations

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
    it.save_usage([])
    import app as app_module
    app_module.app.config["TESTING"] = True
    return it, sync, app_module.app.test_client()


def _day(n):
    return (datetime.now() + timedelta(days=n)).date().isoformat()


def _book(sync, po, qty=56, order_day=None, received_day=None):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    od = order_day or _day(-3)
    sync._apply_events([EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=qty, distributor="US Foods", variety="Plain",
                      warehouse="Manassas, VA", unit="cases"),
        source_message_id=po, source_subject=po, po_number=po,
        po_revision="0000001", po_order_date=od,
        source_received_at=f"{received_day or od}T12:00:00Z")])


def _ledger(po):
    from app import app
    from blueprints.pos import build_po_ledger
    with app.test_request_context():
        return next(r for r in build_po_ledger() if r["po_number"] == po)


# --- Reopen on a Lineage-delivered PO --------------------------------------

def test_reopen_delivered_helper():
    from integrations.po_delivery import reopen_delivered
    now = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
    rec = {"po_number": "X", "status": "delivered", "delivery_appt": "2026-10-19T08:00:00-05:00",
           "delivered_at": "2026-10-19T08:00:00-05:00", "events": []}
    out = reopen_delivered(rec, now, note="wrong PO")
    assert out["status"] == "scheduled" and out["delivered_at"] == ""
    ev = out["events"][-1]
    assert ev["event"] == "reopened" and ev["by"] == "operator"
    assert ev["reopened_delivered_at"] == "2026-10-19T08:00:00-05:00"
    assert rec["status"] == "delivered"                     # input not mutated
    no_appt = reopen_delivered(dict(rec, delivery_appt=""), now)
    assert no_appt["status"] == "requested"
    assert reopen_delivered({"status": "scheduled"}, now)["status"] == "scheduled"


def test_reopen_of_lineage_delivered_po_stays_reopened(env):
    it, sync, c = env
    from integrations import po_delivery as pd
    PO = "7199415O"
    _book(sync, PO)
    it.save_po_delivery({PO: {"po_number": PO, "status": "delivered",
                              "delivery_appt": f"{_day(-1)}T08:00:00-05:00",
                              "delivered_at": f"{_day(-1)}T08:00:00-05:00",
                              "requested_at": f"{_day(-3)}T12:00:00Z", "events": []}})
    inv = it.load_inventory()
    q0 = inv[KEY]["quantity"]
    assert not inv[KEY]["on_order"]
    r = c.post("/api/pending/reopen", json={"po_number": PO}, headers=H).get_json()
    assert r["ok"] and r["removed_cs"] == 56 and r["delivery_reopened"] == "scheduled"
    inv = it.load_inventory()                                # any later read
    assert inv[KEY]["on_order"] and inv[KEY]["on_order"][0]["po_number"] == PO
    assert inv[KEY]["quantity"] == q0 - 56
    trk = it.load_po_delivery()[PO]
    assert trk["status"] == "scheduled" and trk["events"][-1]["event"] == "reopened"
    assert _ledger(PO)["status"] != "arrived"
    # A NEW confirmation releases it again.
    now = datetime.now(timezone.utc)
    rec, change, err = pd.apply_event(trk, {
        "po_number": PO, "event": "delivered",
        "delivered_at": (now - timedelta(hours=2)).isoformat(),
        "evidence": {"sender": "bmorris@onelineage.com", "quote": "shows delivered",
                     "sent": now.isoformat()}}, now)
    assert err is None and change
    it.save_po_delivery({PO: rec})
    inv = it.load_inventory()
    assert not inv[KEY]["on_order"] and inv[KEY]["quantity"] == q0


# --- clearing a ship date ---------------------------------------------------

def test_clearing_ship_date_restores_the_booked_eta(env):
    it, sync, c = env
    PO = "7300000O"
    _book(sync, PO, order_day=_day(-10))
    eta0 = it.load_inventory()[KEY]["on_order"][0]["eta"]
    r = c.post("/api/on-order/ship-date", json={"po_number": PO, "ship_date": _day(1)},
               headers=H).get_json()
    assert it.load_inventory()[KEY]["on_order"][0]["eta"] == r["arrival_date"] != eta0
    c.post("/api/on-order/ship-date", json={"po_number": PO, "ship_date": ""}, headers=H)
    e = it.load_inventory()[KEY]["on_order"][0]
    assert e["eta"] == eta0 and e["arrival_date"] == "" and "arrival_source" not in e


def test_clearing_ship_date_restores_received_date_floor(env):
    """USF re-cut 393072B2 style: PO dated weeks before its email arrived."""
    it, sync, c = env
    PO = "7300002O"
    _book(sync, PO, order_day=_day(-40), received_day=_day(-5))
    eta0 = it.load_inventory()[KEY]["on_order"][0]["eta"]
    assert eta0.startswith(_day(25))
    c.post("/api/on-order/ship-date", json={"po_number": PO, "ship_date": _day(2)}, headers=H)
    c.post("/api/on-order/ship-date", json={"po_number": PO, "ship_date": ""}, headers=H)
    assert it.load_inventory()[KEY]["on_order"][0]["eta"] == eta0


def test_clearing_operator_arrival_reinstates_lineage_hold(env):
    it, sync, c = env
    PO = "7300001O"
    _book(sync, PO, order_day=_day(-10))
    it.save_po_delivery({PO: {"po_number": PO, "status": "requested"}})
    c.post("/api/on-order/ship-date", json={"po_number": PO, "ship_date": _day(-3),
                                            "arrival_date": _day(2)}, headers=H)
    c.post("/api/on-order/ship-date", json={"po_number": PO, "ship_date": ""}, headers=H)
    e = it.load_inventory()[KEY]["on_order"][0]
    assert "arrival_source" not in e
    assert it._delivery_gate(e, it.load_po_delivery()) == ("hold", None)


# --- rebooked_after_count ----------------------------------------------------

def _flagged(it, po="393072B2", eta_day=-2, ship=""):
    inv = it.load_inventory()
    inv[KEY]["last_count_at"] = f"{_day(-5)}T12:00:00"
    inv[KEY]["on_order"] = [{"qty": 56.0, "po_number": po, "po_revision": "0000005",
                             "eta": f"{_day(eta_day)}T00:00:00", "ordered_at": f"{_day(-40)}T00:00:00",
                             "lead_days": 30, "unit": "cs", "ship_date": ship,
                             "rebooked_after_count": _day(-20)}]
    it.save_inventory(inv)


def test_rebooked_after_count_holds_and_shows_on_the_ledger(env):
    it, _sync, _c = env
    _flagged(it)
    inv = it.load_inventory()
    assert inv[KEY]["on_order"] and inv[KEY]["quantity"] == 2       # held
    rec = _ledger("393072B2")
    assert rec["rebooked_after_count"] == _day(-20) and rec["status"] != "arrived"


def test_setting_the_ship_date_confirms_and_releases(env):
    it, _sync, c = env
    _flagged(it)
    c.post("/api/on-order/ship-date", json={"po_number": "393072B2",
                                            "ship_date": _day(-4), "arrival_date": _day(-1)},
           headers=H)
    inv = it.load_inventory()
    assert not inv[KEY]["on_order"] and inv[KEY]["quantity"] == 58.0


def test_lineage_delivery_confirms_and_releases(env):
    it, _sync, _c = env
    _flagged(it)
    it.save_po_delivery({"393072B2": {"po_number": "393072B2", "status": "delivered",
                                      "delivered_at": f"{_day(-1)}T08:00:00"}})
    inv = it.load_inventory()
    assert not inv[KEY]["on_order"] and inv[KEY]["quantity"] == 58.0


# --- Adjust on a split-line SKU ---------------------------------------------

def _split(it):
    PO = "014511649940"
    pk = "poppy seed bagel 4oz [cb - riviera beach]"
    inv = it.load_inventory()
    inv[pk].update(quantity=100.0, last_count_at=f"{_day(-10)}T12:00:00")
    it.save_inventory(inv)
    it.save_usage([
        {"item_key": pk, "amount": -48.0, "source": "on_order_rollover", "po_number": PO,
         "arrival_date": f"{_day(-2)}T00:00:00", "timestamp": f"{_day(-2)}T04:00:00", "unit": "cs"},
        {"item_key": pk, "amount": -32.0, "source": "on_order_rollover", "po_number": PO,
         "arrival_date": f"{_day(-2)}T00:00:00", "timestamp": f"{_day(-2)}T04:00:00", "unit": "cs"},
    ])
    return PO, pk


def _adjust(c, po, lines):
    return c.post("/api/pos/arrived/adjust", json={"po_number": po, "lines": lines},
                  headers=H).get_json()


def test_adjust_unchanged_split_lines_changes_nothing(env):
    it, _sync, c = env
    po, pk = _split(it)
    r = _adjust(c, po, [{"item_key": pk, "qty": 48}, {"item_key": pk, "qty": 32}])
    assert r["ok"] and r["onhand_delta_cs"] == 0
    assert [e["amount"] for e in it.load_usage()] == [-48.0, -32.0]
    assert it.load_inventory()[pk]["quantity"] == 100.0


def test_adjust_moves_only_the_difference_onto_the_newest_row(env):
    it, _sync, c = env
    po, pk = _split(it)
    r = _adjust(c, po, [{"item_key": pk, "qty": 70}])               # 10 short
    assert r["onhand_delta_cs"] == -10
    assert [e["amount"] for e in it.load_usage()[:2]] == [-48.0, -22.0]
    r = _adjust(c, po, [{"item_key": pk, "qty": 100}])              # 30 over
    assert [e["amount"] for e in it.load_usage()[:2]] == [-48.0, -52.0]
    r = _adjust(c, po, [{"item_key": pk, "qty": 10}])               # spills to older row
    assert [e["amount"] for e in it.load_usage()[:2]] == [-10.0, -0.0]
    assert it.load_inventory()[pk]["quantity"] == 30.0              # 100 - 80 + 10


# --- ledger in_transit ---------------------------------------------------------

def test_future_ship_date_is_pending_past_is_in_transit(env):
    it, sync, c = env
    _book(sync, "7500001O", order_day=_day(-2))
    _book(sync, "7500002O", order_day=_day(-2))
    for po, sd in (("7500001O", _day(1)), ("7500002O", _day(-1))):
        c.post("/api/on-order/ship-date", json={"po_number": po, "ship_date": sd,
                                                "arrival_date": _day(6)}, headers=H)
    assert _ledger("7500001O")["status"] == "pending"
    assert _ledger("7500002O")["status"] == "in_transit"
