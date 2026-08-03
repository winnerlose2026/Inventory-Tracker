"""Tests for the Cheney stock-image OCR mapping + safety guards.

Mocks the OCR/zip/openpyxl layers (no RapidOCR model or real .xlsx needed).
Item # is the authoritative variety key (it OCRs cleanly); the description is a
non-blocking sanity note. Guards: unresolved rows and under-read images block.

Regression cover for the 2026-08-03 incident, in which this path overwrote all
36 Cheney on-hand figures with a week of usage:
  * the embedded screenshot in Ross's workbook is the case-movement (USAGE)
    grid, not an on-hand stock table -> must never yield on_hand;
  * the old token filter discarded the literal "1" and "60" (meaning to drop the
    "1:60" pack column), which threw away genuine counts of 1 and 60 and let the
    row fall through to the Mfq.Product Code column, writing mfg codes (1184,
    1171, 1152, 1151, 1157, 1156) into Punta Gorda's quantities.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
import cheney_stock_ocr as C
from integrations.hh_mfg_codes import CHENEY_ITEM_NO_TO_MFG, HH_MFG_CODE_TO_VARIETY

DATE_ROW = [[["Drill Down Reporting : Date Range >= 06/22/2026 AND <= 06/27/2026"]]]

# A clean on-hand stock table: "Item # | Description | Brand | Pack | Size |
# UOM | Stock" and none of the case-movement markers.
STOCK_TEXTS = ["Item #", "Description", "Brand", "Pack", "Size", "UOM", "Stock"]

# _patch() rebinds module globals, so keep the originals to restore in tests
# that exercise the real token reconstruction (otherwise a stub leaks across).
_REAL = {n: getattr(C, n) for n in ("_cell_rows", "_extract_pngs",
                                    "_ocr_page", "_ocr_rows", "_engine")}


def _restore():
    for n, fn in _REAL.items():
        setattr(C, n, fn)


def _patch(rows, texts=None):
    _restore()
    C._cell_rows = lambda b: DATE_ROW
    C._extract_pngs = lambda b: [b"png"]
    C._ocr_page = lambda png: (rows, list(texts if texts is not None else STOCK_TEXTS))
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


# --- 2026-08-03 incident regressions --------------------------------------

def test_case_movement_image_yields_no_on_hand():
    """The real failure: Ross's embedded PNG is the usage grid. Its "Full Cases"
    column summed to 192/108/32 -- exactly what landed in on-hand. Any image
    carrying case-movement markers must produce zero on_hand events."""
    items = ["10153043","10153044","10153045","10153046","10153047","10153048",
             "10153034","10153018","10153019","10153041","10153049","10153042"]
    rows = [{"desc": "", "item": it, "stock": 8, "min_score": 1.0} for it in items]
    _patch(rows, texts=["Products", "Pack", "Dist Item #", "Mfq.Product Code",
                        "Full Cases", "Sum of All Products Activity", "32"])
    wh, cd, events, warn, notes = C.extract_facility(b"x", "HHPuntaGorda.xlsx")
    assert events == [], events
    assert any("not an on-hand stock table" in w for w in warn), warn


def test_image_without_a_stock_header_is_refused():
    """Belt-and-braces: can't positively identify it as a stock table -> no
    on_hand, even with no case-movement marker present."""
    _patch([{"desc": "", "item": "10153018", "stock": 26, "min_score": 1.0}],
           texts=["Item #", "Description", "Brand", "Pack", "Size", "UOM"])
    _wh, _cd, events, warn, _n = C.extract_facility(b"x", "HHOcala.xlsx")
    assert events == [], events
    assert any("no on-hand stock-table header" in w for w in warn), warn


def test_each_case_movement_marker_blocks_on_hand():
    for marker in ("Full Cases", "Mfq.Product Code", "Dist Item #",
                   "Sum of All Products Activity", "Drill Down Reporting",
                   "DSRGroup =MICHAEL ROSS-8564"):
        _patch([{"desc": "", "item": "10153018", "stock": 26, "min_score": 1.0}],
               texts=STOCK_TEXTS + [marker])
        _wh, _cd, events, warn, _n = C.extract_facility(b"x", "HHOcala.xlsx")
        assert events == [], (marker, events)
        assert any("not an on-hand stock table" in w for w in warn), (marker, warn)


def test_mfg_code_as_stock_blocks_facility():
    """No on-hand value may equal an HH_MFG_CODE_TO_VARIETY key. This is the
    Punta Gorda signature: each variety took its OWN mfg code as its count."""
    pairs = [("10153047", 1184), ("10153044", 1171), ("10153019", 1152),
             ("10153034", 1151), ("10153049", 1157), ("10153042", 1156)]
    rows = [{"desc": "", "item": it, "stock": code, "min_score": 1.0}
            for it, code in pairs]
    _patch(rows)
    wh, cd, events, warn, notes = C.extract_facility(b"x", "HHPuntaGorda.xlsx")
    assert any("mfg code" in w for w in warn), warn
    for _it, code in pairs:
        assert str(code) in " ".join(warn), (code, warn)


def test_no_emitted_stock_value_is_ever_a_mfg_code():
    """Property check across every code: a facility whose rows OCR to a mfg
    code must be flagged, never committed."""
    for code in sorted(HH_MFG_CODE_TO_VARIETY):
        _patch([{"desc": "", "item": "10153018", "stock": int(code), "min_score": 1.0}])
        _wh, _cd, events, warn, _n = C.extract_facility(b"x", "HHOcala.xlsx")
        # A warning is what blocks the facility from committing (see
        # ingest_cheney_stock_from_endpoint / cheney_stock_ocr.main).
        assert any("mfg code" in w for w in warn), (code, warn)
        assert str(code) in " ".join(warn), (code, warn)
        # The row is reported, not silently dropped, so the fault is visible.
        assert any(str(int(e["item"]["quantity"])) == code for e in events), code


def test_pack_and_size_tokens_are_dropped_not_values_1_and_60():
    """Ocala Plain was genuinely 60 cases and six Punta Gorda rows were
    genuinely 1. The old filter blacklisted the strings "1" and "60", so those
    real counts vanished. Pack/size must be excluded by SHAPE ("1:60", "60CT"),
    leaving bare 1 and 60 as valid stock."""
    assert C._PACK_RE.match("1:60") and C._PACK_RE.match("1:60 CT")
    assert C._SIZE_RE.match("60CT") and C._SIZE_RE.match("60 CT")
    assert not C._PACK_RE.match("60") and not C._SIZE_RE.match("60")
    assert not C._PACK_RE.match("1") and not C._SIZE_RE.match("1")


def test_ocr_rows_prefers_stock_over_mfg_code_column():
    """End-to-end through the token reconstruction: a row laid out
    Pack | Dist Item # | Mfq.Product Code | Stock must yield Stock -- including
    when Stock is 1, the value the old filter discarded."""
    import pytest
    # numpy + Pillow are OCR-only deps (requirements-ocr.txt), not web-service
    # runtime deps, so skip cleanly where they're absent (e.g. CI) rather than
    # failing and blocking the deploy pipeline -- same pattern as
    # test_cheney_inventory_report.test_extract_stock_image.
    pytest.importorskip("numpy")
    _Image = pytest.importorskip("PIL.Image")
    _restore()

    def fake_engine(arr):
        # (box, text, score); x centres ascending = left-to-right columns
        def box(x):
            return [[x, 10], [x + 8, 10], [x + 8, 20], [x, 20]]
        toks = [(box(10), "BAGEL EGG PARBAKED", 0.99),
                (box(200), "1:60 CT", 0.99),
                (box(300), "10153047", 0.99),
                (box(400), "1184", 0.99),
                (box(500), "1", 0.99)]
        return toks, None
    C._engine = lambda: fake_engine
    import io as _io
    buf = _io.BytesIO()
    _Image.new("RGB", (600, 40), "white").save(buf, format="PNG")
    rows, texts = C._ocr_page(buf.getvalue())
    assert len(rows) == 1, rows
    assert rows[0]["stock"] == 1, rows            # not 1184
    assert rows[0]["item"] == "10153047"
    assert "1184" in texts


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
    print("ALL CHENEY STOCK OCR TESTS PASSED")
