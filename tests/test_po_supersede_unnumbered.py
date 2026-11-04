"""A superseded arrival of a Cheney PO (no revision number) must stop counting.

_reverse_po_entries stamped ``superseded_by_revision`` with the new document's
revision -- "" for every Cheney PO -- and every reader tested its truthiness,
so the retired row stayed a LIVE receipt. The reversal audit row was found by
the truthiness of ``reversal_of_revision`` ("" again) and so counted as a
booked receipt too.

Live, 2026-10-22: PO 014511743693 (Riviera Beach) was re-applied 2026-08-28;
its 08-28 arrival rows (64 Blueberry + 104 Asiago) still read as live next to
the 09-05 re-arrival and the ledger showed 336 cs for a 168 cs PO. Those rows
carry superseded_at, so core.util.row_superseded retires them with no data
repair.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

KEY = "blueberry bagel 4oz [cb - riviera beach]"
WH, DIST = "Riviera Beach, FL", "Cheney Brothers"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
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
    return it, sync


def _day(n):
    return (datetime.now() + timedelta(days=n)).date().isoformat()


def _po(po, qty, order_day, received):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=qty, distributor=DIST, variety="Blueberry",
                      warehouse=WH, unit="cases"),
        source_message_id=f"{po}-{received}", source_subject=po,
        po_number=po, po_revision="", po_order_date=order_day,
        source_received_at=received)


def _count(qty, count_at):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=qty, distributor=DIST, variety="Blueberry",
                      warehouse=WH, unit="cases"),
        source_message_id=f"count-{count_at}", source_subject="count",
        count_date=count_at, source_received_at=f"{count_at}Z")


def _ledger(po):
    from app import app
    from blueprints.pos import build_po_ledger
    with app.test_request_context():
        return next(r for r in build_po_ledger() if r["po_number"] == po)


def _setup_counted(it):
    inv = it.load_inventory()
    inv[KEY].update(quantity=10.0, last_count_at=f"{_day(-12)}T12:00:00",
                    last_count_qty=10.0)
    it.save_inventory(inv)


def test_recut_cheney_po_retires_the_old_arrival(env):
    it, sync = env
    from core.util import rollover_row_live
    PO = "014511999999"
    _setup_counted(it)
    sync._apply_events([_po(PO, 64, _day(-40), f"{_day(-40)}T12:00:00Z")])
    assert it.load_inventory()[KEY]["quantity"] == 74.0          # rolled (ETA -10d)
    sync._apply_events([_po(PO, 56, _day(-40), f"{_day(-38)}T12:00:00Z")])
    inv = it.load_inventory()                                     # re-cut rolls (ETA -8d)
    usage = it.load_usage()
    assert inv[KEY]["quantity"] == 66.0
    old = next(e for e in usage if e.get("amount") == -64.0)
    assert old["superseded_by_revision"] == sync.NO_REV_TAG       # never "" again
    live = [e["amount"] for e in usage if rollover_row_live(e)]
    assert live == [-56.0]
    assert sync._receipts_after_count(usage, KEY, f"{_day(-11)}T12:00:00") == 56.0
    # A late count between the two arrivals: 12 counted + the 56 that landed after.
    sync._apply_events([_count(12, f"{_day(-11)}T12:00:00")])
    assert it.load_inventory()[KEY]["quantity"] == 68.0
    rec = _ledger(PO)
    assert rec["total_cs"] == 56.0 and len(rec["lines"]) == 1


def test_third_recut_leaves_exactly_one_live_arrival(env):
    it, sync = env
    from core.util import rollover_row_live
    PO = "014511999998"
    _setup_counted(it)
    for qty, rcv in ((64, -40), (56, -38), (48, -37)):
        rep = sync._apply_events([_po(PO, qty, _day(-40), f"{_day(rcv)}T12:00:00Z")])
        it.load_inventory()
    # The last supersede reversed ONE line (the 56), not ghosts + audit rows.
    assert "(1 line(s) reversed)" in rep["po_revisions_superseded"][0]
    usage = it.load_usage()
    assert [e["amount"] for e in usage if rollover_row_live(e)] == [-48.0]
    assert it.load_inventory()[KEY]["quantity"] == 58.0
    assert _ledger(PO)["total_cs"] == 48.0


def test_live_shape_of_014511743693_reads_168_cs(env):
    """The exact production rows: the 08-28 arrivals carry
    superseded_by_revision "" + superseded_at; the reversal audit rows carry
    reversal_of_revision ""; the 09-05 re-arrival is live."""
    it, sync = env
    from core.util import rollover_row_live
    PO = "014511743693"
    asi = "asiago bagel 4oz [cb - riviera beach]"
    rows = []
    for key, q in ((KEY, 64.0), (asi, 104.0)):
        rows.append({"item_key": key, "amount": -q, "source": "on_order_rollover",
                     "po_number": PO, "po_revision": "", "arrival_date": "2026-08-28T00:00:00",
                     "timestamp": "2026-08-28T04:07:44", "superseded_by_revision": "",
                     "superseded_at": "2026-08-28T14:30:02.715528", "unit": "cs"})
    for key, q in ((KEY, 64.0), (asi, 104.0)):
        rows.append({"item_key": key, "amount": q, "po_number": PO, "po_revision": "",
                     "reversal_of_revision": "", "timestamp": "2026-08-28T14:30:02",
                     "note": f"Reversed by PO {PO} rev  (supersedes rev )", "unit": "cs"})
    for key, q in ((KEY, 64.0), (asi, 104.0)):
        rows.append({"item_key": key, "amount": -q, "source": "on_order_rollover",
                     "po_number": PO, "po_revision": "", "arrival_date": "2026-09-05T00:00:00",
                     "timestamp": "2026-09-05T00:00:57", "unit": "cs"})
    it.save_usage(rows)
    assert sum(1 for e in rows if rollover_row_live(e)) == 2
    rec = _ledger(PO)
    assert rec["total_cs"] == 168.0 and len(rec["lines"]) == 2
    _hi, active, *_ = sync._highest_applied_rev(rows, PO)
    assert active == [4, 5]                       # only the 09-05 arrivals
    assert sync._booked_content_for_po(it.load_inventory(), rows, PO) == {
        ("blueberry", "riviera beach, fl"): 64.0, ("asiago", "riviera beach, fl"): 104.0}
