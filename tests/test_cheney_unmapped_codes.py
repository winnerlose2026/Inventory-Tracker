"""A Cheney row whose item / mfg code is not in the crosswalk is unmapped --
never resolved from its description; duplicate varieties in one grid are
refused; Pumpernickel is a known variety (review 2026-11-12, item 12)."""
import io

import openpyxl

from integrations.cheney_csv_inventory import parse_inventory_csv
from integrations.cheney_inventory_report import parse_report_xlsx, resolve_variety
from integrations.parsers._common import canonical_variety


def _wb(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


STOCK_HEADER = ["Item #", "Description", "Brand", "Pack", "Size", "UOM", "Stock"]


def test_resolve_variety_rules():
    assert resolve_variety("1150", "anything", "") == ("Plain", False)
    assert resolve_variety("", "BAGEL EVERYTHING", "10153048") == ("Everything", False)
    # A code that is not ours: unmapped, however plain the description reads.
    assert resolve_variety("", "BAGEL PLAIN SLICED 6CT", "20999999") == ("", True)
    assert resolve_variety("7777", "BAGEL PLAIN", "") == ("", True)
    # Blank code cell in a coded grid: a total row, not an error.
    assert resolve_variety("", "Sum of All Products", "") == ("", False)
    # No code column at all: description fallback as before.
    assert resolve_variety("", "BAGEL PLAIN PARBAKED", "",
                           has_code_column=False) == ("Plain", False)


def test_stock_grid_skips_and_reports_an_unmapped_item_number():
    rows = [STOCK_HEADER,
            ["10153018", "BAGEL PLAIN PARBAKED", "H & H", "1", "60 CT", "CS", 120],
            ["20999999", "BAGEL PLAIN SLICED", "OTHER", "6", "6 CT", "CS", 300],
            ["", "Total", "", "", "", "", 420]]
    events, errors = parse_report_xlsx(_wb(rows), "HHBagRVB11-9-2026.xlsx")
    assert [(e["item"]["variety"], e["item"]["quantity"]) for e in events] == [
        ("Plain", 120.0)]
    assert len(errors) == 1 and "unmapped item #" in errors[0] and "20999999" in errors[0]


def test_duplicate_variety_in_one_grid_applies_neither():
    rows = [STOCK_HEADER,
            ["10153018", "BAGEL PLAIN PARBAKED", "H & H", "1", "60 CT", "CS", 120],
            ["10153018", "BAGEL PLAIN PARBAKED", "H & H", "1", "60 CT", "CS", 15],
            ["10153048", "BAGEL EVERYTHING", "H & H", "1", "60 CT", "CS", 80]]
    events, errors = parse_report_xlsx(_wb(rows), "HHBagRVB11-9-2026.xlsx")
    assert [e["item"]["variety"] for e in events] == ["Everything"]
    assert any("duplicate variety 'Plain'" in e for e in errors)


def test_csv_feed_does_not_resolve_an_unknown_item_by_description():
    csv = ("Cheney Item #,Product Description,DC,On Hand Cases\n"
           "10153018,Plain Bagel,Ocala,44\n"
           "20999999,Plain Bagel Sliced,Ocala,300\n")
    events, errors = parse_inventory_csv(csv)
    assert [e["item"]["quantity"] for e in events] == [44]
    assert any("unmapped item #" in e and "20999999" in e for e in errors)
    # No item column: the description still resolves.
    events, errors = parse_inventory_csv(
        "Product Description,DC,On Hand Cases\nPlain Bagel,Ocala,44\n")
    assert len(events) == 1 and not errors


def test_pumpernickel_is_a_known_variety():
    assert canonical_variety("BAGEL PUMPERNICKEL 4OZ") == "Pumpernickel"
    assert canonical_variety("Whole Wheat Everything Bagel") == "Whole Wheat Everything"
