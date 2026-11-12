"""A report that parses only in part says so (review 2026-11-12, item 8).
On-hand alone used to parse with no diagnostic when the usage column was
renamed, leaving weekly usage frozen for weeks."""
import io

import openpyxl

from integrations import bagel_inventory_worksheet as W
from integrations import email_scanner as ES
from integrations import usfoods_inventory_report as R
from integrations.cheney_inventory_report import parse_report_xlsx as cheney_xlsx

from ingest_fakes import XLSX, mime


def _wb(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_html_report_without_a_forecast_column_warns():
    html = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td>"
            "<td>Projected</td></tr>"
            "<tr><td>7095637</td><td>1150</td><td>81</td><td>22</td></tr></table>")
    rep = R.parse_report_html(html)
    assert rep.lines[0].weekly_usage is None
    assert "NO usage / forecast column" in rep.warnings[0] and "Projected" in rep.warnings[0]
    events, errors, _ = ES.parse_message_with_errors(
        mime("maria.hernandez@usfoods.com", "Weekly", html=html))
    assert len(events) == 1
    assert ES.failure_class(next(e for e in errors if "Projected" in e)) == "partial-parse"


def test_xlsx_report_with_a_renamed_usage_column_warns():
    data = _wb([["APN", "Description", "Manufacturer Product Number",
                 "On Hand Quantity", "Shipped Avg Per Wk"],
                [7095637, "BAGEL, PLAIN", 1150, 2, 5]])
    rep = R.parse_report_xlsx(data)
    assert rep.lines and rep.lines[0].weekly_usage is None
    assert "Shipped Avg Per Wk" in rep.warnings[0]
    events, errors, _ = ES.parse_message_with_errors(
        mime("kimberly.cobb@usfoods.com", "July 27 Report",
             attachments=[("amt.xlsx", data, XLSX)]))
    assert len(events) == 1
    assert any(e.startswith("partial parse: ") and "Alcoa, TN" in e for e in errors)
    # The known heading still parses clean.
    ok = _wb([["APN", "Description", "Manufacturer Product Number",
               "On Hand Quantity", "8 Week Average Shipped"],
              [7095637, "BAGEL, PLAIN", 1150, 2, 5]])
    assert R.parse_report_xlsx(ok).warnings == []


def test_worksheet_without_a_usage_column_warns():
    data = _wb([["DATE", None, None, "11.9.26", None],
                ["USF #", "MFG #", "DESCRIPTION", "CS OH", "NOTES"],
                [7095637, 1150, "BAGEL, PLAIN", 48, "ok"]])
    sheet = W.parse_worksheet_xlsx(data, distributor="US Foods", warehouse="Chicago, IL")
    assert sheet.lines[0].cases_on_hand == 48 and sheet.lines[0].weekly_usage is None
    assert "not a usage column" in sheet.warnings[0]
    _ev, errors = ES._inventory_worksheet_to_events(
        data, "michael.via@usfoods.com", "<m>", "Weekly inventory and usage")
    assert any(e.startswith("partial parse: inventory worksheet (Chicago, IL)")
               for e in errors)


def test_cheney_weekly_usage_grid_without_a_stock_grid_warns():
    rows = [
        ["Drill Down Reporting : Date Range >= 11/02/2026 AND <= 11/07/2026"],
        ["Products", "Pack", "Dist Item #", "Mfq.Product Code", "GTIN", "Full Cases"],
        ["BAGEL PLAIN PARBAKED", "1:60 CT", "10153018", "1150", "", 55],
        [],
        ["Item No", "Desc", "On-Hnd Qty"],          # renamed stock header
        ["10153018", "BAGEL PLAIN PARBAKED", 120],
    ]
    events, errors = cheney_xlsx(_wb(rows), "HHBagelsRVBUsage-StockNov2-Nov72026.xlsx")
    assert {e["event_type"] for e in events} == {"usage_rate"}
    assert any(e.startswith("partial parse: ") and "NO on-hand" in e for e in errors)
