"""Regression: a corrected PO that carries a LOWER revision number must win.

The incident (2026-08-25, US Foods Houston PO 505876B2):

    17:03  rev 0000006  Plain 32 / Poppy 16 / WW-Everything 32 / Jalapeno 24
                        = 104 cs, no pumpernickel, wrong Everything SKU
    18:20  rev 0000003  Foley's correction: WW-Everything -> Everything,
                        + 8 cs Pumpernickel = 112 cs (the PO's whole purpose)

`po_revision.is_newer` used to treat a higher numeric revision as
authoritative regardless of date, so the 18:20 correction was rejected as
stale and Houston stayed at 104 cs. US Foods re-numbers downward when a
buyer re-cuts an order, so the number is a tie-break, never an override.

The stale-forward protection that rule used to provide now rides on the
SENDER: a copy forwarded from @hhbagels.com never outranks the vendor's.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

PO = "505876B2"
VENDOR = "Foley, Tom <tom.foley@usfoods.com>"
INTERNAL = "JD Gross <JD@hhbagels.com>"
STALE_AT = "2026-08-25T17:03:00Z"
FIXED_AT = "2026-08-25T18:20:11Z"

STALE = {"Plain": 32.0, "Poppy Seed": 16.0,
         "Whole Wheat Everything": 32.0, "Jalapeno Cheddar": 24.0}
FIXED = {"Plain": 32.0, "Poppy Seed": 16.0,
         "Everything": 32.0, "Jalapeno Cheddar": 24.0, "Pumpernickel": 8.0}


def _setup_temp_inventory(tmp: Path):
    os.environ["DATA_DIR"] = str(tmp)
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    import sync_inventory
    sync_inventory.INVENTORY_FILE = inventory_tracker.INVENTORY_FILE
    return inventory_tracker, sync_inventory


def _seed(inventory_tracker):
    from seed_bagels import BAGELS
    inventory_tracker.save_inventory(
        {b["name"].lower(): dict(b, on_order=[]) for b in BAGELS})
    inventory_tracker.save_usage([])


def _doc(lines, rev, received, sender=VENDOR):
    """One PO document -> one restock event per line."""
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return [
        EmailEvent(
            event_type="restock",
            item=SyncItem(quantity=qty, distributor="US Foods",
                          variety=variety, warehouse="Houston, TX",
                          unit="cases"),
            source_message_id=f"{PO}-{rev}",
            source_subject=f"US Foods PO Request - {PO} {rev}",
            po_number=PO,
            po_revision=rev,
            po_order_date="2026-08-25",
            source_received_at=received,
            source_sender=sender,
        )
        for variety, qty in lines.items()
    ]


def _pending(it):
    """{variety: qty} currently on order for PO 505876B2 at Houston."""
    out = {}
    for item in it.load_inventory().values():
        for p in (item.get("on_order") or []):
            if p.get("po_number") != PO:
                continue
            variety = item["name"].split(" Bagel")[0]
            out[variety] = out.get(variety, 0.0) + float(p.get("qty") or 0)
    return out


def test_lower_numbered_correction_replaces_the_stale_higher_revision():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        sync._apply_events(_doc(STALE, "0000006", STALE_AT), dry_run=False)
        assert _pending(it) == STALE
        assert sum(_pending(it).values()) == 104

        report = sync._apply_events(
            _doc(FIXED, "0000003", FIXED_AT), dry_run=False)

        assert not report.get("po_revisions_skipped"), \
            report.get("po_revisions_skipped")
        assert _pending(it) == FIXED
        # 112 cs, on the 56-case pallet multiple, pumpernickel included.
        assert sum(_pending(it).values()) == 112
        assert sum(_pending(it).values()) % 56 == 0
        # The SKU the buyer dropped must not survive the amendment.
        assert "Whole Wheat Everything" not in _pending(it)


def test_both_documents_in_one_batch_resolve_to_the_correction():
    """A wide-lookback backfill re-reads both copies in a single scan."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        sync._apply_events(
            _doc(STALE, "0000006", STALE_AT) + _doc(FIXED, "0000003", FIXED_AT),
            dry_run=False)

        assert _pending(it) == FIXED
        assert sum(_pending(it).values()) == 112


def test_replaying_the_correction_is_idempotent():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        sync._apply_events(_doc(FIXED, "0000003", FIXED_AT), dry_run=False)
        sync._apply_events(_doc(FIXED, "0000003", FIXED_AT), dry_run=False)

        assert _pending(it) == FIXED
        assert sum(_pending(it).values()) == 112


def test_internal_forward_of_the_stale_copy_cannot_undo_the_correction():
    """The protection the revision number used to provide, via the sender."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        sync._apply_events(_doc(FIXED, "0000003", FIXED_AT), dry_run=False)
        # Someone forwards the old PDF into info@ the next morning.
        report = sync._apply_events(
            _doc(STALE, "0000006", "2026-08-26T09:00:00Z", sender=INTERNAL),
            dry_run=False)

        assert report.get("po_revisions_skipped")
        assert _pending(it) == FIXED
        assert sum(_pending(it).values()) == 112
