"""A plain (non-PO) restock / usage event from an email is applied once, not
on every re-scan of its window (review 2026-11-12, item 13)."""
import sync_inventory as sync
from integrations import email_scanner as ES
from integrations.base import SyncItem

from ingest_fakes import sku

KEY, REC = sku("Plain", "Ocala", "Ocala, FL", distributor="Cheney Brothers", qty=100.0)


def _evt(etype, qty, mid="<csv-1@hhbagels.com>"):
    return ES.EmailEvent(
        event_type=etype,
        item=SyncItem(quantity=qty, distributor="Cheney Brothers", variety="Plain",
                      warehouse="Ocala, FL", unit="cs"),
        source_message_id=mid, source_subject="restock.csv")


def _apply(inv, usage, evt, now):
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    sync._apply_email_event(evt, inv, usage, now, report, dry_run=False)
    return report


def test_restock_and_usage_apply_once_per_message():
    inv, usage = {KEY: dict(REC)}, []
    _apply(inv, usage, _evt("restock", 24), "2026-11-09T06:00:00")
    assert inv[KEY]["quantity"] == 124.0
    assert usage[0]["source_message_id"] == "<csv-1@hhbagels.com>"
    assert usage[0]["event_type"] == "restock"
    # The 12:00 and 18:00 scans re-read the same message.
    for now in ("2026-11-09T12:00:00", "2026-11-09T18:00:00"):
        rep = _apply(inv, usage, _evt("restock", 24), now)
        assert rep["unchanged"] == 1 and rep["replays_skipped"]
    assert inv[KEY]["quantity"] == 124.0 and len(usage) == 1
    # A usage event from the same message is a different event type: applied once.
    _apply(inv, usage, _evt("usage", 10), "2026-11-09T18:00:00")
    _apply(inv, usage, _evt("usage", 10), "2026-11-10T00:00:00")
    assert inv[KEY]["quantity"] == 114.0 and len(usage) == 2


def test_new_message_and_two_lines_in_one_message_still_apply():
    inv, usage = {KEY: dict(REC)}, []
    now = "2026-11-09T06:00:00"
    _apply(inv, usage, _evt("restock", 8), now)      # two lines for the SKU
    _apply(inv, usage, _evt("restock", 8), now)      # in the same apply
    assert inv[KEY]["quantity"] == 116.0
    _apply(inv, usage, _evt("restock", 8, mid="<csv-2@hhbagels.com>"),
           "2026-11-10T06:00:00")
    assert inv[KEY]["quantity"] == 124.0


def test_reversed_row_does_not_block_and_keyless_events_behave_as_before():
    inv, usage = {KEY: dict(REC)}, []
    _apply(inv, usage, _evt("restock", 24), "2026-11-09T06:00:00")
    usage[0]["reversed"] = True
    _apply(inv, usage, _evt("restock", 24), "2026-11-09T12:00:00")
    assert inv[KEY]["quantity"] == 148.0
    _apply(inv, usage, _evt("usage", 5, mid=""), "2026-11-09T12:00:00")
    _apply(inv, usage, _evt("usage", 5, mid=""), "2026-11-09T18:00:00")
    assert inv[KEY]["quantity"] == 138.0
