"""A weekly count is the truth AT ITS DATE; re-reading it must not undo what
happened after it.

The 6-hourly scan re-reads every message in its lookback window, so each
weekly count report is re-applied ~4 times a day for up to two weeks. The
on_hand branch used to reset quantity to ``count + receipts_after_count``
every time, which wiped the daily forecast burn (and any manual Use /
Restock) made since the count. Zebulon Onion in usage.json, 10/06-10/08:
``+0.4471 forecast-daily`` at 05:05, ``-0.45 Email on-hand sync`` at 10:12,
three days running. On-hand never moved between weekly counts.

Two rules now:
  1. the SAME document (date, trust rank, email, figure) is a replay -> only
     the weekly-usage refresh may land;
  2. a count lands as ``count + receipts after it - movements after it``.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

KEY = "onion bagel 4oz [usf - zebulon]"
WH = "Zebulon, NC"
_TODAY = datetime.now(timezone.utc).date()


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


def _count(qty, count_date, received, weekly_usage=None, msg_id="rep-1"):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=qty, distributor="US Foods", variety="Onion",
                      warehouse=WH, unit="cases", weekly_usage=weekly_usage),
        source_message_id=msg_id, source_subject="Weekly report",
        count_date=count_date, source_received_at=received,
        source_sender="rep@usfoods.com",
    )


def _burn(it, amount, day):
    """One forecast-daily row dated `day` (what the 05:05 UTC job writes)."""
    usage = it.load_usage()
    usage.append({"item_key": KEY, "item_name": "Onion Bagel 4oz [USF - Zebulon]",
                  "amount": amount, "unit": "cs", "note": "forecast",
                  "timestamp": f"{day}T05:05:00", "source": "forecast-daily",
                  "forecast_date": day})
    it.save_usage(usage)
    inv = it.load_inventory()
    inv[KEY]["quantity"] = float(inv[KEY]["quantity"]) - amount
    it.save_inventory(inv)


def test_replaying_the_same_count_keeps_the_burn():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        d0 = (_TODAY - timedelta(days=2)).isoformat()
        rcv = f"{d0}T14:00:00Z"
        sync._apply_events([_count(100, d0, rcv, weekly_usage=3.5)], dry_run=False)
        assert it.load_inventory()[KEY]["quantity"] == 100.0

        _burn(it, 0.5, (_TODAY - timedelta(days=1)).isoformat())
        _burn(it, 0.5, _TODAY.isoformat())
        assert it.load_inventory()[KEY]["quantity"] == 99.0

        # The cron reads the same email again.
        rep = sync._apply_events([_count(100, d0, rcv, weekly_usage=3.5)], dry_run=False)
        assert it.load_inventory()[KEY]["quantity"] == 99.0
        assert rep["updated"] == 0
        rows = [e for e in it.load_usage() if e["item_key"] == KEY and not e.get("source")]
        assert len(rows) == 1               # only the original count delta


def test_replay_still_refreshes_weekly_usage():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        d0 = (_TODAY - timedelta(days=2)).isoformat()
        rcv = f"{d0}T14:00:00Z"
        sync._apply_events([_count(100, d0, rcv, weekly_usage=3.5)], dry_run=False)
        _burn(it, 1.0, _TODAY.isoformat())
        rep = sync._apply_events([_count(100, d0, rcv, weekly_usage=4.0)], dry_run=False)
        item = it.load_inventory()[KEY]
        assert item["quantity"] == 99.0
        assert item["weekly_usage"] == 4.0
        assert rep["updated"] == 1


def test_a_new_count_nets_out_burns_dated_after_it():
    """Count taken Monday, applied Wednesday: Tue + Wed burns stay deducted."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        d0 = (_TODAY - timedelta(days=9)).isoformat()
        sync._apply_events([_count(100, d0, f"{d0}T14:00:00Z")], dry_run=False)
        for back in (8, 7, 6, 5, 4, 3, 2, 1, 0):
            _burn(it, 1.0, (_TODAY - timedelta(days=back)).isoformat())
        assert it.load_inventory()[KEY]["quantity"] == 91.0

        # New count dated two days ago (Monday), read today.
        d1 = (_TODAY - timedelta(days=2)).isoformat()
        sync._apply_events([_count(80, d1, f"{_TODAY.isoformat()}T10:00:00Z",
                                   msg_id="rep-2")], dry_run=False)
        # 80 as of d1, minus the burns dated after d1 (yesterday + today).
        assert it.load_inventory()[KEY]["quantity"] == 78.0


def test_manual_use_after_the_count_survives_a_replay():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)
        d0 = (_TODAY - timedelta(days=2)).isoformat()
        rcv = f"{d0}T14:00:00Z"
        sync._apply_events([_count(100, d0, rcv)], dry_run=False)
        it.record_usage("Onion Bagel 4oz [USF - Zebulon]", 10, "pulled for sampling")
        assert it.load_inventory()[KEY]["quantity"] == 90.0
        # Different received stamp (the other mailbox's copy), same figure and date:
        # not an exact replay, so the arithmetic path runs -- and must agree.
        sync._apply_events([_count(100, d0, f"{d0}T14:00:30Z", msg_id="rep-1b")],
                           dry_run=False)
        assert it.load_inventory()[KEY]["quantity"] == 90.0
