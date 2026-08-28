"""on_hand write guards in sync_inventory._apply_email_event.

Regression cover for the 2026-08-03 Cheney incident. Two independent holes let a
screenshot OCR of Michael Ross's USAGE grid replace all 36 real on-hand counts:

  1. No integrity check on the value itself, so H&H mfg codes (1184 = Egg,
     1171 = Blueberry, 1152 = Poppy Seed, 1151 = Onion, 1157 = Whole Wheat
     Everything, 1156 = Whole Wheat) landed as Punta Gorda case counts.
  2. The staleness guard compared `count_date[:10]`. Cheney's weekly workbook is
     stamped with its period-end date, so the correct cell-grid read and the bad
     OCR read of the SAME file both carried count_date 2026-08-01 -- equal at day
     granularity, so whichever applied last won. Tightening to the full
     count_date does NOT separate them; source rank does.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def _setup_temp_inventory(tmp: Path):
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


def _on_hand(qty, variety, warehouse, *, count_date="", source_id="",
             received="", distributor="Cheney Brothers"):
    from integrations.base import SyncItem
    from integrations.email_scanner import EmailEvent
    return EmailEvent(
        event_type="on_hand",
        item=SyncItem(quantity=float(qty), distributor=distributor,
                      variety=variety, warehouse=warehouse, unit="cs"),
        source_message_id=source_id,
        source_subject="Cheney inventory & usage",
        count_date=count_date,
        source_received_at=received,
    )


PGD = "Punta Gorda, FL"
KEY_EGG_PGD = "egg bagel 4oz [cb - punta gorda]"
KEY_PLAIN_OCALA = "plain bagel 4oz [cb - ocala]"


# --- guard 1: a mfg code is never a case count ----------------------------

def test_mfg_code_as_on_hand_is_refused():
    """The exact Punta Gorda write: Egg on-hand 14 -> 1184 (Egg's own code)."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        sync._apply_events([_on_hand(14, "Egg", PGD, count_date="2026-07-25",
                                     source_id="cheney-xlsx:wk30.xlsx#1")],
                           dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_EGG_PGD]["quantity"] == 14

        report = sync._apply_events(
            [_on_hand(1184, "Egg", PGD, count_date="2026-08-01",
                      source_id="cheney-stock-endpoint:Punta Gorda, FL")],
            dry_run=False)
        inv = it._load(it.INVENTORY_FILE)
        assert inv[KEY_EGG_PGD]["quantity"] == 14, "mfg code must not be written"
        assert report.get("rejected"), "rejection must be reported, not silent"
        assert "1184" in " ".join(report["rejected"])


def test_every_mfg_code_is_refused_as_on_hand():
    from integrations.hh_mfg_codes import HH_MFG_CODE_TO_VARIETY
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        for code in sorted(HH_MFG_CODE_TO_VARIETY):
            r = sync._apply_events(
                [_on_hand(int(code), "Plain", "Ocala, FL",
                          source_id="cheney-stock-endpoint:Ocala, FL")],
                dry_run=False)
            assert r.get("rejected"), code
            assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] != int(code)


def test_plausible_counts_near_the_code_range_still_apply():
    """The guard is an exact-match tripwire, not a ceiling.

    The sample values must not themselves be mfg codes -- 1154 used to sit in
    the gap between 1153 and 1155 until Pumpernickel (the October LTO) claimed
    it. Assert the gap instead of trusting a literal.
    """
    from integrations.hh_mfg_codes import HH_MFG_CODE_TO_VARIETY as codes
    for probe in (1, 60, 417, 1149, 1190):
        assert str(probe) not in codes, probe
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        for qty in (1, 60, 417, 1149, 1190):
            sync._apply_events(
                [_on_hand(qty, "Plain", "Ocala, FL",
                          source_id="cheney-xlsx:wk.xlsx#1")], dry_run=False)
            assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == qty, qty


# --- guard 2: source precedence on the same count_date --------------------

def test_ocr_cannot_overwrite_a_cell_grid_count_of_the_same_date():
    """The incident, reproduced: both writes carry count_date 2026-08-01."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)

        # Correct read, straight out of the worksheet cells: Stock = 60.
        sync._apply_events(
            [_on_hand(60, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-xlsx:HHBagelsOcala.xlsx#8")],
            dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60

        # OCR of the embedded usage grid: "Full Cases" = 26 for the same week.
        report = sync._apply_events(
            [_on_hand(26, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-stock-endpoint:Ocala, FL")],
            dry_run=False)
        inv = it._load(it.INVENTORY_FILE)
        assert inv[KEY_PLAIN_OCALA]["quantity"] == 60, \
            "a screenshot OCR must not beat a cell-grid read of the same file"
        assert report.get("stale_skipped"), "the skip must be reported"


def test_cell_grid_does_overwrite_an_earlier_ocr_count_of_the_same_date():
    """Precedence runs the other way too -- the trusted source repairs it."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events(
            [_on_hand(26, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-stock-endpoint:Ocala, FL")],
            dry_run=False)
        sync._apply_events(
            [_on_hand(60, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-xlsx:HHBagelsOcala.xlsx#8")],
            dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60


def test_manual_correction_outranks_everything():
    """A human fixing a number must always land."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events(
            [_on_hand(60, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-xlsx:HHBagelsOcala.xlsx#8")],
            dry_run=False)
        sync._apply_events(
            [_on_hand(48, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="manual-correction:JD")], dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 48


# --- guard 3: date ordering, now at full precision ------------------------

def test_older_count_date_still_loses():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events(
            [_on_hand(60, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-xlsx:wk31.xlsx#1")], dry_run=False)
        sync._apply_events(
            [_on_hand(90, "Plain", "Ocala, FL", count_date="2026-07-25",
                      source_id="cheney-xlsx:wk30.xlsx#1")], dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60


def test_same_day_timestamps_now_order_by_full_count_date():
    """Two counts on one calendar day, distinguished only below the day.
    The old `count_date[:10]` comparison treated these as equal."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events(
            [_on_hand(60, "Plain", "Ocala, FL",
                      count_date="2026-08-01T18:00:00Z",
                      source_id="cheney-xlsx:pm.xlsx#1")], dry_run=False)
        sync._apply_events(
            [_on_hand(90, "Plain", "Ocala, FL",
                      count_date="2026-08-01T06:00:00Z",
                      source_id="cheney-xlsx:am.xlsx#1")], dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60, \
            "the earlier same-day count must not win"


def test_equal_rank_and_date_falls_back_to_email_received_time():
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events(
            [_on_hand(60, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-xlsx:a.xlsx#1",
                      received="2026-08-02T13:47:00Z")], dry_run=False)
        sync._apply_events(
            [_on_hand(90, "Plain", "Ocala, FL", count_date="2026-08-01",
                      source_id="cheney-xlsx:b.xlsx#1",
                      received="2026-08-02T09:00:00Z")], dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60, \
            "the earlier-received copy must not clobber the later one"


def test_first_ever_count_always_lands():
    """No incumbent date/rank -> nothing to compare, so it must apply."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        sync._apply_events(
            [_on_hand(33, "Blueberry", PGD, count_date="2026-08-01",
                      source_id="cheney-stock-endpoint:Punta Gorda, FL")],
            dry_run=False)
        assert it._load(it.INVENTORY_FILE)[
            "blueberry bagel 4oz [cb - punta gorda]"]["quantity"] == 33


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
    print("ALL ON-HAND GUARD TESTS PASSED")


# --- provenance survives the real scan path -------------------------------

def test_scan_path_carries_parser_provenance_so_cellgrid_outranks_ocr():
    """The live scan overwrites source_message_id with the Graph message id, so
    without parser_source a cell-grid read ranked only by the DEFAULT tier and
    every rescan logged a bogus "lower-trust" skip against itself."""
    import sync_inventory as sync
    from integrations.email_scanner import EmailEvent
    from integrations.base import SyncItem

    def evt(parser_source):
        return EmailEvent(
            event_type="on_hand",
            item=SyncItem(quantity=60.0, distributor="Cheney Brothers",
                          variety="Plain", warehouse="Ocala, FL", unit="cs"),
            source_message_id="AAMkAD-graph-message-id-not-a-parser-id",
            source_subject="Usage / Stock",
            count_date="2026-08-01",
            parser_source=parser_source,
        )

    cell = sync._onhand_source_rank(
        getattr(evt("cheney-xlsx:HHBagelsOcala.xlsx#8"), "parser_source", ""),
        "AAMkAD-graph-message-id-not-a-parser-id", "ms365")
    ocr = sync._onhand_source_rank(
        "cheney-stock-endpoint:Ocala, FL", "", "cheney-stock-ocr/endpoint")
    bare = sync._onhand_source_rank("", "AAMkAD-graph-message-id", "ms365")

    assert cell == 60, cell
    assert ocr == 20, ocr
    assert ocr < cell, "OCR must never outrank a cell-grid read"
    assert bare == sync._DEFAULT_ONHAND_RANK
    # and the field really is plumbed onto the dataclass
    assert evt("cheney-xlsx:x#1").parser_source == "cheney-xlsx:x#1"


def test_cellgrid_rescan_of_the_same_count_date_is_not_self_skipped():
    """Rescanning the same workbook must be a clean no-op, not a rank skip."""
    with TemporaryDirectory() as td:
        sys.path.insert(0, str(Path(__file__).parent))
        it, sync = _setup_temp_inventory(Path(td))
        _seed(it)
        from integrations.email_scanner import EmailEvent
        from integrations.base import SyncItem

        def scan_evt():
            return EmailEvent(
                event_type="on_hand",
                item=SyncItem(quantity=60.0, distributor="Cheney Brothers",
                              variety="Plain", warehouse="Ocala, FL", unit="cs"),
                source_message_id="AAMkAD-graph-id",
                source_subject="Usage / Stock",
                count_date="2026-08-01",
                parser_source="cheney-xlsx:HHBagelsOcala.xlsx#8",
            )

        sync._apply_events([scan_evt()], dry_run=False)
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60
        r = sync._apply_events([scan_evt()], dry_run=False)
        assert not r.get("stale_skipped"), r.get("stale_skipped")
        assert r["unchanged"] == 1, r
        assert it._load(it.INVENTORY_FILE)[KEY_PLAIN_OCALA]["quantity"] == 60
