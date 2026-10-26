"""Chicago's inventory worksheet: the usage column beside each CS OH column was
headed "WKLY USE" until mid-July 2026 and plain "USE" after. Both must yield
weekly_usage -- otherwise every count lands with the usage silently dropped.
"""
import io
import sys

sys.path.insert(0, ".")

import openpyxl

from integrations import bagel_inventory_worksheet as W


def _sheet(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _real_layout():
    # Mirrors "HH Bagel Inventory Worksheet.xlsx" (10/26/26): early pairs say
    # WKLY USE, later ones USE, and some CS OH headers carry a trailing space.
    return _sheet([
        ["DATE", None, None, "6.1.26", None, "10.19.26", None, "10.26.26", None],
        ["USF #", "MFG #", "DESCRIPTION", "CS OH", "WKLY USE", "CS OH ", "USE", "CS OH ", "USE"],
        [1055064, 1159, " BAGEL, ASIAGO CHEESE WHEAT 4.06 OZ", 48, 6, 50, 8, 45, 8],
        [6950804, 1153, " BAGEL, SESAME 4.25 OZ", 88, 12, 58, 4, 28, 14],
        [7095637, 1150, " BAGEL, PLAIN 4.25 OZ", 208, 20, 38, 15, 24, 15],
    ])


def test_use_header_after_july_still_gives_weekly_usage():
    ws = W.parse_worksheet_xlsx(_real_layout(), distributor="US Foods",
                                warehouse="Chicago, IL")
    got = {l.variety: (l.cases_on_hand, l.weekly_usage) for l in ws.lines}
    assert ws.snapshot_label == "10.26.26"
    assert got["Asiago"] == (45, 8)
    assert got["Sesame"] == (28, 14)
    assert got["Plain"] == (24, 15)


def test_old_wkly_use_header_still_works():
    data = _sheet([
        ["DATE", None, None, "6.1.26", None],
        ["USF #", "MFG #", "DESCRIPTION", "CS OH", "WKLY USE"],
        [1055064, 1159, "ASIAGO", 48, 6],
    ])
    ws = W.parse_worksheet_xlsx(data, distributor="US Foods", warehouse="Chicago, IL")
    assert [(l.cases_on_hand, l.weekly_usage) for l in ws.lines] == [(48, 6)]


def test_no_usage_column_leaves_usage_unset():
    data = _sheet([
        ["DATE", None, None, "10.26.26"],
        ["USF #", "MFG #", "DESCRIPTION", "CS OH"],
        [1055064, 1159, "ASIAGO", 45],
    ])
    ws = W.parse_worksheet_xlsx(data, distributor="US Foods", warehouse="Chicago, IL")
    assert [(l.cases_on_hand, l.weekly_usage) for l in ws.lines] == [(45, None)]
