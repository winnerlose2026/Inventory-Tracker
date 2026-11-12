"""The distributor's reported ON ORDER is kept (never applied) and
cross-checked against our pending POs (feature D, 2026-11-12)."""
from datetime import datetime, timedelta, timezone

import inventory_tracker as it
import sync_inventory as sync
from core import feed_health
from integrations import email_scanner as ES
from integrations import ingest_health as IH
from integrations.base import SyncItem

from ingest_fakes import isolate, mime, sku

WH = "Zebulon, NC"
NOW = datetime.now(timezone.utc)
AT = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_report_on_order_column_rides_on_the_event_and_is_never_applied():
    html = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td>"
            "<td>ON ORDER ETA 11/20</td><td>Forecast 11/15/2026</td></tr>"
            "<tr><td>7095637</td><td>1150</td><td>81</td><td>96</td><td>22</td></tr>"
            "</table>")
    events, errors, _ = ES.parse_message_with_errors(
        mime("maria.hernandez@usfoods.com", "Weekly", html=html))
    assert not errors and [e.event_type for e in events] == ["on_hand"]
    assert events[0].reported_on_order == 96.0 and events[0].item.quantity == 81.0

    key, rec = sku("Plain", "Zebulon", WH, qty=50.0)
    inv = {key: rec}
    events[0].count_date = AT
    report = {"unmatched": [], "changes": [], "updated": 0, "unchanged": 0}
    sync._apply_email_event(events[0], inv, [], NOW.isoformat(), report, False)
    assert inv[key]["quantity"] == 81.0 and inv[key]["on_order"] == []
    assert inv[key]["reported_on_order"]["qty"] == 96.0
    assert inv[key]["reported_on_order"]["at"] == AT


def _item(variety, reported, pending_rows, at=AT):
    key, rec = sku(variety, "Zebulon", WH)
    rec["on_order"] = pending_rows
    if reported is not None:
        rec["reported_on_order"] = {"qty": reported, "at": at, "source": "<m>"}
    return key, rec


def _po(qty, days_ago, po="7135734C"):
    return {"po_number": po, "qty": qty,
            "ordered_at": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT00:00:00")}


def _setup(tmp_path, monkeypatch, items, usage=()):
    isolate(tmp_path, monkeypatch, dict(items))
    it._write_json(tmp_path / "usage.json", list(usage))


def test_mismatch_over_a_pallet_raises_and_clears(tmp_path, monkeypatch):
    # Report says 168 on order; we hold only 56 pending -> a PO is missing here.
    _setup(tmp_path, monkeypatch, [_item("Plain", 168.0, [_po(56, 5)]),
                                   _item("Sesame", 56.0, [_po(56, 5)])])
    out = IH.check_on_order()
    assert [r["sku"] for r in out[WH]] == ["Plain Bagel 4oz [USF - Zebulon]"]
    msg = feed_health.snapshot()["issues"][f"on-order-mismatch:{WH}"]["message"]
    assert "report 168 cs vs pending 56 cs" in msg
    # The missing PO is booked (dated before the report): matches -> cleared.
    _setup(tmp_path, monkeypatch, [
        _item("Plain", 168.0, [_po(56, 5), _po(112, 4, po="7135735C")]),
        _item("Sesame", 56.0, [_po(56, 5)])])
    assert IH.check_on_order() == {}
    assert f"on-order-mismatch:{WH}" not in feed_health.snapshot()["issues"]


def test_within_a_pallet_is_not_a_mismatch(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, [_item("Plain", 112.0, [_po(56, 5)])])
    assert IH.check_on_order() == {}


def test_timing_is_reconstructed_as_of_the_report(tmp_path, monkeypatch):
    key, rec = _item("Plain", 112.0, [_po(224, 1, po="NEWER")])
    # 112 cs were on order at report time and have ARRIVED since (rollover),
    # and a new 224 cs PO was booked AFTER the report: both are consistent.
    usage = [{"item_key": key, "source": "on_order_rollover", "amount": -112.0,
              "po_number": "OLD", "unit": "cs",
              "arrival_date": (NOW - timedelta(days=1)).isoformat(),
              "timestamp": (NOW - timedelta(days=1)).isoformat()}]
    _setup(tmp_path, monkeypatch, [(key, rec)], usage)
    assert IH.check_on_order() == {}


def test_old_reported_figures_and_the_switch(tmp_path, monkeypatch):
    stale = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _setup(tmp_path, monkeypatch, [_item("Plain", 500.0, [], at=stale)])
    assert IH.check_on_order() == {}
    _setup(tmp_path, monkeypatch, [_item("Plain", 500.0, [])])
    monkeypatch.setenv("ON_ORDER_CROSSCHECK", "off")
    assert IH.run_checks()["checks"]["on_order"] == "disabled"
    monkeypatch.delenv("ON_ORDER_CROSSCHECK")
    assert WH in IH.run_checks()["checks"]["on_order"]


def test_ingest_events_accepts_reported_on_order(tmp_path, monkeypatch):
    """The Houston screenshot reader can post its Order column the same way."""
    key, rec = sku("Plain", "Houston", "Houston, TX")
    isolate(tmp_path, monkeypatch, {key: rec})
    it._write_json(tmp_path / "usage.json", [])
    monkeypatch.setenv("INVENTORY_API_TOKEN", "tok")
    from app import app
    r = app.test_client().post("/api/email/ingest-events", headers={
        "X-Inventory-Token": "tok"}, json={"source": "test", "events": [{
            "event_type": "on_hand", "count_date": AT, "source_message_id": "<h>",
            "reported_on_order": 224,
            "item": {"quantity": 50, "distributor": "US Foods", "variety": "Plain",
                     "warehouse": "Houston, TX", "unit": "cs"}}]})
    assert r.status_code == 200
    assert it.load_inventory()[key]["reported_on_order"]["qty"] == 224.0
