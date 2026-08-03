"""An on-hand count must not erase a delivery that arrived after it.

Regression cover for the 2026-08-03 Alcoa report. PO 2055126H (112 cs, ETA
2026-08-03) rolled into on-hand at 13:34; the July 27 report was re-scanned at
14:15 and reset Alcoa back to its 7/27 level, wiping 104 of the 112 cases. Nine
POs across five warehouses showed the same fingerprint -- but only two had
genuinely missing cases: in the other seven the count was taken AFTER arrival,
so it already included the delivery and re-adding it would double-count. Hence
the comparison is on the receipt's ARRIVAL date, not the row's posting time (a
wide-lookback backfill posts long-past arrivals "now").
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import sync_inventory as sync


def _roll(item_key, qty, arrival, *, ts="2026-08-03T13:34:38", note="",
          reversed_=False):
    return {"item_key": item_key, "amount": -qty, "source": "on_order_rollover",
            "arrival_date": arrival, "timestamp": ts, "note": note,
            "po_number": "2055126H", "reversed": reversed_}


KEY = "plain bagel 4oz [usf - alcoa]"


# --- _receipts_after_count ------------------------------------------------

def test_receipt_after_the_count_is_counted():
    u = [_roll(KEY, 8, "2026-08-03T00:00:00")]
    assert sync._receipts_after_count(u, KEY, "2026-07-27") == 8


def test_receipt_before_the_count_is_ignored():
    """The seven POs that must NOT be re-added -- already inside the count."""
    u = [_roll(KEY, 8, "2026-07-20T00:00:00")]
    assert sync._receipts_after_count(u, KEY, "2026-07-27") == 0


def test_receipt_on_the_count_date_is_ignored():
    """Same-day is ambiguous, so stay conservative and don't re-add."""
    u = [_roll(KEY, 8, "2026-07-27T00:00:00")]
    assert sync._receipts_after_count(u, KEY, "2026-07-27") == 0


def test_posting_timestamp_does_not_decide_it():
    """A backfill posts an OLD arrival "now"; arrival date must win."""
    u = [_roll(KEY, 336, "2026-07-06T00:00:00", ts="2026-08-03T14:15:50")]
    assert sync._receipts_after_count(u, KEY, "2026-08-03") == 0


def test_reversed_and_other_skus_excluded():
    u = [_roll(KEY, 8, "2026-08-03T00:00:00", reversed_=True),
         _roll("egg bagel 4oz [usf - alcoa]", 16, "2026-08-03T00:00:00")]
    assert sync._receipts_after_count(u, KEY, "2026-07-27") == 0


def test_arrival_falls_back_to_the_eta_in_the_note():
    """Rows written before the arrival_date field carry the ETA in the note."""
    u = [{"item_key": KEY, "amount": -8, "source": "on_order_rollover",
          "note": "PO 2055126H arrived (ETA 2026-08-03)",
          "timestamp": "2026-08-03T13:34:38"}]
    assert sync._receipts_after_count(u, KEY, "2026-07-27") == 8


def test_no_count_date_means_no_adjustment():
    u = [_roll(KEY, 8, "2026-08-03T00:00:00")]
    assert sync._receipts_after_count(u, KEY, "") == 0


# --- end to end through _apply_events ------------------------------------

def _setup(tmp: Path):
    os.environ["DATA_DIR"] = str(tmp)
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    sync.INVENTORY_FILE = inventory_tracker.INVENTORY_FILE
    from seed_bagels import BAGELS
    inv = {b["name"].lower(): dict(b, on_order=[]) for b in BAGELS}
    inventory_tracker.save_inventory(inv)
    return inventory_tracker


def _on_hand(qty, variety, warehouse, count_date, distributor="US Foods"):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=float(qty), distributor=distributor,
                      variety=variety, warehouse=warehouse, unit="cs"),
        source_message_id="graph-id", source_subject="July 27 Report",
        count_date=count_date)


def test_the_alcoa_incident_the_delivery_survives_a_rescan():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it = _setup(Path(td))
        # July 27 count of 20 cs lands first.
        it.save_usage([])
        sync._apply_events([_on_hand(20, "Plain", "Alcoa, TN", "2026-07-27")],
                           dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY]["quantity"] == 20

        # PO 2055126H arrives 8/3 with 8 cs of Plain.
        usage = it.load_usage()
        usage.append(_roll(KEY, 8, "2026-08-03T00:00:00"))
        it.save_usage(usage)
        inv = it._load(it.INVENTORY_FILE)
        inv[KEY]["quantity"] = 28.0
        it.save_inventory(inv)

        # The July 27 report is re-scanned. It must NOT drag Plain back to 20.
        r = sync._apply_events([_on_hand(20, "Plain", "Alcoa, TN", "2026-07-27")],
                              dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY]["quantity"] == 28, \
            "the 8 cs that arrived after the count must be preserved"
        assert r["unchanged"] == 1, r

        # And it stays put on further rescans (idempotent, not cumulative).
        sync._apply_events([_on_hand(20, "Plain", "Alcoa, TN", "2026-07-27")],
                           dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY]["quantity"] == 28


def test_a_count_taken_after_arrival_still_wins_outright():
    """La Mirada / Chicago case: don't double-count an absorbed delivery."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it = _setup(Path(td))
        it.save_usage([_roll(KEY, 8, "2026-07-30T00:00:00")])
        inv = it._load(it.INVENTORY_FILE)
        inv[KEY]["quantity"] = 99.0
        it.save_inventory(inv)
        # Rep counts 31 cs on 8/3, after the 7/30 arrival -> 31 is the truth.
        r = sync._apply_events([_on_hand(31, "Plain", "Alcoa, TN", "2026-08-03")],
                              dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY]["quantity"] == 31, r
        assert not r.get("receipts_preserved")


def test_preserved_receipt_is_reported():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it = _setup(Path(td))
        it.save_usage([_roll(KEY, 8, "2026-08-03T00:00:00")])
        r = sync._apply_events([_on_hand(20, "Plain", "Alcoa, TN", "2026-07-27")],
                              dry_run=False)
        assert r.get("receipts_preserved"), r
        msg = " ".join(r["receipts_preserved"])
        assert "reported 20 cs as of 2026-07-27" in msg, msg
        assert "+8 cs arrived after that date -> 28 cs" in msg, msg
        assert it._load(it.INVENTORY_FILE)[KEY]["quantity"] == 28


# --- malformed ETA years --------------------------------------------------

def test_implausible_eta_year_is_not_a_rollover_trigger():
    """PO 8513015G carried eta '0002-07-17', which promoted instantly and then
    showed up as the PO's ETA on the dashboard."""
    import inventory_tracker as it
    assert it._rollover_trigger({"eta": "0002-07-17"}) is None
    assert it._rollover_trigger({"arrival_date": "0002-07-17"}) is None
    assert it._rollover_trigger({"arrival_date": "0002-07-17",
                                 "eta": "2026-07-17"}) is not None
    assert it._rollover_trigger({"eta": "2026-07-17"}) is not None
    assert it._rollover_trigger({"eta": "9999-01-01"}) is None
    assert it._rollover_trigger({}) is None
    assert it._rollover_trigger({"eta": "not-a-date"}) is None
