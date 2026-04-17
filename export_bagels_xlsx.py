#!/usr/bin/env python3
"""Export the unified bagel inventory to an Excel (.xlsx) workbook.

Reads items from the inventory store (populated by seed_bagels.py or the app)
and writes a multi-sheet workbook:

  - Summary         roll-up per distributor
  - Unified List    every SKU from Cheney Brothers and US Foods together
  - Cheney Brothers only Cheney SKUs
  - US Foods        only US Foods SKUs

Usage:
    python export_bagels_xlsx.py [output_path]

If output_path is omitted, writes to bagel_inventory.xlsx in the cwd.
"""

import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from inventory_tracker import load_inventory


HEADER_FILL = PatternFill("solid", fgColor="1F2A44")
HEADER_FONT = Font(bold=True, color="FFFFFF")
SECTION_FILL = PatternFill("solid", fgColor="E8EEF9")
SECTION_FONT = Font(bold=True, color="1F2A44")

HEADERS = [
    "Name", "Distributor", "Category", "Quantity", "Unit",
    "Price per Unit", "Extended Value", "Low-Stock Threshold", "Status",
]


def _status(item: dict) -> str:
    return "LOW" if item["quantity"] <= item["low_stock_threshold"] else "OK"


def _write_header(ws, row=1):
    for col, label in enumerate(HEADERS, start=1):
        cell = ws.cell(row=row, column=col, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 22
    ws.freeze_panes = f"A{row + 1}"


def _write_row(ws, row: int, item: dict):
    extended = item["quantity"] * item["price"]
    values = [
        item["name"],
        item.get("distributor") or "Unassigned",
        item.get("category", ""),
        item["quantity"],
        item["unit"],
        item["price"],
        extended,
        item["low_stock_threshold"],
        _status(item),
    ]
    for col, v in enumerate(values, start=1):
        ws.cell(row=row, column=col, value=v)
    ws.cell(row=row, column=6).number_format = '"$"#,##0.00'
    ws.cell(row=row, column=7).number_format = '"$"#,##0.00'
    if _status(item) == "LOW":
        ws.cell(row=row, column=9).font = Font(bold=True, color="B91C1C")


def _autosize(ws):
    widths = {1: 40, 2: 18, 3: 12, 4: 10, 5: 8, 6: 14, 7: 16, 8: 18, 9: 10}
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def _write_items_sheet(ws, items: list):
    _write_header(ws, row=1)
    items = sorted(items, key=lambda x: (x.get("distributor", ""), x["name"]))
    for i, item in enumerate(items, start=2):
        _write_row(ws, i, item)
    total_row = len(items) + 2
    ws.cell(row=total_row, column=1, value="TOTAL").font = Font(bold=True)
    ws.cell(row=total_row, column=4,
            value=f"=SUM(D2:D{total_row - 1})").font = Font(bold=True)
    ws.cell(row=total_row, column=7,
            value=f"=SUM(G2:G{total_row - 1})").font = Font(bold=True)
    ws.cell(row=total_row, column=7).number_format = '"$"#,##0.00'
    _autosize(ws)


def _write_summary_sheet(ws, inv: dict):
    groups: dict[str, list] = {}
    for item in inv.values():
        groups.setdefault(item.get("distributor") or "Unassigned", []).append(item)

    ws.cell(row=1, column=1, value="Unified Bagel Inventory").font = Font(size=16, bold=True)
    ws.cell(row=2, column=1, value="Cheney Brothers + US Foods").font = Font(italic=True, color="64748B")

    headers = ["Distributor", "SKU Count", "Units on Hand", "Inventory Value", "Low-Stock SKUs"]
    for col, label in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col, value=label)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[4].height = 22

    row = 5
    grand_sku = grand_qty = grand_val = grand_low = 0
    for dist, items in sorted(groups.items()):
        sku_count = len(items)
        qty = sum(i["quantity"] for i in items)
        value = sum(i["quantity"] * i["price"] for i in items)
        low = sum(1 for i in items if i["quantity"] <= i["low_stock_threshold"])
        ws.cell(row=row, column=1, value=dist).font = SECTION_FONT
        ws.cell(row=row, column=1).fill = SECTION_FILL
        ws.cell(row=row, column=2, value=sku_count)
        ws.cell(row=row, column=3, value=qty)
        ws.cell(row=row, column=4, value=value).number_format = '"$"#,##0.00'
        ws.cell(row=row, column=5, value=low)
        grand_sku += sku_count
        grand_qty += qty
        grand_val += value
        grand_low += low
        row += 1

    ws.cell(row=row, column=1, value="TOTAL").font = Font(bold=True)
    ws.cell(row=row, column=2, value=grand_sku).font = Font(bold=True)
    ws.cell(row=row, column=3, value=grand_qty).font = Font(bold=True)
    total_val = ws.cell(row=row, column=4, value=grand_val)
    total_val.font = Font(bold=True)
    total_val.number_format = '"$"#,##0.00'
    ws.cell(row=row, column=5, value=grand_low).font = Font(bold=True)

    for col, w in {1: 22, 2: 12, 3: 15, 4: 18, 5: 16}.items():
        ws.column_dimensions[get_column_letter(col)].width = w


def export(output: Path):
    inv = load_inventory()
    if not inv:
        print("  Inventory is empty. Run: python seed_bagels.py")
        return

    items = list(inv.values())
    cheney = [i for i in items if (i.get("distributor") or "") == "Cheney Brothers"]
    usfoods = [i for i in items if (i.get("distributor") or "") == "US Foods"]

    wb = Workbook()
    _write_summary_sheet(wb.active, inv)
    wb.active.title = "Summary"

    _write_items_sheet(wb.create_sheet("Unified List"), items)
    _write_items_sheet(wb.create_sheet("Cheney Brothers"), cheney)
    _write_items_sheet(wb.create_sheet("US Foods"), usfoods)

    wb.save(output)
    print(f"  Wrote {len(items)} SKUs to {output}")
    print(f"    Cheney Brothers: {len(cheney)} SKUs")
    print(f"    US Foods:        {len(usfoods)} SKUs")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("bagel_inventory.xlsx")
    export(out)
