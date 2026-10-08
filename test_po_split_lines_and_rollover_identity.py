"""Regressions from the 2026-10-08 inbox-vs-Pending-POs audit.

Four defects were found by reconciling JD@ / info@ against the tab:

1. SPLIT LINES. Cheney's Ocala PO 054511767347 listed nine varieties across
   sixteen lines (Everything 48 + 24, Cinnamon Raisin 8 + 24, ...), 224 cs.
   The in-batch dedup kept the max qty per SKU, so it booked 152 cs. The rule
   is now: sum within one document, max across copies of the document.

2. MISSING Mfg#. See test_cheney_po_mfg_fallback.py.

3. ROLLOVER LOSES THE DOCUMENT. Rollover / absorbed usage rows carried no
   `source_received_at`, `source_sender` or `warehouse`. On the next scan the
   same email looked like a newer document that "gained" every SKU, so the PO
   was reversed and re-booked every six hours (Houston 393072B2: 276 reversal
   rows in two weeks; La Mirada 6073804C on 10/8).

4. STALE ORDER DATE. USF re-cuts a PO under its ORIGINAL order date. Houston
   393072B2 (dated 08/10) was re-issued 09/28 for 1,120 cs; ordered_at + 30
   was already in the past, so it rolled straight into on-hand at ingest and
   the 9/29 count absorbed it -- it vanished from the Pending tab. The
   lead-time clock now starts no earlier than the day the email arrived.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

DIST = "Cheney Brothers"
WH = "Ocala, FL"
SENDER = "donotreply@cheneybrothers.com"
PO = "054511767347"

_TODAY = datetime.now(timezone.utc).date()


def _iso(days_ago: int, hhmm: str = "16:00:00") -> str:
    return f"{(_TODAY - timedelta(days=days_ago)).isoformat()}T{hhmm}Z"


def _setup(tmp: Path):
    os.environ["DATA_DIR"] = str(tmp)
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    import sync_inventory
    sync_inventory.INVENTORY_FILE = inventory_tracker.INVENTORY_FILE
    return inventory_tracker, sync_inventory


def _seed(it):
    from seed_bagels import BAGELS
    it.save_inventory({b["name"].lower(): dict(b, on_order=[]) for b in BAGELS})
    it.save_usage([])


def _evt(variety, qty, *, msg_id, received, order_date, po=PO, sender=SENDER,
         dist=DIST, wh=WH):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="restock",
        item=SyncItem(quantity=qty, distributor=dist, variety=variety,
                      warehouse=wh, unit="cases"),
        source_message_id=msg_id, source_subject=po,
        po_number=po, po_revision="", po_order_date=order_date,
        source_received_at=received, source_sender=sender,
    )


def _pending(it, po=PO):
    out = {}
    for item in it.load_inventory().values():
        for p in (item.get("on_order") or []):
            if p.get("po_number") == po:
                v = item["name"].split(" Bagel")[0]
                out[v] = out.get(v, 0.0) + float(p.get("qty") or 0)
    return out


def _onhand(it, wh=WH):
    return {item["name"].split(" Bagel")[0]: float(item.get("quantity") or 0)
            for item in it.load_inventory().values()
            if item.get("warehouse") == wh}


# ---------------------------------------------------------------------------
# 1. split lines
# ---------------------------------------------------------------------------

def test_split_lines_in_one_document_are_summed(monkeypatch):
    """The parser, not the apply path, merges split lines -- at the event
    level two 8 cs Poppy lines are indistinguishable from one line read
    from both mailboxes."""
    from integrations import email_scanner
    from integrations.cheney_po_parser import parse_po_text

    text = """\
Purchase Order Number: 054511767347
Order Date: 09/28/2026
Delivery Date 10/23/2026
Shipping Address OCALA FACILITY - CBI
2801 W SILVER SPRINGS BLVD
OCALA FL  34475
USA
Item Material/Description Brand Pack Size Quantity UM Unit Price         Net Amount
   10 10153019 BAGEL POPPY PARBAKED H & H 001/60   CT      8 CS 26.50 CS 212.00
                                          Mfg#   1152                                       GTIN#: 
   20 10153048 BAGEL EVERYTHING PARBAKED H & H 001/60   CT     48 CS 26.50 CS 1272.00
                                          Mfg#   1158                                       GTIN#: 
   30 10153046 BAGEL CINNAMON RAISIN PARBAKED H & H 001/60   CT      8 CS 26.50 CS 212.00
                                          Mfg#   1155                                       GTIN#: 
   40 10153019 BAGEL POPPY PARBAKED H & H 001/60   CT      8 CS 26.50 CS 212.00
                                          Mfg#   1152                                       GTIN#: 
   50 10153048 BAGEL EVERYTHING PARBAKED H & H 001/60   CT     24 CS 26.50 CS 636.00
                                          Mfg#   1158                                       GTIN#: 
   60 10153046 BAGEL CINNAMON RAISIN PARBAKED H & H 001/60   CT     24 CS 26.50 CS 636.00
                                          Mfg#   1155                                       GTIN#: 
"""
    monkeypatch.setattr(email_scanner, "_cheney_parse_po_pdf",
                        lambda _bytes: parse_po_text(text))
    events, errors = email_scanner._cheney_po_to_events(
        b"", DIST, "m1", "4511767347", received_at=_iso(1), sender=SENDER)
    assert errors == []
    got = {e.item.variety: e.item.quantity for e in events}
    assert got == {"Poppy Seed": 16.0, "Everything": 72.0,
                   "Cinnamon Raisin": 32.0}
    assert len(events) == 3                      # one event per SKU
    assert sum(got.values()) == 120.0


def test_same_document_in_both_mailboxes_is_still_counted_once():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        rcv, od = _iso(1), (_TODAY - timedelta(days=1)).isoformat()
        # JD@ and info@ copies of the SAME PDF (parser already summed lines).
        batch = []
        for mid in ("jd-copy", "info-copy"):
            batch += [
                _evt("Everything", 72, msg_id=mid, received=rcv, order_date=od),
                _evt("Plain", 8, msg_id=mid, received=rcv, order_date=od),
            ]
        rep = sync._apply_events(batch, dry_run=False)
        assert _pending(it) == {"Everything": 72.0, "Plain": 8.0}
        assert rep.get("dedup_dropped"), rep


# ---------------------------------------------------------------------------
# 3. rollover keeps the document identity
# ---------------------------------------------------------------------------

def test_rescanning_an_arrived_po_is_a_no_op():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        # Old enough that ordered_at + 30 and received + 30 are both past:
        # the PO rolls into on-hand on the first load.
        rcv, od = _iso(45), (_TODAY - timedelta(days=45)).isoformat()
        doc = [_evt("Plain", 56, msg_id="m1", received=rcv, order_date=od),
               _evt("Everything", 56, msg_id="m1", received=rcv, order_date=od)]
        sync._apply_events(doc, dry_run=False)
        before = _onhand(it)
        assert before["Plain"] >= 56 and _pending(it) == {}

        rows = [e for e in it.load_usage()
                if e.get("po_number") == PO and e.get("source") == "on_order_rollover"]
        assert rows and all(r.get("source_received_at") == rcv for r in rows)
        assert all(r.get("warehouse") == WH for r in rows)

        # The 6-hourly scan reads the same email again.
        rep = sync._apply_events(doc, dry_run=False)
        assert rep["po_revisions_superseded"] == [], rep
        assert not rep.get("reparse_gained_skus"), rep
        assert rep["po_revisions_skipped"], rep
        assert _onhand(it) == before
        assert _pending(it) == {}


# ---------------------------------------------------------------------------
# 4. stale order date
# ---------------------------------------------------------------------------

def test_recut_po_with_old_order_date_stays_pending():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        # PO dated 49 days ago, re-issued (received) yesterday.
        rcv = _iso(1)
        od = (_TODAY - timedelta(days=49)).isoformat()
        doc = [_evt("Plain", 224, msg_id="m1", received=rcv, order_date=od,
                    po="393072B2", dist="US Foods", wh="Houston, TX",
                    sender="tom.foley@usfoods.com")]
        sync._apply_events(doc, dry_run=False)
        pend = _pending(it, po="393072B2")
        assert pend == {"Plain": 224.0}, pend
        entry = next(p for item in it.load_inventory().values()
                     for p in (item.get("on_order") or [])
                     if p.get("po_number") == "393072B2")
        # ordered_at still shows the PO's printed date; the ETA clock starts
        # at receipt.
        assert entry["ordered_at"][:10] == od
        lead = sync._po_lead_days()
        expected_eta = (_TODAY - timedelta(days=1) + timedelta(days=lead)).isoformat()
        assert entry["eta"][:10] == expected_eta


def test_backlogged_scan_of_an_old_email_still_rolls_over():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        # Genuinely old email: received the day it was ordered, 45 days ago.
        rcv, od = _iso(45), (_TODAY - timedelta(days=45)).isoformat()
        doc = [_evt("Plain", 56, msg_id="m1", received=rcv, order_date=od)]
        sync._apply_events(doc, dry_run=False)
        assert _pending(it) == {}
        assert _onhand(it)["Plain"] >= 56
