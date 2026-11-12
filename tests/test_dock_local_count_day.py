"""The count-absorption day rule compares the DOCK's days, not UTC's.

Houston: a count emailed Mon 6:15 pm CST is stamped Tuesday in UTC. A PO that
arrived Tuesday was "on or before the count's day" and its 168 cs were
dropped as already counted. Both days are now taken at the warehouse's dock,
and a date-only arrival fires at the dock's midnight, not UTC's.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inventory_tracker as it
import sync_inventory as sync
from core.util import dock_local_day, dock_midnight_naive, receipt_inside_count
from integrations.po_delivery import dock_zone

WH = "Houston, TX"
MON_EVENING = "2026-11-10T00:15:00Z"      # Mon 11/09 6:15 pm CST
KEY = "plain bagel 4oz [usf - houston]"


def _inv(count_at=MON_EVENING, eta="2026-11-10T00:00:00"):
    return {KEY: {
        "name": "Plain Bagel 4oz [USF - Houston]", "distributor": "US Foods",
        "warehouse": WH, "unit": "cs", "quantity": 40.0, "weekly_usage": 19.0,
        "last_count_at": count_at, "last_count_qty": 40.0,
        "on_order": [{"po_number": "5550001A", "po_revision": "0000001",
                      "qty": 168.0, "unit": "cs",
                      "ordered_at": "2026-10-11T00:00:00", "eta": eta}]}}


def _roll(monkeypatch, inv):
    monkeypatch.setattr(it, "load_po_delivery", lambda: {})
    monkeypatch.setattr(it, "load_status_overrides", lambda: {})
    it._rollover_on_order(inv)
    usage: list = []
    it._append_rollover_usage(inv, usage)
    return usage


def test_count_day_is_the_docks_day():
    assert dock_local_day(MON_EVENING, WH) == "2026-11-09"
    assert dock_local_day("2026-11-09T18:15:00-06:00", WH) == "2026-11-09"
    # daytime counts are unchanged
    assert dock_local_day("2026-11-10T16:20:00Z", WH) == "2026-11-10"


def test_bare_and_midnight_dates_are_kept_as_written():
    assert dock_local_day("2026-11-10", WH) == "2026-11-10"
    assert dock_local_day("2026-11-10T00:00:00", WH) == "2026-11-10"
    assert dock_local_day("", WH) == ""
    # no warehouse: the stamp's own date, as before
    assert dock_local_day(MON_EVENING, "") == "2026-11-10"


def test_east_tennessee_is_eastern():
    assert dock_zone("Alcoa, TN") == "America/New_York"
    assert dock_zone("Houston, TX") == "America/Chicago"


def test_tuesday_po_is_not_absorbed_by_a_monday_evening_count(monkeypatch):
    inv = _inv()
    usage = _roll(monkeypatch, inv)
    assert inv[KEY]["quantity"] == 208.0
    assert [r["source"] for r in usage] == ["on_order_rollover"]


def test_po_on_the_counts_local_day_is_still_absorbed(monkeypatch):
    inv = _inv(eta="2026-11-09T00:00:00")
    usage = _roll(monkeypatch, inv)
    assert inv[KEY]["quantity"] == 40.0
    assert [r["source"] for r in usage] == ["on_order_absorbed"]
    assert "2026-11-09 count" in usage[0]["note"]


def test_bare_date_count_keeps_working(monkeypatch):
    inv = _inv(count_at="2026-11-10")
    usage = _roll(monkeypatch, inv)
    assert inv[KEY]["quantity"] == 40.0
    assert usage[0]["source"] == "on_order_absorbed"


def test_receipt_row_after_local_count_day_survives_the_count():
    row = {"item_key": KEY, "amount": -168, "source": "on_order_rollover",
           "arrival_date": "2026-11-10T00:00:00", "warehouse": WH}
    assert receipt_inside_count(row, MON_EVENING, "2026-11-10") is False
    assert sync._receipts_after_count([row], KEY, MON_EVENING) == 168
    # legacy row with no warehouse: caller's warehouse decides
    legacy = dict(row); legacy.pop("warehouse")
    assert receipt_inside_count(legacy, MON_EVENING, "2026-11-10") is True
    assert sync._receipts_after_count([legacy], KEY, MON_EVENING, warehouse=WH) == 168
    # a count the next local day absorbs it
    assert receipt_inside_count(row, "2026-11-10T22:00:00Z", "2026-11-10") is True


def test_movement_after_count_uses_the_local_count_day():
    burn = {"item_key": KEY, "amount": 2.7, "source": "forecast-daily",
            "forecast_date": "2026-11-10", "timestamp": "2026-11-10T05:05:00"}
    assert sync._movement_after_count([burn], KEY, MON_EVENING) == 0.0   # old
    assert sync._movement_after_count([burn], KEY, MON_EVENING, warehouse=WH) == 2.7


def test_reversal_keeps_a_receipt_that_arrived_after_the_local_count_day():
    inv = _inv()
    inv[KEY]["on_order"] = []
    inv[KEY]["quantity"] = 208.0
    usage = [{"item_key": KEY, "amount": -168, "source": "on_order_rollover",
              "po_number": "5550001A", "po_revision": "0000001",
              "arrival_date": "2026-11-10T00:00:00", "warehouse": WH}]
    report = {"changes": []}
    sync._reverse_po_entries("5550001A", "0000002", [0], inv, usage,
                             "2026-11-11T10:00:00", report, False)
    # arrived Tuesday, counted Monday evening: NOT inside the count -> reversed
    assert inv[KEY]["quantity"] == 40.0
    assert not report.get("reversals_absorbed")


def test_date_only_arrival_fires_at_the_docks_midnight(monkeypatch):
    day = datetime(2026, 11, 10)
    fire = dock_midnight_naive(day, WH)
    # 00:00 CST == 06:00 UTC
    assert fire.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M") == "2026-11-10T06:00"
    assert dock_midnight_naive(day, "") == day

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return _Clock._now

    monkeypatch.setattr(it, "datetime", _Clock)
    # 01:00 UTC on the 10th = 7 pm Monday in Houston: not yet Tuesday there
    _Clock._now = (datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
                   .astimezone().replace(tzinfo=None))
    inv = _inv(count_at="2026-11-02")
    _roll(monkeypatch, inv)
    assert inv[KEY]["quantity"] == 40.0 and len(inv[KEY]["on_order"]) == 1
    _Clock._now += timedelta(hours=6)
    _roll(monkeypatch, inv)
    assert inv[KEY]["quantity"] == 208.0 and inv[KEY]["on_order"] == []
