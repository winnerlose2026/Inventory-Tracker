"""PO parsers no longer skip a row they cannot read without saying so.

Each parser checks its lines against what the PDF itself prints; a PO that
does not reconcile is held (no events), reported as a scan error and raised
as feed-health issue ``po-total:<po>``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from integrations import email_scanner as es
from integrations import po_health
from integrations.chefs_warehouse_po_parser import parse_po_text as cw_parse
from integrations.cheney_po_parser import parse_po_text as cheney_parse
from integrations.usfoods_po_parser import parse_po_text as usf_parse

USF_HEAD = """\
 PURCHASE ORDER NO.    305202B2 0000003    *        US  F O O D S                *           07/30/2026 13:46:49
                               ORDER DATE: 07/29/26  CANCEL DATE: 00/00/00     PREPAID  X   COLLECT
                               SCHEDULE SHIPMENT TO ARRIVE ON: 08/31/26
  VENDOR 150345
 -------- M A I L   T O -------        -------- S H I P   T O -------
 H&H BAGELS                            3350 NW HOUSTON/B2
 54-18 37TH AVE                        13400 HOLLISTER ROAD
 QUEENS          NY 11377              HOUSTON         TX 77086         ---------- R E M A R K S ----------
 BUYER: 152 T X FOLEY                                                   INFO@HHBAGELS.COM
ORDER ORDER   SCC/GTIN CODE      PACK SIZE - LABEL     ITEM
"""
USF_OK = USF_HEAD + """\
              00859313006502  10/6/4.25OZ -HHMDTWBGES 7095637
  168 CASES   1150            BAGEL, PLN 4.25 Z UNSL PARBK         27.00                          27.00
              10859313006226  6/10/4.06OZ -H&HBAGELS  1055010
   56 CASES   1184            BAGEL, EGG 4.06 Z UNSL HEAT &        27.00                          27.00
 **** TOTALS **** 224   UNITS  19440 WEIGHT  1423 CUBE
"""


@pytest.fixture
def issues(monkeypatch):
    raised = {}

    class _FH:
        @staticmethod
        def raise_issue(key, message, severity="warn"):
            raised[key] = (message, severity)
            return True

        @staticmethod
        def clear_issue(key):
            raised.pop(key, None)

        @staticmethod
        def snapshot():
            return {"feeds": {}, "issues": {}}

    monkeypatch.setattr(po_health, "_fh", lambda: _FH)
    return raised


# ------------------------------------------------------------------ US Foods

def test_usf_clean_po_reconciles():
    po = usf_parse(USF_OK)
    assert po.total_units == 224 and po.integrity_error == ""


def test_usf_tolerates_thousands_singular_and_spaced_unit():
    text = USF_HEAD + """\
              00859313006502  10/6/4.25 OZ -HHMDTWBGES 7095637
  1,168 CASES   1150            BAGEL, PLN 4.25 Z UNSL PARBK         27.00                          27.00
              10859313006226  6/10/4.06OZ -H&H BAGELS  1055010
    1 CASE    1184            BAGEL, EGG 4.06 Z UNSL HEAT &        27.00                          27.00
 **** TOTALS **** 1,169   UNITS  19440 WEIGHT  1423 CUBE
"""
    po = usf_parse(text)
    assert [(l.variety, l.quantity, l.unit) for l in po.lines] == [
        ("Plain", 1168.0, "CASES"), ("Egg", 1.0, "CASES")]
    assert po.integrity_error == ""


def test_usf_unreadable_row_is_an_integrity_error_not_a_short_po():
    text = USF_OK.replace("   56 CASES   1184 ", "   56 CASEZ   1184 ")
    po = usf_parse(text)
    assert len(po.lines) == 1
    assert "168" in po.integrity_error and "224" in po.integrity_error


def test_usf_header_with_no_lines_is_an_integrity_error():
    po = usf_parse(USF_HEAD + " **** TOTALS **** 224   UNITS\n")
    assert po.lines == [] and "no line items" in po.integrity_error


def test_usf_row_count_checked_when_no_totals_line():
    text = USF_OK.replace(" **** TOTALS **** 224   UNITS  19440 WEIGHT  1423 CUBE\n", "")
    assert usf_parse(text).integrity_error == ""
    broken = text.replace("              10859313006226  6/10/4.06OZ -H&HBAGELS  1055010",
                          "              1085931300622X  6/10/4.06OZ -H&HBAGELS  1055010")
    assert "2 item rows" in usf_parse(broken).integrity_error


def test_scan_holds_a_usf_po_that_does_not_reconcile(monkeypatch, issues):
    bad = USF_OK.replace("   56 CASES   1184 ", "   56 CASEZ   1184 ")
    monkeypatch.setattr(es, "_usfoods_parse_po_pdf", lambda b: usf_parse(bad))
    events, errors = es._usfoods_po_to_events(b"", "US Foods", "<m1>", "USF PO 305202")
    assert events == []
    assert any("NOT BOOKED" in e for e in errors)
    assert issues["po-total:305202B2"][1] == "error"
    # the clean copy books both lines
    monkeypatch.setattr(es, "_usfoods_parse_po_pdf", lambda b: usf_parse(USF_OK))
    events, errors = es._usfoods_po_to_events(b"", "US Foods", "<m1>", "USF PO 305202")
    assert len(events) == 2 and not any("NOT BOOKED" in e for e in errors)


# -------------------------------------------------------------------- Cheney

CHENEY = """\
Purchase Order Number: 054511758943
Order Date: 09/09/2026
Vendor No: 119049
Vendor Name: H & H BAGELS
Buyer: STACY LYONS
Delivery Date 10/07/2026
Shipping Address OCALA FACILITY - CBI
2801 W SILVER SPRINGS BLVD
OCALA FL  34475
USA
Item Material/Description Brand Pack Size Quantity UM Unit Price         Net Amount
   10 10153018 BAGEL PLAIN PARBAKED H & H 001/60   CT    104 CS 26.50 CS 2756.00
                                          Mfg#   1150                                       GTIN#:
   20 10153019 BAGEL POPPY PARBAKED H & H 001/60   CT      8 CS 26.50 CS 212.00
                                          Mfg#   1152                                       GTIN#:
"""


def test_cheney_clean_po_reconciles():
    po = cheney_parse(CHENEY)
    assert [l.quantity for l in po.lines] == [104.0, 8.0]
    assert po.integrity_error == "" and po.net_amount_total == 2968.00


def test_cheney_head_tolerates_indent_and_brand_spelling():
    text = (CHENEY
            .replace("   20 10153019 BAGEL POPPY PARBAKED H & H 001/60",
                     "20 10153019 BAGEL POPPY PARBAKED H&H BAGELS 001/60")
            .replace("   10 10153018 BAGEL PLAIN PARBAKED H & H 001/60",
                     " 10 10153018 BAGEL PLAIN PARBAKED 001/60"))
    po = cheney_parse(text)
    assert [(l.variety, l.quantity) for l in po.lines] == [("Plain", 104.0),
                                                           ("Poppy Seed", 8.0)]
    assert po.lines[0].description == "BAGEL PLAIN PARBAKED"
    assert po.integrity_error == ""


def test_cheney_unreadable_row_is_an_integrity_error():
    text = CHENEY.replace("001/60   CT      8 CS 26.50 CS 212.00",
                          "001/60   CT      eight CS 26.50 CS 212.00")
    po = cheney_parse(text)
    assert len(po.lines) == 1 and "could not be read" in po.integrity_error


def test_cheney_line_math_is_checked():
    po = cheney_parse(CHENEY.replace("104 CS 26.50 CS 2756.00", "10 CS 26.50 CS 2756.00"))
    assert "line 10" in po.integrity_error


def test_scan_holds_a_cheney_po_with_unreadable_rows(monkeypatch, issues):
    bad = CHENEY.replace("001/60   CT      8 CS 26.50 CS 212.00",
                         "001/60   CT      eight CS 26.50 CS 212.00")
    monkeypatch.setattr(es, "_cheney_parse_po_pdf", lambda b: cheney_parse(bad))
    events, errors = es._cheney_po_to_events(b"", "Cheney Brothers", "<m2>", "PO")
    assert events == [] and any("NOT BOOKED" in e for e in errors)
    assert "po-total:054511758943" in issues


# ---------------------------------------------------------- Chefs Warehouse

CW = """PURCHASE ORDER # 1226111
*1226111*
 Order Date     10-07-2026
Delivery Date 10-28-2026
Ship To: 600001 Buyer: 9480679
The Chefs' Warehouse Florida, LLC NATALIE LOPEZ
5559 NW 145th ST Receiving 7477 Candlewood Rd. Phone:
OPA LOCKA, FL, 33054 Hanover, MD, 21076 Fax:
Total USD 3,425.90
1  10507796SLD BAGELS HH PLAIN SLICED
10/6 CT CS56.00 CS 31.6768 1,773.90
21163  10507809 BAGELS HH EVERYTHING
10/6 CT CS56.00 CS 29.5000 1,652.00
"""


def test_cw_clean_po_reconciles():
    po = cw_parse(CW)
    assert po.integrity_errors == [] and po.needs_review is False


def test_cw_total_mismatch_needs_review():
    po = cw_parse(CW.replace("Total USD 3,425.90", "Total USD 5,077.90"))
    assert po.needs_review and "Total USD $5,077.90" in po.integrity_errors[0]


def test_cw_zero_qty_line_needs_review():
    po = cw_parse(CW.replace("10/6 CT CS56.00 CS 29.5000 1,652.00",
                             "10/6 CT 56.00 CS 29.5000 1,652.00"))
    assert po.needs_review
    assert any("no quantity" in e for e in po.integrity_errors)


def test_cw_item_not_starting_105_is_reported_unmapped():
    text = CW.replace("Total USD 3,425.90", "Total USD 3,825.90") + (
        "3  20411234 CREAM CHEESE PLAIN 6/3LB\n"
        "6/3 LB CS10.00 CS 40.0000 400.00\n")
    po = cw_parse(text)
    assert len(po.lines) == 3
    assert any("20411234" in d for d in po.unmapped_descriptions)
    assert any("not H&H" in e for e in po.integrity_errors)


def test_cw_apply_marks_needs_review_and_raises_issue(monkeypatch, issues):
    import inventory_tracker
    import sync_inventory as sync
    saved = {}
    monkeypatch.setattr(sync, "load_chefs_warehouse_pos", lambda: [])
    monkeypatch.setattr(sync, "save_chefs_warehouse_pos",
                        lambda recs: saved.update(recs=recs))
    monkeypatch.setattr(inventory_tracker, "load_canceled_pos", lambda: {})
    rec = {"po_number": "1226111", "warehouse": "Opa Locka, FL", "total_usd": 5077.90,
           "total_cs": 112, "lines": [
               {"cw_item": "10507796SLD", "qty": 56, "ext_cost": 1773.90},
               {"cw_item": "10507809", "qty": 56, "ext_cost": 1652.00}]}
    rep = sync._apply_cw_pos([rec])
    assert saved["recs"][0]["needs_review"] is True
    assert rep["needs_review"] and "po-total:1226111" in issues


def test_usf_matching_total_outranks_a_stray_row_shaped_line():
    text = USF_OK.replace(" **** TOTALS ****",
                          "   20 CASES   9999            PALLET NOTE LINE\n **** TOTALS ****")
    po = usf_parse(text)
    assert len(po.lines) == 2 and po.integrity_error == ""


def test_cheney_phone_like_lines_are_not_item_rows():
    text = CHENEY + "  561 8451800 Receiving\n   10 4511758943 reference only\n"
    assert cheney_parse(text).integrity_error == ""
