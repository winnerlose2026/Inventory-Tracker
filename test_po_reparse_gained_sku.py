"""Re-reading the SAME PO must re-apply when the parser learned a new SKU.

`_usfoods_po_to_events` drops any line whose variety it can't resolve, so a
PO ingested before a mfg code existed is booked TRUNCATED -- silently. When
the code is added later, re-scanning the identical PDF hits the idempotent
"not newer than pending" skip and the missing line stays missing forever.

That is exactly how pumpernickel (H&H mfg 1154, the October LTO) stayed at
zero across every DC after the code was added on 2026-08-28: each PO was
waved through as a duplicate of the truncated version already on file.

Same-document replays that gain nothing must still be no-ops.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

PO = "054511747707"
WH = "Ocala, FL"
DIST = "Cheney Brothers"
SENDER = "donotreply@cheneybrothers.com"
AT = "2026-08-21T16:01:54Z"

TRUNCATED = {"Plain": 48.0, "Everything": 40.0, "Sesame": 24.0}
FULL = dict(TRUNCATED, Pumpernickel=16.0)


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


def _doc(lines):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return [
        EmailEvent(
            event_type="restock",
            item=SyncItem(quantity=qty, distributor=DIST, variety=v,
                          warehouse=WH, unit="cases"),
            source_message_id=PO, source_subject=PO,
            po_number=PO, po_revision="", po_order_date="2026-08-21",
            source_received_at=AT, source_sender=SENDER,
        )
        for v, qty in lines.items()
    ]


def _pending(it):
    out = {}
    for item in it.load_inventory().values():
        for p in (item.get("on_order") or []):
            if p.get("po_number") == PO:
                v = item["name"].split(" Bagel")[0]
                out[v] = out.get(v, 0.0) + float(p.get("qty") or 0)
    return out


def test_reparse_with_a_newly_resolved_sku_reapplies():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)

        # Ingested before mfg 1154 existed -> pumpernickel line dropped.
        sync._apply_events(_doc(TRUNCATED), dry_run=False)
        assert _pending(it) == TRUNCATED

        # Same PDF, same revision, same received time -- parser now resolves
        # pumpernickel. This must NOT be skipped as a duplicate.
        report = sync._apply_events(_doc(FULL), dry_run=False)

        assert report.get("reparse_gained_skus"), report.get("po_revisions_skipped")
        assert _pending(it) == FULL
        assert _pending(it)["Pumpernickel"] == 16.0


def test_reparse_does_not_duplicate_the_lines_it_already_had():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)

        sync._apply_events(_doc(TRUNCATED), dry_run=False)
        sync._apply_events(_doc(FULL), dry_run=False)

        inv = it.load_inventory()
        rows = [p for item in inv.values()
                for p in (item.get("on_order") or []) if p.get("po_number") == PO]
        assert len(rows) == len(FULL), rows
        assert _pending(it)["Plain"] == 48.0


def test_identical_replay_that_gains_nothing_is_still_a_no_op():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup(Path(td))
        _seed(it)

        sync._apply_events(_doc(FULL), dry_run=False)
        report = sync._apply_events(_doc(FULL), dry_run=False)

        assert not report.get("reparse_gained_skus")
        assert report.get("po_revisions_skipped")
        assert _pending(it) == FULL
