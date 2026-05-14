"""Regression tests for on_order duplicate prevention + cleanup.

Background: the mailbox scan fetches messages from both JD@ and info@,
and the same PO email is often delivered to both inboxes. Without
dedup, every line item ends up booked twice in `item["on_order"]`,
which then double-counts the restock when the entry rolls over into
`item["quantity"]`.

Two-layer defense:
  1. `sync_inventory._apply_events` dedupes within each PO group before
     applying — prevents NEW duplicates from being booked.
  2. `inventory_tracker._dedup_on_order` runs on every `load_inventory`
     call — cleans up duplicates that pre-date the apply-path dedup, and
     any that slip through later via direct API posts.

Both layers must keep distinct-qty lines under the same PO (those are
legitimate separate line items on the same purchase order).
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def _setup_temp_inventory(tmp: Path):
    """Repoint inventory_tracker (and sync_inventory) at a temp data dir."""
    os.environ["DATA_DIR"] = str(tmp)
    import inventory_tracker
    inventory_tracker.DATA_DIR = tmp
    inventory_tracker.INVENTORY_FILE = tmp / "inventory.json"
    inventory_tracker.USAGE_FILE = tmp / "usage.json"
    import sync_inventory
    sync_inventory.INVENTORY_FILE = inventory_tracker.INVENTORY_FILE
    return inventory_tracker, sync_inventory


def _seed(inventory_tracker) -> dict:
    from seed_bagels import BAGELS
    inv = {b["name"].lower(): dict(b, on_order=[]) for b in BAGELS}
    inventory_tracker.save_inventory(inv)
    inventory_tracker.save_usage([])
    return inv


def test_apply_path_dedupes_duplicate_lines_in_same_batch():
    """Posting the same PO line twice in one batch should book it once."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        from integrations.base import SyncItem
        from integrations.email_scanner import EmailEvent

        def make_evt():
            return EmailEvent(
                event_type="restock",
                item=SyncItem(
                    quantity=8.0,
                    distributor="US Foods",
                    variety="Blueberry",
                    warehouse="Manassas, VA",
                    unit="cases",
                ),
                source_message_id="m1",
                source_subject="USF PO 9999991O",
                po_number="9999991O",
                po_revision="0000001",
            )

        report = sync._apply_events([make_evt(), make_evt()], dry_run=False)
        inv = it._load(it.INVENTORY_FILE)
        pending = inv["blueberry bagel 4oz [usf - manassas]"]["on_order"]
        assert len(pending) == 1, f"expected 1 pending, got {len(pending)}"
        assert report.get("dedup_dropped"), \
            "expected report.dedup_dropped to record the collapse"


def test_load_path_cleans_up_existing_duplicates():
    """Pre-existing duplicate entries should be collapsed on next load."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        dup = {
            "qty": 5.0, "unit": "cs",
            "eta": "2030-12-31T00:00:00",
            "ordered_at": "2026-05-14T14:00:00",
            "po_number": "TESTPO", "po_revision": "rev1",
            "source": "test",
        }
        inv = it._load(it.INVENTORY_FILE)
        inv["plain bagel 4oz [usf - manassas]"]["on_order"] = [
            copy.deepcopy(dup), copy.deepcopy(dup),
        ]
        it.save_inventory(inv)

        reloaded = it.load_inventory()
        plain = reloaded["plain bagel 4oz [usf - manassas]"]["on_order"]
        assert len(plain) == 1, f"expected 1 after dedup, got {len(plain)}"


def test_load_path_keeps_distinct_qty_lines_under_same_po():
    """Same PO with two genuinely different qtys is two separate lines."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        base = {
            "unit": "cs",
            "eta": "2030-12-31T00:00:00",
            "ordered_at": "2026-05-14T14:00:00",
            "po_number": "TESTPO", "po_revision": "rev1",
            "source": "test",
        }
        inv = it._load(it.INVENTORY_FILE)
        inv["everything bagel 4oz [usf - manassas]"]["on_order"] = [
            dict(base, qty=10.0),
            dict(base, qty=20.0),
        ]
        it.save_inventory(inv)

        reloaded = it.load_inventory()
        every = reloaded["everything bagel 4oz [usf - manassas]"]["on_order"]
        assert len(every) == 2, f"expected 2 distinct lines kept, got {len(every)}"
        qtys = sorted(e["qty"] for e in every)
        assert qtys == [10.0, 20.0]


def test_load_path_is_idempotent():
    """Loading twice in a row produces no further changes."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        dup = {
            "qty": 5.0, "unit": "cs",
            "eta": "2030-12-31T00:00:00",
            "ordered_at": "2026-05-14T14:00:00",
            "po_number": "TESTPO", "po_revision": "rev1",
        }
        inv = it._load(it.INVENTORY_FILE)
        inv["plain bagel 4oz [usf - manassas]"]["on_order"] = [
            copy.deepcopy(dup), copy.deepcopy(dup), copy.deepcopy(dup),
        ]
        it.save_inventory(inv)

        first = it.load_inventory()
        second = it.load_inventory()
        assert first["plain bagel 4oz [usf - manassas]"]["on_order"] \
            == second["plain bagel 4oz [usf - manassas]"]["on_order"]
        assert len(second["plain bagel 4oz [usf - manassas]"]["on_order"]) == 1




def test_rebase_ordered_at_rolls_old_pos_into_quantity():
    """A backlogged USF PO dated >30 days ago in source_subject should
    be rebased AND rolled into quantity on next load."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        target = "blueberry bagel 4oz [usf - manassas]"
        inv = it._load(it.INVENTORY_FILE)
        qty_before = inv[target]["quantity"]
        inv[target]["on_order"] = [{
            "qty": 8.0, "unit": "cs",
            "eta": "2026-06-12T00:00:00",
            "ordered_at": "2026-05-13T20:00:00",
            "po_number": "TESTPO1", "po_revision": "0000001",
            "source": "Email Inbox",
            "source_subject": "USF PO 999999 5O/2125 03/25/26 ...",
        }]
        it.save_inventory(inv)

        reloaded = it.load_inventory()
        assert reloaded[target]["quantity"] == qty_before + 8.0
        assert reloaded[target]["on_order"] == []


def test_rebase_keeps_recent_po_pending_with_corrected_dates():
    """A USF PO dated within 30 days should stay pending, but with
    ordered_at rebased to the actual PO date and eta = +lead days."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        target = "plain bagel 4oz [usf - manassas]"
        inv = it._load(it.INVENTORY_FILE)
        inv[target]["on_order"] = [{
            "qty": 24.0, "unit": "cs",
            "eta": "2026-06-12T00:00:00",
            "ordered_at": "2026-05-13T20:00:00",
            "po_number": "TESTPO2", "po_revision": "0000001",
            "source": "Email Inbox",
            "source_subject": "USF PO 888888 5O/2125 05/05/26 ...",
        }]
        it.save_inventory(inv)

        reloaded = it.load_inventory()
        pending = reloaded[target]["on_order"]
        assert len(pending) == 1
        assert pending[0]["ordered_at"].startswith("2026-05-05")
        assert pending[0]["eta"].startswith("2026-06-04")


def test_collapse_keeps_highest_revision_for_same_po():
    """Two pending entries for the same SKU + same po_number but
    different po_revision should collapse to the highest revision."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        target = "asiago bagel 4oz [usf - la mirada]"
        inv = it._load(it.INVENTORY_FILE)
        inv[target]["on_order"] = [
            {"qty": 88.0, "unit": "cs", "eta": "2030-12-31T00:00:00",
             "ordered_at": "2026-05-13T20:00:00",
             "po_number": "5334574C", "po_revision": "0000001"},
            {"qty": 88.0, "unit": "cs", "eta": "2030-12-31T00:00:00",
             "ordered_at": "2026-05-13T20:00:00",
             "po_number": "5334574C", "po_revision": "0000002"},
        ]
        it.save_inventory(inv)

        reloaded = it.load_inventory()
        pending = reloaded[target]["on_order"]
        assert len(pending) == 1
        assert pending[0]["po_revision"] == "0000002"


def test_collapse_preserves_legitimate_two_line_pos():
    """Same SKU + same po_number + same po_revision but different
    quantities is a real two-line PO (or legit duplicate line item)
    and must be preserved -- the collapse only fires across revisions."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, _ = _setup_temp_inventory(Path(td))
        _seed(it)

        target = "blueberry bagel 4oz [usf - manassas]"
        inv = it._load(it.INVENTORY_FILE)
        inv[target]["on_order"] = [
            {"qty": 8.0, "unit": "cs", "eta": "2030-12-31T00:00:00",
             "ordered_at": "2026-05-13T20:00:00",
             "po_number": "4363705O", "po_revision": "0000001"},
            {"qty": 16.0, "unit": "cs", "eta": "2030-12-31T00:00:00",
             "ordered_at": "2026-05-13T20:00:00",
             "po_number": "4363705O", "po_revision": "0000001"},
        ]
        it.save_inventory(inv)

        reloaded = it.load_inventory()
        pending = reloaded[target]["on_order"]
        qtys = sorted(e["qty"] for e in pending)
        assert qtys == [8.0, 16.0]


if __name__ == "__main__":
    test_apply_path_dedupes_duplicate_lines_in_same_batch()
    print("OK test_apply_path_dedupes_duplicate_lines_in_same_batch")
    test_load_path_cleans_up_existing_duplicates()
    print("OK test_load_path_cleans_up_existing_duplicates")
    test_load_path_keeps_distinct_qty_lines_under_same_po()
    print("OK test_load_path_keeps_distinct_qty_lines_under_same_po")
    test_load_path_is_idempotent()
    print("OK test_load_path_is_idempotent")
    test_rebase_ordered_at_rolls_old_pos_into_quantity()
    print("OK test_rebase_ordered_at_rolls_old_pos_into_quantity")
    test_rebase_keeps_recent_po_pending_with_corrected_dates()
    print("OK test_rebase_keeps_recent_po_pending_with_corrected_dates")
    test_collapse_keeps_highest_revision_for_same_po()
    print("OK test_collapse_keeps_highest_revision_for_same_po")
    test_collapse_preserves_legitimate_two_line_pos()
    print("OK test_collapse_preserves_legitimate_two_line_pos")
    print("ALL TESTS PASS")
