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


if __name__ == "__main__":
    test_warehouse_from_filename()
    test_parse_mfg_format()
    test_parse_description_only()
    test_unknown_warehouse_filename()
    print("ALL CHENEY PARSER TESTS PASSED")
