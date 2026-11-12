"""A message with both a PO PDF and a weekly report keeps the report
(review 2026-11-12, item 14)."""
import io

import openpyxl

from integrations import email_scanner as ES
from integrations.cheney_po_parser import CheneyPO, CheneyPOLine
from integrations.usfoods_po_parser import UsFoodsPO, UsFoodsPOLine

from ingest_fakes import PDF, XLSX, mime

TABLE = ("<table><tr><td>ITEM</td><td>Vendor#</td><td>CURRENT ON HAND</td>"
         "<td>Forecast 11/15/2026</td></tr>"
         "<tr><td>7095637</td><td>1150</td><td>81</td><td>22</td></tr></table>")


def _usf_po():
    return UsFoodsPO(po_number="7135734C", po_revision="1", order_date="11/05/26",
                     warehouse="Zebulon, NC",
                     lines=[UsFoodsPOLine(usf_item_no="7095637", description="x",
                                          quantity=56.0, unit="CS", variety="Plain",
                                          list_cost=27.0, net_cost=27.0, scc_gtin="",
                                          pack="10/6/4.25", mfr_prod_no="1150",
                                          case_size=60)])


def _types(events):
    return sorted(e.event_type for e in events)


def test_usf_po_pdf_plus_body_table(monkeypatch):
    monkeypatch.setattr(ES, "_usfoods_parse_po_pdf", lambda b: _usf_po())
    msg = mime("maria.hernandez@usfoods.com", "PO and this week's numbers",
               html=TABLE, attachments=[("PO 7135734C.pdf", b"%PDF", PDF)])
    events, errors, _ = ES.parse_message_with_errors(msg)
    assert _types(events) == ["on_hand", "restock"] and not errors
    count = next(e for e in events if e.event_type == "on_hand")
    assert count.item.quantity == 81.0 and count.count_date


def test_usf_po_pdf_plus_xlsx_report(monkeypatch):
    monkeypatch.setattr(ES, "_usfoods_parse_po_pdf", lambda b: _usf_po())
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["APN", "Description", "Manufacturer Product Number",
               "On Hand Quantity", "8 Week Average Shipped"])
    ws.append([7095637, "BAGEL, PLAIN", 1150, 2, 5])
    buf = io.BytesIO()
    wb.save(buf)
    msg = mime("kimberly.cobb@usfoods.com", "PO + report",
               attachments=[("PO.pdf", b"%PDF", PDF), ("amt.xlsx", buf.getvalue(), XLSX)])
    events, _errors, _ = ES.parse_message_with_errors(msg)
    assert _types(events) == ["on_hand", "restock"]


def test_cheney_po_pdf_plus_stock_workbook(monkeypatch):
    po = CheneyPO(po_number="054511767347", warehouse="Ocala, FL")
    try:
        po.lines = [CheneyPOLine(line_no=1, item_no="10153018", description="x",
                                 quantity=8.0, quantity_um="CS", variety="Plain",
                                 mfg_code="1150", net_cost=26.5, case_size=60)]
    except TypeError:      # dataclass shape differs: build a duck-typed line
        from types import SimpleNamespace
        po.lines = [SimpleNamespace(quantity=8.0, quantity_um="CS", variety="Plain",
                                    mfg_code="1150", net_cost=26.5, case_size=60)]
    monkeypatch.setattr(ES, "_cheney_parse_po_pdf", lambda b: po)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item #", "Description", "Brand", "Pack", "Size", "UOM", "Stock"])
    ws.append(["10153018", "BAGEL PLAIN PARBAKED", "H & H", "1", "60 CT", "CS", 120])
    buf = io.BytesIO()
    wb.save(buf)
    msg = mime("mross@cheneybrothers.com", "PO # 054511767347",
               attachments=[("PO.pdf", b"%PDF", PDF),
                            ("HHBagOcala11-9-2026.xlsx", buf.getvalue(), XLSX)])
    events, _errors, _ = ES.parse_message_with_errors(msg)
    assert _types(events) == ["on_hand", "restock"]
