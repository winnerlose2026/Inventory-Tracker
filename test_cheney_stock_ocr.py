"""Tests for the Cheney stock-image OCR mapping + safety guards.

Mocks the OCR/zip/openpyxl layers (no RapidOCR model or real .xlsx needed).
Item # is the authoritative variety key (it OCRs cleanly); the description is a
non-blocking sanity note. Guards: unresolved rows and under-read images block."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
import cheney_stock_ocr as C
from integrations.hh_mfg_codes import CHENEY_ITEM_NO_TO_MFG, HH_MFG_CODE_TO_VARIETY

DATE_ROW = [[["Drill Down Reporting : Date Range >= 06/22/2026 AND <= 06/27/2026"]]]


def _patch(rows):
    C._cell_rows = lambda b: DATE_ROW
    C._extract_pngs = lambda b: [b"png"]
    C._ocr_rows = lambda png: rows


def test_full_facility_maps_by_item_number():
    items = ["10153043","10153044","10153045","10153046","10153047","10153048",
             "10153034","10153018","10153019","10153041","10153049","10153042"]
    rows = [{"desc": "", "item": it, "stock": 10 + n, "min_score": 1.0}
            for n, it in enumerate(items)]
    _patch(rows)
    wh, cd, events, warn, notes = C.extract_facility(b"x", "HHRivieraBeach.xlsx")
    assert wh == "Riviera Beach, FL" and cd == "2026-06-27"
    got = {e["item"]["variety"] for e in events}
    expected = {HH_MFG_CODE_TO_VARIETY[CHENEY_ITEM_NO_TO_MFG[i]] for i in items}
    assert got == expected and len(events) == 12, (got, warn)
    assert warn == [] and notes == [], (warn, notes)
    assert all(e["count_date"] == "2026-06-27" for e in events)


def test_item_number_wins_over_noisy_description():
    # description says Plain, item# says Everything -> item# authoritative + a note
    _patch([{"desc": "BAGEL PLAIN PARBAKED", "item": "10153048", "stock": 99, "min_score": 1.0}])
    wh, cd, events, warn, notes = C.extract_facility(b"x", "HHOcala.xlsx")
    assert [e["item"]["variety"] for e in events] == ["Everything"], events
    assert any("description->Plain" in n for n in notes), notes


def test_unresolved_row_blocks():
    _patch([{"desc": "BAGEL MYSTERY", "item": "", "stock": 5, "min_score": 1.0}])
    wh, cd, events, warn, notes = C.extract_facility(b"x", "HHPuntaGorda.xlsx")
    assert events == []
    assert any("unresolved" in w for w in warn), warn


if __name__ == "__main__":
    test_full_facility_maps_by_item_number()
    test_item_number_wins_over_noisy_description()
    test_unresolved_row_blocks()
    print("ALL CHENEY STOCK OCR TESTS PASSED")
