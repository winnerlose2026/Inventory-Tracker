"""The Chefs Warehouse DC tag comes from the PDF's Ship To, not the subject.

2026-10-23: the Pending POs tab showed PO 1226111 as "Opa Locka, FL (MD)".
The PDF ships to 600001 / Opa Locka (correctly mapped to the warehouse), but
the buyer typed "PO 1226111 MD" in the email subject and the parenthesised tag
was copied from the subject. PO 1113603 (Hanover ship-to, subject "CHI") was
the same bug.
"""
import blueprints.pos as pos
from integrations.chefs_warehouse_po_parser import cw_dc_code, parse_po_text

_PDF = """PURCHASE ORDER # 1226111
*1226111*
 Order Date     10-07-2026
Delivery Date 10-28-2026
Ship To: 600001 Buyer: 9480679
The Chefs' Warehouse Florida, LLC NATALIE LOPEZ
5559 NW 145th ST Receiving 7477 Candlewood Rd. Phone:
OPA LOCKA, FL, 33054 Hanover, MD, 21076 Fax:
Total USD 3,469.44
1  10507796SLD BAGELS HH PLAIN SLICED
10/6 CT CS56.00 CS 31.6768 1,773.90
21163  10507809 BAGELS HH EVERYTHING
10/6 CT CS56.00 CS 29.5000 1,652.00
"""


def test_parser_tag_follows_ship_to_not_subject():
    po = parse_po_text(_PDF, dc_code="MD")          # subject said MD
    assert po.warehouse == "Opa Locka, FL"
    assert po.dc_code == "FLA"
    assert po.subject_dc_code == "MD"               # kept as a hint


def test_unknown_ship_to_falls_back_to_subject_hint():
    assert cw_dc_code("999999", "", fallback="ny") == "NY"
    assert cw_dc_code("", "Hanover, MD", fallback="CHI") == "MD"


def test_stored_record_with_subject_tag_reads_canonical():
    """Records saved before the fix still carry dc_code='MD'; every reader goes
    through _cw_po_summary, which re-derives the tag."""
    rec = {"po_number": "1226111", "warehouse": "Opa Locka, FL",
           "ship_to_id": "600001", "dc_code": "MD", "lines": []}
    assert pos._cw_po_summary(rec)["dc_code"] == "FLA"
    rec = {"po_number": "1113603", "warehouse": "Hanover, MD",
           "ship_to_id": "400001", "dc_code": "CHI", "lines": []}
    assert pos._cw_po_summary(rec)["dc_code"] == "MD"


def test_apply_path_stores_canonical_tag(monkeypatch):
    import sync_inventory as sync
    saved = {}
    monkeypatch.setattr(sync, "load_chefs_warehouse_pos", lambda: [])
    monkeypatch.setattr(sync, "save_chefs_warehouse_pos",
                        lambda recs: saved.update(recs=recs))
    import inventory_tracker
    monkeypatch.setattr(inventory_tracker, "load_canceled_pos", lambda: {})
    rep = sync._apply_cw_pos([{"po_number": "1226111", "warehouse": "Opa Locka, FL",
                               "ship_to_id": "600001", "dc_code": "MD",
                               "order_date": "10-07-2026", "lines": []}])
    assert rep["added"] == 1
    assert saved["recs"][0]["dc_code"] == "FLA"
    assert rep["changes"][0]["dc_code"] == "FLA"
