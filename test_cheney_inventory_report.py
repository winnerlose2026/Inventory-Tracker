"""Tests for the Cheney per-facility inventory & usage xlsx parser."""
import io
import openpyxl
from integrations.cheney_inventory_report import (
    parse_report_xlsx, warehouse_from_filename)


def _wb(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_warehouse_from_filename():
    assert warehouse_from_filename("H&HRVBMay272026.xlsx") == "Riviera Beach, FL"
    assert warehouse_from_filename("H&HOcalaMay272026.xlsx") == "Ocala, FL"
    assert warehouse_from_filename("H&HPuntaGordaMay272026.xlsx") == "Punta Gorda, FL"
    assert warehouse_from_filename("whatever.xlsx") == ""


def test_parse_mfg_format():
    rows = [
        ["H&H Bagels — Riviera Beach Inventory", "", "", "", ""],
        ["Cheney Item #", "Mfg#", "Description", "Cases On Hand", "Weekly Usage"],
        ["10150011", "1150", "BAGEL PLAIN PARBAKED", "48", "6"],
        ["10153019", "1152", "BAGEL POPPY PARBAKED", "12", "3"],
        ["10158022", "1158", "BAGEL EVERYTHING", "30", "5"],
        ["10199999", "9999", "BAGEL MYSTERY FLAVOR", "5", "1"],
        ["", "", "", "", ""],
    ]
    ev, err = parse_report_xlsx(_wb(rows), "H&HRVBMay272026.xlsx")
    by = {e["item"]["variety"]: e["item"] for e in ev}
    assert set(by) == {"Plain", "Poppy Seed", "Everything"}, set(by)
    assert all(it["warehouse"] == "Riviera Beach, FL" for it in by.values())
    assert by["Plain"]["quantity"] == 48 and by["Plain"]["unit"] == "cs"
    assert by["Poppy Seed"]["weekly_usage"] == 3
    assert all(e["event_type"] == "on_hand" for e in ev)
    assert any("9999" in x for x in err), err  # unmapped surfaced, not silent


def test_parse_description_only():
    rows = [
        ["Description", "On Hand", "Weekly Usage"],
        ["Plain", "20", "4"],
        ["BAGEL POPPY PARBAKED", "10", "2"],          # keyword fallback
        ["Whole Wheat Everything", "6", "1"],          # compound before "Everything"
    ]
    ev, err = parse_report_xlsx(_wb(rows), "H&HOcalaMay272026.xlsx")
    v = {e["item"]["variety"] for e in ev}
    assert v == {"Plain", "Poppy Seed", "Whole Wheat Everything"}, v
    assert all(e["item"]["warehouse"] == "Ocala, FL" for e in ev)


def test_unknown_warehouse_filename():
    ev, err = parse_report_xlsx(_wb([["Description", "On Hand"], ["Plain", "5"]]),
                                "mystery.xlsx")
    assert ev == [] and err



def test_parse_stock_inventory_format():
    """Ross's CB Direct on-hand export: Item # / Description / ... / Stock."""
    rows = [
        ["Riviera Beach"],
        ["08564 MICHAEL ROSS"],
        ["Item #", "Description", "Brand", "Pack", "Size", "UOM", "Stock"],
        ["FROZEN GROCERY"],
        ["10153048", "BAGEL EVERYTHING PARBAKED", "H & H", 1, "60CT", "cs", 184],
        ["10153018", "BAGEL PLAIN PARBAKED", "H & H", 1, "60CT", "cs", 181],
        ["10153049", "BAGEL WHOLE WHEAT EVERYTHING P", "H & H", 1, "60CT", "cs", 227],
    ]
    ev, err = parse_report_xlsx(_wb(rows), "HHBagRVB6-8-2026.xlsx")
    by = {e["item"]["variety"]: e["item"] for e in ev}
    assert set(by) == {"Everything", "Plain", "Whole Wheat Everything"}, set(by)
    assert all(e["event_type"] == "on_hand" for e in ev)
    assert by["Everything"]["quantity"] == 184 and by["Everything"]["unit"] == "cs"
    assert all("weekly_usage" not in it for it in by.values())  # stock sheet carries no usage
    assert all(it["warehouse"] == "Riviera Beach, FL" for it in by.values())
    print("OK test_parse_stock_inventory_format")


def test_parse_case_movement_usage():
    """Ross's monthly case-movement export -> usage_rate (monthly -> weekly)."""
    rows = [
        ["Report Creation Date : 6/8/2026"],
        ["Drill Down Reporting : Date Range >= 05/01/2026 AND <= 05/31/2026"],
        ["DSRGroup =MICHAEL ROSS-8564, DC =01 RIVIERA - 01 RIVIERA, Brands =H & H,"],
        ["Products", "Pack", "Dist Item #", "Mfq.Product Code", "GTIN", "Full Cases"],
        ["Sum of All Products Activity", "", "", "", "", 1147],
        ["BAGEL EVERYTHING PARBAKED", "1:60 CT", "10153048", "1158", "", 258],
        ["BAGEL PLAIN PARBAKED", "1:60 CT", "10153018", "1150", "", 250],
        ["BAGEL WHOLE WHEAT PARBAKED", "1:60 CT", "10153042", "1156", "", 50],
    ]
    ev, err = parse_report_xlsx(_wb(rows), "HHBagRVBMay2026.xlsx")
    assert ev and all(e["event_type"] == "usage_rate" for e in ev), [e["event_type"] for e in ev]
    wk = {e["item"]["variety"]: e["item"]["weekly_usage"] for e in ev}
    # 258 * 7 / 31 = 58.26 ; 250 -> 56.45 ; 50 -> 11.29 ; Sum row skipped.
    assert wk == {"Everything": 58.26, "Plain": 56.45, "Whole Wheat": 11.29}, wk
    assert all(e["item"]["warehouse"] == "Riviera Beach, FL" for e in ev)
    assert all(e["item"]["quantity"] == 0.0 for e in ev)  # rate-only; no on-hand
    assert err == [], err  # Sum-of-activity total carries no code -> skipped silently
    print("OK test_parse_case_movement_usage")


if __name__ == "__main__":
    test_warehouse_from_filename()
    test_parse_mfg_format()
    test_parse_description_only()
    test_unknown_warehouse_filename()
    test_parse_stock_inventory_format()
    test_parse_case_movement_usage()
    print("ALL CHENEY PARSER TESTS PASSED")
