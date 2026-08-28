"""A PO re-apply must not claw back a delivery a later count already absorbed.

The mirror of test_receipts_after_count. That one covers "a count must not
erase a delivery that arrived after it"; this one covers the other direction.

2026-08-28, Alcoa TN. PO 2055126H had rolled into on-hand twice -- the real
arrival (ETA 08-03) and a duplicate (ETA 08-04) -- and Kim's 08-24 count then
reset Alcoa to physical truth, absorbing both. That afternoon a re-scan
resolved pumpernickel (mfg 1154) as a SKU the PO had nothing booked for, which
by design bypasses the idempotent skip. The supersede path then reversed BOTH
arrivals (-224 cs) and re-applied one (+112 cs), leaving Alcoa 112 cs short:
six of twelve SKUs negative, three more at OUT.

Two guards, one per side of the round trip:
  * _reverse_po_entries must not reverse a receipt dated on/before the count.
  * _rollover_on_order must not promote one either -- the re-applied PO landed
    with a back-dated ETA (08-14) that promoted the instant it was booked.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import inventory_tracker as it
import sync_inventory as sync

KEY = "plain bagel 4oz [usf - alcoa]"
NOW = "2026-08-28T14:30:02"


def _item(qty, last_count_at="2026-08-24T19:21:26Z"):
    return {"name": "Plain Bagel 4oz [USF - Alcoa]", "quantity": qty,
            "unit": "cs", "warehouse": "Alcoa, TN",
            "last_count_at": last_count_at}


def _arrival_row(qty, arrival, rev="0000003"):
    return {"item_key": KEY, "item_name": "Plain Bagel 4oz [USF - Alcoa]",
            "amount": -qty, "unit": "cs", "source": "on_order_rollover",
            "arrival_date": arrival, "timestamp": "2026-08-03T13:34:38",
            "note": f"PO 2055126H arrived (ETA {arrival})",
            "po_number": "2055126H", "po_revision": rev}


def _reverse(usage, inv, dry_run=False):
    report = {"changes": []}
    sync._reverse_po_entries("2055126H", "0000003", list(range(len(usage))),
                             inv, usage, NOW, report, dry_run)
    return report


# --- guard A: the reversal side -------------------------------------------

def test_absorbed_receipt_is_not_reversed():
    """The incident, minimised: on-hand must not move."""
    usage = [_arrival_row(8, "2026-08-03")]
    inv = {KEY: _item(17.07)}
    report = _reverse(usage, inv)
    assert inv[KEY]["quantity"] == 17.07
    assert report["reversals_absorbed"]
    assert not any(e.get("reversal_of_revision") for e in usage)


def test_absorbed_row_is_still_retired():
    """Skipping the reversal must not leave the row reversible next scan."""
    usage = [_arrival_row(8, "2026-08-03")]
    inv = {KEY: _item(17.07)}
    _reverse(usage, inv)
    assert usage[0]["superseded_by_revision"] == "0000003"
    assert usage[0]["reversal_skipped_absorbed_by_count"] == "2026-08-24"


def test_duplicate_absorbed_arrivals_both_held():
    """Alcoa's real shape: two arrivals of one PO, both inside the count."""
    usage = [_arrival_row(8, "2026-08-03"), _arrival_row(8, "2026-08-04")]
    inv = {KEY: _item(17.07)}
    _reverse(usage, inv)
    assert inv[KEY]["quantity"] == 17.07


def test_receipt_after_the_count_still_reverses():
    """The guard must not disarm genuine revision corrections."""
    usage = [_arrival_row(8, "2026-08-27")]
    inv = {KEY: _item(17.07)}
    report = _reverse(usage, inv)
    assert inv[KEY]["quantity"] == 9.07
    assert not report.get("reversals_absorbed")
    assert usage[-1]["reversal_of_revision"] == "0000003"


def test_same_day_as_count_is_absorbed():
    usage = [_arrival_row(8, "2026-08-24")]
    inv = {KEY: _item(17.07)}
    _reverse(usage, inv)
    assert inv[KEY]["quantity"] == 17.07


def test_never_counted_sku_still_reverses():
    """Houston has no count on file; nothing can be absorbed."""
    usage = [_arrival_row(8, "2026-08-03")]
    inv = {KEY: _item(17.07, last_count_at="")}
    _reverse(usage, inv)
    assert inv[KEY]["quantity"] == 9.07


def test_arrival_date_wins_over_posting_timestamp():
    """A backfill posts an old arrival "now"; the arrival date decides."""
    row = _arrival_row(8, "2026-08-03")
    row["timestamp"] = "2026-08-28T14:30:02"
    inv = {KEY: _item(17.07)}
    _reverse([row], inv)
    assert inv[KEY]["quantity"] == 17.07


def test_eta_in_note_used_when_arrival_date_missing():
    """Rows written before the arrival_date field carry the ETA in the note."""
    row = _arrival_row(8, "2026-08-03")
    del row["arrival_date"]
    inv = {KEY: _item(17.07)}
    _reverse([row], inv)
    assert inv[KEY]["quantity"] == 17.07


def test_dry_run_leaves_the_row_untouched():
    usage = [_arrival_row(8, "2026-08-03")]
    inv = {KEY: _item(17.07)}
    _reverse(usage, inv, dry_run=True)
    assert "superseded_by_revision" not in usage[0]
    assert inv[KEY]["quantity"] == 17.07


# --- guard B: the receiving side ------------------------------------------

def _pending(qty, eta):
    return {"po_number": "2055126H", "po_revision": "0000003", "qty": qty,
            "eta": eta, "ordered_at": "2026-07-14T00:00:00"}


def test_backdated_arrival_inside_the_count_is_not_promoted():
    """The +112 half of the incident: re-applied PO with a back-dated ETA."""
    inv = {KEY: dict(_item(17.07), on_order=[_pending(8, "2026-08-14T00:00:00")])}
    assert it._rollover_on_order(inv) is True
    assert inv[KEY]["quantity"] == 17.07          # not promoted
    assert inv[KEY]["on_order"] == []             # but cleared out of pending
    assert it._PENDING_ROLLOVER_ABSORBED[0]["qty"] == 8


def test_absorbed_promotion_logs_a_zero_amount_audit_row():
    inv = {KEY: dict(_item(17.07), on_order=[_pending(8, "2026-08-14T00:00:00")])}
    it._rollover_on_order(inv)
    usage: list = []
    it._append_rollover_usage(inv, usage)
    assert len(usage) == 1
    assert usage[0]["amount"] == 0
    assert usage[0]["source"] == "on_order_absorbed"
    # must never be mistaken for a receipt by the count guard
    assert sync._receipts_after_count(usage, KEY, "2026-07-01") == 0


def test_arrival_after_the_count_still_promotes():
    eta = (datetime.now() - timedelta(days=1)).isoformat()
    inv = {KEY: dict(_item(17.07, last_count_at="2026-08-24T19:21:26Z"),
                     on_order=[_pending(8, eta)])}
    assert it._rollover_on_order(inv) is True
    assert inv[KEY]["quantity"] == 25.07
    assert not it._PENDING_ROLLOVER_ABSORBED


def test_uncounted_sku_still_promotes():
    inv = {KEY: dict(_item(17.07, last_count_at=""),
                     on_order=[_pending(8, "2026-08-14T00:00:00")])}
    assert it._rollover_on_order(inv) is True
    assert inv[KEY]["quantity"] == 25.07


def test_future_eta_still_stays_pending():
    eta = (datetime.now() + timedelta(days=5)).isoformat()
    inv = {KEY: dict(_item(17.07), on_order=[_pending(8, eta)])}
    it._rollover_on_order(inv)
    assert inv[KEY]["quantity"] == 17.07
    assert len(inv[KEY]["on_order"]) == 1
    assert not it._PENDING_ROLLOVER_ABSORBED
